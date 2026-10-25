from functools import partial
from typing import ClassVar

import httpx
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Collapsible, Label, Rule, TabbedContent, TabPane

from ..core.api import *
from ..core.api.errors import FicbookAPIError
from ..core.api.models import Fic
from .error import ErrorScreen

FIC_SECTIONS = [
    ("author", "Ваши фанфики:"),
    ("coauthor", "Фанфики доступные вам, как соавтору или сопереводчику:"),
    ("beta", "Фанфики доступные вам, как бете:"),
    ("gamma", "Фанфики доступные вам, как гамме:"),
]


class ClickableLabel(Widget, can_focus=True):
    DEFAULT_CSS = """
    ClickableLabel {
        width: auto;
        height: auto;
        min-height: 1;
        &:focus {
            pointer: pointer;
            text-style: underline;
        }
        &.-active {
            text-style: reverse;
        }
    }

    .clickable_label:hover {
        pointer: pointer;
        text-style: underline;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "press", "Press ClickableLabel", show=False)
    ]

    class Pressed(Message):
        def __init__(self, clabel: ClickableLabel) -> None:
            self.clabel: ClickableLabel = clabel
            super().__init__()

        @property
        def control(self) -> ClickableLabel:
            return self.clabel

    def __init__(
        self, input_label: str, classes: str | None = None, markup: bool = False
    ) -> None:
        self.input_label = input_label
        super().__init__(classes=classes, markup=markup)

    def compose(self) -> ComposeResult:
        yield Label(
            self.input_label, classes="clickable_label", markup=self._render_markup
        )

    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        self.action_press()

    def action_press(self) -> None:
        # Пока идёт подсветка нажатия, повторные нажатия игнорируются
        if self.disabled or not self.display or self.has_class("-active"):
            return
        self.add_class("-active")
        self.set_timer(0.2, partial(self.remove_class, "-active"))
        self.post_message(ClickableLabel.Pressed(self))


class StartPageTab(TabPane):
    def __init__(self, api: FicbookAPI) -> None:
        self.api = api
        super().__init__("Начало работы", id="start_page")

    def compose(self) -> ComposeResult:
        yield Center(Label("Привет, гость!", id="greeting"))
        yield Rule()
        yield VerticalScroll(classes="fics_container")

    async def on_mount(self) -> None:
        try:
            user = await self.api.get_user_info()
            self.query_one("#greeting", Label).update(f"Привет, {user.name}!")

            fics_container = self.query_one(".fics_container", VerticalScroll)

            def gen_fic_collapsible(fic: Fic, classes: str) -> Collapsible:
                return Collapsible(
                    *[
                        ClickableLabel(ch.title, classes="chapter_btn")
                        for ch in fic.chapters
                    ],
                    ClickableLabel(
                        "[bold]Новая глава ->[/]",
                        classes="new_chapter_btn",
                        markup=True,
                    ),  # TODO отключить для всех, кроме автора/соавтора с правами редактирования
                    title=fic.title,
                    classes=classes,
                )

            for role, label in FIC_SECTIONS:
                fics = await self.api.get_fics(role)
                if not fics:
                    continue

                if fics_container.children:  # разделитель
                    await fics_container.mount(Rule())

                await fics_container.mount(
                    Label(label, classes="fics_collapsibles_label"),
                    *[
                        gen_fic_collapsible(fic, f"{role}_fics_collapsible")
                        for fic in fics
                    ],
                )
        except (FicbookAPIError, httpx.HTTPError) as e:
            self.app.push_screen(ErrorScreen(f"{type(e).__name__}: {e}"))


class MainScreen(Screen):
    CSS_PATH = "../tcss/main.tcss"

    def __init__(self, api: FicbookAPI) -> None:
        self.api = api
        super().__init__()

    def compose(self) -> ComposeResult:
        with TabbedContent():
            yield StartPageTab(self.api)
