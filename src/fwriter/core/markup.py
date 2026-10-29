import bisect
import re
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


class _Tags(_Parser):
    """позиции тегов, которые читатель считает тегами; остальное - текст"""

    def __init__(self, text: str) -> None:
        super().__init__(text, convert_charrefs=False)
        self.tags: list[
            tuple[int, int, str, bool]
        ] = []  # начало, конец, имя, закрывающий

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


def tag_list(text: str) -> list[tuple[int, int, str, bool]]:
    """теги, которые видит читатель: начало, конец, имя, закрывающий"""
    tags = _Tags(text)
    tags.feed(text)
    tags.close()
    return tags.tags


def layout(text: str) -> list[Line]:
    """строки читателя до переноса по ширине"""
    state = _Layout()
    state.new_line(0, 0)
    raw = 0
    for start, end, name, closing in tag_list(text):
        state.text(text[raw:start], raw)
        state.tag(start, end, name, closing)
        raw = end
    state.text(text[raw:], raw)
    state.lines[-1].end = len(text)
    if state.block_end == len(state.lines) - 1 and not state.lines[-1].runs:
        state.lines.pop()
    for line in state.lines:
        _hide_spaces(line)
    return state.lines


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


_DASH = re.compile(r"^[ \t\xa0]*(--|–|-)(?=[ \t\xa0])")
_ELLIPSIS = re.compile(r"(?<![.?!])\.\.\.(?!\.)")
_SPACES = re.compile(
    r"(?P<drop>^[ \t]+|[ \t]+\Z|[ \t]+(?=[,.!?:;…]))|(?P<one>[ \t]{2,}|\t)"
)


def _dash_rule(s: str) -> list[Edit]:
    m = _DASH.match(s)
    return [(m.start(1), m.end(1), "—")] if m else []


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


def tweak_kind(old: list[str], new: list[str], last: bool) -> str | None:
    """пометка блока диффа, если стороны отличаются только оформлением"""
    if old == new:
        return None
    if _untab(old) == _untab(new):
        return "оформление абзацев"
    if _typo(old, last) == _typo(new, last):
        return "типографика"
    if _untab(_typo(old, last)) == _untab(_typo(new, last)):
        return "оформление абзацев, типографика"
    return None
