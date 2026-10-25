from collections.abc import Callable
from typing import ClassVar

from textual import work
from textual.app import App
from textual.binding import BindingType
from textual.screen import Screen

from .core.api import *
from .core.api.errors import LoginFailedError
from .screens import *
from .secrets import *


class FWriterApp(App):
    BINDINGS: ClassVar[list[BindingType]] = [("ctrl+c", "quit", "Exit app")]

    MODES: ClassVar[dict[str, str | Callable[[], Screen]]] = {
        "login": LoginScreen,
        "main": "main_screen",
    }

    def on_mount(self) -> None:
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
        self.login_saved(cookies, user_agent)

    # в воркере, чтобы приложение отвечало на клавиши, пока идёт запрос
    @work(exclusive=True)
    async def login_saved(self, cookies: dict[str, str], user_agent: str) -> None:
        try:
            api = await FicbookAPI.login(cookies, user_agent)
        except LoginFailedError as e:
            self.switch_mode("login")
            self.push_screen(ErrorScreen(f"Не удалось войти: {e}"))
            return
        self.install_screen(MainScreen(api), name="main_screen")
        self.switch_mode("main")


def main():
    app = FWriterApp()
    app.run()


if __name__ == "__main__":
    main()
