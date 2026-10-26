import logging
from functools import partial
from typing import ClassVar

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Collapsible, Label, Rule, TabbedContent, TabPane, Tabs

from ..core.api.client import FicbookAPI
from ..core.api.errors import FicbookAPIError, NetworkError, UserNotLoggedInError
from ..core.api.models import Fic, Role, SlimChapterInfo, can_manage
from ..core.storage import DATA_DIR, ChapterMeta, ChapterStorage
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
# shortcut: без связи загрузка просто обрывается; по дизайну - списки из fic.json,
# добавить вместе с офлайн-режимом
STOP_LOADING = (UserNotLoggedInError, NetworkError)


class ClickableLabel(Label, can_focus=True):
    """Намеренный отдельный класс от Button, так легче"""

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
        self.set_timer(0.2, partial(self.remove_class, "-active"))
        self.post_message(ClickableLabel.Pressed(self))


class ChapterButton(ClickableLabel):
    def __init__(self, fic: Fic, chapter: SlimChapterInfo, role: Role) -> None:
        self.fic = fic
        self.chapter = chapter
        self.role: Role = role
        super().__init__(chapter.title, classes="chapter_btn")


def fic_collapsible(fic: Fic, role: Role) -> Collapsible:
    chapters: list[ClickableLabel] = [
        ChapterButton(fic, ch, role) for ch in fic.chapters
    ]
    # TODO readonly: признак выключенного автором редактирования пока не найден
    if can_manage(role, readonly=False):
        chapters.append(
            ClickableLabel(
                "[bold]Новая глава ->[/]",
                classes="new_chapter_btn",
                markup=True,
            )
        )
    return Collapsible(
        *chapters,
        # типизирует title как str, но принимает Content; str разбирается как разметка
        title=Content(fic.title),  # pyright: ignore[reportArgumentType]
        classes=f"{role}_fics_collapsible",
    )


class StartPageTab(TabPane):
    class OpenChapter(Message):
        """Нажата глава; открывает MainScreen."""

        def __init__(self, fic: Fic, chapter: SlimChapterInfo, role: Role) -> None:
            self.fic = fic
            self.chapter = chapter
            self.role = role
            super().__init__()

    def __init__(self, api: FicbookAPI) -> None:
        self.api = api
        super().__init__("Начало работы", id="start_page")

    def compose(self) -> ComposeResult:
        yield Center(Label("Привет, гость!", id="greeting"))
        yield Rule()
        yield VerticalScroll(classes="fics_container")

    def on_mount(self) -> None:
        self.load_start_page()

    # воркер отменяется при удалении вкладки и до закрытия клиента в MainScreen.on_unmount
    @work(exclusive=True, group="start_page")
    async def load_start_page(self) -> None:
        """Ник и списки фанфиков; ошибка одного списка не прячет остальные."""
        try:
            # после входа ник уже есть, запроса не будет
            user = await self.api.get_user_info()
            self.query_one("#greeting", Label).update(Content(f"Привет, {user.name}!"))
        except FicbookAPIError as e:
            report_api_error(self, e, "Не удалось загрузить профиль")
            if isinstance(e, STOP_LOADING):
                return
        fics_container = self.query_one(".fics_container", VerticalScroll)

        # цикл намеренный, пока не уверен как Cloudflare отнесётся к такой ораве запросов
        for role, label in FIC_SECTIONS:
            try:
                fics = await self.api.get_fics(role)
            except FicbookAPIError as e:
                report_api_error(self, e, f"Не удалось загрузить список «{label}»")
                if isinstance(e, STOP_LOADING):
                    return
                continue
            if not fics:
                continue

            if fics_container.children:  # разделитель
                await fics_container.mount(Rule())

            await fics_container.mount(
                Label(f"{label}:", classes="fics_collapsibles_label"),
                *[fic_collapsible(fic, role) for fic in fics],
            )

    @on(ClickableLabel.Pressed, ".chapter_btn")
    def on_chapter_pressed(self, event: ClickableLabel.Pressed) -> None:
        btn = event.clabel
        assert isinstance(btn, ChapterButton)  # Pressed типизирован базовым классом
        self.post_message(StartPageTab.OpenChapter(btn.fic, btn.chapter, btn.role))


class MainScreen(Screen):
    CSS_PATH = "../tcss/main.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f1", "start_page", "Начало работы"),
        # priority: у TextArea ctrl+w удаляет слово (это остаётся на ctrl+backspace)
        Binding("ctrl+w", "close_chapter", "Закрыть главу", priority=True),
    ]

    def __init__(self, api: FicbookAPI, storage: ChapterStorage | None = None) -> None:
        self.api = api
        self.storage = storage or ChapterStorage(DATA_DIR)
        # tab_id глав, которые сейчас скачиваются: повторный клик не шлёт второй GET
        self.fetching: set[str] = set()
        super().__init__()

    def compose(self) -> ComposeResult:
        with TabbedContent():
            yield StartPageTab(self.api)

    # стартовая вкладка не закрывается, она же выбор глав
    def action_start_page(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = "start_page"
        # фокус из скрытой вкладки главы вернул бы её обратно
        tabbed.query_one(Tabs).focus()

    def on_start_page_tab_open_chapter(self, message: StartPageTab.OpenChapter) -> None:
        self.open_chapter(message.fic, message.chapter, message.role)

    # на экране, а не на вкладке: воркер живёт, пока открыты вкладки глав
    # не exclusive: клик по другой главе не должен отменять открытие первой
    @work(group="open_chapter")
    async def open_chapter(
        self, fic: Fic, chapter: SlimChapterInfo, role: Role
    ) -> None:
        """Вкладка с локальным черновиком; если его нет - с текстом с сайта."""
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
        if text is None:
            # от проверки fetching до add синхронно, так что пока идёт запрос,
            # эту главу никто не откроет и черновик для неё не появится
            self.fetching.add(tab_id)
            try:
                site = await self.api.get_chapter(fic.id, chapter.id)
            except FicbookAPIError as e:
                report_api_error(self, e, "Не удалось скачать главу")
                return
            finally:
                self.fetching.discard(tab_id)
            text, title = site.content, site.title
            try:
                self.storage.write_draft(fic.id, chapter.id, text)
            except OSError:
                log.warning(
                    "черновик %s/%s не записан", fic.id, chapter.id, exc_info=True
                )
                self.notify("Не удалось записать черновик на диск", severity="error")

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
                # TODO readonly, как и у "Новой главы"
                title_editable=can_manage(role, readonly=False),
            )
        )
        show()

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
        await self.api.close()
