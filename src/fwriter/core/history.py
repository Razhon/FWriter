"""История главы на диске: слияние (шаг 4 п. 5 дизайна) и восстановление версии."""

from dataclasses import replace
from typing import Literal

from .merge import Choice, Hunk, Manual, merge3, render
from .storage import ChapterMeta, ChapterStorage, LastMerge, MergeChoice, Version

type Parts = list[str | Hunk]


def _parts(
    storage: ChapterStorage, fic_id: str, chapter_id: str, merge: LastMerge
) -> Parts:
    base, site, mine = (
        "" if f is None else storage.read_version(fic_id, chapter_id, f)
        for f in (merge.base, merge.site, merge.mine)
    )
    return merge3(base, site, mine)


def _choices(parts: Parts, merge: LastMerge) -> dict[int, Choice]:
    """Решения из last_merge; если хоть одно не на своём блоке - ни одного."""
    choices: dict[int, Choice] = {}
    for c in merge.choices:
        part = parts[c.part] if 0 <= c.part < len(parts) else None
        if not isinstance(part, Hunk) or part.start != c.start:
            # выборочно решения легли бы не на те блоки
            return {}
        choices[c.part] = Manual(c.text or "") if c.choice == "manual" else c.choice
    return choices


def _saved(parts: Parts, choices: dict[int, Choice]) -> list[MergeChoice]:
    saved = []
    for i, c in choices.items():
        part = parts[i]
        assert isinstance(part, Hunk)
        if isinstance(c, Manual):
            saved.append(MergeChoice(i, part.start, "manual", c.text))
        else:
            saved.append(MergeChoice(i, part.start, c))
    return saved


def _snapshot(
    storage: ChapterStorage, fic_id: str, chapter_id: str, meta: ChapterMeta
) -> Version:
    """Черновик версией; если он равен последней версии - она."""
    draft = storage.read_draft(fic_id, chapter_id) or ""
    last = meta.versions[-1] if meta.versions else None
    if last and storage.read_version(fic_id, chapter_id, last.file) == draft:
        return last
    return storage.add_version(fic_id, chapter_id, draft, meta.title, "manual")


def restore_version(
    storage: ChapterStorage, fic_id: str, chapter_id: str, title: str, v: Version
) -> str:
    """6.5: черновик - версией, текст v - в черновик; ревизия сайта - в restored."""
    text = storage.read_version(fic_id, chapter_id, v.file)
    _snapshot(
        storage, fic_id, chapter_id, storage.load_chapter(fic_id, chapter_id, title)
    )
    storage.write_draft(fic_id, chapter_id, text)
    meta = storage.load_chapter(fic_id, chapter_id, title)
    restored = v.file if v.source == "site" else None
    storage.write_chapter(fic_id, chapter_id, replace(meta, restored=restored))
    return text


def start_merge(
    storage: ChapterStorage, fic_id: str, chapter_id: str, title: str, site: Version
) -> tuple[LastMerge, Parts, dict[int, Choice]]:
    """Шаг 4.1-4.2: снимок черновика и merge3; на длинной главе долго - звать в потоке.

    Решения - отложенные по Esc. OSError и ValueError - от диска.
    """
    meta = storage.load_chapter(fic_id, chapter_id, title)
    # слияние, закрытое без "Готово", начинается заново без второго снимка
    mine = _snapshot(storage, fic_id, chapter_id, meta)
    merge = LastMerge(site.file, mine.file, meta.base)
    parts = _parts(storage, fic_id, chapter_id, merge)
    old = meta.last_merge
    if old is None or replace(old, choices=[]) != merge:
        return merge, parts, {}
    return old, parts, _choices(parts, old)


def load_merge(
    storage: ChapterStorage, fic_id: str, chapter_id: str
) -> (
    tuple[LastMerge, Parts, dict[int, Choice], Version | None]
    | Literal["outdated"]
    | None
):
    """F4: последнее слияние с прежними решениями и ревизией сайта; None - слияний не было.

    "outdated" - base ушла дальше слияния: пересчёт выкинул бы новые правки сайта.
    """
    meta = storage.read_chapter(fic_id, chapter_id)
    merge = meta.last_merge
    if merge is None:
        return None
    if meta.base not in (merge.base, merge.site):
        return "outdated"
    site = next((v for v in meta.versions if v.file == merge.site), None)
    # отложенное: черновик мог поменяться после Esc, старый снимок выкинул бы правки
    if meta.base == merge.base and site is not None:
        return *start_merge(storage, fic_id, chapter_id, meta.title, site), site
    parts = _parts(storage, fic_id, chapter_id, merge)
    return merge, parts, _choices(parts, merge), site


def postpone_merge(
    storage: ChapterStorage,
    fic_id: str,
    chapter_id: str,
    title: str,
    merge: LastMerge,
    parts: Parts,
    choices: dict[int, Choice],
) -> bool:
    """Esc: решения - в last_merge; False - слияние уже закончено, не пишем."""
    meta = storage.load_chapter(fic_id, chapter_id, title)
    if meta.base != merge.base:
        return False
    merge = replace(merge, choices=_saved(parts, choices))
    storage.write_chapter(fic_id, chapter_id, replace(meta, last_merge=merge))
    return True


def finish_merge(
    storage: ChapterStorage,
    fic_id: str,
    chapter_id: str,
    title: str,
    merge: LastMerge,
    parts: Parts,
    choices: dict[int, Choice],
) -> str:
    """Шаг 4.4: результат в черновик, base - ревизия сайта, решения - в last_merge."""
    text = render(parts, choices)
    draft = storage.read_draft(fic_id, chapter_id)
    # при F4 в черновике бывают правки после слияния
    if (
        draft is not None
        and draft != text
        and draft != storage.read_version(fic_id, chapter_id, merge.mine)
    ):
        storage.add_version(fic_id, chapter_id, draft, title, "manual")
    storage.write_draft(fic_id, chapter_id, text)
    meta = storage.load_chapter(fic_id, chapter_id, title)
    merge = replace(merge, choices=_saved(parts, choices))
    storage.write_chapter(
        fic_id, chapter_id, replace(meta, base=merge.site, last_merge=merge)
    )
    return text
