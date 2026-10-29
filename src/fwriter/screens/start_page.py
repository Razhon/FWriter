import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import ClassVar

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, Horizontal, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Collapsible, Label, Rule, TabPane

from ..core.api.client import FicbookAPI
from ..core.api.errors import FicbookAPIError, NetworkError, UserNotLoggedInError
from ..core.api.models import Fic, Role, SlimChapterInfo, can_manage
from ..core.catalog import plan_fic
from ..core.storage import ChapterStorage
from .api_errors import report_api_error, report_disk_error
from .widgets import CONFIRM_DELAY, ClickableLabel

log = logging.getLogger(__name__)

FIC_SECTIONS: list[tuple[Role, str]] = [
    ("author", "Ваши фанфики"),
    ("coauthor", "Фанфики доступные вам, как соавтору или сопереводчику"),
    ("beta", "Фанфики доступные вам, как бете"),
    ("gamma", "Фанфики доступные вам, как гамме"),
]

# без сессии и без связи следующие запросы упадут так же
STOP_LOADING = (UserNotLoggedInError, NetworkError)


class TrashButton(ClickableLabel):
    """Перенос в корзину: первое нажатие спрашивает, второе за CONFIRM_DELAY удаляет.

    chapter None - работа целиком.
    """

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
        self.reset_timer = self.set_timer(CONFIRM_DELAY, self.disarm)

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
        on_site: bool,
        mark: str = "",
        trash: TrashButton | None = None,
    ) -> None:
        """trash - крестик в строке главы, если её можно удалить."""
        self.fic = fic
        self.chapter = chapter
        self.role: Role = role
        self.on_site = on_site
        self.trash = trash
        self.mark = mark
        super().__init__(chapter.title + mark, classes="chapter_btn")

    def show_title(self, shown: tuple[str, bool] | None) -> None:
        """shown - название из chapter.json и не отправлено ли оно; None - с сайта."""
        title, pending = shown or (self.chapter.title, False)
        note = " (название не отправлено)" if pending else ""
        self.update(Content.assemble(title + self.mark, (note, "$text-muted")))

    def press(self) -> None:
        self.post_message(
            ChapterButton.Open(self.fic, self.chapter, self.role, self.on_site)
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # False прячет подсказку в footer у глав с сайта
        return action != "trash" or self.trash is not None

    def action_trash(self) -> None:
        if self.trash:
            self.trash.action_press()


class NewChapterButton(ClickableLabel):
    class Create(Message):
        """Нажата "Новая глава"; создаёт MainScreen."""

        def __init__(self, button: NewChapterButton) -> None:
            self.button = button
            super().__init__()

    def __init__(self, fic: Fic, role: Role) -> None:
        self.fic = fic
        self.role: Role = role
        super().__init__(
            "[bold]Новая глава ->[/]", classes="new_chapter_btn", markup=True
        )

    def press(self) -> None:
        self.post_message(NewChapterButton.Create(self))


class FicCollapsible(Collapsible):
    """Работа в списке; gone_fic - её нет на сайте."""

    def __init__(
        self, *children: Widget, fic: Fic, role: Role, gone_fic: bool, classes: str
    ) -> None:
        self.fic = fic
        self.role: Role = role
        self.gone_fic = gone_fic
        super().__init__(
            *children,
            # типизирует title как str, но принимает Content; str разбирается как разметка
            title=Content(fic.title),  # pyright: ignore[reportArgumentType]
            # сам Collapsible свернётся только в on_mount, а кадр до него мигнёт прокруткой
            classes=f"{classes} -collapsed",
        )

    def chapter_count(self) -> int:
        return len(self.query(ChapterButton))


def chapter_row(
    fic: Fic, chapter: SlimChapterInfo, role: Role, on_site: bool, mark: str = ""
) -> Widget:
    """Кнопка главы; не с сайта - в одной строке с крестиком удаления."""
    # главу с сайта удалить нельзя: синхронизация скачала бы её снова
    if on_site:
        return ChapterButton(fic, chapter, role, on_site, mark)
    btn = TrashButton(fic, chapter)
    return Horizontal(
        ChapterButton(fic, chapter, role, on_site, mark, btn),
        btn,
        classes="chapter_row",
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
        chapter_row(fic, ch, role, on_site=not gone_fic) for ch in fic.chapters
    ]
    chapters += [
        chapter_row(fic, ch, role, False, " (нет на сайте)") for ch in gone or []
    ]
    chapters += [
        chapter_row(fic, ch, role, False, " (не отправлена)") for ch in new or []
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
        chapters.append(NewChapterButton(fic, role))
    return FicCollapsible(
        *chapters,
        fic=fic,
        role=role,
        gone_fic=gone_fic,
        classes=f"{role}_fics_collapsible",
    )


class StartPageTab(TabPane):
    class Relogin(Message):
        """Повторить вход из офлайна; обрабатывает App."""

    def __init__(
        self,
        api: FicbookAPI | None,
        storage: ChapterStorage,
        is_open: Callable[[str, str | None], bool],
    ) -> None:
        """api None - офлайн: список работ из fic.json.

        is_open(fic_id, chapter_id) - открыта ли вкладка главы; chapter_id None - любой.
        """
        self.api = api
        self.storage = storage
        self.is_open = is_open
        # одно уведомление на загрузку, а не на каждый fic.json
        self.write_failed = False
        super().__init__("Начало работы", id="start_page")

    def compose(self) -> ComposeResult:
        if self.api:
            # равные колонки по бокам держат приветствие в центре, пока видна подпись
            yield Horizontal(
                Label("Загружаем список работ…", id="loading", classes="side"),
                Label("Привет, гость!", id="greeting"),
                Label(classes="side"),
                id="greeting_row",
            )
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
            await self.load_online(self.api)
        finally:
            # и при ошибке, и при отмене воркера
            # не remove: без колонки приветствие уедет влево
            self.query("#loading").set(visible=False)

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
        for collapsible in collapsibles:
            self.show_titles(collapsible)

    def show_titles(self, root: Widget) -> None:
        """Названия глав под root: из chapter.json, если глава уже скачана."""
        for btn in root.query(ChapterButton):
            btn.show_title(self.storage.shown_title(btn.fic.id, btn.chapter.id))

    def show_title(self, fic_id: str, chapter_id: str) -> None:
        """Название главы заново: его сохранили во вкладке."""
        for btn in self.query(ChapterButton):
            if (btn.fic.id, btn.chapter.id) == (fic_id, chapter_id):
                btn.show_title(self.storage.shown_title(fic_id, chapter_id))

    def collapsible(self, fic: Fic, role: Role, find_gone: bool) -> Collapsible:
        """Работа вместе с локальными главами: удалёнными на сайте и new-N."""
        gone = self.gone_chapters(fic) if find_gone else []
        return fic_collapsible(fic, role, gone, self.storage.new_chapters(fic.id))

    async def refresh_fic(
        self, fic_id: str, added: SlimChapterInfo | None = None
    ) -> None:
        """Список глав работы заново; added - глава, только что созданная на сайте."""
        container = self.query_one(".fics_container", VerticalScroll)
        for old in list(self.query(FicCollapsible)):
            if old.fic.id != fic_id or old.gone_fic:
                continue
            fic = old.fic
            if added is not None and all(ch.id != added.id for ch in fic.chapters):
                fic = replace(fic, chapters=[*fic.chapters, added])
            new = self.collapsible(fic, old.role, find_gone=not fic.readonly)
            if not old.collapsed:
                new.collapsed = False
                new.remove_class("-collapsed")
            await container.mount(new, after=old)
            self.show_titles(new)
            await old.remove()

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
        # None - вся работа
        target = None if whole else chapter_id
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
        if target is not None:
            await btn.query_ancestor(".chapter_row", Horizontal).remove()
            return
        await collapsible.remove()
        if not any(c.gone_fic for c in self.query(FicCollapsible)):
            await self.query(".gone_section").remove()
