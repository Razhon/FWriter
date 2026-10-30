from functools import partial
from typing import ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.message import Message
from textual.widgets import Label

# окно для второго нажатия, которое подтверждает: корзина, F5
CONFIRM_DELAY = 3.0


class ClickableLabel(Label, can_focus=True):
    """Намеренный отдельный класс от Button, так легче"""

    PRESS_DELAY = 0.2

    DEFAULT_CSS = """
    ClickableLabel {
        pointer: pointer;
        &:focus, &:hover {
            text-style: underline;
        }
        &.-active {
            text-style: reverse;
        }
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

    # markup выключен по умолчанию, в отличие от Label: названия глав приходят с сайта
    def __init__(
        self, label: str | Content, classes: str | None = None, markup: bool = False
    ) -> None:
        super().__init__(label, classes=classes, markup=markup)

    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        self.action_press()

    def action_press(self) -> None:
        # пока идёт подсветка нажатия, повторные нажатия игнорируются
        if self.disabled or not self.display or self.has_class("-active"):
            return
        self.add_class("-active")
        self.set_timer(self.PRESS_DELAY, partial(self.remove_class, "-active"))
        self.press()

    def press(self) -> None:
        """Сообщение о нажатии; подклассы шлют свои."""
        self.post_message(ClickableLabel.Pressed(self))
