from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Log


class ErrorScreen(ModalScreen):
    CSS_PATH = "../tcss/error.tcss"

    def __init__(self, data: str) -> None:
        self.lines = [data]
        super().__init__()

    def add(self, text: str) -> None:
        """Ещё одна ошибка в открытое окно, без повторов."""
        if text in self.lines:
            return
        self.lines.append(text)
        # до on_mount Log ещё нет: строку выведет он
        if self.is_mounted:
            self.query_one(Log).write_line(text)

    def compose(self) -> ComposeResult:
        yield Vertical(
            Center(Label("Ошибка!", id="title")),
            Center(Log(id="data")),
            Center(Button("ОК", variant="default", id="ok")),
            id="dialog",
        )

    def on_mount(self) -> None:
        self.query_one(Log).write_lines(self.lines)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.app.pop_screen()


__all__ = ["ErrorScreen"]
