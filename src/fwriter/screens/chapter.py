import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar, Literal

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Input, Label, TabbedContent, TabPane, TextArea

from ..core.api.client import FicbookAPI
from ..core.api.errors import FicbookAPIError, SiteRejectedError
from ..core.markup import count
from ..core.merge import Choice
from ..core.storage import ChapterMeta, ChapterStorage, LastMerge
from ..core.sync import (
    Parts,
    Synced,
    finish_merge,
    load_merge,
    postpone_merge,
    publish_chapter,
    start_merge,
    sync_chapter,
)
from .api_errors import report_api_error, report_disk_error
from .editor import ChapterEditor
from .menu import MenuScreen
from .merge import MergeScreen, rev_line, version_line
from .widgets import CONFIRM_DELAY

log = logging.getLogger(__name__)

# param главы на сайте
STATUSES = {1: "черновик", 2: "опубликована", 3: "отложена"}


class ChapterTab(TabPane):
    """Глава: текст, название по F2, синхронизация, слияние (F4), отправка (F5), статус (F6).

    draft.txt пишется через 2 с после ввода и при закрытии.
    """

    AUTOSAVE_DELAY = 2.0

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f2", "rename", "Переименовать"),
        Binding("f4", "review", "Слияние"),
        Binding("f5", "publish", "Отправить"),
        Binding("f6", "status", "Статус"),
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
        readonly: bool = False,
        syncing: bool = False,
        publishable: bool = False,
        creates: bool = False,
    ) -> None:
        """api None - офлайн; text None - черновика нет, его скачает синхронизация.

        syncing: синхронизация при монтировании, правки закрыты до synced().
        publishable: главу можно отправить по F5.
        creates: главы нет на сайте (new-N или удалена там), F5 создаст её.
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
        self.save_timer: Timer | None = None
        # ссылки, а не query_one: в on_unmount дочерние виджеты уже сняты
        self.title_input = Input(title)
        self.title_input.display = False
        self.title_before = title
        self.editor = ChapterEditor()
        # не editor.loading: он снимает фокус, и TabbedContent уходит на стартовую вкладку
        self.status = Label(
            "Проверяем сайт…" if self.has_draft else "Глава загружается…"
        )
        self.status.display = syncing
        super().__init__(Content(title))
        self.set_text(text or "", None if syncing else self.read_meta())

    @property
    def busy(self) -> bool:
        """Идёт обмен с сайтом или слияние: второе такое действие не начинаем."""
        return self.syncing or self.occupied

    @contextmanager
    def working(self) -> Iterator[None]:
        """Слияние или смена статуса: F4-F6 и закрытие недоступны."""
        self.occupied = True
        self.refresh_bindings()
        try:
            yield
        finally:
            self.occupied = False
            self.refresh_bindings()

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
        self.syncing = True
        self.editor.read_only = True
        self.title_input.disabled = True
        self.status.update(note)
        self.status.display = True
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
        self.status.display = False
        self.set_text(text, meta)
        self.refresh_bindings()
        self.post_message(self.InfoChanged(self))
        self.warn_newlines()

    def info(self) -> str:
        """Статус на сайте, состояние черновика, символы и слова."""
        parts = []
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
        yield self.status
        yield self.editor

    def on_mount(self) -> None:
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

    def action_hide_title(self) -> None:
        self.editor.focus()

    @on(Input.Submitted)
    def title_submitted(self) -> None:
        self.editor.focus()

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
        text = self.disk_text if self.has_draft else None
        action = (
            "Не удалось обновить главу"
            if self.has_draft
            else "Не удалось скачать главу"
        )
        status, synced = None, None
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
            return
        self.synced(text, self.read_meta(), status)
        if synced is not None and synced.kind == "merge":
            self.merge(synced)

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
                if v is None:
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
        with self.working():
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
        with self.working():
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
