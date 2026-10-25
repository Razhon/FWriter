from collections.abc import Callable
from typing import ClassVar

from textual.app import App
from textual.binding import BindingType
from textual.screen import Screen

from .core.api import *
from .screens import *
from .secrets import *


class FWriterApp(App):
    BINDINGS: ClassVar[list[BindingType]] = [("ctrl+c", "quit", "Exit app")]

    MODES: ClassVar[dict[str, str | Callable[[], Screen]]] = {
        "login": LoginScreen,
        "main": "main_screen",
    }

    async def on_mount(self) -> None:
        try:
            cookies, user_agent = SecretStorage.get_auth()
        except Exception as e:  # noqa: BLE001 у бэкендов keyring свои типы ошибок
            self.notify(
                f"Не удалось прочитать сохранённый вход: {e}",
                severity="warning",
                timeout=6,
                markup=False,
            )
            cookies, user_agent = None, None

        if not cookies or not user_agent:
            self.switch_mode("login")
            return

        result = await FicbookAPI.try_login(cookies, user_agent)
        if result.ok is True:
            self.install_screen(MainScreen(result.api), name="main_screen")
            self.switch_mode("main")
        else:
            self.notify(f"Не удалось войти: {result.error}", severity="warning", markup=False)
            self.switch_mode("login")


def main():
    app = FWriterApp()
    app.run()


if __name__ == "__main__":
    main()
