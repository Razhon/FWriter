import asyncio
from dataclasses import replace

import pytest

from fwriter.core.api.client import FicbookAPI
from fwriter.core.api.errors import NetworkError
from fwriter.core.api.models import (
    Chapter,
    Fic,
    Revision,
    RevisionInfo,
    SlimChapterInfo,
)
from fwriter.core.merge import Hunk, Manual
from fwriter.core.storage import ChapterMeta, ChapterStorage
from fwriter.core.sync import (
    fetch_revisions,
    finish_merge,
    load_merge,
    postpone_merge,
    publish_chapter,
    restore_version,
    start_merge,
    sync_chapter,
)

FIC, CH = "1001", "2001"
MINE = "/home/versions/mine/1001/parts/2001/history"


def rev(text: str, created: str | None = "2026-10-26T00:33:30") -> Revision:
    return Revision(created, "3001", "user-1", "пояснение", text, can_revert=False)


def info(num: int, rev_id: str | None = None) -> RevisionInfo:
    url = f"/home/versions/mine/2001/history/{rev_id or 'last'}"
    return RevisionInfo(num, url, rev_id, "3001", "user-1", "", "2026-10-26T00:33")


class FakeAPI(FicbookAPI):
    """Без HTTP: страницы уже разобраны, разбор проверяет test_parser."""

    def __init__(self, last: Revision, revs: list[RevisionInfo] | None = None) -> None:
        super().__init__({})
        self.last = last
        self.revs = revs or [info(1)]
        self.title = "Глава"
        self.calls: list[str] = []

    async def get_chapter(self, fic_id: str, chapter_id: str) -> Chapter:
        self.calls.append("chapter")
        return Chapter(self.title, self.last.content, True, 1, MINE)

    async def get_chapter_revisions(self, history_url: str) -> list[RevisionInfo]:
        self.calls.append(history_url)
        return self.revs

    async def get_revision(self, url: str) -> Revision:
        self.calls.append(url)
        return self.last


def sync(api: FakeAPI, storage: ChapterStorage, readonly: bool = False):
    return asyncio.run(sync_chapter(api, storage, FIC, CH, "Из fic.json", readonly))


def test_first_open_downloads_and_sets_base(tmp_path):
    storage = ChapterStorage(tmp_path)
    api = FakeAPI(rev("Текст."))
    synced = sync(api, storage)

    assert (synced.kind, synced.text) == ("new", "Текст.")
    assert storage.read_draft(FIC, CH) == "Текст."
    meta = storage.read_chapter(FIC, CH)
    assert (meta.title, meta.history_url) == ("Глава", MINE)
    [v] = meta.versions
    assert meta.base == v.file
    assert (v.source, v.author, v.site_num, v.site_rev) == ("site", "3001", 1, None)

    # второй раз: адрес истории с диска, новых версий нет, статус - снова с сайта
    api.calls.clear()
    again = sync(api, storage)
    assert (again.kind, again.status) == ("same", 1)
    assert api.calls[1] == MINE
    assert len(storage.read_chapter(FIC, CH).versions) == 1


def test_fast_forward(tmp_path):
    storage = ChapterStorage(tmp_path)
    sync(FakeAPI(rev("Старое.")), storage)
    api = FakeAPI(rev("Новое.", "2026-10-27T10:00:00"), [info(2), info(1, "9001")])
    synced = sync(api, storage)

    assert (synced.kind, synced.text) == ("ff", "Новое.")
    assert synced.site is not None and synced.site.site_num == 2
    assert storage.read_draft(FIC, CH) == "Новое."
    meta = storage.read_chapter(FIC, CH)
    assert meta.base == synced.site.file
    # прежняя base осталась версией
    assert [storage.read_version(FIC, CH, v.file) for v in meta.versions] == [
        "Старое.",
        "Новое.",
    ]


def test_edited_draft_needs_merge(tmp_path):
    storage = ChapterStorage(tmp_path)
    sync(FakeAPI(rev("Старое.")), storage)
    base = storage.read_chapter(FIC, CH).base
    storage.write_draft(FIC, CH, "Моё.")
    api = FakeAPI(rev("Новое.", "2026-10-27T10:00:00"), [info(2), info(1, "9001")])

    synced = sync(api, storage)
    assert (synced.kind, synced.text) == ("merge", "Моё.")
    assert storage.read_draft(FIC, CH) == "Моё."
    assert storage.read_chapter(FIC, CH).base == base
    # повторное открытие не плодит копии той же ревизии сайта
    sync(api, storage)
    assert len(storage.read_chapter(FIC, CH).versions) == 2


def test_draft_equal_to_site_moves_base(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_draft(FIC, CH, "Текст.")  # скачан до синхронизации, без base
    synced = sync(FakeAPI(rev("Текст.")), storage)
    assert synced.kind == "same"
    assert storage.read_chapter(FIC, CH).base is not None


def test_sync_keeps_version_written_during_request(tmp_path):
    storage = ChapterStorage(tmp_path)
    sync(FakeAPI(rev("Старое.")), storage)

    class FetchingAPI(FakeAPI):
        async def get_revision(self, url: str) -> Revision:
            # докачка успела записать правку, пока синхронизация ждала сайт
            storage.add_version(FIC, CH, "Докачано.", "Глава", "site", site_rev="9001")
            return await super().get_revision(url)

    sync(FetchingAPI(rev("Старое.")), storage)
    assert "9001" in [v.site_rev for v in storage.read_chapter(FIC, CH).versions]


def test_readonly_uses_participated_history(tmp_path):
    storage = ChapterStorage(tmp_path)
    api = FakeAPI(rev("Текст."))
    sync(api, storage, readonly=True)

    assert "chapter" not in api.calls
    assert api.calls[0] == FicbookAPI.participated_history_url(FIC, CH)
    assert storage.read_chapter(FIC, CH).title == "Из fic.json"


def test_initial_revision_matched_by_text(tmp_path):
    storage = ChapterStorage(tmp_path)
    api = FakeAPI(rev("Текст.", created=None))
    sync(api, storage)
    assert storage.read_chapter(FIC, CH).versions[0].created == "2026-10-26T00:33"
    assert sync(api, storage).kind == "same"

    # тот же автор, другой текст - уже другая ревизия
    api.last = replace(api.last, content="Другое.")
    assert sync(api, storage).kind == "ff"


def test_site_nums_follow_site(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.add_version(FIC, CH, "x", "Глава", "site", site_rev="9001", site_num=5)
    api = FakeAPI(rev("Текст."), [info(2), info(1, "9001")])
    sync(api, storage)
    nums = [v.site_num for v in storage.read_chapter(FIC, CH).versions]
    assert nums == [1, 2]


class PagesAPI(FakeAPI):
    """Страницы правок по адресу; адреса из fail роняют сеть."""

    def __init__(self, pages: dict[str, Revision], fail: str = "") -> None:
        super().__init__(rev(""))
        self.pages, self.fail = pages, fail

    async def get_revision(self, url: str) -> Revision:
        self.calls.append(url)
        if url == self.fail:
            raise NetworkError
        return self.pages[url]


def test_fetch_revisions(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.add_version(
        FIC, CH, "Первая.", "Глава", "site", site_rev="9001", site_num=1
    )
    # бывшая последняя правка: скачана без id, номер 5 устарел - сайт удалил две
    storage.add_version(
        FIC,
        CH,
        "Третья.",
        "Глава",
        "site",
        created="2026-10-26T00:33:30",
        author="3001",
        site_num=5,
    )
    revs = (info(4), info(3, "9003"), info(2, "9002"), info(1, "9001"))
    pages = {
        revs[1].url: rev("Третья."),
        revs[2].url: rev("Вторая.", "2026-10-25T10:00:00"),
    }
    api = PagesAPI(pages, fail=revs[1].url)
    saved = []
    with pytest.raises(NetworkError):
        asyncio.run(
            fetch_revisions(
                api, storage, FIC, CH, "Глава", revs, lambda *a: saved.append(a), 0
            )
        )
    # от старых к новым, успевшее записаться остаётся
    assert api.calls == [revs[2].url, revs[1].url]
    versions = storage.read_chapter(FIC, CH).versions
    assert [v.site_rev for v in versions] == ["9001", "9002", None]
    new = versions[1]
    assert saved == [(new, 1)]
    assert (new.site_num, new.source) == (2, "site")
    assert storage.read_version(FIC, CH, new.file) == "Вторая."

    api = PagesAPI(pages)
    n = asyncio.run(fetch_revisions(api, storage, FIC, CH, "Глава", revs, pause=0))
    # известная правка не скачивается, бывшая последняя получает id и номер
    assert n == 1 and api.calls == [revs[1].url]
    versions = storage.read_chapter(FIC, CH).versions
    assert [(v.site_rev, v.site_num) for v in versions] == [
        ("9001", 1),
        ("9002", 2),
        ("9003", 3),
    ]


def test_fetch_revisions_between_known(tmp_path):
    storage = ChapterStorage(tmp_path)
    # синхронизации записали правки 5 и 7, а 6 вышла между ними
    for num in (5, 7):
        storage.add_version(
            FIC, CH, f"{num}.", "Глава", "site", site_rev=f"900{num}", site_num=num
        )
    storage.add_version(FIC, CH, "Своё.", "Глава", "manual")
    revs = (info(7, "9007"), info(6, "9006"), info(5, "9005"))
    api = PagesAPI({revs[1].url: rev("6.", "2026-10-25T10:00:00")})
    asyncio.run(fetch_revisions(api, storage, FIC, CH, "Глава", revs, pause=0))
    versions = storage.read_chapter(FIC, CH).versions
    assert [v.site_num for v in versions] == [5, 6, 7, None]


def test_fetch_revisions_one_tab_at_a_time(tmp_path):
    storage = ChapterStorage(tmp_path)
    chapters = {
        "2001": (info(3), info(2, "9002"), info(1, "9001")),
        "2002": (info(3), info(2, "9102"), info(1, "9101")),
    }
    pages = {r.url: rev(r.rev_id or "") for revs in chapters.values() for r in revs}
    api = PagesAPI(pages)

    async def both() -> None:
        await asyncio.gather(
            *(
                fetch_revisions(api, storage, FIC, ch, "Глава", revs, pause=0)
                for ch, revs in chapters.items()
            )
        )

    asyncio.run(both())
    # пауза одной докачки не пускает вперёд другую: лимит сайта общий
    assert [url.rsplit("/", 1)[1] for url in api.calls] == [
        "9001",
        "9002",
        "9101",
        "9102",
    ]


class SavingAPI(FakeAPI):
    """save_chapter создаёт на сайте новую последнюю правку."""

    def __init__(self, last: Revision) -> None:
        super().__init__(last)
        self.sent: list[tuple[str, str, str]] = []

    async def save_chapter(self, fic_id, chapter_id, content, title, base) -> None:
        self.sent.append((content, title, base))
        if content != self.last.content:
            self.last = rev(content, "2026-10-28T12:00:00")
            self.revs = [info(len(self.revs) + 1), *self.revs]


def publish(api: FakeAPI, storage: ChapterStorage):
    return asyncio.run(publish_chapter(api, storage, FIC, CH, "Глава"))


def test_publish_sends_and_moves_base(tmp_path):
    storage = ChapterStorage(tmp_path)
    api = SavingAPI(rev("Старое."))
    sync(api, storage)
    storage.write_draft(FIC, CH, "Новое.")

    published = publish(api, storage)
    assert api.sent == [("Новое.", "Глава", "Старое.")]
    assert published.rev is not None and published.rev.site_num == 2
    meta = storage.read_chapter(FIC, CH)
    assert meta.base == published.rev.file
    assert [v.source for v in meta.versions] == ["site", "publish", "site"]
    assert storage.read_draft(FIC, CH) == "Новое."


def test_publish_trims_draft_edges_like_site(tmp_path):
    storage = ChapterStorage(tmp_path)
    api = SavingAPI(rev("Старое."))
    sync(api, storage)
    # сайт срезает края сам; U+00A0 внутри остаётся
    storage.write_draft(FIC, CH, " \t\r\nНовое - да.  \n\n")

    publish(api, storage)
    assert api.sent == [("Новое - да.", "Глава", "Старое.")]
    assert storage.read_draft(FIC, CH) == "Новое - да."
    assert sync(api, storage).kind == "same"


class RevertingAPI(SavingAPI):
    """Откат к правке 9001 с текстом "Старое."."""

    def __init__(self, last: Revision) -> None:
        super().__init__(last)
        self.reverted: list[str] = []

    async def revert_revision(self, url: str, expected: str) -> bool:
        if expected != "Старое.":
            return False
        self.reverted.append(url)
        self.last = replace(rev(expected, "2026-10-28T13:00:00"), comment="возврат")
        self.revs = [info(len(self.revs) + 1), *self.revs]
        return True


class RacedAPI(RevertingAPI):
    """Соавтор правит главу между синхронизацией и откатом."""

    lists = 0

    async def get_chapter_revisions(self, history_url: str) -> list[RevisionInfo]:
        self.lists += 1
        if self.lists == 2:  # второй список при F5 - в _revert
            self.revs = [info(len(self.revs) + 1), *self.revs]
        return await super().get_chapter_revisions(history_url)


def restore_old(api: FakeAPI, storage: ChapterStorage) -> None:
    """На сайте "Старое." (9001), потом "Новое."; в черновик восстановлено старое."""
    sync(api, storage)
    api.last = rev("Новое.", "2026-10-27T10:00:00")
    api.revs = [info(2), info(1, "9001")]
    sync(api, storage)
    old = storage.read_chapter(FIC, CH).versions[0]
    restore_version(storage, FIC, CH, "Глава", old)
    assert storage.read_chapter(FIC, CH).restored == old.file


@pytest.mark.parametrize(
    ("draft", "title", "reverted"),
    [
        ("Старое.", "Глава", True),  # восстановили и не правили
        ("Старое!", "Глава", False),  # правили после восстановления
        ("Старое.", "Новое", False),  # откат не меняет название
    ],
)
def test_publish_reverts_restored(tmp_path, draft: str, title: str, reverted: bool):
    storage = ChapterStorage(tmp_path)
    api = RevertingAPI(rev("Старое."))
    restore_old(api, storage)
    storage.write_draft(FIC, CH, draft)
    storage.set_title(FIC, CH, title)

    published = publish(api, storage)
    if reverted:
        assert api.reverted == ["/home/versions/mine/2001/history/9001"]
        assert api.sent == [] and published.reverted == 1
        assert published.rev is not None and published.rev.site_num == 3
    else:
        assert api.reverted == [] and len(api.sent) == 1
        assert published.reverted is None
    assert storage.read_chapter(FIC, CH).restored is None


def test_publish_skips_revert_after_site_edit(tmp_path):
    # правка соавтора после синхронизации: отката нет, save_chapter сверит base
    storage = ChapterStorage(tmp_path)
    api = RacedAPI(rev("Старое."))
    restore_old(api, storage)
    api.lists = 0
    publish(api, storage)
    assert api.reverted == [] and len(api.sent) == 1


class HistoryDownAPI(SavingAPI):
    """После отправки история не отвечает."""

    async def get_chapter_revisions(self, history_url: str) -> list[RevisionInfo]:
        if self.sent:
            raise NetworkError()
        return await super().get_chapter_revisions(history_url)


def test_publish_survives_history_failure(tmp_path):
    storage = ChapterStorage(tmp_path)
    api = HistoryDownAPI(rev("Старое."))
    sync(api, storage)
    storage.write_draft(FIC, CH, "Новое.")

    published = publish(api, storage)
    assert len(api.sent) == 1
    assert (published.first.kind, published.error, published.after) == (
        "same",
        None,
        None,
    )
    assert storage.read_draft(FIC, CH) == "Новое."


def test_publish_title_only_keeps_base(tmp_path):
    storage = ChapterStorage(tmp_path)
    api = SavingAPI(rev("Текст."))
    sync(api, storage)
    meta = storage.read_chapter(FIC, CH)
    storage.write_chapter(FIC, CH, replace(meta, title="Новое"))
    published = publish(api, storage)
    assert api.sent == [("Текст.", "Новое", "Текст.")] and published.rev is None
    assert storage.read_chapter(FIC, CH).base == meta.base


def test_publish_skips_unchanged_chapter(tmp_path):
    storage = ChapterStorage(tmp_path)
    # разметка сайта не проверяется: отправлять всё равно нечего
    api = SavingAPI(rev("<b>Текст."))
    sync(api, storage)
    versions = storage.read_chapter(FIC, CH).versions
    published = publish(api, storage)
    assert api.sent == []
    assert (published.unchanged, published.error) == (True, None)
    assert storage.read_chapter(FIC, CH).versions == versions


@pytest.mark.parametrize(
    ("draft", "site"), [("Моё.", "Новое."), ("<b>Моё.", "Старое.")]
)
def test_publish_stops_on_merge_or_markup(tmp_path, draft: str, site: str):
    storage = ChapterStorage(tmp_path)
    api = SavingAPI(rev("Старое."))
    sync(api, storage)
    storage.write_draft(FIC, CH, draft)
    if site != "Старое.":
        api.last = rev(site, "2026-10-27T10:00:00")
        api.revs = [info(2), info(1, "9001")]

    published = publish(api, storage)
    assert api.sent == []
    if site == "Старое.":
        assert published.error is not None and published.error.line == 0
    else:
        assert published.first.kind == "merge"
    assert not any(
        v.source == "publish" for v in storage.read_chapter(FIC, CH).versions
    )


@pytest.mark.parametrize(
    ("local", "site", "title", "conflict"),
    [
        ("Глава", "С сайта", "С сайта", None),  # переименовали только на сайте
        ("Моё", "Глава", "Моё", None),  # только у меня
        ("Моё", "С сайта", "Моё", "С сайта"),  # оба, по-разному
        ("Одно", "Одно", "Одно", None),  # оба одинаково
    ],
)
def test_title_merges_like_text(tmp_path, local, site, title, conflict):
    storage = ChapterStorage(tmp_path)
    api = FakeAPI(rev("Текст."))
    sync(api, storage)
    storage.set_title(FIC, CH, local)
    assert storage.shown_title(FIC, CH) == (local, local != "Глава")
    api.title = site

    synced = sync(api, storage)
    assert synced.title_conflict == conflict
    meta = storage.read_chapter(FIC, CH)
    assert (meta.title, meta.site_title) == (title, site)
    assert storage.shown_title(FIC, CH) == (title, title != site)


def test_site_version_keeps_site_title(tmp_path):
    storage = ChapterStorage(tmp_path)
    sync(FakeAPI(rev("Старое.")), storage)
    storage.set_title(FIC, CH, "Моё")
    storage.write_draft(FIC, CH, "Моё.")
    sync(
        FakeAPI(rev("Новое.", "2026-10-27T10:00:00"), [info(2), info(1, "9001")]),
        storage,
    )
    assert [v.title for v in storage.read_chapter(FIC, CH).versions] == [
        "Глава",
        "Глава",
    ]


NEW_MINE = "/home/versions/mine/1001/parts/2002/history"


class CreatingAPI(FakeAPI):
    """create_chapter даёт главе id 2002; дальше она видна как обычная."""

    def __init__(self) -> None:
        super().__init__(rev(""))
        self.created: list[tuple[str, str]] = []

    async def create_chapter(self, fic_id: str, title: str, content: str) -> str:
        self.created.append((title, content))
        self.last = rev(content, "2026-10-29T12:00:00")
        return "2002"

    async def get_chapter(self, fic_id: str, chapter_id: str) -> Chapter:
        self.calls.append("chapter")
        return Chapter(self.title, self.last.content, True, 1, NEW_MINE)


def test_create_chapter_deleted_on_site(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic(FIC, "Работа", [], False), "author")
    # глава, удалённая на сайте: base и история от прежней главы
    sync(FakeAPI(rev("Старое.")), storage)
    storage.write_draft(FIC, CH, "Новое.")
    api = CreatingAPI()

    published = asyncio.run(publish_chapter(api, storage, FIC, CH, "Глава", True))
    assert api.created == [("Глава", "Новое.")]
    assert published.created == "2002" and not published.stranded
    assert not (tmp_path / "fics" / FIC / CH).exists()
    assert storage.read_fic(FIC)[0].chapters == [SlimChapterInfo("2002", "Глава")]
    meta = storage.read_chapter(FIC, "2002")
    # история - новой главы, а не удалённой
    assert meta.history_url == NEW_MINE and api.calls[1] == NEW_MINE
    assert published.rev is not None and meta.base == published.rev.file
    assert storage.read_version(FIC, "2002", published.rev.file) == "Новое."


def test_create_chapter_keeps_draft_when_rename_fails(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic(FIC, "Работа", [], False), "author")
    storage.write_chapter(FIC, "new-1", ChapterMeta("Глава"))
    storage.write_draft(FIC, "new-1", "Новое.")
    # папка с id новой главы уже есть: rename_chapter откажет
    (tmp_path / "fics" / FIC / "2002").mkdir()
    api = CreatingAPI()

    published = asyncio.run(publish_chapter(api, storage, FIC, "new-1", "Глава", True))
    assert api.created == [("Глава", "Новое.")]
    assert published.created == "2002" and published.stranded
    assert storage.read_draft(FIC, "new-1") == "Новое."


def test_create_chapter_stops_on_markup(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_chapter(FIC, "new-1", ChapterMeta("Глава"))
    storage.write_draft(FIC, "new-1", "<b>")
    api = CreatingAPI()
    published = asyncio.run(publish_chapter(api, storage, FIC, "new-1", "Глава", True))
    assert published.error is not None and api.created == []


class CreatedHistoryDownAPI(CreatingAPI):
    async def get_chapter_revisions(self, history_url: str) -> list[RevisionInfo]:
        raise NetworkError()


def test_created_chapter_without_history_can_merge(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic(FIC, "Работа", [], False), "author")
    storage.write_chapter(FIC, "new-1", ChapterMeta("Глава"))
    storage.write_draft(FIC, "new-1", "Было.\n\nОбщее.")
    api = CreatedHistoryDownAPI()
    published = asyncio.run(publish_chapter(api, storage, FIC, "new-1", "Глава", True))
    assert published.created == "2002" and published.after is None
    # base - отправленный текст, хотя история не прочиталась
    meta = storage.read_chapter(FIC, "2002")
    assert meta.base is not None
    assert storage.read_version(FIC, "2002", meta.base) == "Было.\n\nОбщее."

    # соавтор правит на сайте, я - в черновике
    storage.write_draft(FIC, "2002", "Моё.\n\nОбщее.")
    site = FakeAPI(rev("Было.\n\nСайт.", "2026-10-30T10:00:00"))
    synced = asyncio.run(sync_chapter(site, storage, FIC, "2002", "Глава", False))
    assert synced.kind == "merge" and synced.site is not None
    merge, parts, _ = start_merge(storage, FIC, "2002", "Глава", synced.site)
    assert merge.base == meta.base
    text = finish_merge(storage, FIC, "2002", "Глава", merge, parts, {})
    assert text == "Моё.\n\nСайт."


def test_merge_without_base_starts_from_empty(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_draft(FIC, CH, "Моё.")  # chapter.json до исправления: base нет
    synced = sync(FakeAPI(rev("Сайт.")), storage)
    assert synced.kind == "merge" and synced.site is not None

    merge, parts, _ = start_merge(storage, FIC, CH, "Глава", synced.site)
    assert merge.base is None
    # вся глава - один конфликт "сайт против моего"
    assert [p.kind for p in parts if isinstance(p, Hunk)] == ["conflict"]
    assert postpone_merge(storage, FIC, CH, "Глава", merge, parts, {})
    text = finish_merge(storage, FIC, CH, "Глава", merge, parts, {0: "site"})
    assert text == "Сайт."
    assert storage.read_chapter(FIC, CH).base == synced.site.file


def test_restore_version(tmp_path):
    storage = ChapterStorage(tmp_path)
    site = storage.add_version(FIC, CH, "Сайт.", "Глава", "site", site_num=1)
    mine = storage.add_version(FIC, CH, "Своя.", "Глава", "manual")
    storage.write_draft(FIC, CH, "Черновик.")

    # прежний черновик - версией, ревизия сайта - в restored
    assert restore_version(storage, FIC, CH, "Глава", site) == "Сайт."
    assert storage.read_draft(FIC, CH) == "Сайт."
    meta = storage.read_chapter(FIC, CH)
    assert meta.restored == site.file
    assert storage.read_version(FIC, CH, meta.versions[-1].file) == "Черновик."

    # своя версия restored сбрасывает; черновик равен последней версии - без снимка
    storage.add_version(FIC, CH, "Сайт.", "Глава", "manual")
    count = len(storage.read_chapter(FIC, CH).versions)
    assert restore_version(storage, FIC, CH, "Глава", mine) == "Своя."
    meta = storage.read_chapter(FIC, CH)
    assert meta.restored is None
    assert len(meta.versions) == count


def test_merge_start_finish_and_review(tmp_path):
    storage = ChapterStorage(tmp_path)
    sync(FakeAPI(rev("Было.\n\nОбщее.")), storage)
    storage.write_draft(FIC, CH, "Моё.\n\nОбщее.")
    api = FakeAPI(
        rev("Сайт.\n\nОбщее!", "2026-10-27T10:00:00"), [info(2), info(1, "9001")]
    )
    synced = sync(api, storage)
    assert synced.site is not None
    # в шапку - только правки новее base
    assert [r.num for r in synced.revs] == [2]

    merge, parts, choices = start_merge(storage, FIC, CH, "Глава", synced.site)
    assert choices == {}
    # Esc и снова: второго снимка черновика нет, решения вернулись
    base = storage.read_chapter(FIC, CH).base
    assert postpone_merge(storage, FIC, CH, "Глава", merge, parts, {0: "site"})
    assert storage.read_draft(FIC, CH) == "Моё.\n\nОбщее."
    assert storage.read_chapter(FIC, CH).base == base
    merge, _, choices = start_merge(storage, FIC, CH, "Глава", synced.site)
    assert choices == {0: "site"}
    assert storage.read_version(FIC, CH, merge.mine) == "Моё.\n\nОбщее."

    text = finish_merge(storage, FIC, CH, "Глава", merge, parts, {0: Manual("Оба.")})
    # Esc в F4 после "Готово" решений не меняет
    assert not postpone_merge(storage, FIC, CH, "Глава", merge, parts, {0: "site"})
    assert text == storage.read_draft(FIC, CH) == "Оба.\n\nОбщее!"
    meta = storage.read_chapter(FIC, CH)
    assert meta.base == synced.site.file

    # F4: решения возвращаются, правка после слияния сохраняется версией
    storage.write_draft(FIC, CH, "Правка после слияния.")
    loaded = load_merge(storage, FIC, CH)
    assert loaded is not None and loaded != "outdated"
    merge, parts, choices, site = loaded
    assert choices == {0: Manual("Оба.")}
    assert site == synced.site
    count = len(meta.versions)
    finish_merge(storage, FIC, CH, "Глава", merge, parts, {0: "mine"})
    meta = storage.read_chapter(FIC, CH)
    assert storage.read_draft(FIC, CH) == "Моё.\n\nОбщее!"
    assert len(meta.versions) == count + 1
    assert storage.read_version(FIC, CH, meta.versions[-1].file) == (
        "Правка после слияния."
    )

    # решение не на своём блоке: отбрасываются все
    assert meta.last_merge is not None
    meta.last_merge.choices[0].start = 5
    storage.write_chapter(FIC, CH, meta)
    loaded = load_merge(storage, FIC, CH)
    assert loaded is not None and loaded != "outdated" and loaded[2] == {}


def test_postponed_merge_review_takes_new_edits(tmp_path):
    storage = ChapterStorage(tmp_path)
    sync(FakeAPI(rev("Было.\n\nОбщее.")), storage)
    storage.write_draft(FIC, CH, "Моё.\n\nОбщее.")
    synced = sync(FakeAPI(rev("Сайт.\n\nОбщее!", "2026-10-27T10:00:00")), storage)
    assert synced.site is not None
    merge, parts, _ = start_merge(storage, FIC, CH, "Глава", synced.site)
    assert postpone_merge(storage, FIC, CH, "Глава", merge, parts, {0: "site"})
    # докачка после снимка: последним он остаётся, даже если дата сайта (по Москве)
    # позже его даты по местным часам
    storage.add_version(
        FIC, CH, "Старое.", "Глава", "site", created="2099-01-01T00:00:00", at=0
    )
    # F4 без правок: тот же снимок и решения
    loaded = load_merge(storage, FIC, CH)
    assert loaded is not None and loaded != "outdated"
    assert loaded[0].mine == merge.mine and loaded[2] == {0: "site"}

    # правка после Esc: F4 берёт новый снимок, иначе "Готово" выкинуло бы её
    storage.write_draft(FIC, CH, "Моё.\n\nОбщее.\n\nЕщё.")
    loaded = load_merge(storage, FIC, CH)
    assert loaded is not None and loaded != "outdated"
    merge, parts, choices, _ = loaded
    assert choices == {}
    text = finish_merge(storage, FIC, CH, "Глава", merge, parts, {0: "site"})
    assert text == "Сайт.\n\nОбщее!\n\nЕщё."


def test_load_merge_outdated_after_fast_forward(tmp_path):
    storage = ChapterStorage(tmp_path)
    sync(FakeAPI(rev("Было.\n\nОбщее.")), storage)
    storage.write_draft(FIC, CH, "Моё.\n\nОбщее.")
    synced = sync(FakeAPI(rev("Сайт.\n\nОбщее!", "2026-10-27T10:00:00")), storage)
    assert synced.site is not None
    merge, parts, _ = start_merge(storage, FIC, CH, "Глава", synced.site)
    finish_merge(storage, FIC, CH, "Глава", merge, parts, {0: "site"})
    # черновик равен base: новая правка сайта - fast-forward
    ff = sync(FakeAPI(rev("Сайт.\n\nЕщё.", "2026-10-28T10:00:00")), storage)
    assert ff.kind == "ff"
    assert load_merge(storage, FIC, CH) == "outdated"


class CreateDownAPI(CreatingAPI):
    async def create_chapter(self, fic_id: str, title: str, content: str) -> str:
        raise NetworkError()


def test_failed_create_leaves_no_base(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_chapter(FIC, "new-1", ChapterMeta("Глава"))
    storage.write_draft(FIC, "new-1", "Текст.")
    with pytest.raises(NetworkError):
        asyncio.run(
            publish_chapter(CreateDownAPI(), storage, FIC, "new-1", "Глава", True)
        )
    # с base вкладка показала бы "как на сайте"
    assert storage.read_chapter(FIC, "new-1").base is None
