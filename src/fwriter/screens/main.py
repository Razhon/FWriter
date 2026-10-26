import logging
from functools import partial
from typing import ClassVar, Literal

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Collapsible, Label, Rule, TabbedContent, TabPane, Tabs

from ..core.api.client import FicbookAPI
from ..core.api.errors import (
    FicbookAPIError,
    NetworkError,
    SiteError,
    UserNotLoggedInError,
)
from ..core.api.models import Fic, Role, SlimChapterInfo, can_manage
from ..core.storage import DATA_DIR, ChapterMeta, ChapterStorage
from .chapter import ChapterTab
from .error import ErrorScreen

log = logging.getLogger(__name__)

FIC_SECTIONS: list[tuple[Role, str]] = [
    ("author", "Ваши фанфики:"),
    ("coauthor", "Фанфики доступные вам, как соавтору или сопереводчику:"),
    ("beta", "Фанфики доступные вам, как бете:"),
    ("gamma", "Фанфики доступные вам, как гамме:"),
]


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
        # Пока идёт подсветка нажатия, повторные нажатия игнорируются
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
        """Ник и списки фанфиков; все ошибки - в одном окне, без повторов."""
        # dict, а не set: порядок строк как у запросов
        errors: dict[str, None] = {}
        try:
            # после входа ник уже есть, запроса не будет
            user = await self.api.get_user_info()
            self.query_one("#greeting", Label).update(Content(f"Привет, {user.name}!"))
        except UserNotLoggedInError as e:
            self.post_message(MainScreen.SessionExpired(str(e)))
            return
        except FicbookAPIError as e:
            log.warning("ник не загружен", exc_info=True)
            errors[str(e)] = None
        fics_container = self.query_one(".fics_container", VerticalScroll)

        def gen_fic_collapsible(fic: Fic, role: Role) -> Collapsible:
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

        # ошибка одного списка не прячет остальные
        # цикл намеренный, пока не уверен как Cloudflare отнесётся к такой ораве запросов
        for role, label in FIC_SECTIONS:
            try:
                fics = await self.api.get_fics(role)
            except UserNotLoggedInError as e:
                self.post_message(MainScreen.SessionExpired(str(e)))
                return
            except FicbookAPIError as e:
                log.warning("список %s не загружен", role, exc_info=True)
                # без связи все списки падают одинаково: одна строка, а не по разделу
                errors[str(e) if isinstance(e, NetworkError) else f"{label}: {e}"] = (
                    None
                )
                continue
            if not fics:
                continue

            if fics_container.children:  # разделитель
                await fics_container.mount(Rule())

            await fics_container.mount(
                Label(label, classes="fics_collapsibles_label"),
                *[gen_fic_collapsible(fic, role) for fic in fics],
            )

        if errors:
            self.app.push_screen(ErrorScreen("\n".join(errors)))

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

    class SessionExpired(Message):
        """Сессия истекла; обрабатывает App."""

        def __init__(self, reason: str) -> None:
            self.reason = reason
            super().__init__()

    def __init__(self, api: FicbookAPI, storage: ChapterStorage | None = None) -> None:
        self.api = api
        self.storage = storage or ChapterStorage(DATA_DIR)
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

        def read_draft() -> str | None | Literal[False]:
            """False: черновик есть, но не читается; уведомление уже показано."""
            try:
                return self.storage.read_draft(fic.id, chapter.id)
            except OSError, ValueError:  # ValueError: не UTF-8
                log.warning(
                    "черновик %s/%s не прочитан", fic.id, chapter.id, exc_info=True
                )
                self.notify(
                    "Не удалось прочитать черновик главы с диска", severity="error"
                )
                return False

        if tabbed.query(f"#{tab_id}"):
            show()
            return
        text = read_draft()
        if text is False:
            return
        title = chapter.title
        if text is None:
            try:
                site = await self.api.get_chapter(fic.id, chapter.id)
            except UserNotLoggedInError as e:
                self.post_message(MainScreen.SessionExpired(str(e)))
                return
            except FicbookAPIError as e:
                log.warning(
                    "глава %s/%s не загружена", fic.id, chapter.id, exc_info=True
                )
                match e:
                    case SiteError():
                        self.notify(
                            f"Сайт не отдал главу (код {e.status})", severity="error"
                        )
                    case NetworkError():
                        self.notify("Глава недоступна офлайн", severity="warning")
                    case _:
                        self.app.push_screen(ErrorScreen(str(e)))
                return
            # пока шёл запрос, главу могли открыть повторным кликом и уже править
            if tabbed.query(f"#{tab_id}"):
                show()
                return
            # ...или открыть, поправить и закрыть: черновик на диске новее ответа
            if read_draft() is not None:
                return
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
