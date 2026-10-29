import asyncio
from dataclasses import replace

from fwriter.core.api.client import FicbookAPI
from fwriter.core.api.models import Chapter, Revision, RevisionInfo
from fwriter.core.storage import ChapterStorage
from fwriter.core.sync import sync_chapter

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
        self.calls: list[str] = []

    async def get_chapter(self, fic_id: str, chapter_id: str) -> Chapter:
        self.calls.append("chapter")
        return Chapter("Глава", self.last.content, True, 1, MINE)

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
