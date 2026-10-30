import re
from typing import ClassVar

from rich.style import Style
from textual import events
from textual.binding import Binding, BindingType
from textual.geometry import Region, Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets.text_area import Document, Selection

from ..core.markup import (
    Reader,
    Run,
    adds_tag,
    enter,
    next_char,
    prev_char,
    strip_text,
    toggle,
)
from .editor import ChapterEditor
from .layout import DASHES, ScreenLines, paragraph, run_style

_WORD = re.compile(r"\w+")

Location = tuple[int, int]


class VisualEditor(ScrollView, can_focus=True):
    """WYSIWYG: текст, как его увидит читатель, без тегов.

    Текст, курсор и история Ctrl+Z - у ChapterEditor: правки идут в его сырой
    текст через replace(), поэтому у обоих режимов они общие, а правка локальна.
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "visual-editor--hidden",
        "visual-editor--footnote",
        "visual-editor--cursor",
        "visual-editor--selection",
        *(f"visual-editor--{name}" for name in DASHES.values()),
    }

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "move('left')", show=False),
        Binding("right", "move('right')", show=False),
        Binding("up", "move('up')", show=False),
        Binding("down", "move('down')", show=False),
        Binding("home", "move('home')", show=False),
        Binding("end", "move('end')", show=False),
        Binding("ctrl+left", "move('word_left')", show=False),
        Binding("ctrl+right", "move('word_right')", show=False),
        Binding("shift+left", "move('left', True)", show=False),
        Binding("shift+right", "move('right', True)", show=False),
        Binding("shift+up", "move('up', True)", show=False),
        Binding("shift+down", "move('down', True)", show=False),
        Binding("shift+home", "move('home', True)", show=False),
        Binding("shift+end", "move('end', True)", show=False),
        Binding("ctrl+shift+left", "move('word_left', True)", show=False),
        Binding("ctrl+shift+right", "move('word_right', True)", show=False),
        Binding("pageup", "paragraph(-1)", show=False),
        Binding("pagedown", "paragraph(1)", show=False),
        Binding("shift+pageup", "paragraph(-1, True)", show=False),
        Binding("shift+pagedown", "paragraph(1, True)", show=False),
        Binding("backspace", "delete(False)", show=False),
        Binding("delete", "delete(True)", show=False),
        Binding("enter", "enter", show=False),
        Binding("tab", "tab", show=False),
        Binding("ctrl+b", "format('b')", show=False),
        Binding("alt+i", "format('i')", show=False),
        Binding("ctrl+t", "format('s')", show=False),
        Binding("ctrl+e", "format('center')", show=False),
        Binding("ctrl+r", "format('right')", show=False),
        Binding("ctrl+n", "format('footnote')", show=False),
        Binding("ctrl+z", "undo", show=False),
        Binding("ctrl+y", "redo", show=False),
        Binding("ctrl+c", "copy", show=False),
        Binding("ctrl+x", "cut", show=False),
        Binding("ctrl+v", "paste", show=False),
    ]

    def __init__(self, editor: ChapterEditor, reader: Reader) -> None:
        super().__init__()
        self.editor = editor
        self.screen_lines = ScreenLines(reader, hidden="·")
        # наш ввод уже в тексте, а Reader обновит только Changed: до него
        # клавиши сами пересобирают Reader, иначе курсор встал бы по старому тексту
        self.stale = False
        self.goal: int | None = None  # клетка, к которой стремятся ↑↓
        self.selecting = False  # тянем выделение мышью
        # раскладка по текущему Reader: скрытый виджет без ширины её не строит
        self.laid = False

    @property
    def reader(self) -> Reader:
        return self.screen_lines.reader

    @property
    def rows(self) -> list[str]:
        return self.editor.document.lines

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # только для чтения tab уходит к экрану и переводит фокус, как у ChapterEditor
        if action in ("tab", "format"):
            return not self.editor.read_only
        return super().check_action(action, parameters)

    def on_mount(self) -> None:
        self.watch(self.editor, "selection", self.selection_changed, init=False)

    def selection_changed(self) -> None:
        self.refresh()

    def relayout(self) -> None:
        """текст поменялся: Reader вкладки уже обновлён"""
        self.stale = False
        width = self.scrollable_content_region.width
        self.laid = width > 0
        if not self.laid:
            return
        self.screen_lines.relayout(width)
        self.virtual_size = Size(width, self.screen_lines.tops[-1])
        self.refresh()

    def fresh(self) -> bool:
        """раскладка по текущему тексту; False - её ещё нет, клавиши ждут"""
        if self.stale:
            self.reader.update(self.rows)
            self.relayout()
        return self.laid

    def on_resize(self) -> None:
        self.relayout()

    def to_cell(self, loc: Location) -> tuple[int, int]:
        """позиция документа -> экранная строка и клетка"""
        return self.screen_lines.cell(*self.reader.locate(self.rows, *loc))

    def to_location(self, y: int, x: int) -> Location:
        return self.reader.location(self.rows, *self.screen_lines.col(y, x))

    def shown(self) -> None:
        """F7 показал редактор: раскладка по ширине и курсор на экране"""
        self.relayout()
        self.show_cursor()

    def show_cursor(self) -> None:
        if not self.fresh():
            return
        y, x = self.to_cell(self.editor.selection.end)
        self.scroll_to_region(Region(x, y, 1, 1), animate=False)

    def select(self, loc: Location, extend: bool) -> None:
        anchor = self.editor.selection.start if extend else loc
        self.editor.selection = Selection(anchor, loc)
        self.show_cursor()

    def action_move(self, where: str, extend: bool = False) -> None:
        if not self.fresh():
            return
        n, c = self.reader.locate(self.rows, *self.editor.selection.end)
        lines = self.screen_lines
        text = lines.text(n)
        if where in ("up", "down"):
            y, x = lines.cell(n, c)
            self.goal = x if self.goal is None else self.goal
            y += 1 if where == "down" else -1
            if 0 <= y < lines.tops[-1]:
                self.select(self.to_location(y, self.goal), extend)
            return
        self.goal = None
        if where in ("left", "right"):
            self.step(n, c, -1 if where == "left" else 1, extend)
            return
        elif where in ("home", "end"):
            _, a, b, _ = lines.row(lines.cell(n, c)[0])
            c = a if where == "home" else b
        elif where == "word_left" and c == 0:
            n, c = n - 1, -1
        elif where == "word_left":
            c = max(
                (m.start() for m in _WORD.finditer(text) if m.start() < c), default=0
            )
        elif c == len(text):
            n, c = n + 1, 0
        else:
            ends = (m.end() for m in _WORD.finditer(text) if m.end() > c)
            c = min(ends, default=len(text))
        if not 0 <= n < len(self.reader.lines):
            return
        if c < 0:
            c = len(lines.text(n))
        self.select(self.reader.location(self.rows, n, c), extend)

    def action_paragraph(self, delta: int, extend: bool = False) -> None:
        """в начало предыдущего или следующего абзаца - строки читателя"""
        if not self.fresh():
            return
        self.goal = None
        lines = self.screen_lines.lines
        n, c = self.reader.locate(self.rows, *self.editor.selection.end)
        target = paragraph(
            lambda i: all(run.hidden for run in lines[i].runs),
            len(lines),
            n,
            c == 0,
            delta,
        )
        if target is not None:
            self.select(self.reader.location(self.rows, target, 0), extend)
        elif delta > 0:
            end = len(lines) - 1
            self.select(
                self.reader.location(self.rows, end, len(self.screen_lines.text(end))),
                extend,
            )

    def step(self, n: int, c: int, delta: int, extend: bool) -> None:
        """на клетку влево или вправо; отступ <tab> - одна позиция текста, его
        клетки проходим разом"""
        here = self.editor.selection.end
        while True:
            c += delta
            if c < 0:
                n -= 1
                if n < 0:
                    return
                c = len(self.screen_lines.text(n))
            elif c > len(self.screen_lines.text(n)):
                n += 1
                if n >= len(self.screen_lines.lines):
                    return
                c = 0
            loc = self.reader.location(self.rows, n, c)
            if loc != here:
                self.select(loc, extend)
                return

    def replace(
        self, text: str, start: Location, end: Location, tags: bool = False
    ) -> bool:
        """правка сырого текста; ввод, после которого появился бы тег, не
        принимается; tags - тег ставим мы сами (Tab)"""
        if self.editor.read_only:
            return False
        rows = self.rows
        old = "\n".join(rows[start[0] : end[0] + 1])
        cut = sum(len(r) + 1 for r in rows[start[0] : end[0]]) + end[1]
        new = old[: start[1]] + text + old[cut:]
        if not tags and adds_tag(old, new):
            self.notify(
                "Оформление - клавишами (F8), сами теги - в режиме разметки (F7)",
                severity="warning",
            )
            return False
        result = self.editor.replace(text, start, end, maintain_selection_offset=False)
        self.editor.selection = Selection.cursor(result.end_location)
        self.stale = True
        self.goal = None
        self.call_after_refresh(self.show_cursor)
        return True

    def replace_selection(self, text: str) -> bool:
        start, end = sorted(self.editor.selection)
        # теги в выделении остаются: удаляем только то, что видит читатель
        kept = strip_text(self.editor.get_text_range(start, end))
        return self.replace(text + kept, start, end)

    def action_enter(self) -> None:
        selection = self.editor.selection
        if not selection.is_empty:
            self.replace_selection("\n")
            return
        row, col = selection.end
        at, text = enter(self.rows[row], col, row == len(self.rows) - 1)
        if at == col:
            self.replace(text, (row, col), (row, col))
            return
        # новый абзац за закрывающим тегом блока: курсор - в его начало
        if text and not self.replace(text, (row, at), (row, at)):
            return
        self.select((row, at), False)

    def action_tab(self) -> None:
        start, end = sorted(self.editor.selection)
        kept = strip_text(self.editor.get_text_range(start, end))
        self.replace("<tab>" + kept, start, end, tags=True)

    def action_format(self, name: str) -> None:
        if not self.fresh():
            return
        start, end = sorted(self.editor.selection)
        # кусок, на краях которого не открыт ни один тег: не вся глава
        first, stop = self.reader.span(start[0], end[0])
        # и следующая строка: </center> может встать вместо \n перед ней
        rows = self.rows[first : stop + 1]

        def index(loc: Location) -> int:
            return sum(len(r) + 1 for r in rows[: loc[0] - first]) + loc[1]

        result = toggle("\n".join(rows), index(start), index(end), name)
        if isinstance(result, str):
            self.notify(result, severity="warning")
            return
        (lo, hi, part), a, b = result
        doc = self.editor.document
        # без языка подсветки - всегда Document
        assert isinstance(doc, Document)
        base = doc.get_index_from_location((first, 0))
        self.editor.apply_edits([(base + lo, base + hi, part)])
        self.editor.selection = Selection(
            doc.get_location_from_index(base + a),
            doc.get_location_from_index(base + b),
        )
        self.stale = True
        self.goal = None
        self.call_after_refresh(self.show_cursor)

    def action_delete(self, forward: bool) -> None:
        if not self.fresh():
            return
        selection = self.editor.selection
        if not selection.is_empty:
            self.replace_selection("")
            return
        row, col = selection.end
        n, c = self.reader.locate(self.rows, row, col)
        size = len(self.screen_lines.text(n))
        line = self.rows[row]
        span = next_char(line, col) if forward else prev_char(line, col)
        if span is None:
            # до края строки документа только теги: стираем перевод строки
            if forward and row + 1 < len(self.rows):
                self.replace("", (row, len(line)), (row + 1, 0))
            elif not forward and row > 0:
                self.replace("", (row - 1, len(self.rows[row - 1])), (row, 0))
        # символ за краем строки читателя (за </center>) - другой абзац у читателя
        elif (c < size) if forward else (c > 0):
            self.replace("", (row, span[0]), (row, span[1]))

    def action_undo(self) -> None:
        if self.editor.read_only:
            return
        self.editor.action_undo()
        self.stale = True
        self.call_after_refresh(self.show_cursor)

    def action_redo(self) -> None:
        if self.editor.read_only:
            return
        self.editor.action_redo()
        self.stale = True
        self.call_after_refresh(self.show_cursor)

    def action_copy(self) -> None:
        # с тегами: вставка в режиме разметки сохранит оформление
        if text := self.editor.selected_text:
            self.app.copy_to_clipboard(text)

    def action_cut(self) -> None:
        if text := self.editor.selected_text:
            self.app.copy_to_clipboard(text)
            self.replace_selection("")

    def action_paste(self) -> None:
        self.replace_selection(self.app.clipboard)

    def on_paste(self, event: events.Paste) -> None:
        self.replace_selection(event.text)

    def on_key(self, event: events.Key) -> None:
        if event.is_printable and event.character:
            event.stop()
            event.prevent_default()
            self.replace_selection(event.character)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if (offset := event.get_content_offset(self)) is None:
            return
        if not self.fresh():
            return
        self.focus()
        self.goal = None
        loc = self.to_location(offset.y + int(self.scroll_y), offset.x)
        self.select(loc, event.shift)
        self.selecting = True
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.selecting and (offset := event.get_content_offset(self)):
            self.select(self.to_location(offset.y + int(self.scroll_y), offset.x), True)

    def on_mouse_up(self) -> None:
        self.selecting = False
        self.release_mouse()

    def style(self, run: Run) -> Style:
        # без partial: полупрозрачный цвет темы смешан с фоном, а не стал белым
        footnote = self.get_component_rich_style("visual-editor--footnote").color
        style = run_style(run, Style(color=footnote))
        if run.hidden:
            style += Style(
                color=self.get_component_rich_style("visual-editor--hidden").color
            )
        return style

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        row = y + self.scroll_offset.y
        lines = self.screen_lines
        if row >= lines.tops[-1]:
            return Strip.blank(width, self.rich_style)
        n, a, b, indent = lines.row(row)
        text = lines.render(row, self.style)
        shown = lines.text(n)
        for i in range(a, b):
            if name := DASHES.get(shown[i]):
                style = self.get_component_rich_style(f"visual-editor--{name}")
                text.stylize(
                    Style(bgcolor=style.bgcolor), indent + i - a, indent + i - a + 1
                )
        text.pad_right(width)
        start, end = sorted(self.editor.selection)
        if start != end:
            (sy, sx), (ey, ex) = self.to_cell(start), self.to_cell(end)
            if sy <= row <= ey:
                lo = sx if sy == row else indent
                hi = ex if ey == row else indent + b - a
                style = self.get_component_rich_style("visual-editor--selection")
                text.stylize(Style(bgcolor=style.bgcolor), lo, hi)
        if self.has_focus:
            cy, cx = self.to_cell(self.editor.selection.end)
            if cy == row:
                style = self.get_component_rich_style("visual-editor--cursor")
                text.stylize(
                    Style(color=style.color, bgcolor=style.bgcolor), cx, cx + 1
                )
        strip = Strip(text.render(self.app.console), text.cell_len)
        return strip.crop_extend(0, width, None).apply_style(self.rich_style)

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()
