"""Синхронизация главы с сайтом (шаги 1-4 п. 5 дизайна) и отправка на сайт."""

import logging
from dataclasses import dataclass, replace
from typing import Literal

from .api.client import FicbookAPI
from .api.errors import FicbookAPIError
from .api.models import Revision, RevisionInfo
from .markup import MarkupError, check
from .merge import Choice, Hunk, Manual, merge3, render
from .storage import ChapterMeta, ChapterStorage, LastMerge, MergeChoice, Version

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
        newer = tuple(
            r
            for r in revs
            if base is None or base.site_num is None or r.num > base.site_num
        )
        return Synced("merge", draft, site, status, conflict, newer)
    if kind != "same":
        storage.write_draft(fic_id, chapter_id, last.content)
    meta = storage.load_chapter(fic_id, chapter_id, title)
    storage.write_chapter(fic_id, chapter_id, replace(meta, base=site.file))
    return Synced(kind, last.content, site if kind == "ff" else None, status, conflict)


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


def start_merge(
    storage: ChapterStorage, fic_id: str, chapter_id: str, title: str, site: Version
) -> tuple[LastMerge, Parts, dict[int, Choice]]:
    """Шаг 4.1-4.2: снимок черновика и merge3; на длинной главе долго - звать в потоке.

    Решения - отложенные по Esc. OSError и ValueError - от диска.
    """
    meta = storage.load_chapter(fic_id, chapter_id, title)
    draft = storage.read_draft(fic_id, chapter_id) or ""
    last = meta.versions[-1] if meta.versions else None
    # слияние, закрытое без "Готово", начинается заново без второго снимка
    if last and storage.read_version(fic_id, chapter_id, last.file) == draft:
        mine = last
    else:
        mine = storage.add_version(fic_id, chapter_id, draft, meta.title, "manual")
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
    # shortcut: без revert_revision(), он придёт с DiffScreen
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
        storage.write_chapter(fic_id, chapter_id, replace(meta, base=sent.file))
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
