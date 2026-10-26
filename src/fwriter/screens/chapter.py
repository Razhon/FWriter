import logging

from textual import on
from textual.app import ComposeResult
from textual.content import Content
from textual.timer import Timer
from textual.widgets import Input, TabbedContent, TabPane, TextArea

from ..core.storage import ChapterMeta, ChapterStorage

log = logging.getLogger(__name__)


class ChapterTab(TabPane):
    """Название и текст главы; draft.txt пишется через 2 с после ввода и при закрытии."""

    AUTOSAVE_DELAY = 2.0

    def __init__(
        self,
        storage: ChapterStorage,
        fic_id: str,
        chapter_id: str,
        text: str,
        meta: ChapterMeta | None,
        title: str,
        title_editable: bool,
    ) -> None:
        """meta None: chapter.json повреждён, название не сохраняется, чтобы не затереть его."""
        self.storage = storage
        self.fic_id = fic_id
        self.chapter_id = chapter_id
        self.saved_title = meta.title if meta else None
        self.save_timer: Timer | None = None
        # ссылки, а не query_one: в on_unmount дочерние виджеты уже сняты
        self.title_input = Input(title, disabled=not title_editable)
        self.editor = TextArea(text)
        # TextArea приводит переводы строк к одному виду (и \u2028, \x0c - к \n):
        # без правок такой текст не перезаписываем, при правке предупреждаем
        self.saved_text = self.editor.text
        self.newlines_changed = self.saved_text != text
        super().__init__(Content(title), id=self.tab_id(fic_id, chapter_id))

    @staticmethod
    def tab_id(fic_id: str, chapter_id: str) -> str:
        return f"chapter-{fic_id}-{chapter_id}"

    def compose(self) -> ComposeResult:
        yield self.title_input
        yield self.editor

    def on_mount(self) -> None:
        if self.newlines_changed:
            self.notify(
                "В тексте главы разные переводы строк: "
                "после правки они станут одинаковыми во всём тексте",
                severity="warning",
            )

    @on(Input.Changed)
    @on(TextArea.Changed)
    def schedule_save(self) -> None:
        if self.save_timer:
            self.save_timer.stop()
        self.save_timer = self.set_timer(self.AUTOSAVE_DELAY, self.save)

    @on(Input.Changed)
    def rename_tab(self, event: Input.Changed) -> None:
        self.query_ancestor(TabbedContent).get_tab(self).label = Content(event.value)

    def save(self, notify: bool = True) -> bool:
        """False, если записать на диск не удалось; notify=False - без уведомления."""
        text, title = self.editor.text, self.title_input.value
        try:
            if text != self.saved_text:
                self.storage.write_draft(self.fic_id, self.chapter_id, text)
                self.saved_text = text
            if self.saved_title is not None and title != self.saved_title:
                self.storage.set_title(self.fic_id, self.chapter_id, title)
                self.saved_title = title
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
        return True

    # закрытие вкладки, выход и истёкшая сессия снимают вкладку
    def on_unmount(self) -> None:
        # повторять запись больше некому: вкладки уже нет
        if not self.save(notify=False):
            self.notify(
                "Черновик главы не записан на диск, правки потеряны", severity="error"
            )
