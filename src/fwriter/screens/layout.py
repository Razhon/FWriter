import bisect
import re
from collections.abc import Callable

from rich.cells import cell_len, chop_cells
from rich.style import Style
from rich.text import Text

from ..core.markup import Line, Reader, Run

# длинное и среднее тире в моноширинном шрифте не отличить от дефиса:
# фон тире у обоих редакторов, цвет - в main.tcss
DASHES = {"—": "emdash", "–": "endash"}

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


def run_style(run: Run, footnote: Style) -> Style:
    """стиль отрезка читателя; footnote - добавка к тексту сноски"""
    style = Style(
        bold="b" in run.style, italic="i" in run.style, strike="s" in run.style
    )
    return style + footnote if "footnote" in run.style else style


def paragraph(
    blank: Callable[[int], bool], count: int, n: int, start: bool, delta: int
) -> int | None:
    """PgUp/PgDn: строка, в начало которой идёт курсор, мимо пустых строк;
    start - курсор уже в начале строки n; None - дальше абзацев нет"""
    if delta < 0 and not start and not blank(n):
        return n
    n += delta
    while 0 <= n < count and blank(n):
        n += delta
    return n if 0 <= n < count else None


class ScreenLines:
    """Строки Reader с переносом по ширине: экранная строка - срез текста строки читателя.

    hidden - чем рисовать пробелы, которые читатель не увидит; None - пропускать.
    С hidden текст строки той же длины, что у Reader, и столбцы - столбцы Reader.
    """

    def __init__(self, reader: Reader, hidden: str | None) -> None:
        self.reader = reader
        self.hidden = hidden
        self.width = 0
        # перенос по id строки: Reader отдаёт неизменённые строки теми же объектами
        self.wraps: dict[int, tuple[Line, str, list[tuple[int, int]]]] = {}
        self.tops = [0]  # первая экранная строка каждой строки читателя и конец
        # строки Reader на момент relayout(): Reader общий у вкладки, и его
        # обновление до relayout() не должно расходиться с tops
        self.lines: list[Line] = []

    def runs(self, line: Line) -> list[Run]:
        if self.hidden is None:
            return [run for run in line.runs if not run.hidden]
        return line.runs

    def shown(self, run: Run) -> str:
        return self.hidden * len(run.text) if run.hidden and self.hidden else run.text

    def relayout(self, width: int) -> None:
        if width != self.width:
            self.width, self.wraps = width, {}
        old, self.wraps = self.wraps, {}
        self.tops = [0]
        self.lines = list(self.reader.lines)
        for line in self.lines:
            hit = self.wraps.get(id(line)) or old.get(id(line))
            if hit is None or hit[0] is not line:
                text = "".join(self.shown(run) for run in self.runs(line))
                hit = line, text, breaks(text, max(width, 1))
            self.wraps[id(line)] = hit
            self.tops.append(self.tops[-1] + len(hit[2]))

    def text(self, n: int) -> str:
        """текст строки читателя n, как на экране"""
        return self.wraps[id(self.lines[n])][1]

    def line_at(self, y: int) -> int:
        return bisect.bisect_right(self.tops, y) - 1

    def row(self, y: int) -> tuple[int, int, int, int]:
        """строка читателя, срез её текста и отступ выравнивания"""
        n = self.line_at(y)
        line, text, rows = self.wraps[id(self.lines[n])]
        a, b = rows[y - self.tops[n]]
        pad = max(self.width - cell_len(text[a:b]), 0)
        return n, a, b, {"left": 0, "center": pad // 2}.get(line.align, pad)

    def render(self, y: int, style: Callable[[Run], Style]) -> Text:
        """экранная строка y со стилями отрезков, отступ выравнивания - пробелами"""
        n, a, b, indent = self.row(y)
        out = Text(" " * indent, end="")
        at = 0
        for run in self.runs(self.lines[n]):
            size = len(run.text)
            if max(a, at) < min(b, at + size):
                out.append(self.shown(run)[max(a, at) - at : b - at], style(run))
            at += size
        return out

    # shortcut: клетка = символ, широкие символы (CJK, эмодзи) сдвинут курсор,
    # считать по cell_len, если такие тексты появятся
    def cell(self, n: int, col: int) -> tuple[int, int]:
        """столбец строки читателя -> экранная строка и клетка"""
        n = min(n, len(self.lines) - 1)
        _, _, rows = self.wraps[id(self.lines[n])]
        i = max(bisect.bisect_right(rows, col, key=lambda r: r[0]) - 1, 0)
        y = self.tops[n] + i
        _, a, _, indent = self.row(y)
        return y, indent + col - a

    def col(self, y: int, x: int) -> tuple[int, int]:
        """экранная строка и клетка -> строка читателя и столбец"""
        y = min(max(y, 0), self.tops[-1] - 1)
        n, a, b, indent = self.row(y)
        return n, a + min(max(x - indent, 0), b - a)
