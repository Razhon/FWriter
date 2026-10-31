import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import ClassVar

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Label, OptionList, TabPane
from textual.widgets.option_list import Option

from ..core.api.client import FicbookAPI
from ..core.api.errors import FicbookAPIError, NetworkError, UserNotLoggedInError
from ..core.api.models import Fic, Role, SlimChapterInfo, can_manage
from ..core.catalog import plan_fic
from ..core.storage import ChapterMeta, ChapterStorage
from .api_errors import report_api_error, report_disk_error
from .settings import SettingsScreen
from .widgets import CONFIRM_DELAY, ClickableLabel

log = logging.getLogger(__name__)

# роль, раздел полностью (для ошибок) и коротко (для списка и плашки)
FIC_SECTIONS: list[tuple[Role, str, str]] = [
    ("author", "Ваши фанфики", "Ваши"),
    ("coauthor", "Фанфики доступные вам, как соавтору или сопереводчику", "Соавтор"),
    ("beta", "Фанфики доступные вам, как бете", "Бета"),
    ("gamma", "Фанфики доступные вам, как гамме", "Гамма"),
]
GONE_SECTION = "Нет на сайте"

# без сессии и без связи следующие запросы упадут так же
STOP_LOADING = (UserNotLoggedInError, NetworkError)

# id строк в списке глав, которые не главы; у глав id - цифры или new-N
NEW_ID = "+new"
TRASH_FIC_ID = "+trash"

RECENT_SHOWN = 3


def chapters_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} глава"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} главы"
    return f"{n} глав"


@dataclass
class ChapterRow:
    chapter: SlimChapterInfo
    on_site: bool
    mark: str = ""
    # название из chapter.json читается один раз: список перестраивается на каждый шаг курсора
    titled: bool = False
    local: tuple[str, bool] | None = None


@dataclass
class FicEntry:
    """Работа в списке; gone_fic - её нет на сайте."""

    fic: Fic
    role: Role
    rows: list[ChapterRow]
    gone_fic: bool = False

    @property
    def key(self) -> str:
        """id строки в списке работ: одна работа бывает в нескольких разделах."""
        return f"{'gone' if self.gone_fic else self.role}/{self.fic.id}"

    def row(self, chapter_id: str | None) -> ChapterRow | None:
        return next((r for r in self.rows if r.chapter.id == chapter_id), None)


def fic_entry(
    fic: Fic,
    role: Role,
    gone: list[SlimChapterInfo] | None = None,
    new: list[SlimChapterInfo] | None = None,
    gone_fic: bool = False,
) -> FicEntry:
    """gone - черновики глав, удалённых на сайте; new - неотправленные new-N.

    gone_fic - работы нет на сайте: удалить можно любую главу и работу целиком.
    """
    # главу с сайта удалить нельзя: синхронизация скачала бы её снова
    rows = [ChapterRow(ch, on_site=not gone_fic) for ch in fic.chapters]
    rows += [ChapterRow(ch, False, " (нет на сайте)") for ch in gone or []]
    rows += [ChapterRow(ch, False, " (не отправлена)") for ch in new or []]
    return FicEntry(fic, role, rows, gone_fic)


class FicList(OptionList):
    """Работы; буквы открывают поиск внизу блока, фокус остаётся здесь."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("right,enter", "chapters", "Главы", key_display="→"),
        Binding("escape", "close_search", "Сбросить поиск", key_display="Esc"),
    ]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action == "close_search":
            return bool(self.query_ancestor(StartPageTab).needle)
        return True

    def action_chapters(self) -> None:
        chapters = self.query_ancestor(StartPageTab).query_one(ChapterList)
        if chapters.option_count:
            chapters.focus()

    def action_close_search(self) -> None:
        self.query_ancestor(StartPageTab).set_search("")

    def on_key(self, event: events.Key) -> None:
        tab = self.query_ancestor(StartPageTab)
        if event.key == "backspace" and tab.needle:
            tab.set_search(tab.needle[:-1])
        # цифры только внутри поиска: пока он пуст, 1-3 - "Продолжить"
        elif event.character and (
            event.character.isalpha()
            or (tab.needle and (event.character == " " or event.character.isdigit()))
        ):
            tab.set_search(tab.needle + event.character)
        else:
            return
        event.stop()


class ChapterList(OptionList):
    """Главы выбранной работы; Enter открывает, x дважды - в корзину."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "select", "Открыть"),
        Binding("left,escape", "back", "Работы", key_display="←"),
        # вторые клавиши - те же места в русской раскладке
        Binding("x,ч", "trash", "Удалить", key_display="x"),
        Binding("n,т", "new", "Новая глава", key_display="n"),
    ]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # False прячет подсказку в footer
        tab = self.query_ancestor(StartPageTab)
        if action == "trash":
            return tab.can_trash()
        if action == "new":
            return tab.shown is not None and tab.can_create(tab.shown)
        return True

    def action_back(self) -> None:
        self.query_ancestor(StartPageTab).query_one(FicList).focus()

    async def action_trash(self) -> None:
        await self.query_ancestor(StartPageTab).press_trash()

    async def action_new(self) -> None:
        tab = self.query_ancestor(StartPageTab)
        if tab.shown is not None:
            await tab.new_chapter(tab.shown)


class StartPageTab(TabPane):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f7", "settings", "Настройки"),
        Binding("1", "recent(0)", "Продолжить", key_display="1-3"),
        Binding("2", "recent(1)", "Продолжить", show=False),
        Binding("3", "recent(2)", "Продолжить", show=False),
    ]

    class Relogin(Message):
        """Повторить вход из офлайна; обрабатывает App."""

    class GoOffline(Message):
        """Связь пропала посреди загрузки списков; обрабатывает App."""

    class OpenChapter(Message):
        """Выбрана глава; открывает MainScreen."""

        def __init__(
            self, fic: Fic, chapter: SlimChapterInfo, role: Role, on_site: bool
        ) -> None:
            self.fic = fic
            self.chapter = chapter
            self.role: Role = role
            self.on_site = on_site
            super().__init__()

    def __init__(
        self,
        api: FicbookAPI | None,
        storage: ChapterStorage,
        is_open: Callable[[str, str | None], bool],
        settings: dict[str, bool],
    ) -> None:
        """api None - офлайн: список работ из fic.json.

        is_open(fic_id, chapter_id) - открыта ли вкладка главы; chapter_id None - любой.
        settings - флажки автозамены, общие со вкладками глав.
        """
        self.api = api
        self.storage = storage
        self.is_open = is_open
        self.settings = settings
        # одно уведомление на загрузку, а не на каждый fic.json
        self.write_failed = False
        self.entries: list[FicEntry] = []
        self.shown: FicEntry | None = None
        self.needle = ""
        self.recent: list[tuple[FicEntry, ChapterRow]] = []
        # show_recent ждёт UI: два вызова подряд смонтировали бы кнопки дважды
        self.recent_lock = asyncio.Lock()
        # строка, ждущая второго x, и её прежняя надпись
        self.armed: tuple[str, Content] | None = None
        self.disarm_timer: Timer | None = None
        # куда вернуть фокус, когда на вкладку приходят снова; на запуске - AUTO_FOCUS
        self.last_list: FicList | ChapterList | None = None
        super().__init__("Начало работы", id="start_page")

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # только списки: при скрытии вкладки фокус уходит на соседнюю кнопку
        if isinstance(event.widget, FicList | ChapterList):
            self.last_list = event.widget

    def focus_list(self) -> None:
        # set_focus сразу, а не focus(): иначе Hide отдаёт фокус соседней кнопке
        self.screen.set_focus(self.last_list or self.query_one(FicList))

    def compose(self) -> ComposeResult:
        with Horizontal(id="start_head"):
            if self.api:
                yield Label("Привет, гость!", id="greeting")
                yield Label("Загружаем список работ…", id="loading")
            else:
                yield Label("Офлайн: список работ с последнего входа", id="greeting")
                yield ClickableLabel("Повторить вход", classes="relogin_btn")
            yield ClickableLabel(
                Content.assemble("Настройки ", ("F7", "bold $accent")),
                classes="settings_btn",
            )
        yield Horizontal(Label("Продолжить:", classes="recent_head"), id="recent")
        with Horizontal(id="start_main"):
            with Vertical(id="start_left"):
                fics = FicList(id="start_list")
                fics.border_title = "Работы"
                fics.border_subtitle = "начните вводить для поиска"
                yield fics
                search = Label(id="fic_search")
                search.border_title = "Поиск"
                search.border_subtitle = "Esc - сбросить"
                yield search
            with Vertical(id="start_right"):
                yield Horizontal(
                    Label(id="fic_role"),
                    Label(id="fic_title"),
                    Label(id="fic_meta"),
                    id="fic_status",
                )
                chapters = ChapterList(id="fic_chapters")
                chapters.border_title = "Главы"
                chapters.border_subtitle = "Enter - открыть"
                yield chapters

    def on_mount(self) -> None:
        self.load_start_page()

    # воркер отменяется при удалении вкладки и до закрытия клиента в MainScreen.on_unmount
    @work(exclusive=True, group="start_page")
    async def load_start_page(self) -> None:
        """Ник и списки фанфиков; ошибка одного списка не прячет остальные."""
        try:
            if self.api is None:
                await self.load_offline()
            else:
                await self.load_online(self.api)
            # главы для "Продолжить" ищутся в загруженных списках
            await self.show_recent()
        finally:
            # и при ошибке, и при отмене воркера
            self.query("#loading").remove()

    async def load_online(self, api: FicbookAPI) -> None:
        try:
            # после входа ник уже есть, запроса не будет
            user = await api.get_user_info()
            self.query_one("#greeting", Label).update(Content(f"Привет, {user.name}!"))
        except FicbookAPIError as e:
            report_api_error(self, e, "Не удалось загрузить профиль")
            if self.stop_loading(e):
                return

        seen: set[str] = set()
        # без какого-то списка не понять, какие работы пропали с сайта
        all_loaded = True
        # списки одновременно: 4 запроса укладываются в лимит частоты клиента;
        # ответы разбираются по порядку, как раньше
        results = await asyncio.gather(
            *(api.get_fics(role) for role, _, _ in FIC_SECTIONS),
            return_exceptions=True,
        )
        for (role, label, _), fics in zip(FIC_SECTIONS, results, strict=True):
            if isinstance(fics, BaseException):
                if not isinstance(fics, FicbookAPIError):
                    raise fics
                report_api_error(self, fics, f"Не удалось загрузить список «{label}»")
                if self.stop_loading(fics):
                    return
                all_loaded = False
                continue
            seen.update(fic.id for fic in fics)
            entries: list[FicEntry] = []
            for fic in fics:
                readonly: bool | None = fic.readonly
                stored, broken = None, False
                # есть главы в карточке - есть право; без глав различает только /parts
                if not fic.chapters:
                    stored, broken = self.stored_fic(fic.id)
                    try:
                        readonly = await api.is_readonly(fic.id)
                    except FicbookAPIError as e:
                        # раздел не прячем: работа из fic.json, без него - без прав
                        report_api_error(
                            self, e, f"Не удалось проверить права на «{fic.title}»"
                        )
                        if self.stop_loading(e):
                            return
                        readonly = None
                plan = plan_fic(fic, readonly, stored, broken)
                if plan.broken_warning:
                    self.notify(
                        f"Файл работы «{fic.title}» повреждён, список глав не показан",
                        severity="error",
                        markup=False,
                    )
                if plan.save:
                    self.save_fic(plan.fic, role)
                entries.append(self.entry(plan.fic, role, plan.find_gone))
            self.add_entries(entries)

        if all_loaded:
            self.show_gone(seen)

    def stop_loading(self, e: FicbookAPIError) -> bool:
        """Дальше грузить бессмысленно; без связи - офлайн-список вместо пустого."""
        if isinstance(e, NetworkError):
            self.post_message(StartPageTab.GoOffline())
        return isinstance(e, STOP_LOADING)

    async def load_offline(self) -> None:
        """Разделы из fic.json - списка с последнего входа."""
        stored, errors = self.storage.read_fics()
        if errors:
            self.notify(
                "\n".join(errors),
                title="Часть работ не показана",
                severity="error",
                markup=False,
            )
        # fic.json - последний список с сайта
        self.add_entries(
            [self.entry(fic, role, find_gone=not fic.readonly) for fic, role in stored]
        )

    def add_entries(self, entries: list[FicEntry]) -> None:
        self.entries += entries
        self.refresh_fics()

    def entry(self, fic: Fic, role: Role, find_gone: bool) -> FicEntry:
        """Работа вместе с локальными главами: удалёнными на сайте и new-N."""
        gone = self.gone_chapters(fic) if find_gone else []
        return fic_entry(fic, role, gone, self.storage.new_chapters(fic.id))

    def find_entry(self, key: str | None) -> FicEntry | None:
        return next((e for e in self.entries if e.key == key), None)

    def fic_options(self) -> list[Option | None]:
        """Работы по разделам, с поиском: совпадение подсвечено."""
        needle = self.needle.lower()
        groups = [
            (short, [e for e in self.entries if e.role == role and not e.gone_fic])
            for role, _, short in FIC_SECTIONS
        ]
        groups.append((GONE_SECTION, [e for e in self.entries if e.gone_fic]))
        options: list[Option | None] = []
        for short, entries in groups:
            found = [e for e in entries if needle in e.fic.title.lower()]
            if not found:
                continue
            if options:
                options.append(None)
            head = f"{short.upper()}  {len(found)}"
            options.append(
                Option(Content.styled(head, "bold $foreground-muted"), disabled=True)
            )
            for e in found:
                title = Content(e.fic.title)
                if needle:
                    i = e.fic.title.lower().index(needle)
                    title = title.stylize("bold reverse", i, i + len(needle))
                note = f"  {len(e.rows)}" + ("  чтение" if e.fic.readonly else "")
                options.append(
                    Option(
                        Content.assemble(title, (note, "$foreground-muted")),
                        id=e.key,
                    )
                )
        if needle and not options:
            text = Content.styled("ничего не найдено", "$foreground-muted")
            options.append(Option(text, disabled=True))
        return options

    def refresh_fics(self) -> None:
        """Список работ заново; курсор - на той же работе или на первой."""
        fics = self.query_one(FicList)
        current = fics.highlighted_option.id if fics.highlighted_option else None
        options = self.fic_options()
        fics.set_options(options)
        ids = [o.id for o in options if o and o.id]
        key = current if current in ids else next(iter(ids), None)
        if key is not None:
            fics.highlighted = fics.get_option_index(key)
        # set_options не шлёт Highlighted, если номер строки не изменился
        self.show_fic(self.find_entry(key))
        fics.refresh_bindings()

    def set_search(self, needle: str) -> None:
        self.needle = needle
        search = self.query_one("#fic_search", Label)
        search.update(Content(needle))
        search.set_class(bool(needle), "-open")
        fics = self.query_one(FicList)
        fics.border_subtitle = "" if needle else "начните вводить для поиска"
        self.refresh_fics()

    @on(OptionList.OptionHighlighted, "FicList")
    def on_fic_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.show_fic(self.find_entry(event.option.id))

    def show_fic(self, entry: FicEntry | None) -> None:
        """Главы работы справа; у той же работы курсор остаётся на той же главе."""
        self.disarm()
        chapters = self.query_one(ChapterList)
        same = (
            entry is not None and self.shown is not None and self.shown.key == entry.key
        )
        keep = chapters.highlighted_option.id if chapters.highlighted_option else None
        self.shown = entry
        role, title, meta, chip_class = "", "", "", ""
        options: list[Option] = []
        if entry is not None:
            role = next(s for r, _, s in FIC_SECTIONS if r == entry.role)
            chip_class = f"-{entry.role}"
            title = entry.fic.title
            meta = chapters_word(len(entry.rows))
            if entry.gone_fic:
                role, chip_class = GONE_SECTION, "-gone"
                meta += " · работа удалена или у вас больше нет к ней доступа"
            if entry.fic.readonly:
                meta += " · только чтение"
            options = self.chapter_options(entry)
        chip = self.query_one("#fic_role", Label)
        chip.update(role)
        chip.set_classes(chip_class)
        self.query_one("#fic_title", Label).update(Content(title))
        self.query_one("#fic_meta", Label).update(Content(meta))
        chapters.set_options(options)
        if options:
            ids = [o.id for o in options]
            chapters.highlighted = ids.index(keep) if same and keep in ids else 0
        chapters.refresh_bindings()

    def chapter_label(self, entry: FicEntry, row: ChapterRow) -> Content:
        """Название из chapter.json, если глава уже скачана, и пометки."""
        ch = row.chapter
        if not row.titled:
            row.local = self.storage.shown_title(entry.fic.id, ch.id)
            row.titled = True
        title, pending = row.local or (ch.title, False)
        note = " (название не отправлено)" if pending else ""
        if ch.draft:
            note += " (не опубликовано)"
        # не $text-muted: в спане Content его "auto 60%" становится чистым белым
        return Content.assemble(title + row.mark, (note, "$foreground-muted"))

    def chapter_options(self, entry: FicEntry) -> list[Option]:
        options = [
            Option(
                Content.assemble(
                    (f"{n:>2}  ", "$foreground-muted"), self.chapter_label(entry, row)
                ),
                id=row.chapter.id,
            )
            for n, row in enumerate(entry.rows, 1)
        ]
        if entry.gone_fic:
            text = Content.styled("    Удалить работу", "$text-error")
            options.append(Option(text, id=TRASH_FIC_ID))
        if self.can_create(entry):
            text = Content.styled("    + Новая глава", "bold $accent")
            options.append(Option(text, id=NEW_ID))
        return options

    def can_create(self, entry: FicEntry) -> bool:
        return can_manage(entry.role, entry.fic.readonly)

    @on(OptionList.OptionHighlighted, "ChapterList")
    def on_chapter_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.disarm()
        # x в footer - только у глав, которые можно удалить
        event.option_list.refresh_bindings()

    @on(OptionList.OptionSelected, "ChapterList")
    async def on_chapter_selected(self, event: OptionList.OptionSelected) -> None:
        entry = self.shown
        # второе нажатие двойного клика: список уже перестроен
        if entry is None or event.option is not event.option_list.highlighted_option:
            return
        if event.option.id == NEW_ID:
            await self.new_chapter(entry)
        elif event.option.id == TRASH_FIC_ID:
            await self.press_trash()
        elif row := entry.row(event.option.id):
            self.post_message(
                StartPageTab.OpenChapter(
                    entry.fic, row.chapter, entry.role, row.on_site
                )
            )

    @on(OptionList.OptionSelected, "FicList")
    def on_fic_selected(self, event: OptionList.OptionSelected) -> None:
        # клик только выбирает работу; в главы - стрелкой или Enter
        event.stop()

    async def new_chapter(self, entry: FicEntry) -> None:
        if not self.can_create(entry):
            return
        fic, role = entry.fic, entry.role
        # номер - по строкам списка, вместе с удалёнными на сайте и не отправленными
        title = f"Глава {len(entry.rows) + 1}"
        try:
            chapter_id = self.storage.new_chapter_id(fic.id)
            self.storage.write_chapter(fic.id, chapter_id, ChapterMeta(title))
            self.storage.write_draft(fic.id, chapter_id, "")
        except OSError as e:
            report_disk_error(self, e, "Не удалось создать главу", fic.id)
            return
        self.refresh_fic(fic.id)
        chapter = SlimChapterInfo(chapter_id, title)
        self.post_message(StartPageTab.OpenChapter(fic, chapter, role, on_site=False))

    def refresh_fic(
        self,
        fic_id: str,
        added: SlimChapterInfo | None = None,
        readonly: bool = False,
    ) -> None:
        """Список глав работы заново.

        added - глава, только что созданная на сайте; readonly - права выключили сейчас.
        """
        for i, old in enumerate(self.entries):
            if old.fic.id != fic_id or old.gone_fic:
                continue
            fic = replace(old.fic, readonly=True) if readonly else old.fic
            if added is not None and all(ch.id != added.id for ch in fic.chapters):
                fic = replace(fic, chapters=[*fic.chapters, added])
            self.entries[i] = self.entry(fic, old.role, find_gone=not fic.readonly)
        self.refresh_fics()

    async def show_title(self, fic_id: str, chapter_id: str) -> None:
        """Название главы заново: его сохранили во вкладке."""
        for entry in self.entries:
            if entry.fic.id == fic_id and (row := entry.row(chapter_id)):
                row.titled = False
        if self.shown is not None and self.shown.fic.id == fic_id:
            self.show_fic(self.shown)
        await self.show_recent()

    def show_gone(self, seen: set[str]) -> None:
        """Работы из fic.json, которых нет ни в одном списке сайта: черновики остаются."""
        # повреждённые fic.json покажет офлайн-режим, здесь не до них
        stored, _ = self.storage.read_fics(skip=seen)
        gone: list[FicEntry] = []
        for fic, role in stored:
            # без черновика главу уже не скачать; название - из fic.json
            known = {ch.id: ch for ch in fic.chapters}
            chapters = [
                known.get(ch.id, ch) for ch in self.storage.local_chapters(fic.id)
            ]
            new = self.storage.new_chapters(fic.id)
            if chapters or new:
                # readonly: отправлять некуда
                gone.append(
                    fic_entry(
                        Fic(fic.id, fic.title, chapters, True),
                        role,
                        new=new,
                        gone_fic=True,
                    )
                )
        self.add_entries(gone)

    def gone_chapters(self, fic: Fic) -> list[SlimChapterInfo]:
        """Черновики глав, которых нет в списке с сайта: удалены на сайте."""
        return self.storage.local_chapters(fic.id, skip={ch.id for ch in fic.chapters})

    def stored_fic(self, fic_id: str) -> tuple[Fic | None, bool]:
        """Работа из fic.json и признак, что файл есть, но не читается."""
        try:
            return self.storage.read_fic(fic_id)[0], False
        except FileNotFoundError:
            return None, False
        except OSError, ValueError:
            log.warning("fic.json %s не прочитан", fic_id, exc_info=True)
            return None, True

    def save_fic(self, fic: Fic, role: Role) -> None:
        try:
            self.storage.write_fic(fic, role)
        except OSError:
            log.warning("fic.json %s не записан", fic.id, exc_info=True)
            if not self.write_failed:
                self.write_failed = True
                self.notify(
                    "Не удалось сохранить список работ на диск, офлайн он будет неполным",
                    severity="error",
                )

    async def remember(self, fic_id: str, chapter_id: str) -> None:
        """Глава открыта: первой в "Продолжить"."""
        try:
            self.storage.add_recent(fic_id, chapter_id)
        except OSError:
            # только подсказка: без неё работать можно
            log.warning("recent.json не записан", exc_info=True)
            return
        await self.show_recent()

    async def show_recent(self) -> None:
        """Последние открытые главы, которые есть в загруженных списках."""
        async with self.recent_lock:
            await self.build_recent()

    async def build_recent(self) -> None:
        found: list[tuple[FicEntry, ChapterRow]] = []
        for fic_id, chapter_id in self.storage.read_recent():
            for entry in self.entries:
                row = entry.row(chapter_id) if entry.fic.id == fic_id else None
                if row and len(found) < RECENT_SHOWN:
                    found.append((entry, row))
                    break
        bar = self.query_one("#recent", Horizontal)
        await bar.query(".recent_btn").remove()
        # кнопок нет до mount_all, поэтому список и кнопки не расходятся
        self.recent = found
        await bar.mount_all(
            ClickableLabel(
                Content.assemble(
                    (f"{n} ", "bold $accent"),
                    entry.fic.title,
                    " · ",
                    self.chapter_label(entry, replace(row, mark="")),
                ),
                classes="recent_btn",
            )
            for n, (entry, row) in enumerate(found, 1)
        )
        bar.display = bool(self.recent)
        self.refresh_bindings()

    @on(ClickableLabel.Pressed, ".recent_btn")
    def on_recent_pressed(self, event: ClickableLabel.Pressed) -> None:
        self.action_recent(list(self.query(".recent_btn")).index(event.clabel))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action == "recent":
            return len(self.recent) > int(str(parameters[0]))
        return True

    def action_recent(self, index: int) -> None:
        entry, row = self.recent[index]
        self.post_message(
            StartPageTab.OpenChapter(entry.fic, row.chapter, entry.role, row.on_site)
        )

    @on(ClickableLabel.Pressed, ".settings_btn")
    def action_settings(self) -> None:
        self.app.push_screen(SettingsScreen(self.storage, self.settings))

    @on(ClickableLabel.Pressed, ".relogin_btn")
    def on_relogin_pressed(self) -> None:
        self.set_connecting(True)
        self.post_message(StartPageTab.Relogin())

    def set_connecting(self, connecting: bool) -> None:
        """Пока идёт вход, кнопка повтора неактивна: иначе нажатие начинает его заново."""
        btn = self.query_one(".relogin_btn", ClickableLabel)
        btn.update("Подключаемся…" if connecting else "Повторить вход")
        btn.disabled = connecting

    def can_trash(self) -> bool:
        """Удалить можно только то, чего нет на сайте: иначе синхронизация вернёт."""
        opt = self.query_one(ChapterList).highlighted_option
        if self.shown is None or opt is None:
            return False
        row = self.shown.row(opt.id)
        return opt.id == TRASH_FIC_ID or (row is not None and not row.on_site)

    async def press_trash(self) -> None:
        """Первое нажатие спрашивает, второе за CONFIRM_DELAY удаляет."""
        chapters = self.query_one(ChapterList)
        opt = chapters.highlighted_option
        entry = self.shown
        if entry is None or opt is None or opt.id is None or not self.can_trash():
            return
        if self.armed is None or self.armed[0] != opt.id:
            self.disarm()
            prompt = opt.prompt
            assert isinstance(prompt, Content)
            # число глав берём сейчас: часть могли удалить после построения списка
            ask = (
                f"Удалить работу и её главы ({len(entry.rows)})? Нажмите ещё раз"
                if opt.id == TRASH_FIC_ID
                else "Удалить? Нажмите x ещё раз"
            )
            self.armed = (opt.id, prompt)
            chapters.replace_option_prompt(
                opt.id,
                Content.assemble(
                    prompt, "  ", (f" {ask} ", "bold $foreground on $error")
                ),
            )
            self.disarm_timer = self.set_timer(CONFIRM_DELAY, self.disarm)
            return
        self.disarm()
        await self.trash(entry, opt.id)

    def disarm(self) -> None:
        if self.disarm_timer:
            self.disarm_timer.stop()
            self.disarm_timer = None
        if self.armed is None:
            return
        option_id, prompt = self.armed
        self.armed = None
        chapters = self.query_one(ChapterList)
        if option_id in {o.id for o in chapters.options}:
            chapters.replace_option_prompt(option_id, prompt)

    async def trash(self, entry: FicEntry, option_id: str) -> None:
        fic_id = entry.fic.id
        # от работы без глав остался бы только fic.json
        whole = option_id == TRASH_FIC_ID or (entry.gone_fic and len(entry.rows) == 1)
        # None - вся работа
        target = None if whole else option_id
        # иначе автосохранение вкладки создаст черновик заново
        if self.is_open(fic_id, target):
            self.notify("Сначала закройте вкладку главы", severity="warning")
            return
        try:
            if target is None:
                self.storage.trash_fic(fic_id)
            else:
                self.storage.trash_chapter(fic_id, target)
        except OSError as e:
            report_disk_error(
                self, e, "Не удалось перенести в корзину", f"{fic_id}/{target or '*'}"
            )
            return
        if target is None:
            self.entries.remove(entry)
        else:
            entry.rows = [r for r in entry.rows if r.chapter.id != target]
        self.refresh_fics()
        await self.show_recent()
