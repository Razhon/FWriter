from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Label, LoadingIndicator


class ConnectingScreen(Screen):
    """Вход по сохранённым cookies при запуске; без связи можно не ждать таймаута."""

    DEFAULT_CSS = """
    ConnectingScreen LoadingIndicator {
        height: 1;
    }
    """

    class GoOffline(Message):
        """Не ждать входа; обрабатывает App."""

    def compose(self) -> ComposeResult:
        with Middle():
            yield Center(Label("Входим на ficbook…"))
            yield LoadingIndicator()
            yield Center(Button("Работать офлайн", id="offline"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "offline":
            self.post_message(ConnectingScreen.GoOffline())


__all__ = ["ConnectingScreen"]
