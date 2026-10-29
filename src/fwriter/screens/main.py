import asyncio
import logging
from functools import partial
from typing import ClassVar

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, Horizontal, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import Screen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    Collapsible,
    Footer,
    Label,
    Rule,
    TabbedContent,
    TabPane,
    Tabs,
)

from ..core.api.client import FicbookAPI
from ..core.api.errors import FicbookAPIError, NetworkError, UserNotLoggedInError
from ..core.api.models import Fic, Role, SlimChapterInfo, can_manage
from ..core.catalog import plan_fic
from ..core.storage import DATA_DIR, ChapterMeta, ChapterStorage
from ..core.sync import sync_chapter
from .api_errors import report_api_error
from .chapter import ChapterTab

log = logging.getLogger(__name__)

FIC_SECTIONS: list[tuple[Role, str]] = [
    ("author", "Ваши фанфики"),
    ("coauthor", "Фанфики доступные вам, как соавтору или сопереводчику"),
    ("beta", "Фанфики доступные вам, как бете"),
    ("gamma", "Фанфики доступные вам, как гамме"),
]

# без сессии и без связи следующие запросы упадут так же
STOP_LOADING = (UserNotLoggedInError, NetworkError)


class ClickableLabel(Label, can_focus=True):
    """Намеренный отдельный класс от Button, так легче"""

    PRESS_DELAY = 0.2

    DEFAULT_CSS = """
    ClickableLabel {
        pointer: pointer;
        &:focus, &:hover {
            text-style: underline;
        }
        &.-active {
            text-style: reverse;
        }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "press", "Press ClickableLabel", show=False)
    ]

    class Pressed(Message):
        def __init__(self, clabel: ClickableLabel) -> None:
            self.clabel: ClickableLabel = clabel
            super().__init__()

        @property
        def control(self) -> ClickableLabel:
            return self.clabel

    # markup выключен по умолчанию, в отличие от Label: названия глав приходят с сайта
    def __init__(
        self, label: str, classes: str | None = None, markup: bool = False
    ) -> None:
        super().__init__(label, classes=classes, markup=markup)

    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        self.action_press()

    def action_press(self) -> None:
        # пока идёт подсветка нажатия, повторные нажатия игнорируются
        if self.disabled or not self.display or self.has_class("-active"):
            return
        self.add_class("-active")
        self.set_timer(self.PRESS_DELAY, partial(self.remove_class, "-active"))
        self.press()

    def press(self) -> None:
        """Сообщение о нажатии; подклассы шлют свои."""
        self.post_message(ClickableLabel.Pressed(self))


class TrashButton(ClickableLabel):
    """Перенос в корзину: первое нажатие спрашивает, второе за CONFIRM_DELAY удаляет.

    chapter None - работа целиком.
    """

    CONFIRM_DELAY = 3.0

    BINDINGS: ClassVar[list[BindingType]] = [Binding("x", "press", "Удалить")]

    class Confirmed(Message):
        """Второе нажатие: перенести в корзину."""

        def __init__(self, button: TrashButton) -> None:
            self.button = button
            super().__init__()

        @property
        def control(self) -> TrashButton:
            return self.button

    def __init__(
        self,
        fic: Fic,
        chapter: SlimChapterInfo | None,
        label: str = "X",
        prompt: str = "Удалить? X",
    ) -> None:
        self.fic = fic
        self.chapter = chapter
        self.idle_label = label
        self.prompt = prompt
        self.reset_timer: Timer | None = None
        super().__init__(label, classes="trash_btn")

    def press(self) -> None:
        """Второе нажатие шлёт Confirmed; первое только меняет надпись."""
        if self.reset_timer:
            self.disarm()
            self.post_message(TrashButton.Confirmed(self))
            return
        self.add_class("-armed")
        # {n} - глав сейчас: часть могли удалить после построения списка
        n = self.query_ancestor(FicCollapsible).chapter_count()
        self.update(self.prompt.format(n=n))
        self.reset_timer = self.set_timer(self.CONFIRM_DELAY, self.disarm)

    def disarm(self) -> None:
        if self.reset_timer:
            self.reset_timer.stop()
            self.reset_timer = None
        self.remove_class("-armed")
        self.update(self.idle_label)


class ChapterButton(ClickableLabel):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("x", "trash", "Удалить")]

    class Open(Message):
        """Нажата глава; открывает MainScreen."""

        def __init__(
            self, fic: Fic, chapter: SlimChapterInfo, role: Role, on_site: bool
        ) -> None:
            self.fic = fic
            self.chapter = chapter
            self.role = role
            self.on_site = on_site
            super().__init__()

    def __init__(
        self,
        fic: Fic,
        chapter: SlimChapterInfo,
        role: Role,
        mark: str = "",
        trash: TrashButton | None = None,
    ) -> None:
        """trash - крестик в строке главы, если её можно удалить."""
        self.fic = fic
        self.chapter = chapter
        self.role: Role = role
        self.trash = trash
        super().__init__(chapter.title + mark, classes="chapter_btn")

    def press(self) -> None:
        # крестик только у глав, которых нет на сайте
        on_site = self.trash is None
        self.post_message(
            ChapterButton.Open(self.fic, self.chapter, self.role, on_site)
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # False прячет подсказку в footer у глав с сайта
        return action != "trash" or self.trash is not None

    def action_trash(self) -> None:
        if self.trash:
            self.trash.action_press()


class FicCollapsible(Collapsible):
    """Работа в списке; gone_fic - её нет на сайте."""

    def __init__(
        self, *children: Widget, fic: Fic, gone_fic: bool, classes: str
    ) -> None:
        self.fic = fic
        self.gone_fic = gone_fic
        super().__init__(
            *children,
            # типизирует title как str, но принимает Content; str разбирается как разметка
            title=Content(fic.title),  # pyright: ignore[reportArgumentType]
            classes=classes,
        )

    def chapter_count(self) -> int:
        return len(self.query(ChapterButton))


def chapter_row(
    fic: Fic, chapter: SlimChapterInfo, role: Role, mark: str = "", trash: bool = False
) -> Widget:
    """Кнопка главы; с trash - в одной строке с крестиком удаления."""
    if not trash:
        return ChapterButton(fic, chapter, role, mark)
    btn = TrashButton(fic, chapter)
    return Horizontal(
        ChapterButton(fic, chapter, role, mark, btn), btn, classes="chapter_row"
    )


def fic_collapsible(
    fic: Fic,
    role: Role,
    gone: list[SlimChapterInfo] | None = None,
    new: list[SlimChapterInfo] | None = None,
    gone_fic: bool = False,
) -> FicCollapsible:
    """gone - черновики глав, удалённых на сайте; new - неотправленные new-N.

    gone_fic - работы нет на сайте: удалить можно любую главу и работу целиком.
    """
    chapters: list[Widget] = [
        chapter_row(fic, ch, role, trash=gone_fic) for ch in fic.chapters
    ]
    chapters += [
        chapter_row(fic, ch, role, " (нет на сайте)", True) for ch in gone or []
    ]
    chapters += [
        chapter_row(fic, ch, role, " (не отправлена)", True) for ch in new or []
    ]
    if gone_fic:
        chapters.append(
            TrashButton(
                fic,
                None,
                "Удалить работу X",
                "Удалить работу и её главы ({n})? Нажмите ещё раз",
            )
        )
    if can_manage(role, fic.readonly):
        chapters.append(
            ClickableLabel(
                "[bold]Новая глава ->[/]",
                classes="new_chapter_btn",
                markup=True,
            )
        )
    return FicCollapsible(
        *chapters, fic=fic, gone_fic=gone_fic, classes=f"{role}_fics_collapsible"
    )


class StartPageTab(TabPane):
    class Relogin(Message):
        """Повторить вход из офлайна; обрабатывает App."""

    def __init__(self, api: FicbookAPI | None, storage: ChapterStorage) -> None:
        """api None - офлайн: список работ из fic.json."""
        self.api = api
        self.storage = storage
        # одно уведомление на загрузку, а не на каждый fic.json
        self.write_failed = False
        super().__init__("Начало работы", id="start_page")

    def compose(self) -> ComposeResult:
        if self.api:
            yield Center(Label("Привет, гость!", id="greeting"))
        else:
            yield Center(
                Label("Офлайн: список работ с последнего входа", id="greeting")
            )
            yield Center(ClickableLabel("Повторить вход", classes="relogin_btn"))
        yield Rule()
        if self.api:
            yield Label("Загружаем список работ…", id="loading")
        yield VerticalScroll(classes="fics_container")

    def on_mount(self) -> None:
        self.load_start_page()

    # воркер отменяется при удалении вкладки и до закрытия клиента в MainScreen.on_unmount
    @work(exclusive=True, group="start_page")
    async def load_start_page(self) -> None:
        """Ник и списки фанфиков; ошибка одного списка не прячет остальные."""
        if self.api is None:
            await self.load_offline()
            return
        try:
            await self.load_online(self.api)
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
            if isinstance(e, STOP_LOADING):
                return

        seen: set[str] = set()
        # без какого-то списка не понять, какие работы пропали с сайта
        all_loaded = True
        # списки одновременно: 4 запроса укладываются в лимит частоты клиента;
        # ответы разбираются по порядку, как раньше
        results = await asyncio.gather(
            *(api.get_fics(role) for role, _ in FIC_SECTIONS), return_exceptions=True
        )
        for (role, label), fics in zip(FIC_SECTIONS, results, strict=True):
            if isinstance(fics, BaseException):
                if not isinstance(fics, FicbookAPIError):
                    raise fics
                report_api_error(self, fics, f"Не удалось загрузить список «{label}»")
                if isinstance(fics, STOP_LOADING):
                    return
                all_loaded = False
                continue
            seen.update(fic.id for fic in fics)
            if not fics:
                continue
            collapsibles: list[Collapsible] = []
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
                        if isinstance(e, STOP_LOADING):
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
                collapsibles.append(self.collapsible(plan.fic, role, plan.find_gone))
            await self.mount_section(label, collapsibles)

        if all_loaded:
            await self.show_gone(seen)

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
        for role, label in FIC_SECTIONS:
            await self.mount_section(
                label,
                [
                    # fic.json - последний список с сайта
                    self.collapsible(fic, role, find_gone=not fic.readonly)
                    for fic, fic_role in stored
                    if fic_role == role
                ],
            )

    async def mount_section(
        self, label: str, collapsibles: list[Collapsible], classes: str = ""
    ) -> None:
        """classes - у разделителя и подписи, чтобы убрать раздел целиком."""
        if not collapsibles:
            return
        fics_container = self.query_one(".fics_container", VerticalScroll)
        if fics_container.children:  # разделитель
            await fics_container.mount(Rule(classes=classes))
        await fics_container.mount(
            Label(f"{label}:", classes=f"fics_collapsibles_label {classes}"),
            *collapsibles,
        )

    def collapsible(self, fic: Fic, role: Role, find_gone: bool) -> Collapsible:
        """Работа вместе с локальными главами: удалёнными на сайте и new-N."""
        gone = self.gone_chapters(fic) if find_gone else []
        return fic_collapsible(fic, role, gone, self.storage.new_chapters(fic.id))

    async def show_gone(self, seen: set[str]) -> None:
        """Работы из fic.json, которых нет ни в одном списке сайта: черновики остаются."""
        # повреждённые fic.json покажет офлайн-режим, здесь не до них
        stored, _ = self.storage.read_fics(skip=seen)
        gone: list[Collapsible] = []
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
                    fic_collapsible(
                        Fic(fic.id, fic.title, chapters, True),
                        role,
                        new=new,
                        gone_fic=True,
                    )
                )
        await self.mount_section(
            "Нет на сайте (работа удалена или у вас больше нет к ней доступа), "
            "локальные черновики только для чтения",
            gone,
            "gone_section",
        )

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

    @on(ClickableLabel.Pressed, ".relogin_btn")
    def on_relogin_pressed(self) -> None:
        self.set_connecting(True)
        self.post_message(StartPageTab.Relogin())

    def set_connecting(self, connecting: bool) -> None:
        """Пока идёт вход, кнопка повтора неактивна: иначе нажатие начинает его заново."""
        btn = self.query_one(".relogin_btn", ClickableLabel)
        btn.update("Подключаемся…" if connecting else "Повторить вход")
        btn.disabled = connecting

    async def on_trash_button_confirmed(self, event: TrashButton.Confirmed) -> None:
        btn = event.button
        fic_id = btn.fic.id
        chapter_id = btn.chapter.id if btn.chapter else None
        collapsible = btn.query_ancestor(FicCollapsible)
        # от работы без глав остался бы только fic.json
        whole = chapter_id is None or (
            collapsible.gone_fic and collapsible.chapter_count() == 1
        )
        # иначе автосохранение вкладки создаст черновик заново
        if self.query_ancestor(MainScreen).is_open(
            fic_id, None if whole else chapter_id
        ):
            self.notify("Сначала закройте вкладку главы", severity="warning")
            return
        try:
            # whole уже включает chapter_id None; проверка сужает тип для pyright
            if chapter_id is None or whole:
                self.storage.trash_fic(fic_id)
            else:
                self.storage.trash_chapter(fic_id, chapter_id)
        except OSError:
            log.warning(
                "%s/%s не перенесена в корзину",
                fic_id,
                "*" if whole else chapter_id,
                exc_info=True,
            )
            self.notify("Не удалось перенести в корзину", severity="error")
            return
        if not whole:
            await btn.query_ancestor(".chapter_row", Horizontal).remove()
            return
        await collapsible.remove()
        if not any(c.gone_fic for c in self.query(FicCollapsible)):
            await self.query(".gone_section").remove()


class MainScreen(Screen):
    CSS_PATH = "../tcss/main.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f1", "start_page", "Начало работы"),
        # priority: у TextArea ctrl+w удаляет слово (это остаётся на ctrl+backspace)
        Binding("ctrl+w", "close_chapter", "Закрыть главу", priority=True),
    ]

    def __init__(
        self, api: FicbookAPI | None, storage: ChapterStorage | None = None
    ) -> None:
        """api None - офлайн: только локальные черновики."""
        self.api = api
        self.storage = storage or ChapterStorage(DATA_DIR)
        # tab_id глав, которые сейчас скачиваются: повторный клик не шлёт второй GET
        self.fetching: set[str] = set()
        super().__init__()

    def compose(self) -> ComposeResult:
        with TabbedContent():
            yield StartPageTab(self.api, self.storage)
        yield Footer()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # на стартовой вкладке F1 некуда переходить, а закрыть её нельзя
        if action in ("start_page", "close_chapter"):
            # до compose вкладок ещё нет
            return any(t.active != "start_page" for t in self.query(TabbedContent))
        return True

    def on_tabbed_content_tab_activated(self) -> None:
        self.refresh_bindings()

    # стартовая вкладка не закрывается, она же выбор глав
    def action_start_page(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = "start_page"
        # фокус из скрытой вкладки главы вернул бы её обратно
        tabbed.query_one(Tabs).focus()

    def on_chapter_button_open(self, message: ChapterButton.Open) -> None:
        self.open_chapter(message.fic, message.chapter, message.role, message.on_site)

    # на экране, а не на вкладке: воркер живёт, пока открыты вкладки глав
    # не exclusive: клик по другой главе не должен отменять открытие первой
    @work(group="open_chapter")
    async def open_chapter(
        self, fic: Fic, chapter: SlimChapterInfo, role: Role, on_site: bool = True
    ) -> None:
        """Вкладка с черновиком; on_site - сначала догнать его до сайта."""
        tabbed = self.query_one(TabbedContent)
        tab_id = ChapterTab.tab_id(fic.id, chapter.id)

        def show() -> None:
            tabbed.active = tab_id
            # фокус с нажатой главы на скрытой стартовой вкладке вернул бы её обратно
            tabbed.query_one(f"#{tab_id}", ChapterTab).editor.focus()

        if tabbed.query(f"#{tab_id}"):
            show()
            return
        if tab_id in self.fetching:
            self.notify("Глава загружается…")
            return
        try:
            text = self.storage.read_draft(fic.id, chapter.id)
        except OSError, ValueError:  # ValueError: не UTF-8
            log.warning("черновик %s/%s не прочитан", fic.id, chapter.id, exc_info=True)
            self.notify("Не удалось прочитать черновик главы с диска", severity="error")
            return
        title = chapter.title
        if self.api is not None and on_site:
            action = (
                "Не удалось скачать главу"
                if text is None
                else "Не удалось обновить главу"
            )
            # от проверки fetching до add синхронно, так что пока идёт запрос,
            # эту главу никто не откроет и черновик для неё не появится
            self.fetching.add(tab_id)
            try:
                synced = await sync_chapter(
                    self.api, self.storage, fic.id, chapter.id, title, fic.readonly
                )
            except FicbookAPIError as e:
                report_api_error(self, e, action)
            except OSError, ValueError:
                log.warning(
                    "глава %s/%s не обновлена", fic.id, chapter.id, exc_info=True
                )
                self.notify(
                    f"{action}\nОшибка чтения или записи на диске", severity="error"
                )
            else:
                text = synced.text
                if synced.kind == "ff" and synced.site is not None:
                    v = synced.site
                    self.notify(
                        f"Обновлено до правки №{v.site_num} ({v.author_name}, {v.created})"
                    )
                elif synced.kind == "merge":
                    # shortcut: слияния нет до MergeScreen, черновик открывается как есть
                    self.notify(
                        "На сайте новая правка, слияние пока недоступно",
                        severity="warning",
                    )
            finally:
                self.fetching.discard(tab_id)
        if text is None:
            if self.api is None:
                self.notify(
                    "Не удалось скачать главу\nОфлайн, а локальной копии главы нет",
                    severity="warning",
                )
            return

        try:
            meta = self.storage.read_chapter(fic.id, chapter.id)
            title = meta.title
        except FileNotFoundError:
            meta = ChapterMeta(title)
        except OSError, ValueError:
            log.warning(
                "chapter.json %s/%s не прочитан", fic.id, chapter.id, exc_info=True
            )
            meta = None
            self.notify(
                "chapter.json главы повреждён: название не будет сохраняться",
                severity="error",
            )

        await tabbed.add_pane(
            ChapterTab(
                self.storage,
                fic.id,
                chapter.id,
                text,
                meta,
                title,
                title_editable=can_manage(role, fic.readonly),
                readonly=fic.readonly,
            )
        )
        show()

    def is_open(self, fic_id: str, chapter_id: str | None) -> bool:
        """Открыта ли вкладка главы; chapter_id None - любой главы работы."""
        return any(
            tab.fic_id == fic_id and chapter_id in (None, tab.chapter_id)
            for tab in self.query(ChapterTab)
        )

    def save_all(self, notify: bool = True) -> bool:
        """Черновики всех вкладок; False, если хоть один не записан."""
        tabs = self.query(ChapterTab)
        return all([tab.save(notify) for tab in tabs])  # noqa: C419 сохранить все, а не до первой ошибки

    # не записанный черновик остаётся открытым: после закрытия текст был бы только в памяти
    async def action_close_chapter(self) -> None:
        tabbed = self.query_one(TabbedContent)
        pane = tabbed.active_pane
        if isinstance(pane, ChapterTab) and pane.save():
            await tabbed.remove_pane(tabbed.active)

    async def on_unmount(self) -> None:
        if self.api:
            await self.api.close()
