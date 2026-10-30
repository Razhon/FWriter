import bisect
import dataclasses
import random

import pytest

from fwriter.core.markup import (
    AUTOCORRECT,
    TAB_WIDTH,
    TAGS,
    MarkupError,
    Reader,
    adds_tag,
    apply,
    autocorrect,
    check,
    count,
    dashes,
    ellipsis,
    enter,
    fix_tags,
    layout,
    next_char,
    para_style,
    paras_touched,
    prev_char,
    quotes,
    restyle,
    spaces,
    strip_text,
    tag_list,
    to_raw,
    to_screen,
    toggle,
    tweak_kind,
    typography,
    wrap,
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", (0, 0)),
        # схлопнутые пробелы и перевод строки не считаются
        ("Два  слова.\n<tab>ещё", (13, 3)),
        # U+00A0 - не граница слова, теги - не символы
        ("<b>т.\u00a0е.</b>", (5, 1)),
    ],
)
def test_count(text: str, expected: tuple[int, int]):
    assert count(text) == expected


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


READER_PARTS = [
    *(f"<{t}>" for t in TAGS),
    *(f"</{t}>" for t in TAGS),
    *["\n"] * 4,
    *["x", "слово ", " ", "  ", " \t", "<", ">", '"', "</", "&amp"],
    *["<!--", "-->", '<b title="', "<u>", "<?", "<!x"],
]


def test_reader_matches_layout():
    rnd = random.Random(1)
    reader = Reader()
    for _ in range(3000):
        text = "".join(rnd.choice(READER_PARTS) for _ in range(rnd.randint(0, 30)))
        rows = text.split("\n")
        # один Reader на все тексты: куски из кэша должны совпасть с разбором заново
        reader.update(rows)
        starts = [0]
        for row in rows:
            starts.append(starts[-1] + len(row) + 1)
        got = []
        for c, (row, first) in enumerate(reader.chunks):
            end = reader.chunks[c + 1][1] if c + 1 < len(reader.chunks) else None
            at = starts[row]
            for line in reader.lines[first:end]:
                runs = [dataclasses.replace(r, raw=r.raw + at) for r in line.runs]
                got.append((line.align, line.start + at, line.end + at, runs))
        want = [(x.align, x.start, x.end, x.runs) for x in layout(text)]
        assert got == want, text
        # курсор в каждой позиции - строка читателя, которая её содержит
        for raw in range(len(text) + 1):
            row = bisect.bisect_right(starts, raw) - 1
            n = reader.find(rows, row, raw - starts[row])
            assert got[n][1] <= raw and (n + 1 == len(got) or raw < got[n + 1][1])
        # позиция -> клетка -> позиция -> та же клетка (внутри <tab> позиций нет)
        for row, line in enumerate(rows):
            for col in range(len(line) + 1):
                cell = reader.locate(rows, row, col)
                loc = reader.location(rows, *cell)
                assert reader.locate(rows, *loc) == cell, text


@pytest.mark.parametrize(
    ("row", "col", "back", "forward"),
    [
        ("ab", 1, (0, 1), (1, 2)),
        # мимо тегов, <tab> - целиком
        ("a<b>c", 4, (0, 1), (4, 5)),
        ("a<b><tab>", 9, (4, 9), None),
        ("<tab>x", 0, None, (0, 5)),
        # до края строки только теги
        ("<b>x", 3, None, (3, 4)),
        ("x</b>", 1, (0, 1), None),
    ],
)
def test_prev_next_char(row, col, back, forward):
    assert prev_char(row, col) == back
    assert next_char(row, col) == forward


@pytest.mark.parametrize(
    ("old", "new", "added"),
    [
        ("a", "a<u>", True),
        ("a", "a</b>", True),
        ("a", "a<B>", True),
        ("<b>a</b>", "<b>ab</b>", False),
        ("a <", "a <3", False),
        ("a", "a -> b", False),
        # удаление тоже может собрать тег
        ("<x b>", "<b>", True),
    ],
)
def test_adds_tag(old, new, added):
    assert adds_tag(old, new) is added


def test_strip_text():
    assert strip_text("a<b>bc</b>d\n<tab>e") == "<b></b>"


@pytest.mark.parametrize(
    ("text", "a", "b", "name", "result", "selected"),
    [
        ("ab cd ef", 3, 5, "b", "ab <b>cd</b> ef", "cd"),
        ("ab <b>cd</b> ef", 6, 8, "b", "ab cd ef", "cd"),
        # снять с части - тег разрывается
        ("<b>abcdef</b>", 5, 7, "b", "<b>ab</b>cd<b>ef</b>", "cd"),
        # частично оформленное выделение оформляется целиком
        ("a<i>bc</i>d", 0, 3, "b", "<b>a</b><i>bc</i>d", "a"),
        # в каждом абзаце отдельно
        ("ab\ncd", 1, 4, "i", "a<i>b</i>\n<i>c</i>d", "b\nc"),
        # сноска внутри <b>: теги не перепутаны
        ("x<b>yz</b>w", 4, 5, "footnote", "x<b><footnote>y</footnote>z</b>w", "y"),
        ("ab<footnote>cd</footnote>", 13, 14, "footnote", "abcd", "d"),
        (
            "a<center>b</center>c",
            0,
            20,
            "footnote",
            (
                "<footnote>a</footnote><center><footnote>b</footnote></center>"
                "<footnote>c</footnote>"
            ),
            "abc",
        ),
        # выравнивание: абзацы у читателя те же, без лишних пустых строк
        ("A\nB\nC", 2, 2, "center", "A\n<center>B</center>C", ""),
        ("A\n<center>B</center>C", 10, 10, "center", "A\nB\nC", ""),
        ("A\n<center>B</center>C", 10, 10, "right", "A\n<right>B</right>C", ""),
        ("<center>C</center>X", 8, 8, "center", "C\nX", ""),
        ("<center>C</center>\nX", 8, 8, "center", "C\n\nX", ""),
        # блок из нескольких абзацев - целиком
        ("<center>a\nb</center>", 10, 10, "center", "a\nb", ""),
        # выделение на несколько абзацев - все, пустые не трогаем
        (
            "A\n\nB\nC",
            0,
            5,
            "center",
            "<center>A</center>\n<center>B</center>C",
            "A\nB",
        ),
        (
            "<center>A</center>B",
            8,
            19,
            "center",
            "<center>A</center><center>B</center>",
            "AB",
        ),
        ("<center>A</center><center>B</center>", 8, 27, "center", "A\nB", "A\nB"),
        # конец выделения в начале строки её не касается
        ("A\nB", 0, 2, "right", "<right>A</right>B", "A"),
    ],
)
def test_toggle(text, a, b, name, result, selected):
    got = toggle(text, a, b, name)
    assert not isinstance(got, str)
    edit, na, nb = got
    new = apply(text, [edit])
    assert new == result
    assert check(new) is None
    # выделение на том же видимом тексте
    part = new[na:nb]
    tags = sorted((s, e) for s, e, _, _ in tag_list(part))
    shown = "".join(
        part[e:s] for (_, e), (s, _) in zip([(0, 0), *tags], [*tags, (len(part), 0)])
    )
    assert shown == selected


def test_toggle_random():
    def shown(text: str) -> list[str]:
        lines = [
            "".join(r.text for r in line.runs if not r.virtual) for line in layout(text)
        ]
        # пустые строки в конце главы читатель не увидит: F5 их обрежет
        while len(lines) > 1 and not lines[-1]:
            lines.pop()
        return lines

    rnd = random.Random(2)
    names = ["b", "i", "s", "footnote", "center", "right"]
    for _ in range(300):
        words = [rnd.choice(["слово", "x", " ", "\n", "<tab>"]) for _ in range(12)]
        text = "".join(words)
        for _ in range(8):
            a, b = sorted(rnd.randint(0, len(text)) for _ in range(2))
            got = toggle(text, a, b, rnd.choice(names))
            if isinstance(got, str):
                continue
            new = apply(text, [got[0]])
            # теги не перепутаны, текст и абзацы у читателя те же
            assert check(new) is None, (text, a, b, new)
            assert shown(new) == shown(text), (text, a, b, new)
            text = new


def test_toggle_refused():
    assert toggle("ab", 1, 1, "b") == "Выделите текст, который оформить"
    # b снаружи блока изнутри не снять
    assert "F7" in str(toggle("<b><center>x</center></b>", 11, 12, "b"))


@pytest.mark.parametrize(
    ("row", "col", "last", "result"),
    [
        # за блоком в конце главы пустой строки читателя нет - её даёт \n
        ("<center>C</center>", 9, True, (18, "\n")),
        # дальше \n: пустая строка уже есть
        ("<center>C</center>", 9, False, (18, "")),
        ("<center><b>C</b></center>X", 12, False, (25, "\n")),
        ("<center>CD</center>", 9, False, (9, "\n")),
        ("abc", 1, False, (1, "\n")),
    ],
)
def test_enter(row, col, last, result):
    assert enter(row, col, last) == result


def test_reader_span():
    reader = Reader()
    reader.update(["a", "<b>b", "c</b>", "d", "<center>e</center>"])
    assert reader.span(2, 2) == (1, 3)
    assert reader.span(0, 1) == (0, 3)
    assert reader.span(4, 4) == (4, 5)


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


# оформление абзацев

STYLES = {
    "blank": "Раз.\n\nДва.\n\nТри.",
    "indent": "<tab>Раз.\n<tab>Два.\n<tab>Три.",
    "indent_blank": "<tab>Раз.\n\n<tab>Два.\n\n<tab>Три.",
    "plain": "Раз.\nДва.\nТри.",
}


@pytest.mark.parametrize("style", STYLES)
def test_para_style(style: str):
    assert para_style(STYLES[style]) == style


def test_para_style_mixed_and_empty():
    assert para_style("<tab>Раз.\nДва.") == "mixed"
    assert para_style("Раз.\n\nДва.\nТри.") == "mixed"
    assert para_style("") == "plain"
    assert para_style("<center>***</center>") == "plain"


@pytest.mark.parametrize("source", STYLES)
@pytest.mark.parametrize("target", ["blank", "indent", "indent_blank"])
def test_restyle_between_all_styles(source: str, target):
    text = apply(STYLES[source], restyle(STYLES[source], target))
    assert text == STYLES[target]
    assert restyle(text, target) == []


def test_restyle_keeps_blocks_scene_breaks_and_text():
    text = (
        "<center>Глава</center>\nРаз.\nДва <b>жирно\nи дальше</b>.\n\n\nТри."
        "<footnote>сноска\nв две строки</footnote>\n<right>Подпись</right>"
    )
    out = apply(text, restyle(text, "indent_blank"))
    assert out == (
        "<center>Глава</center>\n<tab>Раз.\n\n<tab>Два <b>жирно\n\n<tab>и дальше</b>."
        "\n\n\n<tab>Три.<footnote>сноска\nв две строки</footnote>\n<right>Подпись</right>"
    )
    # кроме \n между абзацами и <tab>, ничего не менялось
    assert out.replace("<tab>", "").replace("\n", "") == text.replace("\n", "")


def test_restyle_after_center_no_extra_blank():
    # у читателя после </center> уже пустая строка: абзац рядом с блоком не трогаем
    text = "<center>***</center>\nРаз.\nДва."
    assert apply(text, restyle(text, "blank")) == "<center>***</center>\nРаз.\n\nДва."


def test_restyle_para_glued_to_block_end():
    # выравнивание в WYSIWYG съедает \n за </center>: абзац идёт сразу за тегом
    text = "<center>***</center><tab>Раз.\n\n<tab>Два."
    assert para_style(text) == "indent_blank"
    out = apply(text, restyle(text, "blank"))
    assert out == "<center>***</center>Раз.\n\nДва."
    assert (
        apply(out, restyle(out, "indent")) == "<center>***</center><tab>Раз.\n<tab>Два."
    )


def test_restyle_selection():
    text = "Раз.\nДва.\nТри.\nЧетыре."
    sel = (text.index("Два"), text.index("Три") + 1)
    assert apply(text, restyle(text, "blank", sel)) == "Раз.\nДва.\n\nТри.\nЧетыре."


def test_restyle_crlf():
    text = "Раз.\r\nДва."
    assert apply(text, restyle(text, "blank")) == "Раз.\r\n\r\nДва."


def test_paras_touched_matches_restyle():
    text = STYLES["plain"]
    assert paras_touched(text, restyle(text, "indent_blank")) == 3
    assert paras_touched(text, restyle(text, "blank")) == 2


def test_restyle_selection_to_line_start():
    # Shift+Down: выделение кончается в начале следующей строки, она не задета
    text = "Раз.\nДва.\nТри."
    sel = (0, text.index("Три"))
    assert apply(text, restyle(text, "blank", sel)) == "Раз.\n\nДва.\nТри."
    edits, _ = typography("- Раз.\n- Два.", (0, len("- Раз.\n")))
    assert apply("- Раз.\n- Два.", edits) == "— Раз.\n- Два."


def test_restyle_tab_inside_inline_tag():
    text = "<b><tab>Раз.</b>\nДва."
    assert para_style(text) == "mixed"
    out = apply(text, restyle(text, "indent"))
    assert out == "<b><tab>Раз.</b>\n<tab>Два."
    assert apply(out, restyle(out, "blank")) == "<b>Раз.</b>\n\nДва."


def test_paras_touched_trailing_blank_lines():
    text = "Раз \n\n"
    edits, _ = typography(text)
    assert apply(text, edits) == "Раз"
    assert paras_touched(text, edits) == 1


# типографика


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("- Привет.", "— Привет."),
        ("<tab><b>-- Привет.</b>", "<tab><b>— Привет.</b>"),
        ("  – Привет.", "  — Привет."),
        # между пробелами - тире, у одного пробела и в слове - нет
        ("Кто-то - тут.", "Кто-то — тут."),
        ("Раз – два -- три\xa0- четыре.", "Раз — два — три\xa0— четыре."),
        ("Раз -два, раз- два, из-за.", "Раз -два, раз- два, из-за."),
        ("Раз --- два.", "Раз --- два."),
        ("Раз <b>-</b> два.", "Раз <b>—</b> два."),
        ("-Привет.", "-Привет."),
        # разделитель сцены - не реплика, в блоке и без него
        ("<center>- - -</center>", "<center>- - -</center>"),
        ("- - -", "- - -"),
        ("<tab>-- – --", "<tab>-- – --"),
        ("<right>Раз\n- - -</right>\n- Да.", "<right>Раз\n- - -</right>\n— Да."),
    ],
)
def test_dashes(text: str, want: str):
    assert apply(text, dashes(text)) == want


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ('Он сказал: "Привет".', "Он сказал: «Привет»."),
        ('"Он сказал "привет" и ушёл"', "«Он сказал „привет“ и ушёл»"),
        ('«Он сказал "привет"»', "«Он сказал „привет“»"),
        ('("да"), —"нет"', "(«да»), —«нет»"),
        ('Он сказал "" и ушёл, "да"', "Он сказал «» и ушёл, «да»"),
        ('(""), ""', "(«»), «»"),
        ('""Война и мир" - роман"', "«„Война и мир“ - роман»"),
        ('"роман - "Война и мир""', "«роман - „Война и мир“»"),
        ('<b>"</b>жирно"', "<b>«</b>жирно»"),
        ('сноска<footnote>"текст"</footnote>', "сноска<footnote>«текст»</footnote>"),
    ],
)
def test_quotes(text: str, want: str):
    edits, skipped = quotes(text)
    assert (apply(text, edits), skipped) == (want, [])


def test_quotes_odd_line_skipped():
    text = 'Раз "два".\nТри "четыре.\nПять "шесть".'
    edits, skipped = quotes(text)
    assert apply(text, edits) == 'Раз «два».\nТри "четыре.\nПять «шесть».'
    assert skipped == [1]


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Ну... да", "Ну… да"),
        ("Что?.. Да!..", "Что?.. Да!.."),
        ("Четыре....", "Четыре...."),
        ("Через <b>..</b>.", "Через <b>..</b>."),
    ],
)
def test_ellipsis(text: str, want: str):
    assert apply(text, ellipsis(text)) == want


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Раз  \t два", "Раз два"),
        ("Раз\tдва", "Раз два"),
        ("  Раз два  ", "Раз два"),
        ("   ", ""),
        ("Раз , два !", "Раз, два!"),
        # после тире пробел перед знаком нужен
        ("Кто — ?", "Кто — ?"),
        ("Кто -  ?", "Кто - ?"),
        ("Кто —\t?", "Кто — ?"),
        # многоточие перед словом открывает фразу
        ("Итог: <i>…когда</i>", "Итог: <i>…когда</i>"),
        ("Итог: ...когда", "Итог: ...когда"),
        ("Итог …", "Итог…"),
        ("Итог ... да", "Итог... да"),
        # после двоеточия знак отдельный
        ("<b>Бездна: </b><b><i>…</i></b>", "<b>Бездна: </b><b><i>…</i></b>"),
        ("Раз (два) , «три» .", "Раз (два), «три»."),
        # пробелы по обе стороны тега убираются, тег остаётся
        ("Да <b> </b>, нет", "Да<b></b>, нет"),
        ("Раз\xa0\xa0два", "Раз\xa0\xa0два"),
        ("\n \nРаз\n\nДва\n  \n", "Раз\n\nДва"),
        ("Раз \r\nДва\r\n\r\n", "Раз\r\nДва"),
    ],
)
def test_spaces(text: str, want: str):
    assert apply(text, spaces(text)) == want


def test_typography_selection_and_tags_kept():
    text = '- Раз "два"...\n- Три "четыре"...\n- Пять...'
    sel = (text.index("Три"), text.index("Три"))
    edits, _ = typography(text, sel)
    assert apply(text, edits) == '- Раз "два"...\n— Три «четыре»…\n- Пять...'


GLAVA = (
    '\n<center>Глава</center>\n<tab>- Ну  ... "да" , а "кто-то" - нет!..\n'
    '\n<tab>Он: <b>"Привет"</b><footnote>"сноска" ...</footnote>\nНечётное "\n  \n'
)


@pytest.mark.parametrize(
    "tweak",
    [
        dashes,
        ellipsis,
        spaces,
        lambda t: quotes(t)[0],
        lambda t: typography(t)[0],
    ],
)
def test_tweaks_idempotent_and_counted(tweak):
    edits = tweak(GLAVA)
    once = apply(GLAVA, edits)
    assert tweak(once) == []
    changed = sum(
        a != b for a, b in zip(GLAVA.split("\n"), once.split("\n"), strict=False)
    )
    assert paras_touched(GLAVA, edits) >= 1
    assert check(once) is None
    # строки сравнимы, пока чистка не убрала крайние пустые
    if once.count("\n") == GLAVA.count("\n"):
        assert paras_touched(GLAVA, edits) == changed


def test_typography_all_at_once():
    edits, skipped = typography(GLAVA)
    assert apply(GLAVA, edits) == (
        "<center>Глава</center>\n<tab>— Ну… «да», а «кто-то» — нет!..\n"
        '\n<tab>Он: <b>«Привет»</b><footnote>«сноска»…</footnote>\nНечётное "'
    )
    assert skipped == [5]


FIX_TAGS = [
    (
        "<center><s><i>Ещ</i></s><i>ё</i><s><i> не.</i></s></center>",
        "<center><i><s>Ещ</s>ё<s> не.</s></i></center>",
    ),
    (
        "<tab><b>Провал: </b><b><i>Не…</i></b>",
        "<tab><b>Провал: <i>Не…</i></b>",
    ),
    ("<b></b>x<i></i>", "x"),
    ("<b>a<b>b</b>c</b>", "<b>abc</b>"),
    # пробел между b или i входит в них, у s остаётся
    ("<i>один</i> <i>два</i>", "<i>один два</i>"),
    ("<s>один</s> <s>два</s>", "<s>один</s> <s>два</s>"),
    ("a<b> </b>b", "a b"),
    ("<b>a</b>\r<b>b</b>", "<b>a\rb</b>"),
    # <tab>, сноска и \n - границы, стек на них не меняется
    ("<b>a</b><tab><b>b</b>", "<b>a</b><tab><b>b</b>"),
    ("<i>a<footnote><i>b</i></footnote></i>", "<i>a<footnote>b</footnote></i>"),
    ("<b>a</b><b>b\nc</b><b>d</b>", "<b>ab\ncd</b>"),
    ("<b>a\n</b>", "<b>a\n</b>"),
]


def _styles(text: str) -> list[tuple[str, frozenset[str]]]:
    # у пробелов b и i не видно
    return [
        (ch, run.style - {"b", "i"} if ch in " \t\r" else run.style)
        for line in layout(text)
        for run in line.runs
        for ch in run.text
    ]


@pytest.mark.parametrize(("text", "want"), FIX_TAGS)
def test_fix_tags(text: str, want: str):
    once = apply(text, fix_tags(text))
    assert once == want
    assert check(once) is None
    assert _styles(once) == _styles(text)
    assert fix_tags(once) == []


def test_fix_tags_selection_and_broken_markup():
    text = "<b>a</b><b>b</b>\n<i>c</i><i>d</i>"
    assert apply(text, fix_tags(text, (len(text) - 2, len(text)))) == (
        "<b>a</b><b>b</b>\n<i>cd</i>"
    )
    assert fix_tags("<b>a</b><b>b") == []
    assert fix_tags("x <<b></b>i>y") == []


# пометка правок твиков в диффе


@pytest.mark.parametrize(
    ("old", "new", "last", "kind"),
    [
        (["Раз."], ["", "Раз."], False, "оформление абзацев"),
        (["Раз.", "Два."], ["<tab>Раз.", "", "<tab>Два."], False, "оформление абзацев"),
        (['Ну... "да"'], ["Ну… «да»"], False, "типографика"),
        (['Ну... "да"'], ['Ну… "да"'], False, "типографика"),
        (
            ["- Раз ...", "Два."],
            ["<tab>— Раз…", "", "<tab>Два."],
            False,
            "оформление абзацев, типографика",
        ),
        (["Раз.  ", ""], ["Раз."], True, "типографика"),
        (["Раз.  ", ""], ["Раз."], False, "оформление абзацев, типографика"),
        (["Раз.", "  "], ["Раз."], False, "оформление абзацев"),
        (["Раз слово."], ["Раз слова."], False, None),
        (["Раз."], ["Раз."], False, None),
        (["<b>Раз</b><b>, два</b>"], ["<b>Раз, два</b>"], False, "разметка"),
        (
            ["<b>Раз: </b><b><i>два</i></b>", "Три , четыре"],
            ["<b>Раз: <i>два</i></b>", "Три, четыре"],
            False,
            "типографика, разметка",
        ),
        (
            ["<b>Раз: </b><b><i>два</i></b>"],
            ["", "<tab><b>Раз: <i>два</i></b>"],
            False,
            "оформление абзацев, разметка",
        ),
    ],
)
def test_tweak_kind(old, new, last, kind):
    assert tweak_kind(old, new, last) == kind


@pytest.mark.parametrize(
    ("text", "sel", "expected"),
    [
        ("ab", (1, 1), "a<b></b>b"),
        ("abc", (0, 2), "<b>ab</b>c"),
        # каждая строка отдельно, пустые и хвост из одного \n не оборачиваются
        ("ab\n\ncd\nef", (1, 7), "a<b>b</b>\n\n<b>cd</b>\nef"),
    ],
)
def test_wrap(text: str, sel: tuple[int, int], expected: str):
    assert apply(text, wrap(text, sel, "b")) == expected


@pytest.mark.parametrize(
    ("row", "col", "edit"),
    [
        ("a--", 3, (1, 3, "—")),
        ("- ", 2, (0, 1, "—")),
        ("<tab>- ", 7, (5, 6, "—")),
        ("а - ", 4, (2, 3, "—")),
        ("а- ", 3, None),  # дефис после слова
        ("<center>- </center>", 10, None),  # разделитель сцены
        ("ну...", 5, (2, 5, "…")),
        ("ну?..", 5, None),
        ("ну?...", 6, None),
        ('"', 1, (0, 1, "«")),
        ('а "б"', 5, (4, 5, "»")),
        ('«а "', 4, (3, 4, "„")),
        ('«а „б"', 6, (5, 6, "“")),
        ('(<footnote>"', 12, (11, 12, "«")),
        ("<b>-</b>-", 9, None),  # через тег не заменяется
        ("a--b", 3, (1, 3, "—")),  # набрано в середине строки
    ],
)
def test_autocorrect(row, col, edit):
    assert autocorrect(row, col, AUTOCORRECT) == edit


def test_autocorrect_off():
    assert autocorrect("a--", 3, ["quotes"]) is None
    assert autocorrect('"', 1, ["quotes"]) == (0, 1, "«")
