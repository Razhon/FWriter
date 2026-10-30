"""Синхронизация главы с сайтом (п. 5 дизайна), докачка правок и отправка на сайт."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from .api.client import FicbookAPI
from .api.errors import FicbookAPIError
from .api.models import Revision, RevisionInfo
from .markup import MarkupError, check
from .storage import ChapterMeta, ChapterStorage, Version

log = logging.getLogger(__name__)

type SyncKind = Literal["same", "new", "ff", "merge"]

EDGE = " \t\r\n "  # что сайт срезает по краям текста


@dataclass(frozen=True)
class Synced:
    """new - первая копия, ff - черновик догнал сайт, merge - нужно слияние."""

    kind: SyncKind
    text: str  # черновик после синхронизации
    site: Version | None = None  # новая ревизия сайта у ff и merge
    status: int | None = None  # param главы на сайте; None у readonly и не у автора
    title_conflict: str | None = None  # название на сайте, если поменяли оба
    revs: tuple[RevisionInfo, ...] = ()  # у merge: правки сайта новее base
    history: tuple[RevisionInfo, ...] = ()  # весь список правок, для докачки


def _same(
    storage: ChapterStorage, fic_id: str, chapter_id: str, v: Version, rev: Revision
) -> bool:
    # без точной даты (изначальная версия) ключ - текст
    if v.author != rev.author:
        return False
    if rev.created is None:
        return storage.read_version(fic_id, chapter_id, v.file) == rev.content
    return v.created == rev.created


def _unchanged(
    storage: ChapterStorage,
    fic_id: str,
    chapter_id: str,
    title: str,
    last: RevisionInfo,
) -> bool:
    """base - последняя правка сайта по ключу из списка, и черновик есть."""
    # shortcut: правка того же автора в ту же минуту с тем же номером после удаления
    # старых не видна до следующей; F5 её не затрёт - save_chapter сверяет base с сайтом
    meta = storage.load_chapter(fic_id, chapter_id, title)
    base = next((v for v in meta.versions if v.file == meta.base), None)
    return (
        base is not None
        and base.site_num == last.num
        and base.author == last.author
        and (base.created or "")[:16] == last.created
        and storage.read_draft(fic_id, chapter_id) is not None
    )


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
    history_url = storage.load_chapter(fic_id, chapter_id, title).history_url
    status, site_title = None, title  # при readonly страницы главы нет (403)
    if not readonly:
        # каждый раз: статус и название меняют и на сайте
        chapter = await api.get_chapter(fic_id, chapter_id)
        status, site_title = chapter.status, chapter.title
        # со страницы каждый раз: с ролью меняется mine/participated
        history_url = chapter.history_url
    else:
        history_url = history_url or api.participated_history_url(fic_id, chapter_id)
    revs = await api.get_chapter_revisions(history_url)
    # полный текст главы качаем, только если сайт мог поменяться
    last = (
        None
        if _unchanged(storage, fic_id, chapter_id, title, revs[0])
        else await api.get_revision(revs[0].url)
    )

    # после сети: между чтением и записью нет await, докачка не затрётся
    meta = storage.load_chapter(fic_id, chapter_id, title)
    meta.history_url = history_url
    meta.title, conflict = _merge_title(meta, site_title)
    meta.site_title = site_title

    if last is None:
        known = next(v for v in meta.versions if v.file == meta.base)
    else:
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
        if draft is None:
            assert last is not None  # без черновика быстрого пути нет
            draft = last.content
        return Synced(
            "same", draft, status=status, title_conflict=conflict, history=tuple(revs)
        )
    assert last is not None  # быстрый путь вышел выше: known там - base

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
        newer = tuple(
            r
            for r in revs
            if base is None or base.site_num is None or r.num > base.site_num
        )
        return Synced("merge", draft, site, status, conflict, newer, tuple(revs))
    if kind != "same":
        storage.write_draft(fic_id, chapter_id, last.content)
    meta = storage.load_chapter(fic_id, chapter_id, title)
    storage.write_chapter(fic_id, chapter_id, replace(meta, base=site.file))
    ff = site if kind == "ff" else None
    return Synced(kind, last.content, ff, status, conflict, history=tuple(revs))


FETCH_LIMIT = 30  # правок за одну синхронизацию: каждая - полный текст главы
FETCH_PAUSE = 1.0  # секунд между страницами: запас лимита для действий человека


async def fetch_revisions(
    api: FicbookAPI,
    storage: ChapterStorage,
    fic_id: str,
    chapter_id: str,
    title: str,
    revs: tuple[RevisionInfo, ...],
    saved: Callable[[Version, int], None] | None = None,
    pause: float = FETCH_PAUSE,
) -> int:
    """Докачка правок без локальной версии; вернёт число скачанных.

    Каждая правка пишется сразу: прерванная докачка ничего не теряет.
    saved получает каждую новую версию и её место в versions.
    """
    # пауза оставляет запас лимита, только пока качает одна вкладка
    async with api.fetching:
        known = {
            v.site_rev for v in storage.load_chapter(fic_id, chapter_id, title).versions
        }
        todo = [r for r in revs if r.rev_id and r.rev_id not in known][:FETCH_LIMIT]
        # новейшие FETCH_LIMIT, но от старых к новым: место версии ищется по site_num,
        # а у бывшей последней правки он верный, только когда её очередь прошла
        for i, info in enumerate(reversed(todo)):
            if i:
                await asyncio.sleep(pause)
            rev = await api.get_revision(info.url)
            meta = storage.load_chapter(fic_id, chapter_id, title)
            # бывшая последняя правка: скачана без id, пока он не был виден
            old = next(
                (
                    v
                    for v in meta.versions
                    if v.source != "manual"
                    and v.site_rev is None
                    and _same(storage, fic_id, chapter_id, v, rev)
                ),
                None,
            )
            if old is not None:
                meta.versions = [
                    # номер мог сдвинуться, если сайт удалил старые правки
                    replace(v, site_rev=info.rev_id, site_num=info.num)
                    if v.file == old.file
                    else v
                    for v in meta.versions
                ]
                storage.write_chapter(fic_id, chapter_id, meta)
                continue
            # перед первой правкой сайта новее этой: пропущенная между синхронизациями
            # бывает новее записанных; в конец не встанет, versions[-1] не меняется
            at = next(
                (
                    i
                    for i, v in enumerate(meta.versions)
                    if v.site_num is not None and v.site_num > info.num
                ),
                0,
            )
            version = storage.add_version(
                fic_id,
                chapter_id,
                rev.content,
                meta.site_title or title,
                "site",
                rev.comment,
                created=rev.created or info.created,
                site_rev=info.rev_id,
                site_num=info.num,
                author=rev.author,
                author_name=rev.author_name,
                at=at,
            )
            if saved is not None:
                saved(version, at)
        return len(todo)


@dataclass(frozen=True)
class Published:
    """Отправка остановлена, если first.kind == "merge", есть error или unchanged."""

    first: Synced  # синхронизация перед отправкой
    error: MarkupError | None = None
    # синхронизация после отправки; None при отправке - история не перечиталась
    after: Synced | None = None
    rev: Version | None = None  # новая base; None - сайт правку не создал
    unchanged: bool = False  # текст и название как на сайте: отправлять нечего
    created: str | None = None  # id главы, созданной на сайте
    # глава создана, но текст остался в старой папке: повтор создаст дубль
    stranded: bool = False
    reverted: int | None = None  # номер правки, к которой откатили


async def publish_chapter(
    api: FicbookAPI,
    storage: ChapterStorage,
    fic_id: str,
    chapter_id: str,
    title: str,
    create: bool = False,
) -> Published:
    """Отправка F5; черновик и название уже на диске.

    create - главы нет на сайте (new-N или удалена там): создать её заново.
    """
    # сайт срезает их сам, и черновик потом не совпал бы с сайтом
    draft = storage.read_draft(fic_id, chapter_id)
    if draft is not None and draft != draft.strip(EDGE):
        storage.write_draft(fic_id, chapter_id, draft.strip(EDGE))
    if create:
        return await _create_chapter(api, storage, fic_id, chapter_id)
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
    reverted = await _revert(api, storage, fic_id, chapter_id, meta, first.text)
    if reverted is None:
        await api.save_chapter(fic_id, chapter_id, first.text, meta.title, base)
    meta = storage.read_chapter(fic_id, chapter_id)
    storage.write_chapter(fic_id, chapter_id, replace(meta, restored=None))
    try:
        after = await sync_chapter(api, storage, fic_id, chapter_id, title, False)
    # глава уже на сайте: base догонит следующая синхронизация
    except FicbookAPIError, OSError, ValueError:
        log.warning(
            "глава %s/%s: история после отправки", fic_id, chapter_id, exc_info=True
        )
        return Published(first, reverted=reverted)
    new = storage.read_chapter(fic_id, chapter_id)
    rev = next((v for v in new.versions if v.file == new.base), None)
    rev = None if new.base == meta.base else rev
    return Published(first, None, after, rev, reverted=reverted)


async def _revert(
    api: FicbookAPI,
    storage: ChapterStorage,
    fic_id: str,
    chapter_id: str,
    meta: ChapterMeta,
    text: str,
) -> int | None:
    """6.5: откат к restored, если черновик и название не правили; номер правки."""
    v = next((v for v in meta.versions if v.file == meta.restored), None)
    if v is None or meta.title != meta.site_title or meta.history_url is None:
        return None
    if storage.read_version(fic_id, chapter_id, v.file) != text:
        return None
    revs = await api.get_chapter_revisions(meta.history_url)
    base = next((b for b in meta.versions if b.file == meta.base), None)
    # главу правили после синхронизации: откат стёр бы правку, save_chapter её заметит
    if base is None or revs[0].num != base.site_num:
        return None
    for r in revs:
        if v.site_rev is not None:
            match = r.rev_id == v.site_rev
        else:
            # id ещё не докачан: ключ до минуты, текст сверит revert_revision
            match = r.author == v.author and (v.created or "")[:16] == r.created
        if match and r.rev_id is not None and await api.revert_revision(r.url, text):
            return r.num
    return None


async def _create_chapter(
    api: FicbookAPI, storage: ChapterStorage, fic_id: str, chapter_id: str
) -> Published:
    text = storage.read_draft(fic_id, chapter_id) or ""
    first = Synced("same", text)
    error = check(text)
    if error is not None:
        return Published(first, error)
    title = storage.read_chapter(fic_id, chapter_id).title
    sent = storage.add_version(fic_id, chapter_id, text, title, "publish")
    # base и история - от удалённой главы
    meta = replace(
        storage.read_chapter(fic_id, chapter_id),
        base=None,
        restored=None,
        history_url=None,
        site_title=None,
    )
    storage.write_chapter(fic_id, chapter_id, meta)
    new_id = await api.create_chapter(fic_id, meta.title, text)
    try:
        # base - отправленный текст, чтобы было с чем сливать, если история не прочитается;
        # до создания вкладка показала бы "как на сайте"
        # перечитать: пока шёл create_chapter, chapter.json мог дописать другой
        chapter = storage.read_chapter(fic_id, chapter_id)
        storage.write_chapter(fic_id, chapter_id, replace(chapter, base=sent.file))
        storage.rename_chapter(fic_id, chapter_id, new_id, meta.title)
    except OSError, ValueError:
        log.warning(
            "глава %s/%s: создана как %s, но не переименована",
            fic_id,
            chapter_id,
            new_id,
            exc_info=True,
        )
        return Published(first, created=new_id, stranded=True)
    try:
        after = await sync_chapter(api, storage, fic_id, new_id, meta.title, False)
    except FicbookAPIError, OSError, ValueError:
        log.warning(
            "глава %s/%s: история после создания", fic_id, new_id, exc_info=True
        )
        return Published(first, created=new_id)
    new = storage.read_chapter(fic_id, new_id)
    rev = next((v for v in new.versions if v.file == new.base), None)
    return Published(first, None, after, rev, created=new_id)
