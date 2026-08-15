from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Log


class ErrorScreen(ModalScreen):
    CSS_PATH = "../tcss/error.tcss"

    def __init__(self, data: str) -> None:
        self.data = data
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Vertical(
            Center(Label("Ошибка!", id="title")),
            Center(Log(id="data")),
            Center(Button("ОК", variant="default", id="ok")),
            id="dialog",
        )

    def on_mount(self) -> None:
        log = self.query_one(Log)
        log.write_line(self.data)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.app.pop_screen()


__all__ = ["ErrorScreen"]
