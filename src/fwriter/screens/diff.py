import re
import time
from difflib import SequenceMatcher
from typing import ClassVar

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, Label, OptionList, Static

from ..core.markup import tweak_kind
from ..core.storage import ChapterMeta, ChapterStorage, Version
from .api_errors import report_disk_error
from .widgets import CONFIRM_DELAY

# слова, невидимые символы из _SHOW_BLANK и переводы строк
_TOKEN = re.compile(r"[ \t\r\xa0]+|\n|[^ \t\r\xa0\n]+")
# изменённые невидимые символы рисуются знаками, иначе правку не видно
_SHOW_BLANK = str.maketrans({" ": "·", "\t": "→", "\r": "␍", "\xa0": "°"})

SOURCES = {"manual": "черновик", "publish": "отправка"}
FILTERS = ("все правки", "без правок оформления")

type Block = tuple[int, list[str], list[str], str | None]


def diff_text(old: list[str], new: list[str], mark: Style | str = "reverse") -> Text:
    """Строки new, слова, которых нет в old, - стилем mark; изменённые пробелы видны."""
    out = Text()
    # сначала по абзацам: пословно по всему блоку - минуты на сотне абзацев
    sm = SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if j1 and j2 > j1:
            out.append("\n", mark if i1 == i2 else None)
        b = _TOKEN.findall("\n".join(new[j1:j2]))
        if tag == "equal":
            out.append("".join(b))
            continue
        a = _TOKEN.findall("\n".join(old[i1:i2]))
        words = SequenceMatcher(None, a, b, autojunk=False)
        for t, _, _, k1, k2 in words.get_opcodes():
            part = "".join(b[k1:k2])
            if t != "equal" and not part.strip(" \t\r\xa0\n"):
                part = part.translate(_SHOW_BLANK)
            out.append(part, None if t == "equal" else mark)
    return out


def diff_blocks(old: list[str], new: list[str]) -> list[Block]:
    """Изменённые блоки абзацев: (абзац в new, было, стало, пометка tweak_kind)."""
    sm = SequenceMatcher(None, old, new, autojunk=False)
    return [
        (j1, old[i1:i2], new[j1:j2], tweak_kind(old[i1:i2], new[j1:j2], last))
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
        for last in [i2 == len(old) and j2 == len(new)]
    ]


def rev_line(num: int | None, author: str | None, created: str, comment: str) -> str:
    """Правка сайта или версия: номер, автор, дата, комментарий."""
    num_part = None if num is None else f"Правка номер {num}"
    # у версий сайта created до секунды, в строке - до минуты
    parts = (num_part, author, created[:16].replace("T", " "), comment)
    return " · ".join(p for p in parts if p)


def version_line(v: Version) -> str:
    # у своих версий вместо автора - откуда снимок
    author = v.author_name or SOURCES.get(v.source)
    return rev_line(v.site_num, author, v.created, v.comment)


def version_cells(v: Version) -> tuple[str, str, str, str]:
    """Колонки списка версий: что, автор, дата, комментарий."""
    what = SOURCES.get(v.source, "") if v.site_num is None else f"правка {v.site_num}"
    return what, v.author_name or "", v.created[:16].replace("T", " "), v.comment


def aligned(rows: list[tuple[str, ...]]) -> list[Text]:
    """Строки с колонками одной ширины: разделители друг под другом."""
    widths = [max(cell_len(r[k]) for r in rows) for k in range(len(rows[0]) - 1)]
    out = []
    for row in rows:
        line = Text()
        for k, cell in enumerate(row):
            if k:
                line.append(" · " if cell else "   ", "dim")
            line.append(cell)
            if k < len(widths):
                line.append(" " * (widths[k] - cell_len(cell)))
        out.append(line)
    return out


class ParagraphDiff(Static):
    """Блок абзацев с подписью в рамке; изменённые слова подсвечены относительно old."""

    # цвет подсветки задаёт CSS экрана
    COMPONENT_CLASSES: ClassVar[set[str]] = {"paragraph-diff--mark"}

    DEFAULT_CSS = """
    ParagraphDiff > .paragraph-diff--mark {
        text-style: reverse;
    }
    """

    def show(self, label: str, old: list[str] | None, new: list[str]) -> None:
        """old None - без подсветки; до монтирования - откладывается до on_mount."""
        self.border_title = label
        self.sides = old, new
        if self.is_mounted:
            self.paint()

    def on_mount(self) -> None:
        if hasattr(self, "sides"):
            self.paint()

    def paint(self) -> None:
        old, new = self.sides
        if not any(new):
            text = Text("(пусто)", "dim italic")
        elif old is None:
            text = Text("\n".join(new))
        else:
            mark = self.get_component_rich_style("paragraph-diff--mark", partial=True)
            text = diff_text(old, new, mark)
        self.update(text)


class CommentScreen(ModalScreen[str | None]):
    """Комментарий к версии: Enter - текст (можно пустой), Esc - None."""

    CSS_PATH = "../tcss/menu.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="menu"):
            yield Label("Комментарий к версии (можно без него):", id="menu_title")
            yield Input()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class DiffScreen(Screen[Version | None]):
    """F3: дифф двух версий; R возвращает версию из "Было" для восстановления."""

    CSS_PATH = "../tcss/diff.tcss"
    # фокус в "Было": ↑↓ листают версии, ←→ ходят по блокам
    AUTO_FOCUS = "#diff_old"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("down,right", "step(1)", "Правки", key_display="↑↓"),
        Binding("up,left", "step(-1)", show=False),
        # буквы и в русской раскладке
        Binding("f,а", "filter", "Фильтр", key_display="f"),
        Binding("r,к", "restore", "Восстановить", key_display="r"),
        Binding("s,ы", "save", "Сохранить версию", key_display="s"),
        Binding("pagedown", "scroll(1)", show=False),
        Binding("pageup", "scroll(-1)", show=False),
        Binding("escape", "dismiss", "Закрыть"),
    ]

    def __init__(
        self,
        storage: ChapterStorage,
        fic_id: str,
        chapter_id: str,
        meta: ChapterMeta,
        draft: str,
        editable: bool,
    ) -> None:
        """draft - черновик на диске; editable - доступны R и S."""
        self.storage = storage
        self.fic_id = fic_id
        self.chapter_id = chapter_id
        self.meta = meta
        self.draft = draft
        self.editable = editable
        # файл версии; None - черновик
        latest = meta.versions[-1].file if meta.versions else None
        # base из повреждённого chapter.json может не быть среди версий
        known = any(v.file == meta.base for v in meta.versions)
        self.old: str | None = meta.base if known else latest
        self.new: str | None = None
        self.texts: dict[str | None, list[str]] = {None: draft.split("\n")}
        self.blocks: list[Block] = []
        # без id: remove_children() снимает старые блоки не сразу
        self.shown: dict[int, Vertical] = {}
        self.filter = 0
        self.current = 0
        self.armed: float | None = None
        # версия не прочиталась: пусто не потому, что версии совпадают
        self.unread = False
        super().__init__()

    @property
    def where(self) -> str:
        return f"{self.fic_id}/{self.chapter_id}"

    @property
    def versions(self) -> list[Version]:
        """От новой к старой."""
        return self.meta.versions[::-1]

    def compose(self) -> ComposeResult:
        yield Label(id="diff_head")
        with Horizontal(id="diff_pick"):
            for id, label in (("diff_old", "Было"), ("diff_new", "Стало")):
                versions = OptionList(id=id)
                versions.border_title = label
                yield versions
        yield VerticalScroll(id="diff_body", can_focus=False)
        yield Footer()

    def on_mount(self) -> None:
        self.fill()
        self.compare()

    def fill(self, keep_scroll: bool = False) -> None:
        """keep_scroll - не прокручивать списки к выбору (версия добавилась сама)."""
        rows = [("Черновик", "", "", ""), *(version_cells(v) for v in self.versions)]
        lines = aligned(rows)
        files = [None, *(v.file for v in self.versions)]
        for id, file in (("#diff_old", self.old), ("#diff_new", self.new)):
            versions = self.query_one(id, OptionList)
            y = versions.scroll_y
            with versions.prevent(OptionList.OptionHighlighted):
                versions.set_options(lines)
                versions.highlighted = files.index(file)
            if keep_scroll:
                # после прокрутки к выбору, которую запускает highlighted
                versions.call_after_refresh(
                    versions.scroll_to, y=y, animate=False, immediate=True
                )

    def add(self, version: Version, at: int | None = None) -> None:
        """Новая версия в списки; выбор и прокрутка в "Было" и "Стало" остаются.

        at - место в versions, как у add_version(); по умолчанию в конец.
        """
        if at is None:
            self.meta.versions.append(version)
        else:
            self.meta.versions.insert(at, version)
        self.fill(keep_scroll=True)

    def text(self, file: str | None) -> list[str]:
        if file not in self.texts:
            assert file is not None
            text = self.storage.read_version(self.fic_id, self.chapter_id, file)
            self.texts[file] = text.split("\n")
        return self.texts[file]

    @on(OptionList.OptionHighlighted)
    def picked(self, event: OptionList.OptionHighlighted) -> None:
        i = event.option_index
        file = None if i == 0 else self.versions[i - 1].file
        if event.option_list.id == "diff_old":
            self.old = file
        else:
            self.new = file
        self.armed = None
        self.compare()

    def compare(self) -> None:
        self.unread = False
        try:
            old, new = self.text(self.old), self.text(self.new)
        except (OSError, ValueError) as e:
            self.unread = True
            report_disk_error(self, e, "Не удалось прочитать версию", self.where)
            old = new = []
        self.blocks = diff_blocks(old, new)
        # первая правка, видимая в фильтре
        self.current = (self.filtered() or [0])[0]
        self.show()

    def filtered(self) -> list[int]:
        return [
            i for i, b in enumerate(self.blocks) if self.filter == 0 or b[3] is None
        ]

    def show_head(self) -> None:
        visible = self.filtered()
        head = f"Дифф: {self.meta.title}"
        if self.current in visible:
            head += f" · правка {visible.index(self.current) + 1} из {len(visible)}"
        head += f" · фильтр: {FILTERS[self.filter]}"
        self.query_one("#diff_head", Label).update(Text(head))

    def show(self) -> None:
        self.show_head()
        body = self.query_one("#diff_body", VerticalScroll)
        body.remove_children()
        self.shown = {}
        visible = self.filtered()
        if not visible:
            if self.unread:
                empty = "Версию не удалось прочитать"
            elif self.blocks:
                empty = "В этом фильтре правок нет"
            else:
                empty = "Версии совпадают"
            body.mount(Label(empty, classes="diff-empty"))
            return
        for i in visible:
            start, old, new, mark = self.blocks[i]
            note = f"абзац {start + 1}" + (f" · {mark}" if mark else "")
            was = ParagraphDiff(classes="diff-old")
            now = ParagraphDiff(classes="diff-new")
            block = Vertical(
                Label(note, classes="diff-note"),
                was,
                now,
                classes="diff-block",
            )
            block.set_class(i == self.current, "-current")
            was.show("Было", new, old)
            now.show("Стало", old, new)
            self.shown[i] = block
            body.mount(block)

    def action_step(self, delta: int) -> None:
        visible = self.filtered()
        if not visible:
            return
        k = visible.index(self.current) + delta
        self.shown[self.current].remove_class("-current")
        self.current = visible[max(0, min(k, len(visible) - 1))]
        block = self.shown[self.current]
        block.add_class("-current")
        self.show_head()
        self.query_one("#diff_body", VerticalScroll).scroll_to_widget(
            block, animate=False, top=True
        )

    def action_filter(self) -> None:
        self.filter = (self.filter + 1) % len(FILTERS)
        visible = self.filtered()
        if visible and self.current not in visible:
            after = [i for i in visible if i > self.current]
            self.current = after[0] if after else visible[-1]
        self.show()

    def action_scroll(self, direction: int) -> None:
        body = self.query_one("#diff_body", VerticalScroll)
        if direction > 0:
            body.scroll_page_down()
        else:
            body.scroll_page_up()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action in ("restore", "save"):
            return self.editable
        return True

    def action_restore(self) -> None:
        version = next((v for v in self.versions if v.file == self.old), None)
        if version is None:
            self.notify('В "Было" выбран черновик: восстанавливать нечего')
            return
        armed, self.armed = self.armed, None
        if armed is None or time.monotonic() - armed > CONFIRM_DELAY:
            self.armed = time.monotonic()
            self.notify(
                "Нажмите R ещё раз, чтобы восстановить эту версию в черновик",
                timeout=CONFIRM_DELAY,
            )
            return
        self.dismiss(version)

    def action_save(self) -> None:
        def saved(comment: str | None) -> None:
            if comment is None:
                return
            try:
                v = self.storage.add_version(
                    self.fic_id,
                    self.chapter_id,
                    self.draft,
                    self.meta.title,
                    "manual",
                    comment,
                )
            except (OSError, ValueError) as e:
                report_disk_error(self, e, "Не удалось сохранить версию", self.where)
                return
            self.add(v)
            self.notify("Черновик сохранён версией")

        self.app.push_screen(CommentScreen(), saved)


__all__ = [
    "DiffScreen",
    "ParagraphDiff",
    "diff_blocks",
    "diff_text",
    "rev_line",
    "version_cells",
    "version_line",
]
