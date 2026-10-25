import asyncio

from textual.app import App
from textual.widgets import Label
from textual.widgets._collapsible import CollapsibleTitle

from fwriter.core.api.client import FicbookAPI
from fwriter.core.api.models import Fic, UserInfo
from fwriter.run import FWriterApp
from fwriter.screens import LoginScreen, MainScreen
from fwriter.secrets import SecretStorage


def test_titles_are_plain_text():
    tricky = "[Kot] a[/]b"

    api = FicbookAPI({})
    api.user = UserInfo(1, tricky)

    async def fake_get_fics(role: str) -> list[Fic]:
        return [Fic(1, tricky, [])] if role == "author" else []

    api.get_fics = fake_get_fics

    class TestApp(App):
        def on_mount(self):
            self.push_screen(MainScreen(api))

    async def run():
        async with TestApp().run_test() as pilot:
            await pilot.pause()
            greeting = pilot.app.screen.query_one("#greeting", Label)
            assert str(greeting.render()) == f"Привет, {tricky}!"
            title = pilot.app.screen.query_one(CollapsibleTitle)
            assert tricky in str(title.render())

    asyncio.run(run())


def test_broken_keyring_opens_login(monkeypatch):
    def broken_get_auth():
        raise RuntimeError("no keyring")

    monkeypatch.setattr(SecretStorage, "get_auth", broken_get_auth)

    async def run():
        async with FWriterApp().run_test() as pilot:
            await pilot.pause()
            assert isinstance(pilot.app.screen, LoginScreen)

    asyncio.run(run())
