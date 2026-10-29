from typing import ClassVar, Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Label, OptionList

from ..core.markup import tweak_kind
from ..core.merge import Choice, Hunk, Manual
from .diff import ParagraphDiff
from .editor import ChapterEditor

KINDS = {
    "site": "сайт",
    "mine": "черновик",
    "same": "оба одинаково",
    "conflict": "КОНФЛИКТ",
}
CHOICES = {"mine": "Черновик", "site": "Сайт", "both": "Оба"}
FILTERS = ("все правки", "без правок оформления", "только конфликты")
# действия, доступные во время ручной правки
EDIT_ACTIONS = {"apply", "cancel"}


class MergeScreen(Screen[tuple[bool, dict[int, Choice]]]):
    """Ревью слияния по блокам: решения и True у "Готово", False у Esc."""

    CSS_PATH = "../tcss/merge.tcss"
    # иначе фокус получит скрытый редактор и заберёт клавиши 1/2/3
    AUTO_FOCUS = ""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "choose('mine')", "Черновик"),
        Binding("2", "choose('site')", "Сайт"),
        Binding("3", "choose('both')", "Оба"),
        # буквы и в русской раскладке: у, с, а на тех же клавишах
        Binding("e,у", "manual", "Вручную", key_display="e"),
        Binding("down,right", "step(1)", "Правки", key_display="↑↓"),
        Binding("up,left", "step(-1)", show=False),
        Binding("c,с", "next_conflict", "Конфликт", key_display="c"),
        Binding("f,а", "filter", "Фильтр", key_display="f"),
        Binding("pagedown", "scroll(1)", show=False),
        Binding("pageup", "scroll(-1)", show=False),
        # у ctrl+s и escape по два действия: какое доступно, решает check_action
        Binding("ctrl+s", "done", "Готово"),
        Binding("ctrl+s", "apply", "Применить"),
        Binding("escape", "close", "Закрыть"),
        Binding("escape", "cancel", "Отмена"),
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
        # блок, который сейчас правится вручную
        self.editing: int | None = None
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

    def chosen(self, i: int) -> Choice | None:
        """Решение блока; у блока не конфликта без решения - его сторона."""
        choice = self.choices.get(i)
        kind = self.hunk(i).kind
        if choice is not None or kind == "conflict":
            return choice
        return "mine" if kind == "mine" else "site"

    def choice_name(self, i: int) -> str:
        choice = self.chosen(i)
        if choice is None:
            return "не выбрано"
        return "Вручную" if isinstance(choice, Manual) else CHOICES[choice]

    def kind_name(self, i: int) -> str:
        marks = self.tweak[i]
        name = KINDS[self.hunk(i).kind]
        return f"{name} · {', '.join(sorted(marks))}" if marks else name

    def compose(self) -> ComposeResult:
        with Horizontal(id="merge_top"):
            yield Label(id="merge_head")
            yield Label(id="merge_count")
        yield Label("\n".join(self.revs), id="merge_revs", markup=False)
        with Horizontal(id="merge_main"):
            hunks = OptionList(id="merge_list")
            hunks.border_title = "Правки"
            # список - для мыши, ↑↓ работают на экране
            hunks.can_focus = False
            yield hunks
            with Vertical(id="merge_right"):
                with Horizontal(id="merge_status"):
                    yield Label(id="merge_kind")
                    yield Label(id="merge_mark")
                with Container(id="merge_work"):
                    with VerticalScroll(id="merge_body", can_focus=False):
                        yield ParagraphDiff(id="merge_base")
                        yield ParagraphDiff(id="merge_site")
                        yield ParagraphDiff(id="merge_mine")
                        yield ParagraphDiff(id="merge_manual")
                    editor = ChapterEditor(id="merge_editor")
                    editor.border_title = "e · Вручную"
                    yield editor
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#merge_revs").display = bool(self.revs)
        self.show()

    def show(self) -> None:
        visible = self.filtered()
        # экраны слияния нескольких глав открываются стопкой
        head = f"Слияние: {self.chapter}"
        if self.current in visible:
            head += f" · правка {visible.index(self.current) + 1} из {len(visible)}"
        head += f" · фильтр: {FILTERS[self.filter]}"
        self.query_one("#merge_head", Label).update(Text(head))
        unresolved = len(self.unresolved())
        count = self.query_one("#merge_count", Label)
        count.update(f"не разобрано конфликтов: {unresolved}")
        count.display = unresolved > 0
        self.refresh_bindings()

        hunks = self.query_one("#merge_list", OptionList)
        hunks.clear_options()
        # None - разделитель между правками
        hunks.add_options(x for i in visible for x in (self.list_item(i), None))
        if self.current in visible:
            hunks.highlighted = visible.index(self.current)

        kind = self.query_one("#merge_kind", Label)
        mark = self.query_one("#merge_mark", Label)
        kind.display = self.query_one("#merge_work").display = self.current in visible
        if self.current not in visible:
            mark.update("В этом фильтре правок нет")
            return
        h = self.hunk(self.current)
        kind.update(Text(self.kind_name(self.current)))
        kind.set_class(h.kind == "conflict", "-conflict")
        name = self.choice_name(self.current)
        mark.update(Text(f"абзац {h.start + 1} · решение: {name}"))
        choice = self.chosen(self.current)
        self.query_one("#merge_base", ParagraphDiff).show("Было", None, h.base)
        site = self.query_one("#merge_site", ParagraphDiff)
        site.show("2 · На сайте", h.base, h.site)
        site.set_class(choice in ("site", "both"), "-chosen")
        mine = self.query_one("#merge_mine", ParagraphDiff)
        mine.show("1 · Черновик", h.base, h.mine)
        mine.set_class(choice in ("mine", "both"), "-chosen")
        manual = self.query_one("#merge_manual", ParagraphDiff)
        manual.display = isinstance(choice, Manual)
        if isinstance(choice, Manual):
            manual.show("e · Вручную", h.base, choice.text.split("\n"))
        self.query_one("#merge_body", VerticalScroll).scroll_home(animate=False)

    def list_item(self, i: int) -> Text:
        conflict = self.hunk(i).kind == "conflict"
        text = Text(f"абз. {self.hunk(i).start + 1}", "bold")
        text.append(f"  {self.kind_name(i)}", "bold" if conflict else "dim")
        text.append("\n→ ")
        text.append(self.choice_name(i), "" if self.chosen(i) else "italic")
        return text

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if self.editing is not None:
            return action in EDIT_ACTIONS
        if action in EDIT_ACTIONS:
            return False
        if action == "done":
            # видно всегда, но недоступно, пока не разобраны конфликты
            return None if self.unresolved() else True
        return True

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
        editor = self.query_one("#merge_editor", ChapterEditor)
        if isinstance(choice, Manual):
            editor.load_text(choice.text)
            editor.border_subtitle = ""
        else:
            editor.load_text("\n".join(self.hunk(current).mine))
            editor.border_subtitle = "начато с черновика"
        # прежний ручной вариант теперь в редакторе
        self.query_one("#merge_manual").display = False
        self.editing = current
        # иначе клик по списку уведёт подсветку с правимого блока
        self.query_one("#merge_list").disabled = True
        self.add_class("-editing")
        self.refresh_bindings()
        editor.focus()
        self.call_after_refresh(self.fit_editor)

    def fit_editor(self) -> None:
        # варианты не влезли над редактором: ставим их слева от него
        if self.query_one("#merge_body", VerticalScroll).max_scroll_y > 0:
            self.add_class("-side")

    def stop_editing(self) -> None:
        self.editing = None
        self.query_one("#merge_list").disabled = False
        self.remove_class("-editing", "-side")
        self.set_focus(None)
        self.show()

    def action_apply(self) -> None:
        if self.editing is not None:
            text = self.query_one("#merge_editor", ChapterEditor).text
            self.choices[self.editing] = Manual(text)
            self.stop_editing()

    @on(OptionList.OptionSelected, "#merge_list")
    def pick(self, event: OptionList.OptionSelected) -> None:
        self.current = self.filtered()[event.option_index]
        self.show()

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

    def action_cancel(self) -> None:
        self.stop_editing()

    def action_close(self) -> None:
        self.dismiss((False, self.choices))

    def action_done(self) -> None:
        if not self.unresolved():
            self.dismiss((True, self.choices))


__all__ = ["MergeScreen"]
