from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Label

from ..core.markup import AUTOCORRECT
from ..core.storage import ChapterStorage
from .api_errors import report_disk_error

LABELS = {
    "dash": "-- → —",
    "space_dash": "дефис между пробелами или в начале абзаца → тире",
    "quotes": '" → «» и „“',
    "ellipsis": "... → …",
}


class SettingsScreen(ModalScreen[None]):
    """Автозамены при вводе в WYSIWYG: каждый флажок сразу пишется в settings.json."""

    CSS_PATH = "../tcss/menu.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,f7", "dismiss", show=False)
    ]

    def __init__(self, storage: ChapterStorage, settings: dict[str, bool]) -> None:
        """settings меняется на месте: его читают открытые вкладки глав."""
        self.storage = storage
        self.settings = settings
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="menu"):
            yield Label("Автозамена при вводе", id="menu_title")
            for key in AUTOCORRECT:
                yield Checkbox(LABELS[key], self.settings[key], id=key)

    @on(Checkbox.Changed)
    def changed(self, event: Checkbox.Changed) -> None:
        key = event.checkbox.id or ""
        self.settings[key] = event.value
        try:
            self.storage.write_settings(self.settings)
        except OSError as e:
            report_disk_error(
                self, e, "Не удалось сохранить настройки", "settings.json"
            )


__all__ = ["SettingsScreen"]
