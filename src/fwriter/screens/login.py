import json

from textual import log, on
from textual.app import ComposeResult
from textual.containers import Center
from textual.screen import Screen
from textual.widgets import Button, Input

from ..core.api import *
from ..secrets import SecretStorage
from .error import ErrorScreen
from .main import MainScreen


class LoginScreen(Screen):
    CSS_PATH = "../tcss/login.tcss"
    ENABLE_COMMAND_PALETTE = False

    def compose(self) -> ComposeResult:
        yield Center(Input(placeholder="Кукисы", id="cookies"))
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
        cookies = None
        try:
            cookies = json.loads(self.query_one("#cookies", Input).value)
        except json.JSONDecodeError:
            pass

        cookies_json: dict | None = (
            {cookie["name"]: cookie["value"] for cookie in cookies}
            if isinstance(cookies, list)
            else None
        )

        if cookies_json is None or len(cookies_json.keys()) < 2:
            self.notify(
                "Неверные cookies!", title="Ошибка", severity="error", timeout=3
            )
            return

        user_agent = self.query_one("#user_agent", Input).value

        result = await FicbookAPI.try_login(cookies_json, user_agent)
        log(result)
        if result.ok is False:
            self.app.push_screen(ErrorScreen(result.error))
            return

        SecretStorage.set_auth(cookies_json, user_agent)

        self.app.install_screen(MainScreen(result.api), name="main_screen")
        self.app.switch_mode("main")

        # if passed:
        #     self.exit((api, self.query_one(FilePathDisplay).text.replace('Выбран - ', "")))
        # else:
        #     self.notify(data, title="Ошибка", severity='error', timeout=3)
