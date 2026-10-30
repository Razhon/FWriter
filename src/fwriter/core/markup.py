import bisect
import itertools
import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

TAGS = {"b", "i", "s", "center", "right", "footnote", "tab"}


@dataclass(frozen=True)
class MarkupError:
    line: int  # с нуля, как в TextArea
    col: int
    message: str


def _line_starts(text: str) -> list[int]:
    return [0, *(m.end() for m in re.finditer("\n", text))]


class _Parser(HTMLParser):
    def __init__(self, text: str, convert_charrefs: bool) -> None:
        super().__init__(convert_charrefs=convert_charrefs)
        self.text = text
        self.starts = _line_starts(text)

    def at(self) -> int:
        line, col = self.getpos()
        return self.starts[line - 1] + col


class _Checker(_Parser):
    def __init__(self, text: str) -> None:
        super().__init__(text, convert_charrefs=True)
        self.stack: list[tuple[str, int, int]] = []
        self.error: MarkupError | None = None

    def fail(self, message: str, pos: tuple[int, int] | None = None) -> None:
        if self.error is None:
            line, col = pos or self.getpos()
            self.error = MarkupError(line - 1, col, message)

    def raw(self) -> str:
        # исходный текст тега: html.parser отдаёт имя уже в нижнем регистре
        start = self.at()
        return self.text[start : self.text.index(">", start) + 1]

    def check_raw(self, tag: str, closing: bool) -> bool:
        raw = self.raw()
        if raw == (f"</{tag}>" if closing else f"<{tag}>"):
            return True
        if raw[2 if closing else 1 :].startswith(tag):
            self.fail(
                f"Тег {raw} с атрибутами или пробелами: читатель увидит его текстом"
            )
        else:
            self.fail(f"Тег {raw} в верхнем регистре: читатель увидит его текстом")
        return False

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag not in TAGS:
            self.fail(f"Неизвестный тег {self.raw()}")
        elif self.check_raw(tag, closing=False) and tag != "tab":
            if tag == "footnote" and any(t == "footnote" for t, _, _ in self.stack):
                self.fail("Сноска внутри сноски")
            self.stack.append((tag, *self.getpos()))

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        self.fail(
            f"Тег {self.raw()} не бывает одиночным с /"
            if tag in TAGS
            else f"Неизвестный тег {self.raw()}"
        )

    def handle_endtag(self, tag: str) -> None:
        if tag == "tab":
            self.fail("</tab> не нужен: <tab> - одиночный тег")
        elif tag not in TAGS:
            self.fail(f"Неизвестный тег {self.raw()}")
        elif not self.check_raw(tag, closing=True):
            pass
        elif self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        elif any(t == tag for t, _, _ in self.stack):
            self.fail(f"</{tag}> закрывает тег раньше вложенного <{self.stack[-1][0]}>")
        else:
            self.fail(f"Лишний </{tag}>: такой тег не открыт")

    def handle_comment(self, data: str) -> None:
        self.fail("Комментарий или </ без имени тега: читатель увидит их текстом")

    def handle_decl(self, decl: str) -> None:
        self.fail("Конструкция <! ...>: читатель увидит её текстом")

    def unknown_decl(self, data: str) -> None:
        self.handle_decl(data)

    def handle_pi(self, data: str) -> None:
        self.fail("Конструкция <? ...>: читатель увидит её текстом")


def check(text: str) -> MarkupError | None:
    """первая ошибка разметки или None"""
    checker = _Checker(text)
    checker.feed(text)
    checker.close()
    if checker.error is None and checker.stack:
        tag, line, col = checker.stack[0]
        checker.fail(f"Тег <{tag}> не закрыт", (line, col))
    return checker.error


# shortcut: ширина <tab> не сверена с сайтом, сверить по div#content
TAB_WIDTH = 4
INLINE = {"b", "i", "s", "footnote"}
BLOCK = {"center", "right"}
_HIDDEN = re.compile(r"^[ \t\r]+|[ \t\r]+$|(?<=[ \t\r])[ \t\r]+")


@dataclass(frozen=True)
class Run:
    text: str  # длина как у сырого, кроме virtual
    raw: int  # у virtual - позиция тега
    style: frozenset[str]
    hidden: bool = False  # пробелы, которые читатель не увидит
    virtual: bool = False  # отступ <tab> и скобки сноски


@dataclass
class Line:
    align: str  # "left", "center", "right"
    start: int  # start и end - сырые позиции для курсора
    end: int
    runs: list[Run]


Tag = tuple[int, int, str, bool]  # начало, конец, имя, закрывающий


class _Tags(_Parser):
    """позиции тегов, которые читатель считает тегами; остальное - текст"""

    def __init__(self, text: str) -> None:
        super().__init__(text, convert_charrefs=False)
        self.tags: list[Tag] = []

    def set_cdata_mode(self, *args: object, **kwargs: object) -> None:
        # <style>, <script> и т. п. для читателя - текст, теги после них работают
        pass

    def handle_starttag(self, tag: str, attrs: object) -> None:
        raw = self.get_starttag_text() or ""
        if tag in TAGS and raw == f"<{tag}>":
            start = self.at()
            self.tags.append((start, start + len(raw), tag, False))

    def handle_endtag(self, tag: str) -> None:
        start = self.at()
        if tag in TAGS and tag != "tab" and self.text.startswith(f"</{tag}>", start):
            self.tags.append((start, start + len(tag) + 3, tag, True))


class _AnyTags(_Parser):
    """все теги, и неизвестные: <u>, <B>, </b > - для запрета ввода тегов"""

    def __init__(self, text: str) -> None:
        super().__init__(text, convert_charrefs=False)
        self.tags: list[str] = []

    def set_cdata_mode(self, *args: object, **kwargs: object) -> None:
        pass

    def handle_starttag(self, tag: str, attrs: object) -> None:
        self.tags.append(self.get_starttag_text() or "")

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag: str) -> None:
        self.tags.append(f"</{tag}>")


def _all_tags(text: str) -> Counter[str]:
    tags = _AnyTags(text)
    tags.feed(text)
    tags.close()
    return Counter(tags.tags)


def adds_tag(old: str, new: str) -> bool:
    """в new есть тег, которого не было в old"""
    return bool(_all_tags(new) - _all_tags(old))


def prev_char(row: str, col: int) -> tuple[int, int] | None:
    """что стирает Backspace в строке документа: символ перед col мимо тегов,
    <tab> - целиком; None - до начала строки только теги"""
    ends = {end: (start, name) for start, end, name, _ in tag_list(row)}
    while col > 0:
        if col not in ends:
            return col - 1, col
        start, name = ends[col]
        if name == "tab":
            return start, col
        col = start
    return None


def next_char(row: str, col: int) -> tuple[int, int] | None:
    """что стирает Del: символ после col мимо тегов, <tab> - целиком"""
    starts = {start: (end, name) for start, end, name, _ in tag_list(row)}
    while col < len(row):
        if col not in starts:
            return col, col + 1
        end, name = starts[col]
        if name == "tab":
            return col, end
        col = end
    return None


def strip_text(text: str) -> str:
    """теги из text без <tab>: удаление выделения в WYSIWYG не ломает разметку,
    а отступ - видимый текст и уходит вместе с выделением"""
    return "".join(
        text[start:end] for start, end, name, _ in tag_list(text) if name != "tab"
    )


def _drop_last(items: list[str], name: str) -> None:
    del items[len(items) - 1 - items[::-1].index(name)]


class _Layout:
    def __init__(self) -> None:
        self.lines: list[Line] = []
        self.inline: list[str] = []
        self.blocks: list[str] = []
        self.content = False
        self.block_end = -1  # строка, начатая закрытием блока

    def align(self) -> str:
        return self.blocks[-1] if self.blocks else "left"

    def new_line(self, end: int, start: int) -> None:
        if self.lines:
            self.lines[-1].end = end
        self.lines.append(Line(self.align(), start, start, []))
        self.content = False

    def add(self, text: str, raw: int, virtual: bool = False) -> None:
        style = frozenset(self.inline)
        self.lines[-1].runs.append(Run(text, raw, style, virtual=virtual))
        if virtual or text.strip(" \t\r"):
            self.content = True

    def text(self, text: str, raw: int) -> None:
        for i, part in enumerate(text.split("\n")):
            if i:
                self.new_line(raw - 1, raw)
            if part:
                self.add(part, raw)
            raw += len(part) + 1

    def tag(self, start: int, end: int, name: str, closing: bool) -> None:
        if name == "tab":
            self.add("\xa0" * TAB_WIDTH, start, virtual=True)
        elif name in BLOCK and not closing:
            self.blocks.append(name)
            if self.content:
                self.new_line(start, end)
            else:
                self.lines[-1].align = name
        elif name in BLOCK:
            if name not in self.blocks:
                self.add(f"</{name}>", start)
                return
            _drop_last(self.blocks, name)
            self.new_line(start, end)
            self.block_end = len(self.lines) - 1
        elif not closing:
            # как на сайте: вложенная сноска не открывается
            if name == "footnote" and name in self.inline:
                return
            self.inline.append(name)
            if name == "footnote":
                self.add("(", start, virtual=True)
        elif name in self.inline:
            if name == "footnote":
                self.add(")", start, virtual=True)
            _drop_last(self.inline, name)
        else:
            self.add(f"</{name}>", start)


def _hide_spaces(line: Line) -> None:
    """пометить пробелы, которые схлопнет pre-line"""
    shown = "".join(run.text for run in line.runs)
    spans = [m.span() for m in _HIDDEN.finditer(shown)]
    if not spans and "\t" not in shown and "\r" not in shown:
        return
    runs: list[Run] = []
    pos = 0
    i = 0
    for run in line.runs:
        end = pos + len(run.text)
        text = run.text.replace("\t", " ").replace("\r", " ")
        at = 0
        while i < len(spans) and spans[i][0] < end:
            a, b = max(spans[i][0], pos) - pos, min(spans[i][1], end) - pos
            if at < a:
                runs.append(Run(text[at:a], run.raw + at, run.style))
            runs.append(Run(text[a:b], run.raw + a, run.style, hidden=True))
            at = b
            if spans[i][1] > end:
                break
            i += 1
        if at < len(text):
            runs.append(
                Run(text[at:], run.raw + at, run.style, run.hidden, run.virtual)
            )
        pos = end
    line.runs = runs


def tag_list(text: str) -> list[Tag]:
    """теги, которые видит читатель: начало, конец, имя, закрывающий"""
    tags = _Tags(text)
    tags.feed(text)
    tags.close()
    return tags.tags


def _lay(
    text: str,
    tags: list[Tag],
    inline: tuple[str, ...] = (),
    blocks: tuple[str, ...] = (),
) -> _Layout:
    state = _Layout()
    state.inline, state.blocks = list(inline), list(blocks)
    state.new_line(0, 0)
    raw = 0
    for start, end, name, closing in tags:
        state.text(text[raw:start], raw)
        state.tag(start, end, name, closing)
        raw = end
    state.text(text[raw:], raw)
    state.lines[-1].end = len(text)
    for line in state.lines:
        _hide_spaces(line)
    return state


def _ends_block(state: _Layout) -> bool:
    return state.block_end == len(state.lines) - 1 and not state.lines[-1].runs


def layout(text: str) -> list[Line]:
    """строки читателя до переноса по ширине"""
    state = _lay(text, tag_list(text))
    if _ends_block(state):
        state.lines.pop()
    return state.lines


@dataclass(frozen=True)
class _Piece:
    lines: list[Line]  # позиции от начала куска
    inline: tuple[str, ...]  # открытые теги после куска
    blocks: tuple[str, ...]
    ends_block: bool
    rows: int


def _parse(text: str, last: bool) -> list[Tag] | None:
    """None - разбор не закончен в конце куска (<!--, <b title=" тянутся дальше)"""
    src = text if last else text + "\n"
    tags = _Tags(src)
    tags.feed(src)
    if last:
        tags.close()
    elif tags.rawdata:
        return None
    return tags.tags


class Reader:
    """layout() по строкам документа: после правки заново разбираются только
    изменённые строки, а собираются ещё и те, перед которыми сменились открытые теги"""

    def __init__(self) -> None:
        # сборка и разбор одной строки документа; не чистятся на каждой правке:
        # закрытие тега возвращает прежние открытые теги и прежние куски
        self.pieces: dict[
            tuple[str, tuple[str, ...], tuple[str, ...], bool], _Piece
        ] = {}
        self.parsed: dict[tuple[str, bool], list[Tag] | None] = {}
        self.lines: list[Line] = []  # позиции от начала своего куска
        self.chunks: list[
            tuple[int, int]
        ] = []  # первая строка документа, первая в lines
        self.open: list[bool] = []  # перед куском открыт тег
        self.rows = 0

    def update(self, rows: list[str]) -> None:
        pieces: dict[tuple[str, tuple[str, ...], tuple[str, ...], bool], _Piece] = {}
        parsed: dict[tuple[str, bool], list[Tag] | None] = {}
        self.lines, self.chunks, self.open = [], [], []
        self.rows = len(rows)
        inline: tuple[str, ...] = ()
        blocks: tuple[str, ...] = ()
        i = 0
        piece = None
        while i < len(rows):
            last = i == len(rows) - 1
            key = (rows[i], inline, blocks, last)
            piece = self.pieces.get(key)
            if piece is None:
                pkey = rows[i], last
                tags = (
                    self.parsed[pkey] if pkey in self.parsed else _parse(rows[i], last)
                )
                parsed[pkey] = tags
                k = 1
                # shortcut: кусок из нескольких строк не кэшируется и разбирается
                # на каждую правку, кэшировать, если такие куски окажутся длинными
                while tags is None:
                    k *= 2
                    tags = _parse("\n".join(rows[i : i + k]), i + k >= len(rows))
                state = _lay("\n".join(rows[i : i + k]), tags, inline, blocks)
                piece = _Piece(
                    state.lines,
                    tuple(state.inline),
                    tuple(state.blocks),
                    _ends_block(state),
                    len(rows[i : i + k]),
                )
            if piece.rows == 1:
                pieces[key] = piece
            self.chunks.append((i, len(self.lines)))
            self.open.append(bool(inline or blocks))
            self.lines.extend(piece.lines)
            inline, blocks = piece.inline, piece.blocks
            i += piece.rows
        if piece and piece.ends_block:
            self.lines.pop()
        self.pieces |= pieces
        self.parsed |= parsed
        if len(self.pieces) > 3 * len(rows) + 100:
            self.pieces, self.parsed = pieces, parsed

    def find(self, rows: list[str], row: int, col: int) -> int:
        """строка читателя, где стоит курсор документа"""
        c = bisect.bisect_right(self.chunks, (row, len(self.lines))) - 1
        first, start = self.chunks[c]
        raw = sum(len(r) + 1 for r in rows[first:row]) + col
        end = self.chunks[c + 1][1] if c + 1 < len(self.chunks) else len(self.lines)
        n = bisect.bisect_right(self.lines, raw, start, end, key=lambda x: x.start)
        return max(0, min(n - 1, len(self.lines) - 1))

    def span(self, first: int, last: int) -> tuple[int, int]:
        """строки документа first..last, расширенные до строк, перед которыми не
        открыт ни один тег: оформлению клавишей хватает разобрать их"""
        c = bisect.bisect_right(self.chunks, first, key=lambda x: x[0]) - 1
        while self.open[c]:
            c -= 1
        d = bisect.bisect_right(self.chunks, last, key=lambda x: x[0])
        while d < len(self.chunks) and self.open[d]:
            d += 1
        end = self.chunks[d][0] if d < len(self.chunks) else self.rows
        return self.chunks[c][0], end

    def _first_row(self, n: int) -> int:
        """первая строка документа у куска строки читателя n"""
        c = bisect.bisect_right(self.chunks, n, key=lambda x: x[1]) - 1
        return self.chunks[c][0]

    def locate(self, rows: list[str], row: int, col: int) -> tuple[int, int]:
        """позиция документа -> строка читателя и столбец в её тексте"""
        n = self.find(rows, row, col)
        first = self._first_row(n)
        raw = sum(len(r) + 1 for r in rows[first:row]) + col
        return n, to_screen([self.lines[n]], raw)[1]

    def location(self, rows: list[str], n: int, col: int) -> tuple[int, int]:
        """строка читателя и столбец -> позиция документа; на границе отрезков -
        конец левого: набранное после "ab" перед <b> останется без оформления"""
        line = self.lines[n]
        raw, at = line.end, 0
        for run in line.runs:
            size = len(run.text)
            if not run.virtual and at < col <= at + size:
                raw = run.raw + col - at
                break
            if col < at + size:
                raw = run.raw if run.virtual else run.raw + col - at
                break
            at += size
        row = self._first_row(n)
        while raw > len(rows[row]):
            raw -= len(rows[row]) + 1
            row += 1
        return row, raw


def count(text: str) -> tuple[int, int]:
    """символы и слова, которые видит читатель: без тегов, переводов строк и схлопнутых пробелов"""
    chars = words = 0
    for line in layout(text):
        shown = "".join(r.text for r in line.runs if not r.hidden and not r.virtual)
        chars += len(shown)
        # не split(): U+00A0 склеивает слова, как у читателя
        words += len(re.findall("[^ \t\r]+", shown))
    return chars, words


def to_raw(lines: list[Line], line: int, col: int) -> int:
    """клетка экрана -> позиция в сырой строке"""
    at = 0
    for run in lines[line].runs:
        if col < at + len(run.text):
            return run.raw if run.virtual else run.raw + col - at
        at += len(run.text)
    return lines[line].end


def to_screen(lines: list[Line], raw: int) -> tuple[int, int]:
    """позиция в сырой строке -> клетка; внутри тега - его край"""
    n = bisect.bisect_right([line.start for line in lines], raw) - 1
    at = 0
    for run in lines[n].runs:
        if run.virtual:
            if raw <= run.raw:
                return n, at
        elif raw <= run.raw + len(run.text):
            return n, at + max(0, raw - run.raw)
        at += len(run.text)
    return n, at


Edit = tuple[int, int, str]  # начало, конец в сырой строке, замена
Style = Literal["blank", "indent", "indent_blank", "plain", "mixed"]


def apply(text: str, edits: list[Edit]) -> str:
    out: list[str] = []
    at = 0
    for start, end, repl in sorted(edits, key=lambda e: e[0]):
        out += (text[at:start], repl)
        at = end
    out.append(text[at:])
    return "".join(out)


def paras_touched(text: str, edits: list[Edit]) -> int:
    starts = _line_starts(text)
    # хвост пустых строк в конце главы считаем по последнему абзацу
    at = [start if end == len(text) else end for start, end, _ in edits]
    return len({bisect.bisect_right(starts, i) - 1 for i in at})


def wrap(text: str, sel: tuple[int, int], tag: str) -> list[Edit]:
    """пара тегов на месте курсора или вокруг выделения, в каждой строке отдельно"""
    a, b = sel
    if a == b:
        return [(a, a, f"<{tag}></{tag}>")]
    edits: list[Edit] = []
    starts = _line_starts(text)
    first = bisect.bisect_right(starts, a) - 1
    for start in starts[first : bisect.bisect_right(starts, b)]:
        end = text.find("\n", start)
        end = len(text) if end < 0 else end
        lo, hi = max(a, start), min(b, end)
        # пустые строки и кусок выделения из одного перевода строки не оборачиваем
        if lo < hi:
            edits += [(lo, lo, f"<{tag}>"), (hi, hi, f"</{tag}>")]
    return edits


def _span(text: str, sel: tuple[int, int] | None) -> tuple[int, int]:
    """выделение, которое кончается в начале строки, эту строку не касается"""
    if sel is None:
        return 0, len(text)
    a, b = sel
    return a, b - 1 if b > a and text[b - 1] == "\n" else b


def _sel_lines(text: str, sel: tuple[int, int] | None) -> range:
    starts = _line_starts(text)
    a, b = _span(text, sel)
    return range(bisect.bisect_right(starts, a) - 1, bisect.bisect_right(starts, b))


# оформление абзацев


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    kind: str  # "para", "empty", "fixed" - блок, его не трогаем


# <tab> в начале абзаца, в том числе внутри <b>, <i>, <s>
_TAB_AT = re.compile(r"[ \t]*(?:<[bis]>)*<tab>")


def _units(text: str) -> list[_Unit]:
    """сырые строки, склеенные по \\n внутри блока или сноски"""
    tags = tag_list(text)
    units: list[_Unit] = []
    blocks = 0
    footnote = False
    fixed = False
    start = ti = 0
    for nl in [*(m.start() for m in re.finditer("\n", text)), len(text)]:
        while ti < len(tags) and tags[ti][0] < nl:
            _, _, name, closing = tags[ti]
            ti += 1
            if name in BLOCK:
                fixed = True
                blocks += -1 if closing else 1
            elif name == "footnote":
                footnote = not closing
        if blocks > 0 or footnote:
            continue
        empty = not text[start:nl].strip(" \t\r")
        kind = "fixed" if fixed else "empty" if empty else "para"
        units.append(_Unit(start, nl, kind))
        start, fixed = nl + 1, False
    return units


def para_style(text: str) -> Style:
    """способ отделения абзацев по большинству; при равенстве - mixed"""
    tabs = [0, 0]
    gaps = [0, 0]  # промежутков без пустой строки и ровно с одной
    prev = False
    gap = 0
    for unit in _units(text):
        if unit.kind == "empty":
            gap += 1
            continue
        if unit.kind == "para":
            tabs[bool(_TAB_AT.match(text, unit.start, unit.end))] += 1
            if prev and gap < 2:
                gaps[gap] += 1
        prev, gap = unit.kind == "para", 0
    if not any(tabs):
        return "plain"
    if tabs[0] == tabs[1] or (gaps[0] == gaps[1] and gaps[0]):
        return "mixed"
    tab, blank = tabs[1] > tabs[0], gaps[1] > gaps[0]
    if tab:
        return "indent_blank" if blank else "indent"
    return "blank" if blank else "plain"


def restyle(
    text: str,
    style: Literal["blank", "indent", "indent_blank"],
    sel: tuple[int, int] | None = None,
) -> list[Edit]:
    """правки переводов строк между абзацами и <tab> в их начале"""
    a, b = _span(text, sel)
    tab, blank = style != "blank", style != "indent"
    edits: list[Edit] = []
    prev = False  # предыдущий абзац выделен и между нами только пустые строки
    gap: list[_Unit] = []
    for unit in _units(text):
        if unit.kind == "empty":
            gap.append(unit)
            continue
        inside = unit.kind == "para" and unit.start <= b and unit.end >= a
        if inside and prev:
            if blank and not gap:
                crlf = text[unit.start - 2 : unit.start] == "\r\n"
                edits.append((unit.start, unit.start, "\r\n" if crlf else "\n"))
            elif not blank and len(gap) == 1:
                edits.append((gap[0].start, unit.start, ""))
        if inside:
            m = _TAB_AT.match(text, unit.start, unit.end)
            if tab and not m:
                edits.append((unit.start, unit.start, "<tab>"))
            elif not tab and m:
                edits.append((m.end() - 5, m.end(), ""))
        prev, gap = inside, []
    return edits


# типографика: правила работают с текстом строки без тегов


@dataclass(frozen=True)
class _Plain:
    line: int
    text: str
    offs: list[int]  # начало куска в text
    raws: list[int]  # его сырая позиция; -1 у скобок сноски
    block: bool  # строка в <center> или <right>, не абзац


def _plain_lines(text: str, sel: tuple[int, int] | None) -> list[_Plain]:
    tags = tag_list(text)
    scope = _sel_lines(text, sel)
    out: list[_Plain] = []
    footnote = False
    blocks = 0
    ti = 0
    for n, start in enumerate(_line_starts(text)):
        end = text.find("\n", start)
        end = len(text) if end < 0 else end
        if end > start and text[end - 1] == "\r":
            end -= 1
        parts: list[str] = []
        offs: list[int] = []
        raws: list[int] = []
        size = 0
        raw = start
        block = blocks > 0
        while ti < len(tags) and tags[ti][0] < end:
            tag_start, tag_end, name, closing = tags[ti]
            ti += 1
            if name in BLOCK:
                block = True
                blocks += -1 if closing else 1
            pieces = [(text[raw:tag_start], raw)] if tag_start > raw else []
            if name == "footnote" and closing == footnote:
                footnote = not closing
                pieces.append((")" if closing else "(", -1))
            for s, at in pieces:
                parts.append(s)
                offs.append(size)
                raws.append(at)
                size += len(s)
            raw = tag_end
        if end > raw:
            parts.append(text[raw:end])
            offs.append(size)
            raws.append(raw)
        if n in scope:
            out.append(_Plain(n, "".join(parts), offs, raws, block))
    return out


def _to_raw(p: _Plain, a: int, b: int, repl: str) -> list[Edit]:
    """правка текста строки -> сырые правки; замена через тег не делается"""
    out: list[Edit] = []
    for i in range(bisect.bisect_right(p.offs, a) - 1, len(p.offs)):
        off = p.offs[i]
        if off >= b:
            break
        size = (p.offs[i + 1] if i + 1 < len(p.offs) else len(p.text)) - off
        lo, hi = max(a, off), min(b, off + size)
        if lo >= hi:
            continue
        if p.raws[i] < 0:
            if repl:
                return []
            continue
        out.append((p.raws[i] + lo - off, p.raws[i] + hi - off, ""))
    if not repl:
        return out
    if len(out) != 1 or out[0][1] - out[0][0] != b - a:
        return []
    return [(out[0][0], out[0][1], repl)]


# в начале строки или между пробелами; дефис в слове и у одного пробела не тире
_DASH = re.compile(r"(?:^|(?<=[ \t\xa0]))(?:--|–|-)(?=[ \t\xa0])")
# разделитель сцены без <center>
_DASHES_ONLY = re.compile(r"[-–— \t\xa0]*")
_ELLIPSIS = re.compile(r"(?<![.?!])\.\.\.(?!\.)")
# пробел перед знаком убирается только после слова или скобки: после тире или
# двоеточия знак отдельный; многоточие перед словом открывает фразу
_SPACES = re.compile(
    r"(?P<drop>^[ \t]+|[ \t]+\Z"
    r"|(?<=[\w)\]»“\"'])[ \t]+(?=[,.!?:;…])(?!(?:\.\.\.|…)\w))"
    r"|(?P<one>[ \t]{2,}|\t)"
)


def _dash_rule(s: str) -> list[Edit]:
    if _DASHES_ONLY.fullmatch(s):
        return []
    return [(m.start(), m.end(), "—") for m in _DASH.finditer(s)]


def _ellipsis_rule(s: str) -> list[Edit]:
    return [(m.start(), m.end(), "…") for m in _ELLIPSIS.finditer(s)]


def _spaces_rule(s: str) -> list[Edit]:
    out: list[Edit] = []
    for m in _SPACES.finditer(s):
        a, b = m.span()
        if m.group("drop"):
            out.append((a, b, ""))
            continue
        if s[a] == "\t":
            out.append((a, a + 1, " "))
        if b > a + 1:
            out.append((a + 1, b, ""))
    return out


_QUOTE_END = {"", " ", "\t", "\xa0", ",", ".", "!", "?", ":", ";", "…", ")", "»", "“"}


def _quotes_rule(s: str) -> list[Edit] | None:
    """None, если прямых кавычек нечётное число"""
    if s.count('"') % 2:
        return None
    out: list[Edit] = []
    stack: list[str] = []
    opened = False  # предыдущий символ - открывающая кавычка
    for i, ch in enumerate(s):
        was_opened, opened = opened, False
        if ch in "«„":
            stack.append(ch)
            opened = True
        elif ch in "»“" and stack and stack[-1] == ("«" if ch == "»" else "„"):
            stack.pop()
        elif ch == '"':
            # после открывающей - открывающая, только если дальше текст: "" - пустые
            nested = was_opened and s[i + 1 : i + 2] not in _QUOTE_END
            if i == 0 or nested or s[i - 1] in " \t\xa0(—":
                mark = "„" if stack else "«"
                out.append((i, i + 1, mark))
                stack.append(mark)
                opened = True
            else:
                top = stack.pop() if stack else "«"
                out.append((i, i + 1, "»" if top == "«" else "“"))
    return out


def _run(lines: list[_Plain], rule: Callable[[str], list[Edit]]) -> list[Edit]:
    return [e for p in lines for a, b, r in rule(p.text) for e in _to_raw(p, a, b, r)]


def _dashes(lines: list[_Plain]) -> list[Edit]:
    return _run([p for p in lines if not p.block], _dash_rule)


def dashes(text: str, sel: tuple[int, int] | None = None) -> list[Edit]:
    return _dashes(_plain_lines(text, sel))


def ellipsis(text: str, sel: tuple[int, int] | None = None) -> list[Edit]:
    return _run(_plain_lines(text, sel), _ellipsis_rule)


def _quotes(lines: list[_Plain]) -> tuple[list[Edit], list[int]]:
    edits: list[Edit] = []
    skipped: list[int] = []
    for p in lines:
        found = _quotes_rule(p.text)
        if found is None:
            skipped.append(p.line)
        else:
            edits += [e for a, b, r in found for e in _to_raw(p, a, b, r)]
    return edits, skipped


def quotes(
    text: str, sel: tuple[int, int] | None = None
) -> tuple[list[Edit], list[int]]:
    """правки и номера строк, где кавычки не расставлены"""
    return _quotes(_plain_lines(text, sel))


def _edges(text: str, sel: tuple[int, int] | None) -> list[Edit]:
    """пустые строки до первого и после последнего абзаца главы"""
    starts = _line_starts(text)
    lines = zip(starts, [s - 1 for s in starts[1:]] + [len(text)], strict=True)
    full = [(a, b) for a, b in lines if text[a:b].strip(" \t\r")]
    if not full:
        return [(0, len(text), "")] if text and sel is None else []
    a, b = _span(text, sel)
    out: list[Edit] = []
    first = full[0][0]
    if first and a <= first:
        out.append((0, first, ""))
    last = full[-1][1]
    if text[last - 1 : last] == "\r":
        last -= 1
    if last < len(text) and b >= full[-1][0]:
        out.append((last, len(text), ""))
    return out


def _spaces(
    text: str, sel: tuple[int, int] | None, lines: list[_Plain], edges: bool
) -> list[Edit]:
    edits = _run(lines, _spaces_rule)
    if not edges:
        return edits
    cut = _edges(text, sel)
    inner = [e for e in edits if not any(a <= e[0] < b for a, b, _ in cut)]
    return [*inner, *cut]


def spaces(text: str, sel: tuple[int, int] | None = None) -> list[Edit]:
    return _spaces(text, sel, _plain_lines(text, sel), edges=True)


def _typography(
    text: str, sel: tuple[int, int] | None, edges: bool
) -> tuple[list[Edit], list[int]]:
    lines = _plain_lines(text, sel)
    found, skipped = _quotes(lines)
    found += _dashes(lines) + _run(lines, _ellipsis_rule)
    found += _spaces(text, sel, lines, edges)
    # правки пунктов не пересекаются; на всякий случай пересечение не применяем
    edits: list[Edit] = []
    for e in sorted(found):
        if not edits or e[0] >= edits[-1][1]:
            edits.append(e)
    return edits, skipped


def typography(
    text: str, sel: tuple[int, int] | None = None
) -> tuple[list[Edit], list[int]]:
    """пункты 5-8 одной правкой"""
    return _typography(text, sel, edges=True)


# починка тегов: b, i, s заново по стилю каждого символа

_STYLE = ("b", "i", "s")
_Chunk = tuple[int, int, list[str], list[tuple[str, set[str]]], list[str]]


def _top(stack: list[str]) -> list[str]:
    """b, i, s над последним блоком или сноской: их можно переставлять"""
    i = len(stack)
    while i and stack[i - 1] in _STYLE:
        i -= 1
    return stack[i:]


def _need(stack: list[str]) -> set[str]:
    top = _top(stack)
    return set(top) - set(stack[: len(stack) - len(top)])


def _chunks(text: str) -> list[_Chunk]:
    """куски между \\n, <tab>, сноской и блоками"""
    out: list[_Chunk] = []
    stack: list[str] = []
    start, raw = 0, 0
    top: list[str] = []
    pieces: list[tuple[str, set[str]]] = []
    for tag_start, tag_end, name, closing in [
        *tag_list(text),
        (len(text), len(text), "", True),
    ]:
        for i, part in enumerate(text[raw:tag_start].split("\n")):
            if i:
                out.append((start, raw - 1, top, pieces, _top(stack)))
                start, top, pieces = raw, _top(stack), []
            if part:
                pieces.append((part, _need(stack)))
            raw += len(part) + 1
        raw = tag_end
        if name in _STYLE:
            if closing:
                _drop_last(stack, name)
            else:
                stack.append(name)
            continue
        out.append((start, tag_start, top, pieces, _top(stack)))
        if name in BLOCK or name == "footnote":
            if closing:
                _drop_last(stack, name)
            else:
                stack.append(name)
        start, top, pieces = tag_end, _top(stack), []
    return out


def _merge(pieces: list[tuple[str, set[str]]]) -> list[tuple[str, set[str]]]:
    out: list[tuple[str, set[str]]] = []
    for s, style in pieces:
        if out and out[-1][1] == style:
            out[-1] = (out[-1][0] + s, style)
        else:
            out.append((s, set(style)))
    return out


def _retag(start: list[str], pieces: list[tuple[str, set[str]]], end: list[str]) -> str:
    pieces = _merge(pieces)
    # пробелы между двумя b или i входят в них; у s зачёркнутый пробел видно
    for k, (s, style) in enumerate(pieces):
        if s.strip(" \t\r"):
            continue
        prev = pieces[k - 1][1] if k else set(start)
        nxt = pieces[k + 1][1] if k + 1 < len(pieces) else set(end)
        for name in ("b", "i"):
            if name in prev and name in nxt:
                style.add(name)
            elif name not in prev and name not in nxt:
                style.discard(name)
    pieces = _merge(pieces)
    out: list[str] = []
    cur = list(start)
    for k, (s, style) in enumerate(pieces):
        while not set(cur) <= style:
            out.append(f"</{cur.pop()}>")

        def key(name: str, k: int = k) -> tuple[int, int, int]:
            # снаружи - тег, который держится дольше
            n = k
            while n < len(pieces) and name in pieces[n][1]:
                n += 1
            at = end.index(name) if name in end else len(end)
            return -n, at, _STYLE.index(name)

        for name in sorted(style - set(cur), key=key):
            out.append(f"<{name}>")
            cur.append(name)
        out.append(s)
    while cur != end[: len(cur)]:
        out.append(f"</{cur.pop()}>")
    out += (f"<{name}>" for name in end[len(cur) :])
    return "".join(out)


def fix_tags(text: str, sel: tuple[int, int] | None = None) -> list[Edit]:
    """b, i, s без пустых, разорванных и перепутанных тегов; текст и стили не меняются"""
    if check(text) is not None:
        return []
    scope = _sel_lines(text, sel)
    starts = _line_starts(text)
    edits: list[Edit] = []
    for start, end, top, pieces, close in _chunks(text):
        if bisect.bisect_right(starts, start) - 1 not in scope:
            continue
        new = _retag(top, pieces, close)
        if new != text[start:end]:
            edits.append((start, end, new))
    # склейка текста без тегов между ними может сделать из "<" и буквы новый тег
    return edits if check(apply(text, edits)) is None else []


# клавиши оформления в WYSIWYG: text - кусок, на краях которого не открыт ни один тег

CANT_FORMAT = (
    "Так не оформить: выделение пересекает сноску или выровненный абзац. "
    "Можно в режиме разметки (F7)"
)


def enter(row: str, col: int, last: bool) -> tuple[int, str]:
    """Enter в строке документа: куда и что вставить. В конце выровненной строки
    новый абзац начинается за </center> или </right>, как на сайте: если дальше
    \n, пустая строка у читателя там уже есть, иначе её даёт \n за тегом"""
    at = col
    for start, end, name, closing in tag_list(row[col:]):
        if col + start != at or not closing:
            break
        at = col + end
        if name in BLOCK:
            return at, "" if at == len(row) and not last else "\n"
    return col, "\n"


def _wrap(text: str, a: int, b: int, name: str) -> list[Edit] | None:
    """пара тегов вокруг a..b; b, i, s, открытые на краях, закрываются перед
    тегом и открываются за ним, чтобы теги не перепутались; None - край
    пересекает сноску или блок"""
    tags = tag_list(text)
    for start, end, tag, closing in tags:
        if start == a and (closing or tag in BLOCK):
            a = end
        if start == b and closing and tag in _STYLE:
            b = end
    b = max(a, b)
    stack: list[str] = []
    at_a: list[str] | None = None
    at_b: list[str] = []
    low = 0
    inside: set[str] = set()
    for start, _, tag, closing in [*tags, (len(text), len(text), "", False)]:
        if at_a is None and start >= a:
            at_a, low = list(stack), len(stack)
        if start >= b:
            at_b = list(stack)
            break
        if tag == "tab":
            continue
        if closing:
            _drop_last(stack, tag)
        else:
            stack.append(tag)
        if at_a is not None:
            low = min(low, len(stack))
            inside.add(tag)
    assert at_a is not None
    outer, moved_a, moved_b = at_a[:low], at_a[low:], at_b[low:]
    if any(tag not in _STYLE for tag in moved_a + moved_b):
        return None
    # вложенная сноска у читателя не открывается, блок в сноске или блоке - тоже
    kin = BLOCK if name in BLOCK else {name}
    if "footnote" in outer or kin & (set(outer) | inside):
        return None

    def around(moved: list[str], tag: str) -> str:
        closed = "".join(f"</{t}>" for t in reversed(moved))
        return closed + tag + "".join(f"<{t}>" for t in moved)

    return [
        (a, a, around(moved_a, f"<{name}>")),
        (b, b, around(moved_b, f"</{name}>")),
    ]


def _flip(text: str, a: int, b: int, name: str, on: bool) -> str:
    """b, i или s у символов a..b, теги затронутых кусков заново через _retag()"""
    out: list[str] = []
    at = 0
    for start, end, top, pieces, close in _chunks(text):
        if end <= a or start >= b:
            continue
        pos: list[int] = []
        k = start
        for tag_start, tag_end, _, _ in tag_list(text[start:end]):
            pos += range(k, start + tag_start)
            k = start + tag_end
        pos += range(k, end)
        chars = [(ch, set(style)) for part, style in pieces for ch in part]
        for p, (_, style) in zip(pos, chars, strict=True):
            if a <= p < b:
                if on:
                    style.add(name)
                else:
                    style.discard(name)
        out += (text[at:start], _retag(top, chars, close))
        at = end
    out.append(text[at:])
    return "".join(out)


def _footnote(text: str, chosen: list[tuple[int, int]], on: bool) -> str | None:
    """сноска вокруг выбранных символов (строка читателя, позиция) - в каждой
    строке отдельно; снять - убрать сноски, где есть выбранный символ"""
    edits: list[Edit] = []
    if on:
        for _, group in itertools.groupby(chosen, key=lambda x: x[0]):
            ps = [p for _, p in group]
            wrapped = _wrap(text, ps[0], ps[-1] + 1, "footnote")
            if wrapped is None:
                return None
            edits += wrapped
        return apply(text, edits)
    picked = {p for _, p in chosen}
    stack: list[tuple[int, int]] = []
    for start, end, tag, closing in tag_list(text):
        if tag == "tab":
            continue
        if not closing:
            stack.append((start, end))
            continue
        o_start, o_end = stack.pop()
        if tag == "footnote" and any(o_end <= p < start for p in picked):
            edits += [(o_start, o_end, ""), (start, end, "")]
    return apply(text, edits)


def _texts(lines: list[Line]) -> list[str]:
    return ["".join(run.text for run in line.runs) for line in lines]


def _align(text: str, lines: list[Line], n: int, name: str) -> str | None:
    """выровнять строку читателя n; уже так выровнена - убрать выравнивание
    у её блока целиком, иначе сменить его; абзацы у читателя не меняются"""
    line = lines[n]
    if line.align == "left":
        wrapped = _wrap(text, line.start, line.end, name)
        if wrapped is None:
            return None
        # </center>\n - лишняя пустая строка у читателя: тег встаёт вместо \n;
        # перед пустой последней строкой так нельзя - она пропадёт
        start, _, close = wrapped[1]
        if text.startswith("\n", start):
            eaten = apply(text, [wrapped[0], (start, start + 1, close)])
            if _texts(layout(eaten)) == _texts(lines):
                return eaten
        return apply(text, wrapped)
    stack: list[tuple[int, int, str]] = []
    block: tuple[int, int, str] | None = None
    for start, end, tag, closing in tag_list(text):
        if block is None and start >= line.end:
            block = next(t for t in reversed(stack) if t[2] in BLOCK)
        if tag == "tab":
            continue
        if not closing:
            stack.append((start, end, tag))
            continue
        if stack.pop() is not block:
            continue
        assert block is not None
        if line.align != name:
            edits = [(block[0], block[1], f"<{name}>"), (start, end, f"</{name}>")]
            return apply(text, edits)
        # тег блока разделял абзацы: на его место \n, если без него они склеятся
        for o, c in itertools.product(("", "\n"), repeat=2):
            new = apply(text, [(block[0], block[1], o), (start, end, c)])
            if _texts(layout(new)) == _texts(lines):
                return new
        return None
    raise AssertionError("блок без закрывающего тега")


def _raws(line: Line) -> list[int]:
    """сырые позиции символов текста строки, без отступа и скобок сноски"""
    return [
        run.raw + i
        for run in line.runs
        if not run.virtual
        for i in range(len(run.text))
    ]


def _spot(lines: list[Line], raw: int) -> tuple[int, int]:
    """строка читателя и сколько её символов текста до raw: не меняется,
    когда меняются только теги"""
    n = to_screen(lines, raw)[0]
    return n, bisect.bisect_left(_raws(lines[n]), raw)


def _unspot(lines: list[Line], n: int, k: int) -> int:
    raws = _raws(lines[n])
    return raws[k] if k < len(raws) else lines[n].end


def toggle(text: str, a: int, b: int, name: str) -> tuple[Edit, int, int] | str:
    """клавиша оформления: b, i, s, footnote - у выделения a..b, center и right -
    у абзаца под a; уже оформлено - снять. Правка и новое выделение или почему нельзя"""
    lines = layout(text)
    if name in BLOCK:
        new = _align(text, lines, to_screen(lines, a)[0], name)
    else:
        chosen = [
            (n, p, run.style)
            for n, line in enumerate(lines)
            for run in line.runs
            if not run.virtual and not run.hidden
            for i in range(len(run.text))
            if a <= (p := run.raw + i) < b
        ]
        if not chosen:
            return "Выделите текст, который оформить"
        on = not all(name in style for _, _, style in chosen)
        if name == "footnote":
            new = _footnote(text, [(n, p) for n, p, _ in chosen], on)
        else:
            new = _flip(text, a, b, name, on)
    if new is None:
        return CANT_FORMAT
    if new == text:
        return (
            "Оформление задано снаружи сноски или выровненного абзаца: "
            "снимите его в режиме разметки (F7)"
        )
    lo = len(os.path.commonprefix([text, new]))
    tail = len(os.path.commonprefix([text[lo:][::-1], new[lo:][::-1]]))
    new_lines = layout(new)
    return (
        (lo, len(text) - tail, new[lo : len(new) - tail]),
        _unspot(new_lines, *_spot(lines, a)),
        _unspot(new_lines, *_spot(lines, b)),
    )


def _untab(lines: list[str]) -> list[str]:
    return [
        _TAB_AT.sub(lambda m: m.group()[:-5], s, count=1)
        for s in lines
        if s.strip(" \t\r")
    ]


def _typo(lines: list[str], last: bool) -> list[str]:
    text = "\n".join(lines)
    text = apply(text, _typography(text, None, edges=False)[0])
    if last:
        text = re.sub(r"(?:\n[ \t\r]*)+\Z", "", text)
    return text.split("\n")


def _retagged(lines: list[str]) -> list[str]:
    text = "\n".join(lines)
    return apply(text, fix_tags(text)).split("\n")


_KINDS = ("оформление абзацев", "типографика", "разметка")


def _forms(lines: list[str], last: bool) -> dict[tuple[str, ...], list[str]]:
    # разметка и типографика дорогие: каждое сочетание считается один раз
    retagged = _retagged(lines)
    return {
        (): lines,
        ("типографика",): _typo(lines, last),
        ("разметка",): retagged,
        ("типографика", "разметка"): _typo(retagged, last),
    }


def tweak_kind(old: list[str], new: list[str], last: bool) -> str | None:
    """пометка блока диффа, если стороны отличаются только оформлением"""
    if old == new:
        return None
    # частый случай после твика абзацев, без дорогих _forms
    if _untab(old) == _untab(new):
        return "оформление абзацев"
    sides = _forms(old, last), _forms(new, last)
    # самая короткая пометка: сначала по одному виду, потом сочетания
    for n in range(1, len(_KINDS) + 1):
        for kinds in itertools.combinations(_KINDS, n):
            key = tuple(k for k in kinds if k != "оформление абзацев")
            a, b = (side[key] for side in sides)
            if "оформление абзацев" in kinds:
                a, b = _untab(a), _untab(b)
            if a == b:
                return ", ".join(kinds)
    return None


# меню F9

Tweak = Literal[
    "typography",
    "blank",
    "indent",
    "indent_blank",
    "dashes",
    "quotes",
    "ellipsis",
    "spaces",
    "fix_tags",
]


def tweak(
    text: str, name: Tweak, sel: tuple[int, int] | None = None
) -> tuple[list[Edit], list[int]]:
    """правки пункта меню F9 и строки, где кавычки не расставлены"""
    match name:
        case "typography":
            return typography(text, sel)
        case "quotes":
            return quotes(text, sel)
        case "blank" | "indent" | "indent_blank":
            return restyle(text, name, sel), []
        case "dashes":
            return dashes(text, sel), []
        case "ellipsis":
            return ellipsis(text, sel), []
        case "spaces":
            return spaces(text, sel), []
        case "fix_tags":
            return fix_tags(text, sel), []
