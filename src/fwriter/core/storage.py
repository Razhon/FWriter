import json
import logging
import os
import sys
from collections.abc import Container
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from io import TextIOWrapper
from pathlib import Path
from typing import Literal, get_args

from platformdirs import user_data_dir

from .api.models import Fic, Role, SlimChapterInfo

log = logging.getLogger(__name__)

# appauthor=False: без лишней вложенной папки fwriter/fwriter в Windows
DATA_DIR = Path(user_data_dir("fwriter", appauthor=False))


type Source = Literal["manual", "site", "publish"]
type Decision = Literal["mine", "site", "both", "manual"]


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
    author: str | None = None  # slug: ключ, не меняется
    author_name: str | None = None  # ник, только для показа


@dataclass
class MergeChoice:
    """Решение по блоку слияния: part - индекс в результате merge3()."""

    part: int
    start: int  # Hunk.start: проверка, что под индексом тот же блок
    choice: Decision
    text: str | None = None  # только у "manual"


@dataclass
class LastMerge:
    """Файлы версий последнего слияния и решения по его блокам."""

    base: str
    site: str
    mine: str
    choices: list[MergeChoice] = field(default_factory=list)


@dataclass
class ChapterMeta:
    """chapter.json: название черновика и история версий главы."""

    title: str
    base: str | None = None  # файл версии сайта, от которой ведётся черновик
    restored: str | None = None
    history_url: str | None = None  # mine или participated, как дал сайт
    last_merge: LastMerge | None = None
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


def _trash(src: Path, dest_dir: Path, name: str) -> Path:
    """src -> dest_dir/<name>-<время>[-N]; путь в корзине."""
    if not src.is_dir():
        raise FileNotFoundError(f"Папки {src} нет")
    now = datetime.now()  # noqa: DTZ005 локальное время, как в именах версий
    stem = f"{name}-{now.strftime('%Y-%m-%dT%H-%M-%S')}"
    dest, n = dest_dir / stem, 1
    # replace на занятую папку ведёт себя по-разному в разных ОС
    while dest.exists():
        n += 1
        dest = dest_dir / f"{stem}-{n}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    log.info("%s перенесена в корзину: %s", src, dest)
    return dest


def _dump(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _opt(value: object, kind: type) -> bool:
    return value is None or isinstance(value, kind)


def _merge_ok(merge: LastMerge) -> bool:
    files = (merge.base, merge.site, merge.mine)
    return all(isinstance(s, str) for s in files) and all(
        all(type(n) is int for n in (c.part, c.start))
        and c.choice in get_args(Decision.__value__)
        and (isinstance(c.text, str) if c.choice == "manual" else c.text is None)
        for c in merge.choices
    )


def _check_chapter(chapter: ChapterMeta) -> None:
    """ValueError, если в chapter.json поле не того типа: иначе упадёт уже UI."""
    merge = chapter.last_merge
    for v in chapter.versions:
        if not (
            all(isinstance(s, str) for s in (v.file, v.title, v.created, v.comment))
            and v.source in get_args(Source.__value__)
            and all(_opt(s, str) for s in (v.site_rev, v.author, v.author_name))
            and _opt(v.site_num, int)
            and not isinstance(v.site_num, bool)
        ):
            raise ValueError(f"версия {v!r}")
    if not (
        isinstance(chapter.title, str)
        and _opt(chapter.base, str)
        and _opt(chapter.restored, str)
        and _opt(chapter.history_url, str)
        and (merge is None or _merge_ok(merge))
    ):
        raise ValueError("поля главы")


class ChapterStorage:
    """Черновики, версии и fic.json на диске; о сети и UI ничего не знает.

    Повреждённые fic.json и chapter.json дают ValueError, отсутствующие -
    FileNotFoundError; тексты черновика и версий читаются независимо от них.
    """

    def __init__(self, root: Path) -> None:
        self.fics = root / "fics"
        self.trash = root / "trash"
        # shortcut: чтение-правка-запись fic.json и chapter.json без блокировки,
        # async-воркеры её не прервут; нужен lock, если звать из @work(thread=True)

    def _fic(self, fic_id: str) -> Path:
        # id с сайта идут в путь: "../" не должен выйти за пределы fics/
        if not fic_id.isdecimal():
            raise ValueError(f"Недопустимый id работы {fic_id!r}")
        return self.fics / fic_id

    def _chapter(self, fic_id: str, chapter_id: str) -> Path:
        if not chapter_id.removeprefix("new-").isdecimal():
            raise ValueError(f"Недопустимый id главы {chapter_id!r}")
        return self._fic(fic_id) / chapter_id

    def write_fic(self, fic: Fic, role: Role) -> None:
        path = self._fic(fic.id) / "fic.json"
        text = _dump(
            {
                "title": fic.title,
                "role": role,
                "readonly": fic.readonly,
                "chapters": [asdict(ch) for ch in fic.chapters],
            }
        )
        # при входе пишется каждая работа: тот же список - без лишнего fsync
        try:
            if _read(path) == text:
                return
        except OSError, ValueError:
            pass
        _write(path, text)

    def read_fic(self, fic_id: str) -> tuple[Fic, Role]:
        return self._read_fic(self._fic(fic_id) / "fic.json")

    def read_fics(
        self, skip: Container[str] = ()
    ) -> tuple[list[tuple[Fic, Role]], list[str]]:
        """Фанфики из fic.json, кроме skip: [(фанфик, роль)] и ошибки.

        Повреждённый fic.json пропускается, чтобы не прятать остальные фанфики;
        его ошибку показывает UI.
        """
        fics: list[tuple[Fic, Role]] = []
        errors: list[str] = []
        for path in sorted(self.fics.glob("*/fic.json")):
            # чужая папка: с её id упали бы _fic() и списки глав
            if not path.parent.name.isdecimal() or path.parent.name in skip:
                continue
            try:
                fics.append(self._read_fic(path))
            except OSError, ValueError:
                log.warning("fic.json не прочитан: %s", path, exc_info=True)
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
            merge = data.pop("last_merge", None)
            if merge is not None:
                choices = [MergeChoice(**c) for c in merge.pop("choices", [])]
                merge = LastMerge(**merge, choices=choices)
            # лишний ключ - повреждение: иначе запись потеряла бы поля новой версии
            chapter = ChapterMeta(**data, last_merge=merge, versions=versions)
            _check_chapter(chapter)
            return chapter
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            raise ValueError(f"Файл {path} повреждён") from e

    def load_chapter(self, fic_id: str, chapter_id: str, title: str) -> ChapterMeta:
        """Нет chapter.json - пустая история; повреждённый - ValueError, не затираем."""
        try:
            return self.read_chapter(fic_id, chapter_id)
        except FileNotFoundError:
            return ChapterMeta(title)

    def write_chapter(self, fic_id: str, chapter_id: str, chapter: ChapterMeta) -> None:
        # нет поля и null читаются одинаково - как значение по умолчанию
        data = asdict(
            chapter, dict_factory=lambda kv: {k: v for k, v in kv if v is not None}
        )
        _write(self._chapter(fic_id, chapter_id) / "chapter.json", _dump(data))

    def set_title(self, fic_id: str, chapter_id: str, title: str) -> None:
        chapter = self.load_chapter(fic_id, chapter_id, title)
        self.write_chapter(fic_id, chapter_id, replace(chapter, title=title))

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
        author_name: str | None = None,
    ) -> Version:
        """Снимок text в versions/ и запись о нём в chapter.json.

        created по умолчанию - текущее время; у ревизий сайта - дата с сайта.
        """
        # до снимка: при повреждённом chapter.json не будет лишнего файла
        chapter = self.load_chapter(fic_id, chapter_id, title)
        now = datetime.now()  # noqa: DTZ005 локальное время без пояса, как даты на сайте
        versions = self._chapter(fic_id, chapter_id) / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        stem = now.strftime("%Y-%m-%dT%H-%M-%S")
        file, n = f"{stem}.txt", 1
        # несколько снимков за секунду: синхронизация скачивает ревизии пачкой
        # "x" занимает имя атомарно, os.replace затёр бы чужой снимок
        while True:
            try:
                (versions / file).open("x").close()
                break
            except FileExistsError:
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
            author_name=author_name,
        )
        chapter.versions.append(version)
        self.write_chapter(fic_id, chapter_id, chapter)
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
            for p in self._fic(fic_id).glob("new-*/draft.txt")
            if p.parent.name[4:].isdecimal()
        ]
        # по номеру: new-10 после new-2
        paths.sort(key=lambda p: int(p.parent.name[4:]))
        return [self._slim(fic_id, p.parent.name) for p in paths]

    def local_chapters(
        self, fic_id: str, skip: Container[str] = ()
    ) -> list[SlimChapterInfo]:
        """Главы с сайта, у которых есть черновик, кроме skip; название - как у new_chapters."""
        # skip отсекаем до _slim: чтение chapter.json - основная цена
        paths = [
            p
            for p in self._fic(fic_id).glob("*/draft.txt")
            if p.parent.name.isdecimal() and p.parent.name not in skip
        ]
        paths.sort(key=lambda p: int(p.parent.name))
        return [self._slim(fic_id, p.parent.name) for p in paths]

    def _slim(self, fic_id: str, chapter_id: str) -> SlimChapterInfo:
        try:
            title = self.read_chapter(fic_id, chapter_id).title
        except OSError, ValueError:
            title = chapter_id
        return SlimChapterInfo(chapter_id, title)

    def trash_chapter(self, fic_id: str, chapter_id: str) -> Path:
        """Папку главы - в trash/<fic_id>/: файлы не стираются, вернуть можно вручную."""
        return _trash(
            self._chapter(fic_id, chapter_id), self.trash / fic_id, chapter_id
        )

    def trash_fic(self, fic_id: str) -> Path:
        """Папку работы целиком, с fic.json и new-N, - в trash/."""
        return _trash(self._fic(fic_id), self.trash, fic_id)

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
        fic, role = self.read_fic(fic_id)
        # глава уже есть, если список успели обновить с сайта
        if all(ch.id != new_id for ch in fic.chapters):
            fic.chapters.append(SlimChapterInfo(new_id, title))
        self.write_fic(fic, role)
        self._chapter(fic_id, old_id).rename(new)
