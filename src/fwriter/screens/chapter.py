import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar, Literal, cast

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.geometry import Offset
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Input, Static, TabbedContent, TabPane, TextArea
from textual.widgets.text_area import Document

from ..core.api.client import FicbookAPI
from ..core.api.errors import FicbookAPIError, SiteRejectedError
from ..core.api.models import RevisionInfo
from ..core.history import (
    Parts,
    finish_merge,
    load_merge,
    postpone_merge,
    restore_version,
    start_merge,
)
from ..core.markup import (
    Edit,
    MarkupError,
    Reader,
    Tweak,
    check,
    count,
    para_style,
    paras_touched,
    tweak,
)
from ..core.merge import Choice
from ..core.storage import ChapterMeta, ChapterStorage, LastMerge, Version
from ..core.sync import (
    Synced,
    fetch_revisions,
    publish_chapter,
    sync_chapter,
)
from .api_errors import report_api_error, report_disk_error
from .diff import DiffScreen, rev_line, version_line
from .editor import ChapterEditor, ReaderPanel
from .menu import HelpScreen, MenuScreen
from .merge import MergeScreen
from .visual import VisualEditor
from .widgets import CONFIRM_DELAY

log = logging.getLogger(__name__)

# param главы на сайте
STATUSES = {1: "черновик", 2: "опубликована", 3: "отложена"}


class ChapterTab(TabPane):
    """Глава: текст, название по F2, дифф (F3), слияние (F4), отправка (F5), статус (F6).

    draft.txt пишется через 2 с после ввода и при закрытии.
    """

    AUTOSAVE_DELAY = 2.0
    CHECK_DELAY = 0.3

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f2", "rename", "Переименовать"),
        Binding("f3", "diff", "Дифф"),
        Binding("f4", "review", "Слияние"),
        Binding("f5", "publish", "Отправить"),
        Binding("f6", "status", "Статус"),
        Binding("f7", "mode", "Режим"),
        Binding("f8", "help", "Справка"),
        Binding("f9", "tweaks", "Твики"),
        Binding("escape", "hide_title", show=False),
    ]
    # param главы: действие сайта, пункт меню, текст ошибки
    STATUS_ACTIONS: ClassVar[
        dict[
            int, tuple[Literal["show_part", "hide_part", "disable_auto_pub"], str, str]
        ]
    ] = {
        1: ("show_part", "Опубликовать", "Не удалось опубликовать главу"),
        2: ("hide_part", "Скрыть в черновики", "Не удалось скрыть главу"),
        3: (
            "disable_auto_pub",
            "Отменить отложенную публикацию",
            "Не удалось отменить отложенную публикацию",
        ),
    }

    class InfoChanged(Message):
        """Поменялась строка о главе для footer."""

        def __init__(self, tab: ChapterTab) -> None:
            self.tab = tab
            super().__init__()

    class TitleSaved(Message):
        """chapter.json главы перезаписан: название могло поменяться."""

        def __init__(self, tab: ChapterTab) -> None:
            self.tab = tab
            super().__init__()

    class Created(Message):
        """F5 создал главу на сайте, chapter_id уже новый: список работы перестраивает MainScreen."""

        def __init__(self, tab: ChapterTab) -> None:
            self.tab = tab
            super().__init__()

    class Dropped(Message):
        """Глава не скачалась, показывать нечего: вкладку снимает MainScreen."""

        def __init__(self, tab: ChapterTab) -> None:
            self.tab = tab
            super().__init__()

    def __init__(
        self,
        api: FicbookAPI | None,
        storage: ChapterStorage,
        fic_id: str,
        chapter_id: str,
        text: str | None,
        title: str,
        title_editable: bool,
        settings: dict[str, bool],
        readonly: bool = False,
        syncing: bool = False,
        publishable: bool = False,
        creates: bool = False,
    ) -> None:
        """api None - офлайн; text None - черновика нет, его скачает синхронизация.

        syncing: синхронизация при монтировании, правки закрыты до synced().
        publishable: главу можно отправить по F5.
        creates: главы нет на сайте (new-N или удалена там), F5 создаст её.
        settings: флажки автозамены.
        """
        self.api = api
        self.storage = storage
        self.fic_id = fic_id
        self.chapter_id = chapter_id
        self.has_draft = text is not None
        self.title_editable = title_editable
        self.readonly = readonly
        self.syncing = syncing
        self.publishable = publishable
        self.creates = creates
        # слияние (от снимка до закрытия MergeScreen) или смена статуса
        self.occupied = False
        # время первого F5: отправляет второе нажатие
        self.publish_armed: float | None = None
        self.site_status: int | None = None
        # текст сразу после твика F9: если он прежний, твик продолжает серию
        self.tweaked: str | None = None
        # список правок последней синхронизации: по нему идёт докачка
        self.history: tuple[RevisionInfo, ...] = ()
        self.save_timer: Timer | None = None
        # ссылки, а не query_one: в on_unmount дочерние виджеты уже сняты
        self.title_input = Input(title)
        self.title_input.display = False
        self.title_before = title
        # текст, курсор и история Ctrl+Z - у editor, оба вида рисуют один Reader
        self.reader = Reader()
        self.editor = ChapterEditor()
        self.panel = ReaderPanel(self.reader)
        self.visual = VisualEditor(self.editor, self.reader, settings)
        # WYSIWYG по умолчанию; с ошибкой разметки show_error() переключит на F7
        self.editor.display = self.panel.display = False
        self.markup_error = Static(id="markup_error")
        self.markup_error.display = False
        # что сейчас идёт, первым в строке о главе: плашка над текстом сдвигала бы его;
        # не editor.loading: он снимает фокус, и TabbedContent уходит на стартовую вкладку
        self.note: str | None = None
        if syncing:
            self.note = "Проверяем сайт…" if self.has_draft else "Глава загружается…"
        super().__init__(Content(title))
        self.set_text(text or "", None if syncing else self.read_meta())

    @property
    def busy(self) -> bool:
        """Идёт обмен с сайтом или слияние: второе такое действие не начинаем."""
        return self.syncing or self.occupied

    @contextmanager
    def working(self, halt: bool = False) -> Iterator[None]:
        """Слияние, дифф или смена статуса: F3-F6 и закрытие недоступны.

        halt - на время прервать докачку: start_merge() пишет chapter.json в потоке.
        """
        if halt:
            self.stop_fetch()
        self.occupied = True
        self.refresh_bindings()
        try:
            yield
        finally:
            self.occupied = False
            self.refresh_bindings()
            if halt and not self.syncing:
                self.fetch()

    def set_text(self, text: str, meta: ChapterMeta | None) -> None:
        self.saved_title = None if self.syncing or meta is None else meta.title
        if meta:
            self.title_input.value = meta.title
        self.title_input.disabled = self.syncing or not self.title_editable
        self.editor.read_only = self.syncing or self.readonly
        # тот же текст не перезагружаем: курсор остался бы в начале
        if text != self.editor.text:
            self.editor.load_text(text)
        # TextArea приводит переводы строк к одному виду (и \u2028, \x0c - к \n):
        # без правок такой текст не перезаписываем, при правке предупреждаем
        self.saved_text = self.editor.text
        self.disk_text = text
        self.newlines_changed = self.saved_text != text
        self.base_text = self.read_base(meta)
        # count() на длинной главе - десятки мс: пересчёт при сохранении, не на каждую клавишу
        self.counts = count(self.saved_text)

    @property
    def where(self) -> str:
        return f"{self.fic_id}/{self.chapter_id}"

    def read_meta(self) -> ChapterMeta | None:
        """None: chapter.json повреждён."""
        try:
            return self.storage.read_chapter(self.fic_id, self.chapter_id)
        except FileNotFoundError:
            return ChapterMeta(self.title_input.value)
        except OSError, ValueError:
            log.warning(
                "chapter.json %s/%s не прочитан",
                self.fic_id,
                self.chapter_id,
                exc_info=True,
            )
            self.notify(
                "chapter.json главы повреждён: название не будет сохраняться",
                severity="error",
            )
            return None

    def read_base(self, meta: ChapterMeta | None) -> str | None:
        """Текст сайта, от которого ведётся черновик; None - не с чем сравнить."""
        if self.syncing or meta is None or meta.base is None:
            return None
        try:
            return self.storage.read_version(self.fic_id, self.chapter_id, meta.base)
        except OSError, ValueError:
            log.warning(
                "base %s/%s не прочитана", self.fic_id, self.chapter_id, exc_info=True
            )
            return None

    def exchanging(self, note: str) -> None:
        """Правки закрыты до synced(): идёт обмен с сайтом."""
        self.stop_fetch()
        self.syncing = True
        self.editor.read_only = True
        self.title_input.disabled = True
        self.note = note
        self.refresh_bindings()
        self.post_message(self.InfoChanged(self))

    def synced(
        self, text: str, meta: ChapterMeta | None, status: int | None = None
    ) -> None:
        """Синхронизация кончилась: текст после неё, правки открыты."""
        self.syncing = False
        self.site_status = status
        # синхронизация берёт название с сайта и отмечает отправленное
        self.post_message(self.TitleSaved(self))
        self.note = None
        self.set_text(text, meta)
        self.refresh_bindings()
        self.post_message(self.InfoChanged(self))
        self.warn_newlines()

    def info(self) -> str:
        """Статус на сайте, состояние черновика, символы и слова."""
        parts = [] if self.note is None else [self.note]
        if self.site_status is not None:
            parts.append(STATUSES.get(self.site_status, f"статус {self.site_status}"))
        text = self.editor.text
        if not self.syncing:
            title_changed = (
                self.saved_title is not None
                and self.title_input.value != self.saved_title
            )
            unsaved = text != self.saved_text or title_changed
            parts.append("не сохранено" if unsaved else "сохранено")
            if self.base_text is not None:
                # без правок на диске лежит disk_text, а не текст после TextArea
                draft = self.disk_text if text == self.saved_text else text
                parts.append(
                    "как на сайте" if draft == self.base_text else "есть правки"
                )
        chars, words = self.counts
        parts.append(f"{chars} зн., {words} сл.")
        return " · ".join(parts)

    def compose(self) -> ComposeResult:
        yield self.title_input
        # без контейнеров: фокус редактора дошёл бы до TabbedContent
        # позже, чем закрытие вкладки при ошибке синхронизации
        yield self.editor
        yield self.panel
        yield self.visual
        yield self.markup_error

    def on_mount(self) -> None:
        self.reread()
        self.watch(self.editor, "scroll_y", self.follow_scroll, init=False)
        if self.syncing:
            self.sync()
        else:
            self.warn_newlines()

    def warn_newlines(self) -> None:
        if self.newlines_changed:
            self.notify(
                "В тексте главы разные переводы строк: "
                "после правки они станут одинаковыми во всём тексте",
                severity="warning",
            )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action == "rename":
            return self.title_editable and not self.syncing
        if action == "hide_title":
            return self.title_input.display
        if action == "publish":
            return self.api is not None and self.publishable and not self.busy
        if action == "review":
            return not self.readonly and not self.busy
        if action == "diff":
            return not self.busy
        if action == "tweaks":
            return not self.readonly and not self.busy
        if action == "status":
            return (
                self.api is not None
                and self.title_editable
                and self.site_status in self.STATUS_ACTIONS
                and not self.busy
            )
        return True

    def action_rename(self) -> None:
        self.title_before = self.title_input.value
        self.title_input.display = True
        self.title_input.focus()

    @property
    def active(self) -> ChapterEditor | VisualEditor:
        """редактор текущего режима"""
        return self.visual if self.visual.display else self.editor

    def action_mode(self) -> None:
        """F7: WYSIWYG или разметка с панелью читателя, курсор на том же месте"""
        wysiwyg = not self.visual.display
        if wysiwyg and (error := check(self.editor.text)) is not None:
            self.editor.cursor_location = (error.line, error.col)
            self.notify(
                f"WYSIWYG недоступен: ошибка разметки, строка {error.line + 1}: "
                f"{error.message}",
                severity="error",
            )
            return
        self.switch(wysiwyg)

    def switch(self, wysiwyg: bool) -> None:
        # фокус - только если он был в тексте: фокус в скрытой вкладке её откроет
        refocus = self.screen.focused in (self.editor, self.visual)
        self.editor.display = self.panel.display = not wysiwyg
        self.visual.display = wysiwyg
        if refocus:
            self.active.focus()
        if wysiwyg:
            # ширина у показанного виджета будет после отрисовки
            self.visual.call_after_refresh(self.visual.shown)
        else:
            self.editor.scroll_cursor_visible()

    def action_hide_title(self) -> None:
        self.active.focus()

    @on(Input.Submitted)
    def title_submitted(self) -> None:
        self.active.focus()

    @on(Input.Blurred)
    def hide_title(self) -> None:
        # без названия вкладка осталась бы без подписи
        if self.title_input.value == "":
            self.title_input.value = self.title_before
        self.title_input.display = False

    @on(Input.Changed)
    @on(TextArea.Changed)
    def schedule_save(self) -> None:
        if self.save_timer:
            self.save_timer.stop()
        self.save_timer = self.set_timer(self.AUTOSAVE_DELAY, self.save)
        self.post_message(self.InfoChanged(self))

    @on(TextArea.Changed)
    def reread(self) -> None:
        self.reader.update(self.editor.document.lines)
        self.panel.relayout()
        self.visual.relayout()
        self.follow_cursor()
        self.check_markup()

    @on(TextArea.SelectionChanged)
    def follow_cursor(self) -> None:
        self.panel.show(self.editor.document.lines, *self.editor.cursor_location)

    def follow_scroll(self) -> None:
        # по абзацам: строки читателя и редактора разной высоты, пустые строки скрыты
        wrapped = self.editor.wrapped_document
        top = int(self.editor.scroll_y)
        row = wrapped.offset_to_location(Offset(0, top))[0]
        shift = top - wrapped.location_to_offset((row, 0)).y
        self.panel.align(self.editor.document.lines, row, shift)

    @work(exclusive=True, group="check")
    async def check_markup(self) -> None:
        # exclusive отменяет прежнюю проверку: проверяем после паузы в наборе
        await asyncio.sleep(self.CHECK_DELAY)
        self.show_error(await asyncio.to_thread(check, self.editor.text))

    def show_error(self, error: MarkupError | None) -> None:
        self.editor.mark_error(error and error.line)
        self.markup_error.display = error is not None
        if error:
            self.markup_error.update(f"Строка {error.line + 1}: {error.message}")
        # без тегов битую разметку однозначно не показать
        if error and self.visual.display:
            self.editor.cursor_location = (error.line, error.col)
            self.switch(False)
            self.notify(
                f"Ошибка разметки в строке {error.line + 1}: {error.message}. "
                "Включён режим разметки (F7)",
                severity="warning",
            )

    @on(Input.Changed)
    def rename_tab(self, event: Input.Changed) -> None:
        self.query_ancestor(TabbedContent).get_tab(self).label = Content(event.value)

    def save(self, notify: bool = True) -> bool:
        """False, если записать на диск не удалось; notify=False - без уведомления."""
        text, title = self.editor.text, self.title_input.value
        try:
            if text != self.saved_text:
                self.storage.write_draft(self.fic_id, self.chapter_id, text)
                self.saved_text = self.disk_text = text
            # пустое поле - правка ещё идёт: hide_title вернёт прежнее название
            if self.saved_title is not None and title and title != self.saved_title:
                self.storage.set_title(self.fic_id, self.chapter_id, title)
                self.saved_title = title
                self.post_message(self.TitleSaved(self))
        # ValueError: chapter.json повредили, пока вкладка открыта
        except OSError, ValueError:
            log.warning(
                "черновик %s/%s не записан", self.fic_id, self.chapter_id, exc_info=True
            )
            if notify:
                self.notify(
                    "Не удалось записать черновик на диск, повторю при следующей правке",
                    severity="error",
                )
            return False
        finally:
            if self.is_attached:
                self.counts = count(text)
                self.post_message(self.InfoChanged(self))
        return True

    # закрытие вкладки, выход и истёкшая сессия снимают вкладку
    def on_unmount(self) -> None:
        # повторять запись больше некому: вкладки уже нет
        if not self.save(notify=False):
            self.notify(
                "Черновик главы не записан на диск, правки потеряны", severity="error"
            )

    # вкладку не закрыть, пока syncing: воркер доживает до конца
    @work(group="sync")
    async def sync(self) -> None:
        """Догнать черновик до сайта; fast-forward не подменит текст под руками."""
        if self.api is None:
            return
        action = (
            "Не удалось обновить главу"
            if self.has_draft
            else "Не удалось скачать главу"
        )
        synced = await self.pull(action)
        # после слияния докачку запустит working()
        if synced is None:
            return
        if synced.kind == "merge":
            self.merge(synced)
        else:
            self.fetch()

    async def pull(self, action: str) -> Synced | None:
        """Синхронизация и текст после неё во вкладку; None - сайт не ответил."""
        assert self.api is not None
        text = self.disk_text if self.has_draft else None
        status, synced = self.site_status, None
        self.stop_fetch()
        try:
            synced = await sync_chapter(
                self.api,
                self.storage,
                self.fic_id,
                self.chapter_id,
                self.title_input.value,
                self.readonly,
            )
        except FicbookAPIError as e:
            report_api_error(self, e, action)
        except (OSError, ValueError) as e:
            report_disk_error(self, e, action, self.where)
        else:
            text, status = synced.text, synced.status
            self.report_synced(synced)
        if text is None:
            self.post_message(self.Dropped(self))
            return None
        self.synced(text, self.read_meta(), status)
        if synced is not None:
            self.history = synced.history
        return synced

    def stop_fetch(self) -> None:
        # sync_chapter читает chapter.json до сети и пишет после: докачка затёрлась бы
        self.workers.cancel_group(self, "fetch")

    @work(group="fetch", exclusive=True)
    async def fetch(self) -> None:
        """Шаг 5 синхронизации: правки сайта в версии, без уведомлений."""
        if self.api is None or not self.history:
            return
        try:
            n = await fetch_revisions(
                self.api,
                self.storage,
                self.fic_id,
                self.chapter_id,
                self.title_input.value,
                self.history,
                self.fetched,
            )
        except FicbookAPIError, OSError, ValueError:
            # продолжится при следующей синхронизации
            log.warning(
                "глава %s/%s: докачка правок прервана",
                self.fic_id,
                self.chapter_id,
                exc_info=True,
            )
        else:
            log.info(
                "глава %s/%s: докачано правок: %d", self.fic_id, self.chapter_id, n
            )

    def fetched(self, version: Version, at: int) -> None:
        # открытый дифф этой главы сразу видит новую правку, даже под диалогом
        for screen in self.app.screen_stack:
            if isinstance(screen, DiffScreen) and screen.where == self.where:
                screen.add(version, at)

    def report_synced(self, synced: Synced) -> None:
        if synced.title_conflict is not None:
            self.notify(
                f"Название изменили и на сайте: «{synced.title_conflict}»",
                severity="warning",
            )
        if synced.kind == "ff" and synced.site is not None:
            v = synced.site
            self.notify(
                f"Обновлено до правки номер {v.site_num} ({v.author_name}, {v.created})"
            )

    def action_publish(self) -> None:
        if self.creates and not self.title_editable:
            self.notify(
                "Главы нет на сайте, а создавать главы в этой работе вам нельзя",
                severity="warning",
            )
            return
        armed, self.publish_armed = self.publish_armed, None
        if armed is None or time.monotonic() - armed > CONFIRM_DELAY:
            self.publish_armed = time.monotonic()
            prompt = "Нажмите F5 ещё раз, чтобы отправить"
            if self.creates and self.chapter_id.startswith("new-"):
                prompt = "Нажмите F5 ещё раз, чтобы создать главу на сайте"
            elif self.creates:
                prompt = (
                    "Глава удалена на сайте. "
                    "Нажмите F5 ещё раз, чтобы создать её заново"
                )
            self.notify(prompt, timeout=CONFIRM_DELAY)
            return
        if self.save():
            self.publish()

    # пока идёт отправка, правки закрыты: черновик равен отправленному тексту
    @work(group="publish")
    async def publish(self) -> None:
        if self.api is None:
            return
        self.exchanging("Отправляем…")
        status, error, created = self.site_status, None, None
        merging = None
        try:
            published = await publish_chapter(
                self.api,
                self.storage,
                self.fic_id,
                self.chapter_id,
                self.title_input.value,
                self.creates,
            )
        except FicbookAPIError as e:
            report_api_error(self, e, "Не удалось отправить главу")
            # глава могла создаться до ошибки; не создана наверняка только при отказе сайта
            if self.creates and not isinstance(e, SiteRejectedError):
                self.notify(
                    "Проверьте на сайте, не создалась ли глава, прежде чем "
                    "отправлять её снова",
                    severity="warning",
                )
        except (OSError, ValueError) as e:
            report_disk_error(self, e, "Не удалось отправить главу", self.where)
        else:
            synced = published.after or published.first
            status, error = synced.status, published.error
            self.history = synced.history
            if not published.stranded:
                created = published.created
            self.report_synced(published.first)
            if published.after is not None:
                self.report_synced(published.after)
            if published.first.kind == "merge":
                merging = published.first
                self.notify("Сначала слияние с правкой сайта, потом снова F5")
            if published.stranded:
                # повторная отправка старой папки создала бы на сайте дубль
                self.publishable = False
                self.notify(
                    "Глава создана на сайте, но черновик не перенесён: ошибка на диске. "
                    "Не отправляйте его снова - на сайте появится вторая такая глава",
                    severity="error",
                    timeout=30,
                )
            elif published.unchanged:
                self.notify("Глава не отличается от сайта, отправлять нечего")
            elif published.created is not None:
                # история догонится при следующем открытии: это не ошибка
                self.notify("Глава создана на сайте как черновик")
            elif published.after is not None:
                v = published.rev
                if published.reverted is not None:
                    n = published.reverted
                    self.notify(f"Отправлено откатом к правке номер {n}")
                elif v is None:
                    self.notify("Отправлено")
                else:
                    self.notify(f"Отправлено (правка номер {v.site_num})")
            elif published.first.kind != "merge" and error is None:
                self.notify(
                    "Отправлено, но историю правок перечитать не удалось",
                    severity="warning",
                )
        if created is not None:
            # папка уже перенесена: дальше вкладка пишет под id с сайта
            self.chapter_id, self.creates = created, False
            self.post_message(self.Created(self))
        # после ошибки на диске мог остаться текст fast-forward
        try:
            text = self.storage.read_draft(self.fic_id, self.chapter_id)
        except OSError, ValueError:
            text = None
        meta = self.read_meta()
        self.synced(self.editor.text if text is None else text, meta, status)
        if merging is not None:
            self.merge(merging)
        else:
            self.fetch()
        if error is not None:
            self.editor.cursor_location = (error.line, error.col)
            self.notify(
                f"Ошибка разметки, строка {error.line + 1}: {error.message}",
                severity="error",
            )

    @work(group="merge")
    async def merge(self, synced: Synced) -> None:
        """Шаг 4 синхронизации: снимок черновика, MergeScreen и результат в черновик."""
        if synced.site is None or self.busy:
            return
        with self.working(halt=True):
            try:
                merge, parts, choices = await asyncio.to_thread(
                    start_merge,
                    self.storage,
                    self.fic_id,
                    self.chapter_id,
                    self.title_input.value,
                    synced.site,
                )
            except (OSError, ValueError) as e:
                report_disk_error(
                    self, e, "Не удалось начать слияние с правкой сайта", self.where
                )
                return
            revs = [
                rev_line(r.num, r.author_name, r.created, r.comment)
                for r in synced.revs
            ]
            await self.review(merge, parts, choices, revs)

    async def review(
        self,
        merge: LastMerge,
        parts: Parts,
        choices: dict[int, Choice],
        revs: list[str],
    ) -> None:
        screen = MergeScreen(self.title_input.value, parts, choices, revs)
        done, choices = await self.app.push_screen_wait(screen)
        title = self.title_input.value
        if not done:
            self.postpone(merge, parts, choices)
            return
        # правки, набранные до MergeScreen, ещё ждут автосохранения: иначе пропадут
        # без снимка; save() сам сообщает об ошибке, решения ждут F4
        if not self.save():
            self.postpone(merge, parts, choices)
            return
        try:
            text = finish_merge(
                self.storage,
                self.fic_id,
                self.chapter_id,
                title,
                merge,
                parts,
                choices,
            )
        except (OSError, ValueError) as e:
            report_disk_error(self, e, "Не удалось записать слияние", self.where)
            return
        self.set_text(text, self.read_meta())
        self.post_message(self.InfoChanged(self))
        self.notify("Слияние записано в черновик")

    def postpone(
        self, merge: LastMerge, parts: Parts, choices: dict[int, Choice]
    ) -> None:
        """Esc в MergeScreen: черновик прежний, решения ждут следующего слияния."""
        try:
            saved = postpone_merge(
                self.storage,
                self.fic_id,
                self.chapter_id,
                self.title_input.value,
                merge,
                parts,
                choices,
            )
        except OSError, ValueError:
            log.warning(
                "глава %s/%s: решения слияния не записаны",
                self.fic_id,
                self.chapter_id,
                exc_info=True,
            )
            self.notify(
                "Слияние не закончено, черновик не изменён\n"
                "Решения не сохранились: ошибка чтения или записи на диске",
                severity="warning",
            )
            return
        if saved:
            self.notify("Слияние отложено, черновик не изменён\nF4 вернёт к нему")
        else:
            self.notify("Черновик не изменён")

    def action_review(self) -> None:
        if self.save():
            self.review_last()

    @work(group="merge")
    async def review_last(self) -> None:
        """F4: последнее слияние с прежними решениями."""
        if self.busy:
            return
        with self.working(halt=True):
            try:
                loaded = await asyncio.to_thread(
                    load_merge, self.storage, self.fic_id, self.chapter_id
                )
            except (OSError, ValueError) as e:
                report_disk_error(
                    self, e, "Не удалось прочитать последнее слияние", self.where
                )
                return
            if loaded is None:
                self.notify("В этой главе ещё не было слияний")
                return
            if loaded == "outdated":
                self.notify(
                    "После слияния глава обновлялась с сайта, "
                    "пересмотреть его уже нельзя",
                    severity="warning",
                )
                return
            merge, parts, choices, site = loaded
            revs = [] if site is None else [version_line(site)]
            await self.review(merge, parts, choices, revs)

    def action_diff(self) -> None:
        if self.save():
            self.show_diff()

    @work(group="merge")
    async def show_diff(self) -> None:
        """F3: дифф версий; восстановленная версия - в черновик."""
        if self.busy:
            return
        title = self.title_input.value
        with self.working():
            try:
                meta = self.storage.load_chapter(self.fic_id, self.chapter_id, title)
            except (OSError, ValueError) as e:
                report_disk_error(
                    self, e, "Не удалось прочитать версии главы", self.where
                )
                return
            screen = DiffScreen(
                self.storage,
                self.fic_id,
                self.chapter_id,
                meta,
                self.disk_text,
                not self.readonly,
            )
            version = await self.app.push_screen_wait(screen)
            if version is None:
                return
            try:
                text = restore_version(
                    self.storage, self.fic_id, self.chapter_id, title, version
                )
            except (OSError, ValueError) as e:
                report_disk_error(self, e, "Не удалось восстановить версию", self.where)
                return
            self.set_text(text, self.read_meta())
            self.post_message(self.InfoChanged(self))
            self.notify(f"Восстановлена версия: {version_line(version)}")

    def action_status(self) -> None:
        if self.site_status is None:
            return
        action, label, _ = self.STATUS_ACTIONS[self.site_status]

        def chosen(result: str | None) -> None:
            if result is not None:
                self.change_status()

        confirm = {"show_part": "Нажмите ещё раз, чтобы опубликовать"}
        self.app.push_screen(
            MenuScreen("Статус главы", [(action, label)], confirm), chosen
        )

    # текст не отправляется, поэтому правки не закрываются
    @work(group="chapter_status")
    async def change_status(self) -> None:
        if self.api is None or self.site_status not in self.STATUS_ACTIONS:
            return
        action, _, failed = self.STATUS_ACTIONS[self.site_status]
        status = self.site_status
        with self.working():
            try:
                await self.api.set_chapter_status(self.chapter_id, action)
                try:
                    chapter = await self.api.get_chapter(self.fic_id, self.chapter_id)
                except FicbookAPIError as e:
                    report_api_error(
                        self, e, "Статус изменён, но перечитать главу не удалось"
                    )
                else:
                    status = chapter.status
                    if status is not None:
                        self.notify(f"Статус главы: {STATUSES.get(status, status)}")
            except FicbookAPIError as e:
                report_api_error(self, e, failed)
            # внутри with: bindings пересчитаются уже с новым статусом
            self.site_status = status
            self.post_message(self.InfoChanged(self))

    TWEAKS: ClassVar[dict[Tweak, str]] = {
        "typography": "Вся типографика сразу",
        "blank": "Абзацы: пустая строка",
        "indent": "Абзацы: красная строка",
        "indent_blank": "Абзацы: красная и пустая строка",
        "dashes": "Тире",
        "quotes": "Кавычки «» и „“",
        "ellipsis": "Многоточие",
        "spaces": "Чистка пробелов",
        "fix_tags": "Починка тегов",
    }

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_tweaks(self) -> None:
        if self.save():
            self.tweaks()

    @work(group="sync")
    async def tweaks(self) -> None:
        """F9: свежий текст с сайта, меню с числами абзацев, пункт - одной правкой."""
        if self.busy:
            return
        # твик меняет много абзацев: правки сайта сливаем до него, а не после
        if self.api is not None and not self.creates:
            self.exchanging("Проверяем сайт…")
            synced = await self.pull("Сайт не проверен")
            if synced is not None and synced.kind == "merge":
                self.notify("Сначала слияние с правкой сайта, потом снова F9")
                self.merge(synced)
                return
            if synced is not None:
                self.fetch()
        text = self.editor.text
        if (error := check(text)) is not None:
            self.editor.cursor_location = (error.line, error.col)
            self.notify(
                f"Твики недоступны: ошибка разметки, строка {error.line + 1}: "
                f"{error.message}",
                severity="error",
            )
            return
        doc = self.editor.document
        assert isinstance(doc, Document)
        a, b = sorted(map(doc.get_index_from_location, self.editor.selection))
        sel = (a, b) if a != b else None
        with self.working():
            self.note = "Считаем…"
            self.post_message(self.InfoChanged(self))
            try:
                plan = await asyncio.to_thread(self.plan_tweaks, text, sel)
            finally:
                self.note = None
                self.post_message(self.InfoChanged(self))
            style = plan.pop("style")
            items, disabled = [], set()
            for name, label in self.TWEAKS.items():
                n = plan[name][2]
                if name == style:
                    label += " (сейчас)"
                items.append((name, f"{label} · {n}"))
                if not n:
                    disabled.add(name)
            title = "Твики: " + ("выделение" if sel else "вся глава")
            if style in ("mixed", "plain"):
                title += " · абзацы " + (
                    "смешанные" if style == "mixed" else "сплошные"
                )
            chosen = await self.app.push_screen_wait(
                MenuScreen(title, items, disabled=disabled)
            )
        if chosen is None:
            return
        if self.editor.text != text:
            self.notify("Текст изменился, пока открывалось меню: нажмите F9 ещё раз")
            return
        name = cast(Tweak, chosen)
        edits, bad, _ = plan[name]
        self.apply_tweak(self.TWEAKS[name], edits, bad)

    @staticmethod
    def plan_tweaks(text: str, sel: tuple[int, int] | None) -> dict:
        """Правки всех пунктов F9 и число абзацев; style - способ отделения абзацев."""
        plan: dict = {"style": para_style(text)}
        for name in ChapterTab.TWEAKS:
            edits, bad = tweak(text, name, sel)
            plan[name] = (edits, bad, paras_touched(text, edits) if edits else 0)
        return plan

    def apply_tweak(self, label: str, edits: list[Edit], bad: list[int]) -> None:
        text = self.editor.text
        # первый твик серии - версией: вся серия откатывается одним восстановлением
        if text != self.tweaked:
            if not self.save():
                return
            try:
                self.storage.add_version(
                    self.fic_id,
                    self.chapter_id,
                    text,
                    self.title_input.value,
                    "manual",
                    f"до твика „{label}“",
                )
            except (OSError, ValueError) as e:
                report_disk_error(self, e, "Не удалось сохранить версию", self.where)
                return
        cursor = self.editor.cursor_location
        self.editor.apply_edits(edits)
        self.editor.cursor_location = cursor
        self.tweaked = self.editor.text
        if bad:
            # номер строки после правок: чистка пробелов убирает пустые строки в начале
            at = len("\n".join(text.split("\n")[: bad[0]])) + (bad[0] > 0)
            at += sum(len(r) - (e - s) for s, e, r in edits if e <= at)
            doc = self.editor.document
            assert isinstance(doc, Document)
            self.editor.cursor_location = doc.get_location_from_index(at)
            self.notify(
                f"Кавычки не расставлены в абзацах: {len(bad)}", severity="warning"
            )
