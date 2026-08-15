from textual.app import ComposeResult
from textual.containers import Center, Container
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Collapsible, Label, TabbedContent, TabPane

from ..core.api import *
from ..core.api.models import UserInfo


class StartPageTab(TabPane):
    def __init__(self, api: FicbookAPI) -> None:
        self.api = api
        super().__init__("Начало работы", id="start_page")

    def compose(self) -> ComposeResult:
        yield Center(Label("Привет, гость!", id="greeting"))
        yield Center(id="fics_container")

    async def on_mount(self) -> None:
        user = await self.api.get_user_info()
        self.query_one("#greeting", Label).update(f"Привет, {user.name}!")

        fics_container = self.query_one("#fics_container", Center)

        author_fics = await self.api.get_author_fics()
        if author_fics:
            author_container = Container(
                *[
                    Collapsible(
                        *[Label(ch.title) for ch in fic.chapters], title=fic.title
                    )
                    for fic in author_fics
                ],
                id="author_fics_container",
            )
            await fics_container.mount(author_container)

        beta_fics = await self.api.get_beta_fics()
        if beta_fics:
            beta_container = Container(
                *[
                    Collapsible(
                        *[Label(ch.title) for ch in fic.chapters], title=fic.title
                    )
                    for fic in beta_fics
                ],
                id="beta_fics_container",
            )
            await fics_container.mount(beta_container)


class MainScreen(Screen):
    CSS_PATH = "../tcss/main.tcss"

    def __init__(self, api: FicbookAPI) -> None:
        self.api = api
        super().__init__()

    def compose(self) -> ComposeResult:
        with TabbedContent():
            yield StartPageTab(self.api)

    async def on_mount(self) -> None:
        print(await self.api.get_user_info())
        # print(await self.api.get_author_fics())
        # print(await self.api.get_beta_fics())
