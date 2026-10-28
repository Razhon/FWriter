import json

import pytest

from fwriter.core.api.models import Fic, SlimChapterInfo
from fwriter.core.storage import ChapterMeta, ChapterStorage, lock_data_dir


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
        "1", "2", "два", "Глава", "site", site_num=3, author="user-1"
    )

    assert v1.file != v2.file
    assert storage.read_chapter("1", "2") == ChapterMeta("Глава", versions=[v1, v2])
    assert v1.comment == "коммент"
    assert (v2.site_num, v2.author, v2.site_rev) == (3, "user-1", None)
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
    storage.write_draft("1", "new-x", "не наша")

    # после перезапуска: глав нет в fic.json, пустая new-1 без текста не нужна
    restarted = ChapterStorage(tmp_path)
    assert restarted.new_chapters("1") == [
        SlimChapterInfo("new-2", "Вторая"),
        SlimChapterInfo("new-10", "new-10"),
    ]
    assert restarted.read_draft("1", "new-10") == "десятая"


@pytest.mark.parametrize("broken", ["{", "[]", '{"title": "x"}', '{"versions": [1]}'])
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


def test_manual_version_has_no_site_fields(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.add_version("1", "2", "раз", "Глава", "manual")
    storage.add_version("1", "2", "два", "Глава", "site", author="user-1")

    data = json.loads(
        (tmp_path / "fics" / "1" / "2" / "chapter.json").read_text("utf-8")
    )
    manual, site = data["versions"]
    assert manual.keys().isdisjoint({"site_rev", "site_num", "author"})
    assert (site["site_rev"], site["author"]) == (None, "user-1")
    assert [v.source for v in storage.read_chapter("1", "2").versions] == [
        "manual",
        "site",
    ]


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
