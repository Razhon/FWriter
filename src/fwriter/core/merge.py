from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

Kind = Literal["site", "mine", "same", "conflict"]


@dataclass(frozen=True)
class Hunk:
    start: int  # номер первого абзаца в base
    base: list[str]
    site: list[str]
    mine: list[str]
    kind: Kind
    gap: bool  # соседние строки base пустые: "оба" разделяется пустой строкой


@dataclass(frozen=True)
class Manual:
    text: str


Choice = Literal["mine", "site", "both"] | Manual


@dataclass(frozen=True)
class _Change:
    i1: int
    i2: int
    lines: list[str]
    side: Literal["site", "mine"]
    old: list[str]  # base[i1:i2]


def _blank(lines: list[str]) -> bool:
    # пробел - только U+0020 и \t: неразрывный пробел читатель видит
    return not any(line.strip(" \t") for line in lines)


def _changes(
    base: list[str], other: list[str], side: Literal["site", "mine"]
) -> list[_Change]:
    # autojunk выкинул бы частые строки, например пустые абзацы
    # shortcut: квадратично по числу пустых строк, ~1 с на 2000 строк - если главы дорастут, считать в потоке
    sm = SequenceMatcher(None, base, other, autojunk=False)
    out: list[_Change] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        # замену N абзацев на N режем по одному, чтобы конфликт был только там, где он есть
        if tag == "replace" and i2 - i1 == j2 - j1:
            out += [
                _Change(i1 + k, i1 + k + 1, [other[j1 + k]], side, [base[i1 + k]])
                for k in range(i2 - i1)
            ]
        else:
            out.append(_Change(i1, i2, other[j1:j2], side, base[i1:i2]))
    return out


def _overlaps(a: _Change, b: _Change, base: list[str]) -> bool:
    if a.side == b.side:
        return False
    # общие абзацы base или вставки в одно место
    if a.i1 < b.i2 and b.i1 < a.i2 or a.i1 == a.i2 == b.i1 == b.i2:
        return True
    # рядом - и через пустые строки: SequenceMatcher цепляет их к любой из правок
    lo, hi = sorted((a, b), key=lambda c: c.i1)
    if not _blank(base[lo.i2 : hi.i1]):
        return False
    a_blank, b_blank = _blank(a.lines + a.old), _blank(b.lines + b.old)
    if a_blank or b_blank:
        # иначе лишний промежуток или слипшиеся абзацы
        other = a if b_blank else b
        return a_blank == b_blank or len(other.lines) != len(other.old)
    # одинаковая вставка, выровненная по-разному, задвоилась бы
    return bool(set(a.lines) & set(b.lines) - {""})


def _apply(base: list[str], start: int, end: int, changes: list[_Change]) -> list[str]:
    out: list[str] = []
    pos = start
    for ch in changes:
        out += base[pos : ch.i1] + ch.lines
        pos = ch.i2
    return out + base[pos:end]


def merge3(base: str, site: str, mine: str) -> list[str | Hunk]:
    """Неизменённые абзацы (str) и изменённые блоки (Hunk) по порядку."""
    b = base.split("\n")
    changes = sorted(
        _changes(b, site.split("\n"), "site") + _changes(b, mine.split("\n"), "mine"),
        key=lambda c: (c.i1, c.i2),
    )
    groups: list[list[_Change]] = []
    for ch in changes:
        hit = None
        # через пустые строки правка достаёт и до групп раньше последней
        for k in range(len(groups) - 1, -1, -1):
            if any(_overlaps(ch, c, b) for c in groups[k]):
                hit = k
            if not _blank(b[max(c.i2 for c in groups[k]) : ch.i1]):
                break
        if hit is None:
            groups.append([ch])
        else:
            groups[hit:] = [[c for g in groups[hit:] for c in g] + [ch]]

    parts: list[str | Hunk] = []
    pos = 0
    for group in groups:
        start = min(c.i1 for c in group)
        end = max(c.i2 for c in group)
        parts += b[pos:start]
        s = [c for c in group if c.side == "site"]
        m = [c for c in group if c.side == "mine"]
        site_lines = _apply(b, start, end, s)
        mine_lines = _apply(b, start, end, m)
        if not m:
            kind: Kind = "site"
        elif not s:
            kind = "mine"
        else:
            kind = "same" if site_lines == mine_lines else "conflict"
        around = b[max(start - 1, 0) : start] + b[end : end + 1]
        gap = bool(around) and _blank(around)
        parts.append(Hunk(start, b[start:end], site_lines, mine_lines, kind, gap))
        pos = end
    return parts + b[pos:]


def render(parts: list[str | Hunk], choices: dict[int, Choice]) -> str:
    """choices - по индексу блока в parts; без выбора берётся kind, конфликт обязателен."""
    out: list[str] = []
    for i, part in enumerate(parts):
        if isinstance(part, str):
            out.append(part)
            continue
        choice = choices.get(i)
        if isinstance(choice, Manual):
            out += choice.text.split("\n") if choice.text else []
        elif choice == "both":
            sep = part.gap and not _blank(part.mine[-1:]) and not _blank(part.site[:1])
            out += part.mine + [""] * sep + part.site
        elif choice == "site" or (choice is None and part.kind in ("site", "same")):
            out += part.site
        elif choice == "mine" or (choice is None and part.kind == "mine"):
            out += part.mine
        else:
            raise ValueError(f"конфликт в абзаце {part.start + 1} не разобран")
    return "\n".join(out)
