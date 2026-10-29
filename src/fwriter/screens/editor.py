from typing import ClassVar

from rich.text import Text
from textual.actions import SkipAction
from textual.binding import Binding, BindingType
from textual.widgets import TextArea
from textual.widgets.text_area import Document

from ..core.markup import TAGS, apply, tag_list, wrap


class ChapterEditor(TextArea):
    """Сырая разметка: клавиши тегов и подсветка тегов, которые видит читатель."""

    # длинное и среднее тире в моноширинном шрифте не отличить от дефиса
    DASHES: ClassVar[dict[str, str]] = {"—": "emdash", "–": "endash"}

    # цвет каждого тега и фон тире - в main.tcss
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        f"chapter-editor--{t}" for t in (*TAGS, *DASHES.values())
    }

    # ctrl+e у TextArea - конец строки, его дублирует End
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("tab", "tag('tab')", show=False),
        Binding("ctrl+b", "tag('b')", show=False),
        Binding("alt+i", "tag('i')", show=False),
        Binding("ctrl+t", "tag('s')", show=False),
        Binding("ctrl+e", "tag('center')", show=False),
        Binding("ctrl+r", "tag('right')", show=False),
        Binding("ctrl+n", "tag('footnote')", show=False),
    ]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # только для чтения tab уходит к экрану и переводит фокус
        if action == "tag":
            return not self.read_only
        return super().check_action(action, parameters)

    def action_tag(self, tag: str) -> None:
        doc = self.document
        # без языка подсветки - всегда Document
        assert isinstance(doc, Document)
        a, b = sorted(map(doc.get_index_from_location, self.selection))
        if tag == "tab":
            # <tab> одиночный: заменяет выделение
            edits = [(a, b, "<tab>")]
        else:
            edits = wrap(self.text, (a, b), tag)
        if not edits:
            return
        # одной заменой: каждая вставка длиннее символа - отдельный шаг Ctrl+Z
        lo, hi = edits[0][0], edits[-1][1]
        part = apply(self.text[lo:hi], [(s - lo, e - lo, r) for s, e, r in edits])
        self.replace(
            part,
            doc.get_location_from_index(lo),
            doc.get_location_from_index(hi),
            maintain_selection_offset=False,
        )
        # пара без выделения - курсор между тегами, иначе после вставленного
        at = a + len(tag) + 2 if a == b and tag != "tab" else lo + len(part)
        self.move_cursor(doc.get_location_from_index(at))

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        for start, end, name, _ in tag_list(line.plain):
            style = self.get_component_rich_style(
                f"chapter-editor--{name}", partial=True
            )
            line.stylize(style, start, end)
        for i, ch in enumerate(line.plain):
            if name := self.DASHES.get(ch):
                style = self.get_component_rich_style(
                    f"chapter-editor--{name}", partial=True
                )
                line.stylize(style, i, i + 1)
        return line

    # f6 и f7 у TextArea - выделение строки и всего текста, а у нас статус и режим
    def action_select_line(self) -> None:
        raise SkipAction()

    def action_select_all(self) -> None:
        raise SkipAction()

    def on_resize(self) -> None:
        # TextArea переносит строки по Resize без refresh(): строки, нарисованные
        # до этого, остаются со старым переносом до первого сдвига курсора
        self.refresh()
