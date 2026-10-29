import logging
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Input, Label, TabbedContent, TabPane, TextArea

from ..core.markup import count
from ..core.storage import ChapterMeta, ChapterStorage

log = logging.getLogger(__name__)

# param главы на сайте
STATUSES = {1: "черновик", 2: "опубликована", 3: "отложена"}


class ChapterEditor(TextArea):
    def on_resize(self) -> None:
        # TextArea переносит строки по Resize без refresh(): строки, нарисованные
        # до этого, остаются со старым переносом до первого сдвига курсора
        self.refresh()


class ChapterTab(TabPane):
    """Текст главы; название правится по F2. draft.txt пишется через 2 с после ввода и при закрытии."""

    AUTOSAVE_DELAY = 2.0

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f2", "rename", "Переименовать"),
        Binding("escape", "hide_title", show=False),
    ]

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

    def __init__(
        self,
        storage: ChapterStorage,
        fic_id: str,
        chapter_id: str,
        text: str,
        meta: ChapterMeta | None,
        title: str,
        title_editable: bool,
        readonly: bool = False,
        syncing: bool = False,
        publishable: bool = False,
    ) -> None:
        """meta None: chapter.json повреждён, название не сохраняется, чтобы не затереть его.

        syncing: идёт синхронизация, правки закрыты до synced().
        publishable: глава есть на сайте, и её можно отправить по F5.
        """
        self.storage = storage
        self.fic_id = fic_id
        self.chapter_id = chapter_id
        self.title_editable = title_editable
        self.readonly = readonly
        self.syncing = syncing
        self.publishable = publishable
        self.site_status: int | None = None
        self.save_timer: Timer | None = None
        # ссылки, а не query_one: в on_unmount дочерние виджеты уже сняты
        self.title_input = Input(title)
        self.title_input.display = False
        self.title_before = title
        self.editor = ChapterEditor()
        self.status = Label("Проверяем сайт…")
        self.status.display = syncing
        self.set_text(text, meta)
        super().__init__(Content(title), id=self.tab_id(fic_id, chapter_id))

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

    @staticmethod
    def tab_id(fic_id: str, chapter_id: str) -> str:
        return f"chapter-{fic_id}-{chapter_id}"

    def compose(self) -> ComposeResult:
        yield self.title_input
        yield self.status
        yield self.editor

    def on_mount(self) -> None:
        if not self.syncing:
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
