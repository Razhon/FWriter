"""Синхронизация главы с сайтом при открытии, шаги 1-4 п. 5 дизайна."""

from dataclasses import dataclass, replace
from typing import Literal

from .api.client import FicbookAPI
from .api.models import Revision, RevisionInfo
from .storage import ChapterStorage, Version

type SyncKind = Literal["same", "new", "ff", "merge"]


@dataclass(frozen=True)
class Synced:
    """new - первая копия, ff - черновик догнал сайт, merge - нужно слияние."""

    kind: SyncKind
    text: str  # черновик после синхронизации
    site: Version | None = None  # новая ревизия сайта у ff и merge
    status: int | None = None  # param главы на сайте; None у readonly и не у автора


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
    status = None
    if not readonly:
        # каждый раз: статус меняют и на сайте
        chapter = await api.get_chapter(fic_id, chapter_id)
        status = chapter.status
        if meta.history_url is None:
            meta.history_url = chapter.history_url
            if not meta.versions:
                meta.title = chapter.title
    elif meta.history_url is None:
        # страницы главы нет (403), название - из fic.json
        meta.history_url = api.participated_history_url(fic_id, chapter_id)
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
        return Synced("same", last.content if draft is None else draft, status=status)

    site = known or storage.add_version(
        fic_id,
        chapter_id,
        last.content,
        meta.title,
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
        return Synced("merge", draft, site, status)
    if kind != "same":
        storage.write_draft(fic_id, chapter_id, last.content)
    meta = storage.load_chapter(fic_id, chapter_id, title)
    storage.write_chapter(fic_id, chapter_id, replace(meta, base=site.file))
    return Synced(kind, last.content, site if kind == "ff" else None, status)
