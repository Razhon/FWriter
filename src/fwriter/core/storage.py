import json
import os
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from io import TextIOWrapper
from pathlib import Path
from typing import Literal, get_args

from platformdirs import user_data_dir

from .api.models import Fic, Role, SlimChapterInfo

# appauthor=False: без лишней вложенной папки fwriter/fwriter в Windows
DATA_DIR = Path(user_data_dir("fwriter", appauthor=False))


type Source = Literal["manual", "site", "publish"]


@dataclass
class Version:
    """Запись о снимке в chapter.json; site_* и author - только у ревизий сайта."""

    file: str
    title: str
    source: Source
    created: str
    comment: str = ""
    site_rev: str | None = None  # None у последней ревизии, пока не появится следующая
    site_num: int | None = None
    author: str | None = None


@dataclass
class ChapterMeta:
    """chapter.json: название черновика и история версий главы."""

    title: str
    base: str | None = None  # файл версии сайта, от которой ведётся черновик
    restored: str | None = None
    last_merge: dict[str, str] | None = None  # {"base", "site", "mine"} -> файл
    versions: list[Version] = field(default_factory=list)


def _write(path: Path, text: str) -> None:
    """Через временный файл: при сбое старый файл остаётся целым."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # newline="": текст хранится байт в байт, без подмены \n на \r\n в Windows
    with tmp.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # без fsync папки Linux может потерять rename при отключении питания
    # и нарушить порядок записей (версия до chapter.json); в Windows так нельзя
    if sys.platform != "win32":
        try:
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # файл уже на месте; часть сетевых и FUSE-систем не даёт открыть
            # папку или сделать ей fsync
            pass


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", newline="")


def lock_data_dir(root: Path) -> TextIOWrapper:
    """Блокировка от второй копии, пока файл открыт; OSError, если занято."""
    # не данные, поэтому мимо _write(); не удаляем, иначе третья копия тоже возьмёт
    root.mkdir(parents=True, exist_ok=True)
    f = (root / "fwriter.lock").open("a")  # не "w": не обрезать занятый файл
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)  # locking блокирует с текущей позиции
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise
    return f


def _dump(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


class ChapterStorage:
    """Черновики, версии и fic.json на диске; о сети и UI ничего не знает.

    Повреждённые fic.json и chapter.json дают ValueError, отсутствующие -
    FileNotFoundError; тексты черновика и версий читаются независимо от них.
    """

    def __init__(self, root: Path) -> None:
        self.fics = root / "fics"

    def _chapter(self, fic_id: str, chapter_id: str) -> Path:
        return self.fics / fic_id / chapter_id

    def write_fic(self, fic: Fic, role: Role) -> None:
        _write(
            self.fics / fic.id / "fic.json",
            _dump(
                {
                    "title": fic.title,
                    "role": role,
                    "readonly": fic.readonly,
                    "chapters": [asdict(ch) for ch in fic.chapters],
                }
            ),
        )

    def read_fic(self, fic_id: str) -> tuple[Fic, Role]:
        return self._read_fic(self.fics / fic_id / "fic.json")

    def read_fics(self) -> tuple[list[tuple[Fic, Role]], list[str]]:
        """Фанфики из fic.json для офлайн-режима: [(фанфик, роль)] и ошибки.

        Повреждённый fic.json пропускается, чтобы не прятать остальные фанфики;
        его ошибку показывает UI.
        """
        fics: list[tuple[Fic, Role]] = []
        errors: list[str] = []
        for path in sorted(self.fics.glob("*/fic.json")):
            try:
                fics.append(self._read_fic(path))
            except OSError, ValueError:
                errors.append(f"Файл {path} повреждён")
        return fics, errors

    @staticmethod
    def _read_fic(path: Path) -> tuple[Fic, Role]:
        try:
            data = json.loads(_read(path))
            role, readonly = data["role"], data["readonly"]
            # иначе фанфик молча пропадёт из всех разделов списка
            if role not in get_args(Role) or not isinstance(readonly, bool):
                raise ValueError(f"роль {role!r}, readonly {readonly!r}")
            chapters = [SlimChapterInfo(**ch) for ch in data["chapters"]]
            return Fic(path.parent.name, data["title"], chapters, readonly), role
        except (ValueError, TypeError, KeyError) as e:
            raise ValueError(f"Файл {path} повреждён") from e

    def read_draft(self, fic_id: str, chapter_id: str) -> str | None:
        """None, если локальной копии главы нет."""
        try:
            return _read(self._chapter(fic_id, chapter_id) / "draft.txt")
        except FileNotFoundError:
            return None

    def write_draft(self, fic_id: str, chapter_id: str, text: str) -> None:
        """Ввод пользователя; слияние и восстановление сначала делают add_version."""
        _write(self._chapter(fic_id, chapter_id) / "draft.txt", text)

    def read_chapter(self, fic_id: str, chapter_id: str) -> ChapterMeta:
        path = self._chapter(fic_id, chapter_id) / "chapter.json"
        try:
            data = json.loads(_read(path))
            versions = [Version(**v) for v in data.pop("versions")]
            return ChapterMeta(**data, versions=versions)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            raise ValueError(f"Файл {path} повреждён") from e

    def _load_chapter(self, fic_id: str, chapter_id: str, title: str) -> ChapterMeta:
        """Нет chapter.json - пустая история; повреждённый - ValueError, не затираем."""
        try:
            return self.read_chapter(fic_id, chapter_id)
        except FileNotFoundError:
            return ChapterMeta(title)

    def _write_chapter(
        self, fic_id: str, chapter_id: str, chapter: ChapterMeta
    ) -> None:
        data = asdict(chapter)
        # у site и publish null осмыслен (site_rev последней ревизии), у manual полей нет
        for v in data["versions"]:
            if v["source"] == "manual":
                del v["site_rev"], v["site_num"], v["author"]
        _write(self._chapter(fic_id, chapter_id) / "chapter.json", _dump(data))

    def set_title(self, fic_id: str, chapter_id: str, title: str) -> None:
        chapter = self._load_chapter(fic_id, chapter_id, title)
        self._write_chapter(fic_id, chapter_id, replace(chapter, title=title))

    def read_version(self, fic_id: str, chapter_id: str, file: str) -> str:
        return _read(self._chapter(fic_id, chapter_id) / "versions" / file)

    def add_version(
        self,
        fic_id: str,
        chapter_id: str,
        text: str,
        title: str,
        source: Source,
        comment: str = "",
        *,
        created: str | None = None,
        site_rev: str | None = None,
        site_num: int | None = None,
        author: str | None = None,
    ) -> Version:
        """Снимок text в versions/ и запись о нём в chapter.json.

        created по умолчанию - текущее время; у ревизий сайта - дата с сайта.
        """
        # до снимка: при повреждённом chapter.json не будет лишнего файла
        chapter = self._load_chapter(fic_id, chapter_id, title)
        now = datetime.now()  # noqa: DTZ005 локальное время без пояса, как даты на сайте
        versions = self._chapter(fic_id, chapter_id) / "versions"
        stem = now.strftime("%Y-%m-%dT%H-%M-%S")
        file, n = f"{stem}.txt", 1
        # несколько снимков за секунду: синхронизация скачивает ревизии пачкой
        while (versions / file).exists():
            n += 1
            file = f"{stem}-{n}.txt"
        # сначала текст: при сбое останется лишний файл, а не запись без файла
        _write(versions / file, text)
        version = Version(
            file=file,
            title=title,
            source=source,
            created=created or now.isoformat(timespec="seconds"),
            comment=comment,
            site_rev=site_rev,
            site_num=site_num,
            author=author,
        )
        chapter.versions.append(version)
        self._write_chapter(fic_id, chapter_id, chapter)
        return version

    def new_chapter_id(self, fic_id: str) -> str:
        """Свободный id new-N для главы, которой ещё нет на сайте; папка создаётся."""
        n = 1
        while True:
            chapter_id = f"new-{n}"
            try:
                self._chapter(fic_id, chapter_id).mkdir(parents=True)
            except FileExistsError:
                n += 1
                continue
            return chapter_id

    def new_chapters(self, fic_id: str) -> list[SlimChapterInfo]:
        """Неотправленные new-N с текстом: их нет ни в fic.json, ни на сайте.

        Название - из chapter.json, а если его нет или он повреждён - id,
        чтобы текст всё равно можно было открыть.
        """
        paths = [
            p
            for p in (self.fics / fic_id).glob("new-*/draft.txt")
            if p.parent.name[4:].isdecimal()
        ]
        # по номеру: new-10 после new-2
        paths.sort(key=lambda p: int(p.parent.name[4:]))
        return [self._slim(fic_id, p.parent.name) for p in paths]

    def local_chapters(self, fic_id: str) -> list[SlimChapterInfo]:
        """Главы с сайта, у которых есть черновик; название - как у new_chapters."""
        paths = [
            p
            for p in (self.fics / fic_id).glob("*/draft.txt")
            if p.parent.name.isdecimal()
        ]
        paths.sort(key=lambda p: int(p.parent.name))
        return [self._slim(fic_id, p.parent.name) for p in paths]

    def _slim(self, fic_id: str, chapter_id: str) -> SlimChapterInfo:
        try:
            title = self.read_chapter(fic_id, chapter_id).title
        except OSError, ValueError:
            title = chapter_id
        return SlimChapterInfo(chapter_id, title)

    def rename_chapter(self, fic_id: str, old_id: str, new_id: str, title: str) -> None:
        """new-N -> id с сайта после создания главы; глава дописывается в fic.json.

        Сначала fic.json, потом папка: при сбое на любом шаге текст остаётся
        в new-N и виден через new_chapters(), а переименованную папку
        без записи в fic.json не нашёл бы ни один список.

        Риск дубля: при ошибке глава уже создана на сайте, а текст лежит
        в new-N; повторная отправка new-N создаст на сайте вторую главу.
        Предупреждать об этом должен UI отправки.
        """
        new = self._chapter(fic_id, new_id)
        # до записи fic.json: rename на занятую папку ведёт себя по-разному в разных ОС
        if new.exists():
            raise FileExistsError(f"Папка {new} уже существует")
        fic, role = self._read_fic(self.fics / fic_id / "fic.json")
        # глава уже есть, если список успели обновить с сайта
        if all(ch.id != new_id for ch in fic.chapters):
            fic.chapters.append(SlimChapterInfo(new_id, title))
        self.write_fic(fic, role)
        self._chapter(fic_id, old_id).rename(new)
