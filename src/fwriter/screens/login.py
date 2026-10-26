import logging

from textual import on
from textual.app import ComposeResult
from textual.containers import Center
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Input

from ..core.api.client import FicbookAPI
from ..core.api.errors import LoginFailedError
from ..secrets import SecretStorage
from .error import ErrorScreen

log = logging.getLogger(__name__)


class LoginScreen(Screen):
    CSS_PATH = "../tcss/login.tcss"
    ENABLE_COMMAND_PALETTE = False

    class LoggedIn(Message):
        """Вход удался; App открывает MainScreen."""

        def __init__(self, api: FicbookAPI) -> None:
            self.api = api
            super().__init__()

    def compose(self) -> ComposeResult:
        yield Center(Input(placeholder="Кукисы", id="cookies", password=True))
        yield Center(Input(placeholder="User-Agent", id="user_agent"))
        yield Center(Button(label="Войти", id="login", disabled=True))

    # после истёкшей сессии экран возвращается с выключенной кнопкой
    def on_screen_resume(self):
        self.update_login_button_state()

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
        if not user_agent.isascii():  # httpx кодирует заголовки только в ASCII
            event.button.disabled = False
            self.notify(
                "В User-Agent допустимы только ASCII-символы",
                title="Ошибка",
                severity="error",
                timeout=5,
            )
            return

        try:
            api = await FicbookAPI.login(cookies, user_agent)
        except LoginFailedError as e:
            log.warning("вход не удался", exc_info=True)
            event.button.disabled = False
            self.app.push_screen(ErrorScreen(str(e)))
            return

        try:
            SecretStorage.set_auth(cookies, user_agent)
        except Exception:  # pywintypes.error на Windows, у других бэкендов свои типы
            log.warning("вход не сохранён", exc_info=True)
            self.notify(
                "Не удалось сохранить вход: в следующий раз придётся войти снова",
                severity="warning",
                timeout=6,
            )

        self.post_message(LoginScreen.LoggedIn(api))
