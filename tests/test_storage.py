import json

import pytest

from fwriter.core.api.models import Fic, SlimChapterInfo
from fwriter.core.storage import ChapterMeta, ChapterStorage


def test_draft_roundtrip(tmp_path):
    storage = ChapterStorage(tmp_path)
    assert storage.read_draft("1", "2") is None

    text = "<center>Глава</center>\n<tab>Абзац.\r\nХвост без перевода строки"
    storage.write_draft("1", "2", text)
    assert storage.read_draft("1", "2") == text


def test_version_adds_record(tmp_path):
    storage = ChapterStorage(tmp_path)
    chapter = ChapterMeta(title="Глава")
    # два снимка подряд почти всегда в одну секунду: файлы не должны совпасть
    v1 = storage.add_version("1", "2", chapter, "раз", "Глава", "manual", "коммент")
    v2 = storage.add_version(
        "1", "2", chapter, "два", "Глава", "site", site_num=3, author="user-1"
    )

    assert v1.file != v2.file
    assert storage.read_chapter("1", "2") == chapter
    assert chapter.versions == [v1, v2]
    assert v1.comment == "коммент"
    assert (v2.site_num, v2.author, v2.site_rev) == (3, "user-1", None)
    assert storage.read_version("1", "2", v1.file) == "раз"
    assert storage.read_version("1", "2", v2.file) == "два"


def test_new_chapter_renamed(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Фик", []), "author", readonly=False)
    assert storage.new_chapter_id("1") == "new-1"
    assert storage.new_chapter_id("1") == "new-2"
    storage.write_draft("1", "new-1", "текст")

    storage.rename_chapter("1", "new-1", "500", "Новая")

    assert storage.read_draft("1", "500") == "текст"
    assert storage.read_draft("1", "new-1") is None
    assert storage.new_chapter_id("1") == "new-1"
    [(fic, _, _)], _ = storage.read_fics()
    assert fic.chapters == [SlimChapterInfo("500", "Новая")]


def test_rename_after_site_refresh_no_duplicate(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.new_chapter_id("1")
    storage.write_draft("1", "new-1", "текст")
    storage.write_fic(
        Fic("1", "Фик", [SlimChapterInfo("500", "Новая")]), "author", readonly=False
    )

    storage.rename_chapter("1", "new-1", "500", "Новая")

    [(fic, _, _)], _ = storage.read_fics()
    assert fic.chapters == [SlimChapterInfo("500", "Новая")]
    assert storage.read_draft("1", "500") == "текст"


@pytest.mark.parametrize("broken", ["{", "[]", '{"chapters": {}}', None])
def test_rename_failure_keeps_new_chapter(tmp_path, monkeypatch, broken: str | None):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Фик", []), "author", readonly=False)
    storage.new_chapter_id("1")
    storage.write_draft("1", "new-1", "текст")
    if broken is None:  # meta.json цел, но запись не удалась

        def fail(*_):
            raise OSError("диск полон")

        monkeypatch.setattr("fwriter.core.storage._write", fail)
    else:
        (tmp_path / "fics" / "1" / "meta.json").write_text(broken, encoding="utf-8")

    with pytest.raises(OSError if broken is None else ValueError):
        storage.rename_chapter("1", "new-1", "500", "Новая")

    assert storage.new_chapters("1") == [SlimChapterInfo("new-1", "new-1")]
    assert storage.read_draft("1", "500") is None


def test_rename_folder_failure_keeps_text(tmp_path, monkeypatch):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Фик", []), "author", readonly=False)
    storage.new_chapter_id("1")
    storage.write_draft("1", "new-1", "текст")

    def fail(*_):
        raise OSError("нет доступа")

    monkeypatch.setattr("pathlib.Path.rename", fail)
    with pytest.raises(OSError):
        storage.rename_chapter("1", "new-1", "500", "Новая")

    # глава в meta.json без локальной копии, текст — в new-1
    [(fic, _, _)], _ = storage.read_fics()
    assert fic.chapters == [SlimChapterInfo("500", "Новая")]
    assert storage.read_draft("1", "500") is None
    assert storage.new_chapters("1") == [SlimChapterInfo("new-1", "new-1")]
    assert storage.read_draft("1", "new-1") == "текст"


def test_new_chapters_survive_restart(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("1", "Фик", []), "author", readonly=False)
    ids = [storage.new_chapter_id("1") for _ in range(10)]
    storage.write_draft("1", ids[9], "десятая")
    storage.write_draft("1", ids[1], "вторая")
    storage.write_chapter("1", ids[1], ChapterMeta(title="Вторая"))
    storage.write_draft("1", "new-x", "не наша")

    # после перезапуска: глав нет в meta.json, пустая new-1 без текста не нужна
    restarted = ChapterStorage(tmp_path)
    assert restarted.new_chapters("1") == [
        SlimChapterInfo("new-2", "Вторая"),
        SlimChapterInfo("new-10", "new-10"),
    ]
    assert restarted.read_draft("1", "new-10") == "десятая"


@pytest.mark.parametrize("broken", ["{", "[]", '{"title": "x"}', '{"versions": [1]}'])
def test_broken_chapter_json_keeps_texts(tmp_path, broken: str):
    storage = ChapterStorage(tmp_path)
    chapter = ChapterMeta(title="Глава")
    v = storage.add_version("1", "2", chapter, "версия", "Глава", "manual")
    storage.write_draft("1", "2", "черновик")
    (tmp_path / "fics" / "1" / "2" / "chapter.json").write_text(broken)

    with pytest.raises(ValueError):
        storage.read_chapter("1", "2")
    assert storage.read_draft("1", "2") == "черновик"
    assert storage.read_version("1", "2", v.file) == "версия"


def test_fics_roundtrip(tmp_path):
    storage = ChapterStorage(tmp_path)
    fic = Fic("7", "Фик", [SlimChapterInfo("70", "Глава 1")])
    storage.write_fic(fic, "beta", readonly=True)
    storage.write_fic(Fic("8", "Другой", []), "author", readonly=False)

    assert storage.read_fics() == (
        [(fic, "beta", True), (Fic("8", "Другой", []), "author", False)],
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
def test_broken_meta_json_skipped(tmp_path, broken: str):
    storage = ChapterStorage(tmp_path)
    fic = Fic("7", "Фик", [])
    storage.write_fic(fic, "author", readonly=False)
    (tmp_path / "fics" / "8").mkdir()
    (tmp_path / "fics" / "8" / "meta.json").write_text(broken)

    fics, errors = storage.read_fics()

    assert fics == [(fic, "author", False)]
    assert errors == [f"Файл {tmp_path / 'fics' / '8' / 'meta.json'} повреждён"]


def test_manual_version_has_no_site_fields(tmp_path):
    storage = ChapterStorage(tmp_path)
    chapter = ChapterMeta(title="Глава")
    storage.add_version("1", "2", chapter, "раз", "Глава", "manual")
    storage.add_version("1", "2", chapter, "два", "Глава", "site", author="user-1")

    data = json.loads(
        (tmp_path / "fics" / "1" / "2" / "chapter.json").read_text("utf-8")
    )
    manual, site = data["versions"]
    assert manual.keys().isdisjoint({"site_rev", "site_num", "author"})
    assert (site["site_rev"], site["author"]) == (None, "user-1")
    assert storage.read_chapter("1", "2") == chapter
