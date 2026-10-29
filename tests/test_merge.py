import pytest

from fwriter.core.merge import Hunk, Manual, merge3, render

BASE = "a\nb\nc\nd"


def hunks(parts: list[str | Hunk]) -> list[Hunk]:
    return [p for p in parts if isinstance(p, Hunk)]


def test_one_side_only():
    parts = merge3(BASE, "a\nB\nc\nd", BASE)
    assert [(h.start, h.kind) for h in hunks(parts)] == [(1, "site")]
    assert render(parts, {}) == "a\nB\nc\nd"

    parts = merge3(BASE, BASE, "a\nb\nC\nd")
    assert [h.kind for h in hunks(parts)] == ["mine"]
    assert render(parts, {}) == "a\nb\nC\nd"


def test_fast_forward():
    site = "a\nB\nc\nd\ne"
    parts = merge3(BASE, site, BASE)
    assert {h.kind for h in hunks(parts)} == {"site"}
    assert render(parts, {}) == site


def test_different_paragraphs():
    parts = merge3(BASE, "A\nb\nc\nd", "a\nb\nc\nD")
    assert [h.kind for h in hunks(parts)] == ["site", "mine"]
    assert render(parts, {}) == "A\nb\nc\nD"


def test_same_edit():
    parts = merge3(BASE, "a\nX\nc\nd", "a\nX\nc\nd")
    assert [h.kind for h in hunks(parts)] == ["same"]
    assert render(parts, {}) == "a\nX\nc\nd"


def test_conflict_and_choices():
    parts = merge3(BASE, "a\nS\nc\nd", "a\nM\nc\nd")
    [h] = hunks(parts)
    assert (h.kind, h.base, h.site, h.mine) == ("conflict", ["b"], ["S"], ["M"])
    i = parts.index(h)
    with pytest.raises(ValueError):
        render(parts, {})
    assert render(parts, {i: "site"}) == "a\nS\nc\nd"
    assert render(parts, {i: "mine"}) == "a\nM\nc\nd"
    assert render(parts, {i: "both"}) == "a\nM\nS\nc\nd"
    assert render(parts, {i: Manual("x\ny")}) == "a\nx\ny\nc\nd"
    assert render(parts, {i: Manual("")}) == "a\nc\nd"


@pytest.mark.parametrize(
    ("base", "site", "mine", "both"),
    [
        ("A\n\nB\n\nC", "A\n\nS\n\nC", "A\n\nM\n\nC", "A\n\nM\n\nS\n\nC"),
        ("B\n\nC", "S\n\nC", "M\n\nC", "M\n\nS\n\nC"),
        # пустая строка уже на стыке вариантов
        ("A\n\nB", "A\n\nP\n\nB", "A\n\nQ\n\nB", "A\n\nQ\n\nP\n\nB"),
        (
            "A\n\nB\n\nC",
            "A\n\nX\n\nB\n\nC",
            "A\n\nX\n\nB2\n\nC",
            "A\n\nX\n\nB2\n\nX\n\nB\n\nC",
        ),
        # стиль \n: пустая строка только с одной стороны - разрыв сцены
        ("A\n\nB\nC", "A\n\nS\nC", "A\n\nM\nC", "A\n\nM\nS\nC"),
        ("B", "S", "M", "M\nS"),
    ],
)
def test_both_keeps_paragraphs_apart(base: str, site: str, mine: str, both: str):
    parts = merge3(base, site, mine)
    [h] = hunks(parts)
    assert render(parts, {parts.index(h): "both"}) == both


def test_equal_length_replace_is_split():
    # сайт правил b и c подряд, я только c: b принимается сам
    parts = merge3(BASE, "a\nB\nC\nd", "a\nb\nX\nd")
    assert [(h.start, h.kind) for h in hunks(parts)] == [(1, "site"), (2, "conflict")]


def test_one_edit_covers_two_others():
    # сайт заменил b и c одним абзацем, я правил их по отдельности: конфликт один, целиком
    parts = merge3(BASE, "a\nX\nd", "a\nB\nC\nd")
    assert [(h.kind, h.base) for h in hunks(parts)] == [("conflict", ["b", "c"])]


def test_insert_and_delete_next_to_other_edit():
    # вставка сайта после b вплотную к моей правке c
    parts = merge3(BASE, "a\nb\nNEW\nc\nd", "a\nb\nC\nd")
    assert [h.kind for h in hunks(parts)] == ["site", "mine"]
    assert render(parts, {}) == "a\nb\nNEW\nC\nd"
    # удаление b на сайте рядом с моей правкой c
    parts = merge3(BASE, "a\nc\nd", "a\nb\nC\nd")
    assert render(parts, {}) == "a\nC\nd"


def test_insert_inside_deleted_block_conflicts():
    parts = merge3(BASE, "a\nd", "a\nb\nNEW\nc\nd")
    assert [h.kind for h in hunks(parts)] == ["conflict"]


def test_restyle_merges_with_edits_inside_paragraphs():
    # я перевёл \n <-> \n\n, сайт правил абзац внутри
    parts = merge3("A\nB\nC", "A\nB2\nC", "A\n\nB\n\nC")
    assert render(parts, {}) == "A\n\nB2\n\nC"
    parts = merge3("A\n\nB\n\nC", "A\n\nB2\n\nC", "A\nB\nC")
    assert render(parts, {}) == "A\nB2\nC"


@pytest.mark.parametrize(
    ("base", "site", "mine"),
    [
        ("A\nB", "A\nP\nB", "A\n\nB"),
        ("A\nB\nC", "A\nC", "A\n\nB\n\nC"),
        ("A\n\nB", "A\n\nP\n\nB", "A\nB"),
        ("A\n\nB\n\nC", "A\n\nC", "A\nB\nC"),
    ],
)
def test_restyle_next_to_inserted_or_deleted_paragraph_conflicts(
    base: str, site: str, mine: str
):
    # иначе вышел бы лишний промежуток ("A\n\n\nC") или смесь способов
    parts = merge3(base, site, mine)
    [h] = hunks(parts)
    assert h.kind == "conflict"
    # "Сайт" и повтор твика доводят текст до нужного вида
    assert render(parts, {parts.index(h): "site"}) == site


def test_same_insert_aligned_differently_conflicts():
    # оба вставили X, я ещё правил B: пустые строки выровнены по-разному
    parts = merge3("A\n\nB\n\nC", "A\n\nX\n\nB\n\nC", "A\n\nX\n\nB2\n\nC")
    assert [h.kind for h in hunks(parts)] == ["conflict"]
    # правки соседних абзацев через пустую строку по-прежнему сливаются
    parts = merge3("A\n\nB\n\nC", "A2\n\nB\n\nC", "A\n\nB2\n\nC")
    assert render(parts, {}) == "A2\n\nB2\n\nC"


def test_change_reaches_group_before_last():
    # сайт и я по-разному переставили B: один конфликт, а не два блока
    parts = merge3("A\nC", "B\n", "\nB")
    assert [(h.kind, h.base) for h in hunks(parts)] == [("conflict", ["A", "C"])]


def test_inserts_at_same_place():
    parts = merge3(BASE, "a\nS\nb\nc\nd", "a\nM\nb\nc\nd")
    [h] = hunks(parts)
    assert h.kind == "conflict"
    assert render(parts, {parts.index(h): "both"}) == "a\nM\nS\nb\nc\nd"


def test_bytes_kept():
    base = "a\r\n\nb\n"
    mine = "a\r\n\nB\n"
    assert render(merge3(base, base, mine), {}) == mine
    assert render(merge3(base, base, base), {}) == base


def test_repeated_empty_lines():
    # без autojunk=False частые пустые строки ломали бы выравнивание
    base = "\n".join(["x", ""] * 150)
    mine = base.replace("x", "y", 1)
    site = base[: -len("x\n")] + "z\n"
    assert render(merge3(base, site, mine), {}) == mine[: -len("x\n")] + "z\n"
