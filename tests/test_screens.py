import asyncio

from textual.app import App
from textual.widgets import Collapsible, Input, Label, Log
from textual.widgets._collapsible import CollapsibleTitle

from fwriter.core.api.client import FicbookAPI
from fwriter.core.api.errors import APIChangedError, LoginFailedError
from fwriter.core.api.models import Fic, UserInfo
from fwriter.run import FWriterApp
from fwriter.screens import ErrorScreen, LoginScreen, MainScreen
from fwriter.screens.main import FIC_SECTIONS
from fwriter.secrets import SecretStorage


def test_titles_are_plain_text():
    tricky = "[Kot] a[/]b"

    api = FicbookAPI({})
    api.user = UserInfo(1, tricky)

    async def fake_get_fics(role: str) -> list[Fic]:
        return [Fic("1", tricky, [])] if role == "author" else []

    api.get_fics = fake_get_fics

    class TestApp(App):
        def on_mount(self):
            self.push_screen(MainScreen(api))

    async def run():
        async with TestApp().run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
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


def test_failed_fic_list_keeps_others():
    api = FicbookAPI({})
    api.user = UserInfo(1, "Кот")

    async def fake_get_fics(role: str) -> list[Fic]:
        if role == "coauthor":
            raise APIChangedError("сломалось")
        return [Fic("1", role, [])]

    api.get_fics = fake_get_fics
    main = MainScreen(api)

    class TestApp(App):
        def on_mount(self):
            self.push_screen(main)

    async def run():
        async with TestApp().run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            titles = [str(c.title) for c in main.query(Collapsible)]
            assert titles == ["author", "beta", "gamma"]
            assert isinstance(pilot.app.screen, ErrorScreen)
            log = pilot.app.screen.query_one(Log)
            assert any("сломалось" in line for line in log.lines)

    asyncio.run(run())


def test_sections_match_api_roles():
    assert [r for r, _ in FIC_SECTIONS] == list(FicbookAPI.FIC_LISTS)


def test_failed_saved_login_opens_login_with_error(monkeypatch):
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: ({"rme": "1"}, "ua"))

    async def failed_login(cookies, user_agent):
        raise LoginFailedError("ConnectTimeout: ")

    monkeypatch.setattr(FicbookAPI, "login", failed_login)

    async def run():
        async with FWriterApp().run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(pilot.app.screen, ErrorScreen)
            assert isinstance(pilot.app.screen_stack[-2], LoginScreen)

    asyncio.run(run())


def test_non_ascii_user_agent_is_rejected(monkeypatch):
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: (None, None))
    called: list[bool] = []

    async def fake_login(cookies, user_agent):
        called.append(True)
        raise LoginFailedError("не должен вызываться")

    monkeypatch.setattr(FicbookAPI, "login", fake_login)
    cookies = '[{"name": "rme", "value": "1"}, {"name": "PHPSESSID", "value": "2"}]'

    async def run():
        async with FWriterApp().run_test() as pilot:
            await pilot.pause()
            pilot.app.screen.query_one("#cookies", Input).value = cookies
            pilot.app.screen.query_one("#user_agent", Input).value = "Mozilla\xa05.0"
            await pilot.pause()
            await pilot.click("#login")
            await pilot.pause()
            assert not called
            assert isinstance(pilot.app.screen, LoginScreen)

    asyncio.run(run())
