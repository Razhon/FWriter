import pytest

from fwriter.core.markup import (
    TAB_WIDTH,
    TAGS,
    MarkupError,
    check,
    layout,
    to_raw,
    to_screen,
)

VALID = (
    "<tab>Начало <b>жирного <i>и курсива</i></b>.\n"
    "<center>Центр\nвторая строка</center>после\n"
    "<right><s>было</s></right>\n"
    "сноска<footnote>текст <b>сноски</b></footnote>\n"
    "<3, a < b, &lt;b&gt; и незакрытое <b"
)


def test_valid():
    assert check(VALID) is None


@pytest.mark.parametrize(
    ("text", "line", "col", "word"),
    [
        ("a\n<b>x", 1, 0, "не закрыт"),
        ("<i>x</i>\n<b>x<i>y</i>", 1, 0, "не закрыт"),
        ("x</b>", 0, 1, "Лишний"),
        ("<u>x</u>", 0, 0, "Неизвестный"),
        ("<p>x</p>", 0, 0, "Неизвестный"),
        ("ok\n<B>x</B>", 1, 0, "верхнем регистре"),
        ("<b>x</B>", 0, 4, "верхнем регистре"),
        ('<b class="x">x</b>', 0, 0, "атрибутами"),
        ("<tab/>x", 0, 0, "одиночным"),
        ("<tab>x</tab>", 0, 6, "</tab>"),
        ("<b><i>x</b></i>", 0, 7, "раньше вложенного"),
        ("<footnote>a<footnote>b</footnote></footnote>", 0, 11, "внутри сноски"),
        ("a <!-- <u> --> b", 0, 2, "Комментарий"),
    ],
)
def test_errors(text: str, line: int, col: int, word: str):
    error = check(text)
    assert isinstance(error, MarkupError)
    assert (error.line, error.col) == (line, col)
    assert word in error.message


def lines(text: str) -> list[tuple[str, str]]:
    # (выравнивание, текст строки как у читателя: без скрытых пробелов)
    return [
        (line.align, "".join(r.text for r in line.runs if not r.hidden))
        for line in layout(text)
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A\n<center>C</center>B", [("left", "A"), ("center", "C"), ("left", "B")]),
        ("A<center>C</center>B", [("left", "A"), ("center", "C"), ("left", "B")]),
        (
            "<center>C</center>\nB",
            [("center", "C"), ("left", ""), ("left", "B")],
        ),
        ("<center>a\nb</center>", [("center", "a"), ("center", "b")]),
        ("<right>a</right><center>b</center>", [("right", "a"), ("center", "b")]),
        ("A\n\n<center>C</center>", [("left", "A"), ("left", ""), ("center", "C")]),
        ("A\n", [("left", "A"), ("left", "")]),
        ("  a \t <b> b</b>  ", [("left", "a b")]),
        ("a\xa0\xa0b", [("left", "a\xa0\xa0b")]),
        ("a\r\nb", [("left", "a"), ("left", "b")]),
        ("x<footnote>n</footnote>", [("left", "x(n)")]),
        ("<tab>t", [("left", "\xa0" * TAB_WIDTH + "t")]),
        # битая разметка - как у читателя
        ("</b>x<u>y</u><B>z", [("left", "</b>x<u>y</u><B>z")]),
        ("<center>a\nb", [("center", "a"), ("center", "b")]),
        ("<style><b>y</b>", [("left", "<style>y")]),
        (
            "<footnote>a<footnote>b</footnote>c</footnote>",
            [("left", "(ab)c</footnote>")],
        ),
    ],
)
def test_layout_lines(text: str, expected: list[tuple[str, str]]):
    assert lines(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        VALID,
        "<tab><b>x\ny</b> <footnote>n <i>m</i></footnote>.",
        "<right>r</right>\n\nB  c",
    ],
)
def test_check_and_layout_agree(text: str):
    # что проверка пропустила как тег, то отрисовка не показывает текстом
    assert check(text) is None
    shown = "".join(r.text for line in layout(text) for r in line.runs if not r.virtual)
    for tag in TAGS:
        assert f"<{tag}>" not in shown
        assert f"</{tag}>" not in shown


def test_layout_styles_span_lines():
    [first, second] = layout("<b>x\ny</b>z <b><i>q</b></i>")
    assert [(r.text, r.style) for r in first.runs] == [("x", {"b"})]
    assert [(r.text, r.style) for r in second.runs] == [
        ("y", {"b"}),
        ("z ", frozenset()),
        ("q", {"b", "i"}),
    ]


def test_layout_hidden_spaces_stay_on_screen():
    [line] = layout("  a   b ")
    assert [(r.text, r.hidden) for r in line.runs] == [
        ("  ", True),
        ("a ", False),
        ("  ", True),
        ("b", False),
        (" ", True),
    ]


MAP_TEXTS = [
    "A\n<center>C</center>B",
    "<center>C</center>\nB  c",
    "<tab><b>x\ny</b> <footnote>n <i>m</i></footnote>.",
    "</b>x<u>y</u>\n\n<right>r</right>",
]


@pytest.mark.parametrize("text", MAP_TEXTS)
def test_layout_position_map(text: str):
    result = layout(text)
    # каждая клетка экрана -> сырая позиция -> та же клетка
    for n, line in enumerate(result):
        width = sum(len(r.text) for r in line.runs)
        for col in range(width + 1):
            raw = to_raw(result, n, col)
            if not any(r.virtual and r.raw == raw for r in line.runs):
                assert to_screen(result, raw) == (n, col)
    # каждая сырая позиция попадает в клетку, по порядку текста
    cells = [to_screen(result, raw) for raw in range(len(text) + 1)]
    assert cells == sorted(cells)
    # видимые символы на месте
    for line in result:
        for r in line.runs:
            if not r.virtual:
                assert (
                    text[r.raw : r.raw + len(r.text)]
                    .replace("\t", " ")
                    .replace("\r", " ")
                    == r.text
                )
