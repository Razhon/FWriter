"""Синхронизация главы с сайтом (шаги 1-4 п. 5 дизайна) и отправка на сайт."""

import logging
from dataclasses import dataclass, replace
from typing import Literal

from .api.client import FicbookAPI
from .api.errors import FicbookAPIError
from .api.models import Revision, RevisionInfo
from .markup import MarkupError, check
from .storage import ChapterMeta, ChapterStorage, Version

log = logging.getLogger(__name__)

type SyncKind = Literal["same", "new", "ff", "merge"]


@dataclass(frozen=True)
class Synced:
    """new - первая копия, ff - черновик догнал сайт, merge - нужно слияние."""

    kind: SyncKind
    text: str  # черновик после синхронизации
    site: Version | None = None  # новая ревизия сайта у ff и merge
    status: int | None = None  # param главы на сайте; None у readonly и не у автора
    title_conflict: str | None = None  # название на сайте, если поменяли оба


def _same(
    storage: ChapterStorage, fic_id: str, chapter_id: str, v: Version, rev: Revision
) -> bool:
    # без точной даты (изначальная версия) ключ - текст
    if v.author != rev.author:
        return False
    if rev.created is None:
        return storage.read_version(fic_id, chapter_id, v.file) == rev.content
    return v.created == rev.created


def _renumber(
    v: Version, nums: dict[str, int], known: Version | None, last: RevisionInfo
) -> Version:
    if v.site_rev in nums:
        return replace(v, site_num=nums[v.site_rev])
    if known is not None and v.file == known.file:
        return replace(v, site_num=last.num)
    return v


def _merge_title(meta: ChapterMeta, site: str) -> tuple[str, str | None]:
    """Название как текст: правка одной стороны берётся, обеих - остаётся своя."""
    prev = meta.site_title
    if prev is None:
        # первое открытие или chapter.json до site_title
        return (site if not meta.versions else meta.title), None
    if site == prev or meta.title == site:
        return meta.title, None
    if meta.title == prev:
        return site, None
    return meta.title, site


async def sync_chapter(
    api: FicbookAPI,
    storage: ChapterStorage,
    fic_id: str,
    chapter_id: str,
    title: str,
    readonly: bool,
) -> Synced:
    """Догоняет черновик до последней правки сайта; слияние - дело UI.

    FicbookAPIError - от сайта; OSError и ValueError - от диска.
    """
    meta = storage.load_chapter(fic_id, chapter_id, title)
    status, site_title = None, title  # при readonly страницы главы нет (403)
    if not readonly:
        # каждый раз: статус и название меняют и на сайте
        chapter = await api.get_chapter(fic_id, chapter_id)
        status, site_title = chapter.status, chapter.title
        if meta.history_url is None:
            meta.history_url = chapter.history_url
    elif meta.history_url is None:
        meta.history_url = api.participated_history_url(fic_id, chapter_id)
    meta.title, conflict = _merge_title(meta, site_title)
    meta.site_title = site_title
    revs = await api.get_chapter_revisions(meta.history_url)
    last = await api.get_revision(revs[0].url)

    known = next(
        (
            v
            for v in meta.versions
            if v.source != "manual" and _same(storage, fic_id, chapter_id, v, last)
        ),
        None,
    )
    # номера сдвигаются, когда сайт удаляет старые правки
    nums = {r.rev_id: r.num for r in revs if r.rev_id}
    meta.versions = [_renumber(v, nums, known, revs[0]) for v in meta.versions]
    storage.write_chapter(fic_id, chapter_id, meta)

    base = next((v for v in meta.versions if v.file == meta.base), None)
    if known is not None and base is not None and base.file == known.file:
        draft = storage.read_draft(fic_id, chapter_id)
        text = last.content if draft is None else draft
        return Synced("same", text, status=status, title_conflict=conflict)

    site = known or storage.add_version(
        fic_id,
        chapter_id,
        last.content,
        site_title,
        "site",
        last.comment,
        created=last.created or revs[0].created,
        site_num=revs[0].num,
        author=last.author,
        author_name=last.author_name,
    )
    draft = storage.read_draft(fic_id, chapter_id)
    kind: SyncKind
    if draft is None:
        kind = "new"
    elif draft == last.content:
        kind = "same"
    elif base is not None and draft == storage.read_version(
        fic_id, chapter_id, base.file
    ):
        kind = "ff"  # черновик равен base, а она уже в versions/
    else:
        return Synced("merge", draft, site, status, conflict)
    if kind != "same":
        storage.write_draft(fic_id, chapter_id, last.content)
    meta = storage.load_chapter(fic_id, chapter_id, title)
    storage.write_chapter(fic_id, chapter_id, replace(meta, base=site.file))
    return Synced(kind, last.content, site if kind == "ff" else None, status, conflict)


@dataclass(frozen=True)
class Published:
    """Отправка остановлена, если first.kind == "merge", есть error или unchanged."""

    first: Synced  # синхронизация перед отправкой
    error: MarkupError | None = None
    # синхронизация после отправки; None при отправке - история не перечиталась
    after: Synced | None = None
    rev: Version | None = None  # новая base; None - сайт правку не создал
    unchanged: bool = False  # текст и название как на сайте: отправлять нечего


async def publish_chapter(
    api: FicbookAPI, storage: ChapterStorage, fic_id: str, chapter_id: str, title: str
) -> Published:
    """Отправка F5; черновик и название уже на диске."""
    # shortcut: только save_chapter(), остальное - с DiffScreen и "Новой главой"
    first = await sync_chapter(api, storage, fic_id, chapter_id, title, False)
    if first.kind == "merge":
        return Published(first)
    meta = storage.read_chapter(fic_id, chapter_id)
    assert meta.base is not None
    base = storage.read_version(fic_id, chapter_id, meta.base)
    # после синхронизации base - последняя правка сайта
    if first.text == base and meta.title == meta.site_title:
        return Published(first, unchanged=True)
    error = check(first.text)
    if error is not None:
        return Published(first, error)
    storage.add_version(fic_id, chapter_id, first.text, meta.title, "publish")
    await api.save_chapter(fic_id, chapter_id, first.text, meta.title, base)
    try:
        after = await sync_chapter(api, storage, fic_id, chapter_id, title, False)
    # глава уже на сайте: base догонит следующая синхронизация
    except FicbookAPIError, OSError, ValueError:
        log.warning(
            "глава %s/%s: история после отправки", fic_id, chapter_id, exc_info=True
        )
        return Published(first)
    new = storage.read_chapter(fic_id, chapter_id)
    rev = next((v for v in new.versions if v.file == new.base), None)
    return Published(first, None, after, None if new.base == meta.base else rev)
