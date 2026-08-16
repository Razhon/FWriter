from functools import partial
from typing import Self

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Container, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Label, Rule, TabbedContent, TabPane

from ..core.api import *
from ..core.api.models import Fic, UserInfo


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
            # tint: $background 30%;
        }
    }
    
    .clickable_label {
        
        &:hover {
            pointer: pointer;
            text-style: underline;
        }
    }
    
    
    
    """
    
    BINDINGS = [Binding("enter", "press", "Press ClickableLabel", show=False)]
    
    class Pressed(Message):
        def __init__(self, clabel: ClickableLabel) -> None:
            self.clabel: ClickableLabel = clabel
            super().__init__()

        @property
        def control(self) -> ClickableLabel:
            return self.clabel

    def __init__(self, input_label: str, classes: str | None = None) -> None:
        self.input_label = input_label
        self.active_effect_duration = 0.2
        super().__init__(classes=classes)

    def compose(self) -> ComposeResult:  
        yield Label(self.input_label, classes="clickable_label")
        
    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        if not self.has_class("-active"):
            self.press()
            
    def press(self) -> Self:
        if self.disabled or not self.display:
            return self
        # Manage the "active" effect:
        self._start_active_affect()
        # ...and let other components know that we've just been clicked:
        self.post_message(ClickableLabel.Pressed(self))
        return self
    
    def _start_active_affect(self) -> None:
        if self.active_effect_duration > 0:
            self.add_class("-active")
            self.set_timer(
                self.active_effect_duration, partial(self.remove_class, "-active")
            )
    
    def action_press(self) -> None:
        if not self.has_class("-active"):
            self.press()
        
    

class StartPageTab(TabPane):
    def __init__(self, api: FicbookAPI) -> None:
        self.api = api
        super().__init__("Начало работы", id="start_page")

    def compose(self) -> ComposeResult:
        yield Center(Label("Привет, гость!", id="greeting"))
        yield Rule()
        yield VerticalScroll(classes="fics_container")

    async def on_mount(self) -> None:
        user = await self.api.get_user_info()
        self.query_one("#greeting", Label).update(f"Привет, {user.name}!")

        fics_container = self.query_one(".fics_container", VerticalScroll)

        def gen_fic_collapsible(fic: Fic, classes: str) -> Collapsible:
            return Collapsible(
                *[ClickableLabel(ch.title, classes="chapter_btn") for ch in fic.chapters],
                ClickableLabel("[bold]Новая глава ->[/]", classes="new_chapter_btn"),
                title=fic.title,
                classes=classes,
            )

        author_fics = await self.api.get_author_fics()
        if author_fics:
            await fics_container.mount(
                Label("Ваши фанфики:", classes="fics_collapsibles_label")
            )
            await fics_container.mount(
                *[
                    gen_fic_collapsible(fic, "author_fic_collapsible")
                    for fic in author_fics
                ]
            )

        beta_fics = await self.api.get_beta_fics()
        if beta_fics:
            await fics_container.mount(
                Rule(),
                Label(
                    "Фанфики доступные вам, как бете:",
                    classes="fics_collapsibles_label",
                )
            )
            await fics_container.mount(
                *[
                    gen_fic_collapsible(fic, "beta_fics_collapsible")
                    for fic in beta_fics
                ]
            )


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
