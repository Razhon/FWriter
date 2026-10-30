import logging
from typing import ClassVar

from textual import events, work
from textual.app import ComposeResult
from textual.binding import ActiveBinding, Binding, BindingType
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Label, Tab, TabbedContent

from ..core.api.client import FicbookAPI
from ..core.api.models import Fic, Role, SlimChapterInfo, can_manage
from ..core.storage import DATA_DIR, ChapterStorage
from ..secrets import SecretStorage
from .chapter import ChapterTab
from .start_page import StartPageTab

log = logging.getLogger(__name__)


class MainScreen(Screen):
    """Вкладки: стартовая и главы. Что делают с главой, решает сама ChapterTab."""

    CSS_PATH = "../tcss/main.tcss"
    AUTO_FOCUS = "#start_list"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f1", "start_page", "Начало работы"),
    ]
    # footer иначе идёт по цепочке от фокуса: ^w от TextArea встаёт первым
    FOOTER_ORDER = ("f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9")

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:
        bindings = super().active_bindings
        rank = {key: i for i, key in enumerate(self.FOOTER_ORDER)}
        return dict(sorted(bindings.items(), key=lambda kv: rank.get(kv[0], len(rank))))

    def __init__(
        self, api: FicbookAPI | None, storage: ChapterStorage | None = None
    ) -> None:
        """api None - офлайн: только локальные черновики."""
        self.api = api
        self.storage = storage or ChapterStorage(DATA_DIR)
        # один словарь у всех вкладок: SettingsScreen меняет его на месте
        self.settings, self.settings_ok = self.storage.read_settings()
        super().__init__()

    def on_mount(self) -> None:
        if not self.settings_ok:
            self.notify(
                "Файл настроек повреждён: все автозамены включены", severity="warning"
            )

    def compose(self) -> ComposeResult:
        with TabbedContent():
            yield StartPageTab(self.api, self.storage, self.is_open, self.settings)
        with Horizontal(id="bottom_bar"):
            yield Footer()
            yield Label(id="chapter_info")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # на стартовой вкладке F1 некуда переходить, а закрыть её нельзя;
        # до compose вкладок ещё нет
        if action == "start_page":
            return any(t.active != "start_page" for t in self.query(TabbedContent))
        return True

    def on_tabbed_content_tab_activated(self) -> None:
        # в главе ctrl+p не нужен; footer перерисуется от refresh_bindings
        self.query_one(Footer).show_command_palette = (
            self.query_one(TabbedContent).active == "start_page"
        )
        self.refresh_bindings()
        self.show_info()
        # и после закрытия вкладки главы: иначе фокус на Tabs, мимо клавиш списка
        if self.query_one(TabbedContent).active == "start_page":
            self.query_one(StartPageTab).focus_list()

    async def on_chapter_tab_title_saved(self, message: ChapterTab.TitleSaved) -> None:
        tab = message.tab
        await self.query_one(StartPageTab).show_title(tab.fic_id, tab.chapter_id)

    def on_chapter_tab_info_changed(self, message: ChapterTab.InfoChanged) -> None:
        if message.tab is self.query_one(TabbedContent).active_pane:
            self.show_info()

    async def on_chapter_tab_created(self, message: ChapterTab.Created) -> None:
        tab = message.tab
        start = self.query_one(StartPageTab)
        start.refresh_fic(
            tab.fic_id, SlimChapterInfo(tab.chapter_id, tab.title_input.value)
        )
        # new-N в "Продолжить" больше не найдётся
        await start.remember(tab.fic_id, tab.chapter_id)

    async def on_chapter_tab_dropped(self, message: ChapterTab.Dropped) -> None:
        await self.query_one(TabbedContent).remove_pane(message.tab.id or "")

    def show_info(self) -> None:
        pane = self.query_one(TabbedContent).active_pane
        info = pane.info() if isinstance(pane, ChapterTab) else ""
        self.query_one("#chapter_info", Label).update(info)

    # стартовая вкладка не закрывается, она же выбор глав
    def action_start_page(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = "start_page"
        # фокус из скрытой вкладки главы вернул бы её обратно; сразу, а не focus():
        # иначе Hide успевает отдать фокус соседу на скрытой вкладке
        self.query_one(StartPageTab).focus_list()

    def on_start_page_tab_open_chapter(self, message: StartPageTab.OpenChapter) -> None:
        self.open_chapter(message.fic, message.chapter, message.role, message.on_site)

    # не exclusive: клик по другой главе не должен отменять открытие первой
    @work(group="open_chapter")
    async def open_chapter(
        self, fic: Fic, chapter: SlimChapterInfo, role: Role, on_site: bool = True
    ) -> None:
        """Вкладка с черновиком; on_site - вкладка сначала догонит его до сайта."""
        tabbed = self.query_one(TabbedContent)
        tab = self.find_tab(fic.id, chapter.id)
        if tab is None:
            try:
                text = self.storage.read_draft(fic.id, chapter.id)
            except OSError, ValueError:  # ValueError: не UTF-8
                log.warning(
                    "черновик %s/%s не прочитан", fic.id, chapter.id, exc_info=True
                )
                self.notify(
                    "Не удалось прочитать черновик главы с диска", severity="error"
                )
                return
            syncing = self.api is not None and on_site
            if text is None and not syncing:
                self.notify(
                    "Не удалось скачать главу\nОфлайн, а локальной копии главы нет",
                    severity="warning",
                )
                return
            tab = ChapterTab(
                self.api,
                self.storage,
                fic.id,
                chapter.id,
                text,
                chapter.title,
                title_editable=can_manage(role, fic.readonly),
                readonly=fic.readonly,
                syncing=syncing,
                publishable=not fic.readonly and (syncing or not on_site),
                creates=not on_site,
                settings=self.settings,
            )
            await tabbed.add_pane(tab)
        tabbed.active = tab.id or ""
        # фокус с нажатой главы на скрытой стартовой вкладке вернул бы её обратно;
        # сразу, а не focus(): иначе Hide отдаёт его соседней кнопке
        self.set_focus(tab.active)
        await self.query_one(StartPageTab).remember(fic.id, chapter.id)

    # по полям, а не по id виджета: после создания на сайте chapter_id вкладки меняется
    def find_tab(self, fic_id: str, chapter_id: str) -> ChapterTab | None:
        for tab in self.query(ChapterTab):
            if (tab.fic_id, tab.chapter_id) == (fic_id, chapter_id):
                return tab
        return None

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

    # пкм по вкладке закрывает главу: ctrl+w нужен TextArea, alt+backspace приходит как он
    async def on_click(self, event: events.Click) -> None:
        if event.button != 3 or not isinstance(event.widget, Tab):
            return
        tabbed = self.query_one(TabbedContent)
        pane = next(
            (p for p in self.query(ChapterTab) if tabbed.get_tab(p) is event.widget),
            None,
        )
        # закрытие отменило бы обмен с сайтом на полпути, его воркеры живут во вкладке;
        # не записанный черновик остаётся открытым: после закрытия текст был бы только в памяти
        if pane is not None and not pane.busy and pane.save():
            await tabbed.remove_pane(pane.id or "")

    async def on_unmount(self) -> None:
        if self.api:
            # сайт меняет PHPSESSID; без записи после перезапуска ушла бы старая
            if cookies := self.api.renewed_cookies():
                try:
                    SecretStorage.set_cookies(cookies)
                except Exception:  # бэкенды keyring бросают свои типы
                    log.warning("обновлённые cookies не сохранены", exc_info=True)
            await self.api.close()
