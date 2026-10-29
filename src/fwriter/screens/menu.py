from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option


class MenuScreen(ModalScreen[str | None]):
    """Список действий: выбор возвращает id пункта, Esc - None."""

    CSS_PATH = "../tcss/menu.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss", show=False)]

    def __init__(
        self,
        title: str,
        items: list[tuple[str, str]],
        confirm: dict[str, str] | None = None,
    ) -> None:
        """items - (id, подпись); confirm - id: подпись, с которой пункт выбирают второй раз."""
        self.title_text = title
        self.items = items
        self.confirm = confirm or {}
        self.armed: str | None = None
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="menu"):
            yield Label(self.title_text, id="menu_title")
            yield OptionList(*(Option(label, id) for id, label in self.items))

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        id = event.option_id
        if id is None:
            return
        if id in self.confirm and self.armed != id:
            options = event.option_list
            if self.armed is not None:
                options.replace_option_prompt(self.armed, dict(self.items)[self.armed])
            self.armed = id
            options.replace_option_prompt(id, self.confirm[id])
            return
        self.dismiss(id)


__all__ = ["MenuScreen"]
