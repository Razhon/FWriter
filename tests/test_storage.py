import json
from datetime import datetime
from pathlib import Path

import pytest

from fwriter.core.api.models import Fic, SlimChapterInfo
from fwriter.core.storage import (
    ChapterMeta,
    ChapterStorage,
    LastMerge,
    MergeChoice,
    lock_data_dir,
)


def test_draft_roundtrip(tmp_path):
    storage = ChapterStorage(tmp_path)
    assert storage.read_draft("1", "2") is None

    text = "<center>Глава</center>\n<tab>Абзац.\r\nХвост без перевода строки"
    storage.write_draft("1", "2", text)
    assert storage.read_draft("1", "2") == text


def test_version_adds_record(tmp_path):
    storage = ChapterStorage(tmp_path)
    # два снимка подряд почти всегда в одну секунду: файлы не должны совпасть
    v1 = storage.add_version("1", "2", "раз", "Глава", "manual", "коммент")
    v2 = storage.add_version(
        "1",
        "2",
        "два",
        "Глава",
        "site",
        site_num=3,
        author="00000000-0000-7000-8000-000000003001",
        author_name="user-1",
    )

    assert v1.file != v2.file
    assert storage.read_chapter("1", "2") == ChapterMeta("Глава", versions=[v1, v2])
    assert v1.comment == "коммент"
    assert (v2.site_num, v2.author, v2.author_name, v2.site_rev) == (
        3,
        "00000000-0000-7000-8000-000000003001",
        "user-1",
        None,
    )
    assert storage.read_version("1", "2", v1.file) == "раз"
    assert storage.read_version("1", "2", v2.file) == "два"


def test_new_chapter_renamed(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Фик", []), "author")
    assert storage.new_chapter_id("1") == "new-1"
    assert storage.new_chapter_id("1") == "new-2"
    storage.write_draft("1", "new-1", "текст")

    storage.rename_chapter("1", "new-1", "500", "Новая")

    assert storage.read_draft("1", "500") == "текст"
    assert storage.read_draft("1", "new-1") is None
    assert storage.new_chapter_id("1") == "new-1"
    [(fic, _)], _ = storage.read_fics()
    assert fic.chapters == [SlimChapterInfo("500", "Новая")]


def test_rename_after_site_refresh_no_duplicate(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.new_chapter_id("1")
    storage.write_draft("1", "new-1", "текст")
    storage.write_fic(Fic("1", "Фик", [SlimChapterInfo("500", "Новая")]), "author")

    storage.rename_chapter("1", "new-1", "500", "Новая")

    [(fic, _)], _ = storage.read_fics()
    assert fic.chapters == [SlimChapterInfo("500", "Новая")]
    assert storage.read_draft("1", "500") == "текст"


@pytest.mark.parametrize(
    "broken",
    [
        "{",
        "[]",
        '{"chapters": {}}',
        # роль проверяется так же, как в read_fics: файл не перезаписывается
        '{"title": "Фик", "role": "boss", "readonly": false, "chapters": []}',
        None,
    ],
)
def test_rename_failure_keeps_new_chapter(tmp_path, monkeypatch, broken: str | None):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Фик", []), "author")
    storage.new_chapter_id("1")
    storage.write_draft("1", "new-1", "текст")
    if broken is None:  # fic.json цел, но запись не удалась

        def fail(*_):
            raise OSError("диск полон")

        monkeypatch.setattr("fwriter.core.storage._write", fail)
    else:
        (tmp_path / "fics" / "1" / "fic.json").write_text(broken, encoding="utf-8")

    with pytest.raises(OSError if broken is None else ValueError):
        storage.rename_chapter("1", "new-1", "500", "Новая")

    assert storage.new_chapters("1") == [SlimChapterInfo("new-1", "new-1")]
    assert storage.read_draft("1", "500") is None


def test_rename_to_existing_folder_changes_nothing(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Фик", []), "author")
    storage.new_chapter_id("1")
    storage.write_draft("1", "new-1", "текст")
    storage.write_draft("1", "500", "с сайта")

    with pytest.raises(FileExistsError):
        storage.rename_chapter("1", "new-1", "500", "Новая")

    [(fic, _)], _ = storage.read_fics()
    assert fic.chapters == []
    assert storage.read_draft("1", "500") == "с сайта"
    assert storage.read_draft("1", "new-1") == "текст"


def test_rename_folder_failure_keeps_text(tmp_path, monkeypatch):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Фик", []), "author")
    storage.new_chapter_id("1")
    storage.write_draft("1", "new-1", "текст")

    def fail(*_):
        raise OSError("нет доступа")

    monkeypatch.setattr("pathlib.Path.rename", fail)
    with pytest.raises(OSError):
        storage.rename_chapter("1", "new-1", "500", "Новая")

    # глава в fic.json без локальной копии, текст - в new-1
    [(fic, _)], _ = storage.read_fics()
    assert fic.chapters == [SlimChapterInfo("500", "Новая")]
    assert storage.read_draft("1", "500") is None
    assert storage.new_chapters("1") == [SlimChapterInfo("new-1", "new-1")]
    assert storage.read_draft("1", "new-1") == "текст"


def test_new_chapters_survive_restart(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Фик", []), "author")
    ids = [storage.new_chapter_id("1") for _ in range(10)]
    storage.write_draft("1", ids[9], "десятая")
    storage.write_draft("1", ids[1], "вторая")
    storage.set_title("1", ids[1], "Вторая")
    (tmp_path / "fics" / "1" / "new-x").mkdir()
    (tmp_path / "fics" / "1" / "new-x" / "draft.txt").write_text("не наша")

    # после перезапуска: глав нет в fic.json, пустая new-1 без текста не нужна
    restarted = ChapterStorage(tmp_path)
    assert restarted.new_chapters("1") == [
        SlimChapterInfo("new-2", "Вторая"),
        SlimChapterInfo("new-10", "new-10"),
    ]
    assert restarted.read_draft("1", "new-10") == "десятая"


@pytest.mark.parametrize(
    "broken",
    [
        "{",
        "[]",
        '{"title": "x"}',
        '{"versions": [1]}',
        '{"title": 5, "versions": []}',
        '{"title": "x", "last_merge": [1], "versions": []}',
        '{"title": "x", "last_merge": {"base": "a", "site": "b"}, "versions": []}',
        *(
            '{"title": "x", "versions": [], "last_merge": {"base": "a", "site": "b",'
            f' "mine": "c", "choices": [{choice}]}}}}'
            for choice in (
                '{"part": 1, "start": 0, "choice": "foo"}',
                '{"part": 1, "start": 0, "choice": "manual"}',
                '{"part": 1, "start": 0, "choice": "site", "text": "x"}',
                '{"part": "1", "start": 0, "choice": "site"}',
                '{"part": true, "start": 0, "choice": "site"}',
                '{"part": 1, "choice": "site"}',
            )
        ),
        '{"title": "x", "versions": [], "new_field": 1}',
        (
            '{"title": "x", "versions": [{"file": "a.txt", "title": "x",'
            ' "source": "foo", "created": "2026-01-01T00:00:00"}]}'
        ),
        (
            '{"title": "x", "versions": [{"file": "a.txt", "title": "x",'
            ' "source": "site", "created": "2026-01-01T00:00:00", "site_num": "3"}]}'
        ),
    ],
)
def test_broken_chapter_json_keeps_texts(tmp_path, broken: str):
    storage = ChapterStorage(tmp_path)
    v = storage.add_version("1", "2", "версия", "Глава", "manual")
    storage.write_draft("1", "2", "черновик")
    (tmp_path / "fics" / "1" / "2" / "chapter.json").write_text(broken)

    with pytest.raises(ValueError):
        storage.read_chapter("1", "2")
    assert storage.read_draft("1", "2") == "черновик"
    assert storage.read_version("1", "2", v.file) == "версия"


def test_fics_roundtrip(tmp_path):
    storage = ChapterStorage(tmp_path)
    fic = Fic("7", "Фик", [SlimChapterInfo("70", "Глава 1")], readonly=True)
    storage.write_fic(fic, "beta")
    storage.write_fic(Fic("8", "Другой", []), "author")

    assert storage.read_fics() == (
        [(fic, "beta"), (Fic("8", "Другой", []), "author")],
        [],
    )


def test_write_fic_skips_same_text(tmp_path, monkeypatch):
    storage = ChapterStorage(tmp_path)
    fic = Fic("7", "Фик", [SlimChapterInfo("70", "Глава 1")])
    storage.write_fic(fic, "author")
    written: list[Path] = []
    monkeypatch.setattr("fwriter.core.storage._write", lambda p, t: written.append(p))

    storage.write_fic(fic, "author")
    assert written == []
    storage.write_fic(fic, "beta")
    assert written == [tmp_path / "fics" / "7" / "fic.json"]


@pytest.mark.parametrize(
    "broken",
    [
        "{",
        '{"title": "x", "role": "author", "readonly": false}',
        '{"title": "x", "role": "boss", "readonly": false, "chapters": []}',
        '{"title": "x", "role": "author", "readonly": "no", "chapters": []}',
    ],
)
def test_broken_fic_json_skipped(tmp_path, broken: str):
    storage = ChapterStorage(tmp_path)
    fic = Fic("7", "Фик", [])
    storage.write_fic(fic, "author")
    (tmp_path / "fics" / "8").mkdir()
    (tmp_path / "fics" / "8" / "fic.json").write_text(broken)

    fics, errors = storage.read_fics()

    assert fics == [(fic, "author")]
    assert errors == [f"Файл {tmp_path / 'fics' / '8' / 'fic.json'} повреждён"]
    # пропущенный не читается: ошибки нет
    assert storage.read_fics(skip={"8"}) == ([(fic, "author")], [])


def test_version_omits_empty_fields(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.add_version("1", "2", "раз", "Глава", "manual")
    storage.add_version(
        "1", "2", "два", "Глава", "site", author="3001", author_name="user-1"
    )

    data = json.loads(
        (tmp_path / "fics" / "1" / "2" / "chapter.json").read_text("utf-8")
    )
    manual, site = data["versions"]
    assert manual.keys().isdisjoint({"site_rev", "site_num", "author", "author_name"})
    assert "site_rev" not in site
    assert (site["author"], site["author_name"]) == ("3001", "user-1")
    assert data.keys().isdisjoint({"base", "restored", "last_merge"})
    assert [v.source for v in storage.read_chapter("1", "2").versions] == [
        "manual",
        "site",
    ]


def test_last_merge_roundtrip(tmp_path):
    storage = ChapterStorage(tmp_path)
    path = tmp_path / "fics" / "1" / "2" / "chapter.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"title": "x", "versions": [], "last_merge": {"base": "a", "site": "b",'
        ' "mine": "c", "choices": [{"part": 1, "start": 0, "choice": "site"},'
        ' {"part": 3, "start": 5, "choice": "manual", "text": ""}]}}',
        encoding="utf-8",
    )
    merge = LastMerge(
        base="a",
        site="b",
        mine="c",
        choices=[MergeChoice(1, 0, "site"), MergeChoice(3, 5, "manual", "")],
    )
    assert storage.read_chapter("1", "2").last_merge == merge

    # перезапись chapter.json сохраняет решения, пустой text у "manual" не теряется
    storage.set_title("1", "2", "y")
    assert storage.read_chapter("1", "2").last_merge == merge
    site, manual = json.loads(path.read_text("utf-8"))["last_merge"]["choices"]
    assert "text" not in site
    assert manual["text"] == ""


def test_set_title_keeps_versions(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.set_title("1", "2", "Старое")
    v = storage.add_version("1", "2", "текст", "Старое", "manual")
    # название пишется поверх файла на диске, а не поверх копии без версии
    storage.set_title("1", "2", "Новое")
    assert storage.read_chapter("1", "2") == ChapterMeta("Новое", versions=[v])


def test_broken_chapter_json_not_overwritten_by_storage(tmp_path):
    storage = ChapterStorage(tmp_path)
    path = tmp_path / "fics" / "1" / "2" / "chapter.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    with pytest.raises(ValueError):
        storage.set_title("1", "2", "Новое")
    with pytest.raises(ValueError):
        storage.add_version("1", "2", "текст", "Глава", "manual")
    assert path.read_text(encoding="utf-8") == "{"
    assert not (path.parent / "versions").exists()


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 1)


def test_version_file_not_overwritten(tmp_path, monkeypatch):
    storage = ChapterStorage(tmp_path)
    taken = tmp_path / "fics" / "1" / "2" / "versions" / "2026-01-01T00-00-00.txt"
    taken.parent.mkdir(parents=True)
    taken.write_text("чужой", encoding="utf-8")
    monkeypatch.setattr("fwriter.core.storage.datetime", _Frozen)
    # файл появился уже после проверки exists()
    monkeypatch.setattr(Path, "exists", lambda self: False)

    v = storage.add_version("1", "2", "свой", "Глава", "manual")

    assert v.file == "2026-01-01T00-00-00-2.txt"
    assert taken.read_text(encoding="utf-8") == "чужой"


@pytest.mark.parametrize(
    ("fic_id", "chapter_id"), [("..", "2"), ("1", ".."), ("1", "new-../x")]
)
def test_bad_ids_rejected(tmp_path, fic_id: str, chapter_id: str):
    storage = ChapterStorage(tmp_path / "data")

    with pytest.raises(ValueError):
        storage.write_draft(fic_id, chapter_id, "текст")
    assert not (tmp_path / "data").exists()


def test_lock_data_dir_once(tmp_path):
    with lock_data_dir(tmp_path), pytest.raises(OSError):
        lock_data_dir(tmp_path)
    # после закрытия файла блокировка свободна
    lock_data_dir(tmp_path).close()


def test_local_chapters(tmp_path):
    """Главы с сайта с черновиком, по номеру; new-N и папки без текста не входят."""
    storage = ChapterStorage(tmp_path)
    storage.write_draft("1", "10", "десятая")
    storage.write_draft("1", "9", "девятая")
    storage.set_title("1", "9", "Девятая")
    storage.write_draft("1", "new-1", "новая")
    (tmp_path / "fics" / "1" / "11").mkdir()

    assert storage.local_chapters("1") == [
        SlimChapterInfo("9", "Девятая"),
        SlimChapterInfo("10", "10"),
    ]


def test_local_chapters_skip_not_read(tmp_path, monkeypatch):
    """Пропущенные главы отсекаются до чтения chapter.json."""
    storage = ChapterStorage(tmp_path)
    storage.write_draft("1", "9", "девятая")
    storage.write_draft("1", "10", "десятая")
    read: list[str] = []
    real = storage.read_chapter
    monkeypatch.setattr(
        storage, "read_chapter", lambda f, c: read.append(c) or real(f, c)
    )

    assert storage.local_chapters("1", skip={"9"}) == [SlimChapterInfo("10", "10")]
    assert read == ["10"]


def test_trash_chapter_and_fic(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Работа", []), "author")
    storage.write_draft("1", "2", "Удалённая.")
    storage.add_version("1", "2", "Удалённая.", "Глава", "manual")
    storage.write_draft("1", storage.new_chapter_id("1"), "Новая.")

    trashed = storage.trash_chapter("1", "2")
    assert trashed.parent == tmp_path / "trash" / "1"
    assert (trashed / "draft.txt").read_text(encoding="utf-8") == "Удалённая."
    assert len(list((trashed / "versions").iterdir())) == 1
    assert storage.local_chapters("1") == []

    # та же new-1 дважды за секунду: прежняя копия в корзине не затирается
    first = storage.trash_chapter("1", "new-1")
    storage.write_draft("1", storage.new_chapter_id("1"), "Ещё одна.")
    second = storage.trash_chapter("1", "new-1")
    assert first != second
    assert (first / "draft.txt").read_text(encoding="utf-8") == "Новая."
    assert storage.new_chapters("1") == []

    storage.write_draft("1", "3", "Текст.")
    trashed = storage.trash_fic("1")
    assert (trashed / "fic.json").exists() and (trashed / "3" / "draft.txt").exists()
    assert storage.read_fics() == ([], [])
    with pytest.raises(FileNotFoundError):
        storage.trash_fic("1")


def test_recent(tmp_path):
    storage = ChapterStorage(tmp_path)
    assert storage.read_recent() == []
    storage.add_recent("1", "2")
    storage.add_recent("1", "new-1")
    storage.add_recent("1", "2")
    assert storage.read_recent() == [("1", "2"), ("1", "new-1")]
    for n in range(20):
        storage.add_recent("1", str(n))
    assert len(storage.read_recent()) == 10
    with pytest.raises(ValueError):
        storage.add_recent("../1", "2")
    # повреждённый файл - пустой список, а не ошибка
    (tmp_path / "recent.json").write_text("[1, [2]]", encoding="utf-8")
    assert storage.read_recent() == []


def test_recent_not_a_list(tmp_path):
    storage = ChapterStorage(tmp_path)
    (tmp_path / "recent.json").write_text('{"ab": 1}', encoding="utf-8")
    assert storage.read_recent() == []


def test_rename_chapter_drops_old_recent(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Фик", []), "author")
    storage.new_chapter_id("1")
    storage.write_draft("1", "new-1", "текст")
    storage.add_recent("1", "new-1")
    storage.add_recent("1", "7")

    storage.rename_chapter("1", "new-1", "500", "Новая")

    assert storage.read_recent() == [("1", "7")]
