from textual import on
from textual.app import ComposeResult
from textual.containers import Center
from textual.screen import Screen
from textual.widgets import Button, Input

from ..core.api import *
from ..core.api.errors import LoginFailedError
from ..secrets import SecretStorage
from .error import ErrorScreen
from .main import MainScreen


class LoginScreen(Screen):
    CSS_PATH = "../tcss/login.tcss"
    ENABLE_COMMAND_PALETTE = False

    def compose(self) -> ComposeResult:
        yield Center(Input(placeholder="Кукисы", id="cookies", password=True))
        yield Center(Input(placeholder="User-Agent", id="user_agent"))
        yield Center(Button(label="Войти", id="login", disabled=True))

    @on(Input.Changed)
    def on_input(self):
        self.update_login_button_state()

    def update_login_button_state(self):
        def is_form_valid() -> bool:
            return (
                self.query_one("#cookies", Input).value != ""
                and self.query_one("#user_agent", Input).value != ""
            )

        self.query_one("#login", Button).disabled = not is_form_valid()

    @on(Button.Pressed)
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "login":
            return
        event.button.disabled = True

        cookies = FicbookAPI.parse_cookies(self.query_one("#cookies", Input).value)
        if cookies is None:
            event.button.disabled = False
            self.notify(
                f"Неверные cookies! Нужны: {', '.join(sorted(FicbookAPI.AUTH_COOKIES))}",
                title="Ошибка",
                severity="error",
                timeout=5,
            )
            return

        user_agent = self.query_one("#user_agent", Input).value

        try:
            api = await FicbookAPI.login(cookies, user_agent)
        except LoginFailedError as e:
            event.button.disabled = False
            self.app.push_screen(ErrorScreen(str(e)))
            return

        try:
            SecretStorage.set_auth(cookies, user_agent)
        except Exception as e:  # noqa: BLE001 — pywintypes.error на Windows, у других бэкендов свои типы
            self.notify(
                f"Не удалось сохранить вход: {e}. В следующий раз придётся войти снова.",
                severity="warning",
                timeout=6,
                markup=False,
            )

        self.app.install_screen(MainScreen(api), name="main_screen")
        self.app.switch_mode("main")
