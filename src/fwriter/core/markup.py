import bisect
import re
from dataclasses import dataclass
from html.parser import HTMLParser

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


def layout(text: str) -> list[Line]:
    """строки читателя до переноса по ширине"""
    tags = _Tags(text)
    tags.feed(text)
    tags.close()
    state = _Layout()
    state.new_line(0, 0)
    raw = 0
    for start, end, name, closing in tags.tags:
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
