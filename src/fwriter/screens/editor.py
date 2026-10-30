from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual.actions import SkipAction
from textual.binding import Binding, BindingType
from textual.geometry import Region, Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import TextArea
from textual.widgets.text_area import Document

from ..core.markup import TAGS, Edit, Reader, Run, apply, tag_list, wrap
from .layout import DASHES, ScreenLines, paragraph, run_style


class ChapterEditor(TextArea):
    """Сырая разметка: клавиши тегов и подсветка тегов, которые видит читатель."""

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
        Binding("pageup", "paragraph(-1)", show=False),
        Binding("pagedown", "paragraph(1)", show=False),
        Binding("shift+pageup", "paragraph(-1, True)", show=False),
        Binding("shift+pagedown", "paragraph(1, True)", show=False),
    ]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # только для чтения tab уходит к экрану и переводит фокус
        # undo TextArea read_only не проверяет: во время F5 откат затёр бы ответ сайта
        if action in ("tag", "undo", "redo"):
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

    def action_paragraph(self, delta: int, extend: bool = False) -> None:
        """в начало предыдущего или следующего абзаца - сырой строки"""
        rows = self.document.lines
        row, col = self.cursor_location
        target = paragraph(
            lambda i: rows[i].strip(" \t\r") == "", len(rows), row, col == 0, delta
        )
        if target is not None:
            self.move_cursor((target, 0), select=extend)
        elif delta > 0:
            self.move_cursor((len(rows) - 1, len(rows[-1])), select=extend)

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
            if name := DASHES.get(ch):
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


class ReaderPanel(ScrollView, can_focus=False):
    """Глава, как её увидит читатель: строки Reader вкладки с переносом по ширине."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "reader-panel--footnote",
        "reader-panel--current",
    }

    def __init__(self, reader: Reader) -> None:
        super().__init__()
        self.screen_lines = ScreenLines(reader, hidden=None)
        self.current = 0  # строка читателя под курсором редактора

    @property
    def reader(self) -> Reader:
        return self.screen_lines.reader

    def show(self, rows: list[str], row: int, col: int) -> None:
        """подсветить строку читателя под курсором и прокрутить к ней"""
        self.current = self.reader.find(rows, row, col)
        tops = self.screen_lines.tops
        if len(tops) > self.current + 1:
            top, bottom = tops[self.current], tops[self.current + 1]
            self.scroll_to_region(Region(0, top, 1, bottom - top), animate=False)
        self.refresh()

    def align(self, rows: list[str], row: int, shift: int) -> None:
        """прокрутить вровень с редактором: его верхняя строка документа row,
        shift - сколько её экранных строк ушло выше края"""
        n = self.reader.find(rows, row, 0)
        tops = self.screen_lines.tops
        if len(tops) > n + 1:
            top, bottom = tops[n], tops[n + 1]
            self.scroll_to(y=top + min(shift, max(bottom - top - 1, 0)), animate=False)

    def style(self, run: Run) -> Style:
        # без partial: полупрозрачный цвет темы смешан с фоном, а не стал белым
        footnote = self.get_component_rich_style("reader-panel--footnote").color
        return run_style(run, Style(color=footnote))

    def relayout(self) -> None:
        width = self.scrollable_content_region.width
        if width <= 0:
            return
        self.screen_lines.relayout(width)
        self.virtual_size = Size(width, self.screen_lines.tops[-1])
        self.refresh()

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        row = y + self.scroll_offset.y
        style = self.rich_style
        if row >= self.screen_lines.tops[-1]:
            return Strip.blank(width, style)
        if self.screen_lines.line_at(row) == self.current:
            style += self.get_component_rich_style("reader-panel--current")
        text = self.screen_lines.render(row, self.style)
        strip = Strip(text.render(self.app.console), text.cell_len)
        return strip.crop_extend(0, width, None).apply_style(style)

    def on_resize(self) -> None:
        self.relayout()

    def notify_style_update(self) -> None:
        super().notify_style_update()
        self.relayout()
