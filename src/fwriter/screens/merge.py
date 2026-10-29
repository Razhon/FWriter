from typing import ClassVar, Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Label

from ..core.markup import tweak_kind
from ..core.merge import Choice, Hunk, Manual
from ..core.storage import Version
from .diff import ParagraphDiff
from .editor import ChapterEditor

KINDS = {
    "site": "сайт",
    "mine": "моё",
    "same": "оба одинаково",
    "conflict": "КОНФЛИКТ",
}
CHOICES = {"mine": "Моё", "site": "Сайт", "both": "Оба"}
FILTERS = ("все правки", "без правок оформления", "только конфликты")


class ManualScreen(ModalScreen[str | None]):
    """Ручной вариант блока: Ctrl+S - готово, Esc - отмена."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "done", "Готово"),
        Binding("escape", "dismiss", "Отмена"),
    ]

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="manual"):
            yield Label("Вручную", id="manual_title")
            yield ChapterEditor(self.text)
        yield Footer()

    def action_done(self) -> None:
        self.dismiss(self.query_one(ChapterEditor).text)


class MergeScreen(Screen[tuple[bool, dict[int, Choice]]]):
    """Ревью слияния по блокам: решения и True у "Готово", False у Esc."""

    CSS_PATH = "../tcss/merge.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "choose('mine')", "Моё"),
        Binding("2", "choose('site')", "Сайт"),
        Binding("3", "choose('both')", "Оба"),
        Binding("e", "manual", "Вручную"),
        Binding("down,right", "step(1)", "Правки", key_display="↑↓"),
        Binding("up,left", "step(-1)", show=False),
        Binding("c", "next_conflict", "Конфликт"),
        Binding("f", "filter", "Фильтр"),
        Binding("pagedown", "scroll(1)", show=False),
        Binding("pageup", "scroll(-1)", show=False),
        Binding("escape", "close", "Закрыть"),
    ]

    def __init__(
        self,
        chapter: str,
        parts: list[str | Hunk],
        choices: dict[int, Choice],
        revs: list[str],
    ) -> None:
        """chapter - название главы; choices - прежние решения (F4); revs - правки сайта."""
        self.chapter = chapter
        self.parts = parts
        self.choices = dict(choices)
        self.revs = revs
        self.hunks = [i for i, p in enumerate(parts) if isinstance(p, Hunk)]
        last = len(parts) - 1
        # метки правки только оформления: tweak_kind() у каждой изменённой стороны;
        # None - есть правка не оформления
        self.tweak: dict[int, set[str] | None] = {}
        for i in self.hunks:
            h = self.hunk(i)
            kinds = [
                tweak_kind(h.base, side, i == last)
                for side in (h.site, h.mine)
                if side != h.base
            ]
            self.tweak[i] = None if None in kinds else {k for k in kinds if k}
        self.filter = 0
        self.current = self.hunks[0] if self.hunks else None
        super().__init__()

    def hunk(self, i: int) -> Hunk:
        part = self.parts[i]
        assert isinstance(part, Hunk)
        return part

    def filtered(self) -> list[int]:
        if self.filter == 2:
            return [i for i in self.hunks if self.hunk(i).kind == "conflict"]
        if self.filter == 1:
            return [
                i
                for i in self.hunks
                if self.hunk(i).kind == "conflict" or self.tweak[i] is None
            ]
        return self.hunks

    def unresolved(self) -> list[int]:
        return [
            i
            for i in self.hunks
            if self.hunk(i).kind == "conflict" and i not in self.choices
        ]

    def compose(self) -> ComposeResult:
        yield Label(id="merge_head")
        yield Label("\n".join(self.revs), id="merge_revs", markup=False)
        with VerticalScroll(id="merge_body", can_focus=False):
            yield Label(id="merge_mark")
            yield ParagraphDiff(id="merge_base")
            yield ParagraphDiff(id="merge_site")
            yield ParagraphDiff(id="merge_mine")
            yield ParagraphDiff(id="merge_manual")
        yield Button("Готово", id="merge_done", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#merge_revs").display = bool(self.revs)
        self.show()

    def show(self) -> None:
        visible = self.filtered()
        head = f"Фильтр: {FILTERS[self.filter]}"
        if self.current in visible:
            head = (
                f"Правка {visible.index(self.current) + 1} из {len(visible)} · {head}"
            )
        # экраны слияния нескольких глав открываются стопкой
        head = f"Слияние: {self.chapter}\n{head}"
        unresolved = len(self.unresolved())
        if unresolved:
            head += f" · не разобрано конфликтов: {unresolved}"
        self.query_one("#merge_done", Button).disabled = unresolved > 0
        body = self.query_one("#merge_body")
        body.display = self.current in visible
        if self.current not in visible:
            head += "\nВ этом фильтре правок нет"
            self.query_one("#merge_head", Label).update(Text(head))
            return
        h = self.hunk(self.current)
        head += f"\nабзац {h.start + 1}"
        self.query_one("#merge_head", Label).update(Text(head))
        choice = self.choices.get(self.current)
        if isinstance(choice, Manual):
            chosen = "Вручную"
        elif choice is not None:
            chosen = CHOICES[choice]
        elif h.kind == "conflict":
            chosen = "не выбрано"
        else:
            chosen = "Моё" if h.kind == "mine" else "Сайт"
        mark = f"[{KINDS[h.kind]}]"
        marks = self.tweak[self.current]
        if marks is not None:
            mark += f" {', '.join(sorted(marks))}"
        self.query_one("#merge_mark", Label).update(Text(f"{mark}  Решение: {chosen}"))
        self.query_one("#merge_base", ParagraphDiff).show("Было", None, h.base)
        self.query_one("#merge_site", ParagraphDiff).show("На сайте", h.base, h.site)
        self.query_one("#merge_mine", ParagraphDiff).show("У меня", h.base, h.mine)
        manual = self.query_one("#merge_manual", ParagraphDiff)
        manual.display = isinstance(choice, Manual)
        if isinstance(choice, Manual):
            manual.show("Вручную", h.base, choice.text.split("\n"))
        body.scroll_home(animate=False)

    def action_choose(self, choice: Literal["mine", "site", "both"]) -> None:
        # в пустом фильтре current скрыт: решение легло бы на невидимый блок
        if self.current is not None and self.current in self.filtered():
            self.choices[self.current] = choice
            self.show()

    def action_manual(self) -> None:
        current = self.current
        if current is None or current not in self.filtered():
            return
        choice = self.choices.get(current)
        text = (
            choice.text
            if isinstance(choice, Manual)
            else "\n".join(self.hunk(current).mine)
        )

        def done(result: str | None) -> None:
            if result is not None:
                self.choices[current] = Manual(result)
                self.show()

        self.app.push_screen(ManualScreen(text), done)

    def action_step(self, delta: int) -> None:
        visible = self.filtered()
        if not visible:
            return
        if self.current in visible:
            k = visible.index(self.current) + delta
            self.current = visible[max(0, min(k, len(visible) - 1))]
        else:
            self.current = visible[0]
        self.show()

    def action_next_conflict(self) -> None:
        left = self.unresolved()
        if not left:
            self.notify("Неразобранных конфликтов нет")
            return
        # следующий после текущего, по кругу
        current = -1 if self.current is None else self.current
        self.current = next((i for i in left if i > current), left[0])
        if self.current not in self.filtered():
            self.filter = 0
        self.show()

    def action_filter(self) -> None:
        self.filter = (self.filter + 1) % len(FILTERS)
        visible = self.filtered()
        if visible and self.current not in visible:
            current = -1 if self.current is None else self.current
            after = [i for i in visible if i > current]
            self.current = after[0] if after else visible[-1]
        self.show()

    def action_scroll(self, direction: int) -> None:
        body = self.query_one("#merge_body", VerticalScroll)
        if direction > 0:
            body.scroll_page_down()
        else:
            body.scroll_page_up()

    def action_close(self) -> None:
        self.dismiss((False, self.choices))

    @on(Button.Pressed, "#merge_done")
    def done(self) -> None:
        self.dismiss((True, self.choices))


def rev_line(num: int | None, author: str | None, created: str, comment: str) -> str:
    """Правка сайта для шапки MergeScreen."""
    # у своей версии publish ника нет
    num_part = None if num is None else f"Правка номер {num}"
    # у версий сайта created до секунды, в шапке - до минуты
    parts = (num_part, author, created[:16].replace("T", " "), comment)
    return " · ".join(p for p in parts if p)


def version_line(v: Version) -> str:
    return rev_line(v.site_num, v.author_name, v.created, v.comment)


__all__ = ["MergeScreen", "rev_line", "version_line"]
