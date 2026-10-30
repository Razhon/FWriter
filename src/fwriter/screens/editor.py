import bisect
import re
from typing import ClassVar

from rich.cells import cell_len, chop_cells
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.actions import SkipAction
from textual.binding import Binding, BindingType
from textual.geometry import Region, Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import TextArea
from textual.widgets.text_area import Document

from ..core.markup import TAGS, Edit, Line, Reader, apply, tag_list, wrap


class ChapterEditor(TextArea):
    """Сырая разметка: клавиши тегов и подсветка тегов, которые видит читатель."""

    # длинное и среднее тире в моноширинном шрифте не отличить от дефиса
    DASHES: ClassVar[dict[str, str]] = {"—": "emdash", "–": "endash"}

    # цвет каждого тега и фон тире - в main.tcss
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        f"chapter-editor--{t}" for t in (*TAGS, *DASHES.values(), "error")
    }

    # строка с первой ошибкой разметки
    error_line: int | None = None

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
        end = self.apply_edits(edits)
        # пара без выделения - курсор между тегами, иначе после вставленного
        at = a + len(tag) + 2 if a == b and tag != "tab" else end
        self.move_cursor(doc.get_location_from_index(at))

    def apply_edits(self, edits: list[Edit]) -> int:
        """Правки одной заменой - один шаг Ctrl+Z; возвращает конец вставленного."""
        doc = self.document
        assert isinstance(doc, Document)
        # каждая вставка длиннее символа - отдельный шаг Ctrl+Z
        lo, hi = min(e[0] for e in edits), max(e[1] for e in edits)
        part = apply(self.text[lo:hi], [(s - lo, e - lo, r) for s, e, r in edits])
        self.replace(
            part,
            doc.get_location_from_index(lo),
            doc.get_location_from_index(hi),
            maintain_selection_offset=False,
        )
        return lo + len(part)

    def mark_error(self, line: int | None) -> None:
        if line != self.error_line:
            self.error_line = line
            self.notify_style_update()
            self.refresh()

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        if line_index == self.error_line:
            line.stylize(
                self.get_component_rich_style("chapter-editor--error", partial=True)
            )
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


_WORD = re.compile("[^ ]* *")


def breaks(text: str, width: int) -> list[tuple[int, int]]:
    """перенос по пробелам: срезы экранных строк без пробелов на конце"""
    out: list[tuple[int, int]] = []
    start = used = 0
    for m in _WORD.finditer(text):
        word = m.group().rstrip(" ")
        if used and word and used + cell_len(word) > width:
            out.append((start, m.start()))
            start, used = m.start(), 0
        used += cell_len(m.group())
    out.append((start, len(text)))
    folded = []
    for a, b in out:
        while b > a and text[b - 1] == " ":
            b -= 1
        # слово длиннее ширины режем, как overflow-wrap у читателя
        for piece in chop_cells(text[a:b], width) or [""]:
            folded.append((a, a + len(piece)))
            a += len(piece)
    return folded


class ReaderPanel(ScrollView, can_focus=False):
    """Глава, как её увидит читатель: строки markup.Reader с переносом по ширине."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "reader-panel--footnote",
        "reader-panel--current",
    }

    def __init__(self) -> None:
        super().__init__()
        self.reader = Reader()
        # перенос по id строки: Reader отдаёт неизменённые строки теми же объектами
        self.wraps: dict[int, tuple[Line, list[Strip]]] = {}
        self.tops = [0]  # первая экранная строка каждой строки читателя и конец
        self.current = 0  # строка читателя под курсором редактора

    def update(self, rows: list[str]) -> None:
        self.reader.update(rows)
        self.relayout()

    def show(self, rows: list[str], row: int, col: int) -> None:
        """подсветить строку читателя под курсором и прокрутить к ней"""
        self.current = self.reader.find(rows, row, col)
        if len(self.tops) > self.current + 1:
            top, bottom = self.tops[self.current], self.tops[self.current + 1]
            self.scroll_to_region(Region(0, top, 1, bottom - top), animate=False)
        self.refresh()

    def align(self, rows: list[str], row: int, shift: int) -> None:
        """прокрутить вровень с редактором: его верхняя строка документа row,
        shift - сколько её экранных строк ушло выше края"""
        n = self.reader.find(rows, row, 0)
        if len(self.tops) > n + 1:
            top, bottom = self.tops[n], self.tops[n + 1]
            self.scroll_to(y=top + min(shift, max(bottom - top - 1, 0)), animate=False)

    def wrap(self, line: Line, width: int) -> list[Strip]:
        # без partial: полупрозрачный цвет темы смешан с фоном, а не стал белым
        footnote = Style(
            color=self.get_component_rich_style("reader-panel--footnote").color
        )
        parts: list[tuple[int, str, Style]] = []  # начало в строке, текст, стиль
        at = 0
        for run in line.runs:
            if not run.hidden:
                style = Style(
                    bold="b" in run.style,
                    italic="i" in run.style,
                    strike="s" in run.style,
                )
                if "footnote" in run.style:
                    style += footnote
                parts.append((at, run.text, style))
                at += len(run.text)
        strips = []
        shown = "".join(text for _, text, _ in parts)
        for a, b in breaks(shown, width):
            pad = width - cell_len(shown[a:b])
            indent = {"left": 0, "center": pad // 2}.get(line.align, pad)
            segments = [Segment(" " * indent)]
            for o, text, style in parts:
                if max(a, o) < min(b, o + len(text)):
                    segments.append(Segment(text[max(a, o) - o : b - o], style))
            strips.append(Strip(segments))
        return strips

    def relayout(self) -> None:
        width = self.scrollable_content_region.width
        if width <= 0:
            return
        old, self.wraps = self.wraps, {}
        self.tops = [0]
        for line in self.reader.lines:
            hit = self.wraps.get(id(line)) or old.get(id(line))
            if hit is None or hit[0] is not line:
                hit = line, self.wrap(line, width)
            self.wraps[id(line)] = hit
            self.tops.append(self.tops[-1] + len(hit[1]))
        self.virtual_size = Size(width, self.tops[-1])
        self.refresh()

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        row = y + self.scroll_offset.y
        style = self.rich_style
        if row >= self.tops[-1]:
            return Strip.blank(width, style)
        n = bisect.bisect_right(self.tops, row) - 1
        strip = self.wraps[id(self.reader.lines[n])][1][row - self.tops[n]]
        if n == self.current:
            style += self.get_component_rich_style("reader-panel--current")
        return strip.crop_extend(0, width, None).apply_style(style)

    def on_resize(self) -> None:
        self.wraps.clear()
        self.relayout()

    def notify_style_update(self) -> None:
        super().notify_style_update()
        self.wraps.clear()
        self.relayout()
