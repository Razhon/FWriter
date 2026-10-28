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
    def __init__(
        self, fic: Fic, chapter: SlimChapterInfo, role: Role, mark: str = ""
    ) -> None:
        self.fic = fic
        self.chapter = chapter
        self.role: Role = role
        super().__init__(chapter.title + mark, classes="chapter_btn")


def fic_collapsible(
    fic: Fic,
    role: Role,
    gone: list[SlimChapterInfo] | None = None,
    new: list[SlimChapterInfo] | None = None,
) -> Collapsible:
    """gone - черновики глав, удалённых на сайте; new - неотправленные new-N."""
    chapters: list[ClickableLabel] = [
        ChapterButton(fic, ch, role) for ch in fic.chapters
    ]
    chapters += [ChapterButton(fic, ch, role, " (нет на сайте)") for ch in gone or []]
    chapters += [ChapterButton(fic, ch, role, " (не отправлена)") for ch in new or []]
    if can_manage(role, fic.readonly):
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
            # после входа ник уже есть, запроса не будет
            user = await self.api.get_user_info()
            self.query_one("#greeting", Label).update(Content(f"Привет, {user.name}!"))
        except FicbookAPIError as e:
            report_api_error(self, e, "Не удалось загрузить профиль")
            if isinstance(e, STOP_LOADING):
                return

        seen: set[str] = set()
        # без какого-то списка не понять, какие работы пропали с сайта
        all_loaded = True
        for role, label in FIC_SECTIONS:
            try:
                fics = await self.api.get_fics(role)
            except FicbookAPIError as e:
                report_api_error(self, e, f"Не удалось загрузить список «{label}»")
                if isinstance(e, STOP_LOADING):
                    return
                all_loaded = False
                continue
            seen.update(fic.id for fic in fics)
            if not fics:
                continue
            collapsibles: list[Collapsible] = []
            for fic in fics:
                broken = False
                # есть главы в карточке - есть право; без глав различает только /parts
                if not fic.chapters:
                    stored, broken = self.stored_fic(fic.id)
                    try:
                        fic.readonly = await self.api.is_readonly(fic.id)
                    except FicbookAPIError as e:
                        # раздел не прячем: работа из fic.json, без него - без прав
                        report_api_error(
                            self, e, f"Не удалось проверить права на «{fic.title}»"
                        )
                        if isinstance(e, STOP_LOADING):
                            return
                        fic.readonly = stored.readonly if stored else True
                        fic.chapters = stored.chapters if stored else []
                        # список не с сайта: удалённые главы не определить
                        collapsibles.append(
                            self.collapsible(fic, role, find_gone=False)
                        )
                        continue
                    # сайт не отдаёт главы работы без прав: остаётся последний список
                    if fic.readonly and stored:
                        fic.chapters = stored.chapters
                # битый fic.json - единственный след списка глав работы без прав
                if fic.readonly and broken:
                    self.notify(
                        f"Файл работы «{fic.title}» повреждён, список глав не показан",
                        severity="error",
                        markup=False,
                    )
                else:
                    self.save_fic(fic, role)
                # без прав список глав не с сайта: удалённые не определить
                collapsibles.append(
                    self.collapsible(fic, role, find_gone=not fic.readonly)
                )
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

    async def mount_section(self, label: str, collapsibles: list[Collapsible]) -> None:
        if not collapsibles:
            return
        fics_container = self.query_one(".fics_container", VerticalScroll)
        if fics_container.children:  # разделитель
            await fics_container.mount(Rule())
        await fics_container.mount(
            Label(f"{label}:", classes="fics_collapsibles_label"), *collapsibles
        )

    def collapsible(self, fic: Fic, role: Role, find_gone: bool) -> Collapsible:
        """Работа вместе с локальными главами: удалёнными на сайте и new-N."""
        gone = self.gone_chapters(fic) if find_gone else []
        return fic_collapsible(fic, role, gone, self.storage.new_chapters(fic.id))

    async def show_gone(self, seen: set[str]) -> None:
        """Работы из fic.json, которых нет ни в одном списке сайта: черновики остаются."""
        # повреждённые fic.json покажет офлайн-режим, здесь не до них
        stored, _ = self.storage.read_fics()
        gone: list[Collapsible] = []
        for fic, role in stored:
            if fic.id in seen:
                continue
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
                        Fic(fic.id, fic.title, chapters, True), role, new=new
                    )
                )
        await self.mount_section(
            "Нет на сайте (работа удалена или у вас больше нет к ней доступа), "
            "локальные черновики только для чтения",
            gone,
        )

    def gone_chapters(self, fic: Fic) -> list[SlimChapterInfo]:
        """Черновики глав, которых нет в списке с сайта: удалены на сайте."""
        on_site = {ch.id for ch in fic.chapters}
        return [
            ch for ch in self.storage.local_chapters(fic.id) if ch.id not in on_site
        ]

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
        self.post_message(StartPageTab.Relogin())

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
            if self.api is None:
                self.notify(
                    "Не удалось скачать главу\nОфлайн, а локальной копии главы нет",
                    severity="warning",
                )
                return
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
                title_editable=can_manage(role, fic.readonly),
                readonly=fic.readonly,
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
        if self.api:
            await self.api.close()
