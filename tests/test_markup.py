import pytest

from fwriter.core.markup import (
    TAB_WIDTH,
    TAGS,
    MarkupError,
    apply,
    check,
    count,
    dashes,
    ellipsis,
    layout,
    para_style,
    paras_touched,
    quotes,
    restyle,
    spaces,
    to_raw,
    to_screen,
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
        ("Кто-то - тут.", "Кто-то - тут."),
        ("-Привет.", "-Привет."),
        # разделитель сцены в блоке - не реплика
        ("<center>- - -</center>", "<center>- - -</center>"),
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
        "<center>Глава</center>\n<tab>— Ну… «да», а «кто-то» - нет!..\n"
        '\n<tab>Он: <b>«Привет»</b><footnote>«сноска»…</footnote>\nНечётное "'
    )
    assert skipped == [5]


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
