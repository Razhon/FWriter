from collections.abc import Callable
from typing import ClassVar

from textual.app import App
from textual.binding import BindingType
from textual.screen import Screen

from .core.api import *
from .screens import *
from .secrets import *


class FWriterApp(App):
    """A Textual app to manage stopwatches."""

    BINDINGS: ClassVar[list[BindingType]] = [("ctrl+c", "quit", "Exit app")]

    MODES: ClassVar[dict[str, str | Callable[[], Screen]]] = {
        "login": LoginScreen,
        "main": "main_screen",
    }

    api: FicbookAPI | None = None

    # def compose(self) -> ComposeResult:
    #     """Create child widgets for the app."""
    #     yield Header()
    #     yield Footer()

    async def on_mount(self) -> None:
        cookies, user_agent = SecretStorage.get_auth()
        if not cookies or not user_agent:
            self.switch_mode("login")
            return

        result = await FicbookAPI.try_login(cookies, user_agent)
        if result.ok is True:
            self.install_screen(MainScreen(result.api), name="main_screen")
            self.switch_mode("main")


def main():
    app = FWriterApp()
    app.run()


if __name__ == "__main__":
    main()
