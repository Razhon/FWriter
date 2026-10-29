import asyncio
import logging
from dataclasses import replace

import pytest
from textual.app import App
from textual.geometry import Region
from textual.widgets import Collapsible, Input, Label, Log, TabbedContent
from textual.widgets._collapsible import CollapsibleTitle
from textual.widgets.text_area import Selection

from fwriter.core.api.client import FicbookAPI
from fwriter.core.api.errors import (
    APIChangedError,
    FicbookAPIError,
    LoginFailedError,
    NetworkError,
    SiteError,
    SiteRejectedError,
    UserNotLoggedInError,
)
from fwriter.core.api.models import Chapter, Fic, SlimChapterInfo, UserInfo
from fwriter.core.markup import MarkupError
from fwriter.core.storage import ChapterStorage, Version
from fwriter.core.sync import Published, Synced
from fwriter.run import FWriterApp, setup_logging
from fwriter.screens import (
    ConnectingScreen,
    ErrorScreen,
    LoginScreen,
    MainScreen,
    SessionExpired,
)
from fwriter.screens.api_errors import report_api_error
from fwriter.screens.chapter import ChapterEditor, ChapterTab
from fwriter.screens.main import (
    FIC_SECTIONS,
    ChapterButton,
    ClickableLabel,
    NewChapterButton,
    TrashButton,
)
from fwriter.screens.menu import MenuScreen
from fwriter.secrets import SecretStorage


@pytest.fixture(autouse=True)
def offline_api(tmp_path, monkeypatch):
    """MainScreen без storage не пишет в DATA_DIR, проверка прав не ходит на сайт."""
    monkeypatch.setattr("fwriter.screens.main.DATA_DIR", tmp_path)

    async def not_readonly(self, fic_id: str) -> bool:
        return False

    monkeypatch.setattr(FicbookAPI, "is_readonly", not_readonly)
    monkeypatch.setattr("fwriter.screens.main.sync_chapter", fake_sync)
    # тесты не ждут подсветку нажатия
    monkeypatch.setattr(ClickableLabel, "PRESS_DELAY", 0.01)


async def fake_sync(api, storage, fic_id, chapter_id, title, readonly) -> Synced:
    """Синхронизация без истории: черновик с диска, иначе get_chapter()."""
    text = storage.read_draft(fic_id, chapter_id)
    if text is not None:
        return Synced("same", text)
    site = await api.get_chapter(fic_id, chapter_id)
    storage.write_draft(fic_id, chapter_id, site.content)
    return Synced("new", site.content)


def test_titles_are_plain_text():
    tricky = "[Kot] a[/]b"

    api = FicbookAPI({})
    api.user = UserInfo("1", tricky)

    async def fake_get_fics(role: str) -> list[Fic]:
        return [Fic("1", tricky, [])] if role == "author" else []

    api.get_fics = fake_get_fics

    class TestApp(App):
        def on_mount(self):
            self.push_screen(MainScreen(api))

    async def run():
        async with TestApp().run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            greeting = pilot.app.screen.query_one("#greeting", Label)
            assert str(greeting.render()) == f"Привет, {tricky}!"
            title = pilot.app.screen.query_one(CollapsibleTitle)
            assert tricky in str(title.render())

    asyncio.run(run())


def test_broken_keyring_opens_login(monkeypatch, caplog):
    def broken_get_auth():
        raise RuntimeError("no keyring")

    monkeypatch.setattr(SecretStorage, "get_auth", broken_get_auth)

    async def run():
        async with FWriterApp().run_test() as pilot:
            await pilot.pause()
            assert isinstance(pilot.app.screen, LoginScreen)
            # текст бэкенда keyring - только в лог
            messages = [n.message for n in pilot.app._notifications]
            assert messages == ["Не удалось прочитать сохранённый вход, войдите снова"]

    asyncio.run(run())
    assert any(r.exc_info for r in caplog.records)


def test_failed_fic_list_keeps_others():
    api = FicbookAPI({})
    api.user = UserInfo("1", "Кот")

    async def fake_get_fics(role: str) -> list[Fic]:
        if role in ("coauthor", "beta"):
            raise APIChangedError(f"сломалось {role}")
        return [Fic("1", role, [])]

    api.get_fics = fake_get_fics
    main = MainScreen(api)

    class TestApp(App):
        def on_mount(self):
            self.push_screen(main)

    async def run():
        async with TestApp().run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            titles = [str(c.title) for c in main.query(Collapsible)]
            assert titles == ["author", "gamma"]
            assert not main.query_one("#loading").visible
            # обе ошибки - в одном окне
            errors = [s for s in pilot.app.screen_stack if isinstance(s, ErrorScreen)]
            assert len(errors) == 1
            lines = errors[0].query_one(Log).lines
            assert "сломалось coauthor" in lines
            assert "сломалось beta" in lines

    asyncio.run(run())


def test_offline_start_page_shows_one_error(caplog):
    api = FicbookAPI({})
    fetched: list[str] = []

    async def no_user() -> UserInfo:
        raise NetworkError("Нет связи с сайтом")

    async def no_fics(role: str) -> list[Fic]:
        fetched.append(role)
        raise NetworkError("Нет связи с сайтом")

    api.get_user_info = no_user
    api.get_fics = no_fics
    main = MainScreen(api)

    class TestApp(App):
        def on_mount(self):
            self.push_screen(main)

    async def run():
        async with TestApp().run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            messages = [n.message for n in pilot.app._notifications]
            assert messages == ["Не удалось загрузить профиль\nНет связи с сайтом"]
            assert not isinstance(pilot.app.screen, ErrorScreen)

    asyncio.run(run())
    # без связи остальные запросы не шлются
    assert fetched == []
    # UI прячет подробности, в логе они должны остаться
    records = [r for r in caplog.records if r.name == "fwriter.screens.api_errors"]
    assert len(records) == 1
    assert records[0].exc_info


def test_setup_logging(tmp_path, monkeypatch):
    monkeypatch.setenv("FWRITER_DEBUG", "1")
    root = logging.getLogger()
    handlers = list(root.handlers)
    try:
        setup_logging(tmp_path / "logs")
        logging.getLogger("fwriter.test").debug("наш debug")
        logging.getLogger("httpx").debug("чужой debug")
        logging.getLogger("httpx").warning("чужой warning")
    finally:
        for h in set(root.handlers) - set(handlers):
            h.close()
            root.removeHandler(h)
        logging.getLogger("fwriter").setLevel(logging.NOTSET)
    text = (tmp_path / "logs" / "fwriter.log").read_text(encoding="utf-8")
    assert "наш debug" in text
    assert "чужой warning" in text
    # на DEBUG httpx и httpcore пишут подробности запросов
    assert "чужой debug" not in text


def test_sections_match_api_roles():
    assert [r for r, _ in FIC_SECTIONS] == list(FicbookAPI.FIC_LISTS)


def test_failed_saved_login_opens_login_with_error(monkeypatch):
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: ({"rme": "1"}, "ua"))

    async def failed_login(cookies, user_agent):
        raise LoginFailedError("Нет связи с сайтом")

    monkeypatch.setattr(FicbookAPI, "login", failed_login)

    async def run():
        async with FWriterApp().run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(pilot.app.screen, ErrorScreen)
            assert isinstance(pilot.app.screen_stack[-2], LoginScreen)

    asyncio.run(run())


def test_non_ascii_user_agent_is_rejected(monkeypatch):
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: (None, None))
    called: list[bool] = []

    async def fake_login(cookies, user_agent):
        called.append(True)
        raise LoginFailedError("не должен вызываться")

    monkeypatch.setattr(FicbookAPI, "login", fake_login)
    cookies = '[{"name": "rme", "value": "1"}, {"name": "PHPSESSID", "value": "2"}]'

    async def run():
        async with FWriterApp().run_test() as pilot:
            await pilot.pause()
            pilot.app.screen.query_one("#cookies", Input).value = cookies
            pilot.app.screen.query_one("#user_agent", Input).value = "Mozilla\xa05.0"
            await pilot.pause()
            await pilot.click("#login")
            await pilot.pause()
            assert not called
            assert isinstance(pilot.app.screen, LoginScreen)

    asyncio.run(run())


def test_new_chapter_only_for_managers():
    api = FicbookAPI({})
    api.user = UserInfo("1", "Кот")

    async def fake_get_fics(role: str) -> list[Fic]:
        return [Fic("1", role, []), Fic("2", f"{role} ro", [])]

    async def fake_is_readonly(fic_id: str) -> bool:
        return fic_id == "2"

    api.get_fics = fake_get_fics
    api.is_readonly = fake_is_readonly
    main = MainScreen(api)

    class TestApp(App):
        def on_mount(self):
            self.push_screen(main)

    async def run():
        async with TestApp().run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            with_button = [
                str(c.title)
                for c in main.query(Collapsible)
                if c.query(".new_chapter_btn")
            ]
            assert with_button == ["author", "coauthor"]

    asyncio.run(run())


def test_expired_session_opens_login(monkeypatch):
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: ({"rme": "1"}, "ua"))
    api = FicbookAPI({})
    api.user = UserInfo("1", "Кот")

    async def fake_login(cookies, user_agent):
        return api

    async def expired(role: str) -> list[Fic]:
        raise UserNotLoggedInError("истекла")

    async def no_fics(role: str) -> list[Fic]:
        return []

    monkeypatch.setattr(FicbookAPI, "login", fake_login)
    api.get_fics = expired

    async def run():
        app = FWriterApp()
        async with app.run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(pilot.app.screen, ErrorScreen)
            assert isinstance(pilot.app.screen_stack[-2], LoginScreen)
            assert api.client.is_closed
            # повторный вход ставит новый MainScreen
            api.get_fics = no_fics
            app.open_main(api)
            await pilot.pause()
            assert isinstance(pilot.app.screen, MainScreen)

    asyncio.run(run())


FIC = Fic("00000000", "Фанфик", [SlimChapterInfo("00000000", "Глава")])


def chapter_titles(main: MainScreen) -> list[str]:
    return [str(b.render()) for b in main.query(".chapter_btn")]


def test_readonly_fic_keeps_known_chapters(tmp_path):
    """Сайт не отдаёт главы работы без прав: в fic.json и на экране остаётся прошлый список."""
    api = api_without_fics()
    readonly = False

    async def fake_get_fics(role: str) -> list[Fic]:
        if role != "coauthor":
            return []
        chapters = [] if readonly else list(FIC.chapters)
        return [Fic(FIC.id, FIC.title, chapters)]

    async def fake_is_readonly(fic_id: str) -> bool:
        return readonly

    api.get_fics = fake_get_fics
    api.is_readonly = fake_is_readonly
    storage = ChapterStorage(tmp_path)

    async def load() -> list[str]:
        main = MainScreen(api, storage)
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            return chapter_titles(main)

    assert asyncio.run(load()) == ["Глава"]
    readonly = True
    assert asyncio.run(load()) == ["Глава"]
    assert storage.read_fic(FIC.id) == (
        Fic(FIC.id, FIC.title, FIC.chapters, readonly=True),
        "coauthor",
    )


def test_readonly_fic_keeps_broken_fic_json(tmp_path):
    """Битый fic.json работы без прав не затирается пустым списком глав."""
    api = api_without_fics()

    async def fake_get_fics(role: str) -> list[Fic]:
        return [Fic(FIC.id, FIC.title, [])] if role == "coauthor" else []

    async def fake_is_readonly(fic_id: str) -> bool:
        return True

    api.get_fics = fake_get_fics
    api.is_readonly = fake_is_readonly
    path = tmp_path / "fics" / FIC.id / "fic.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    main = MainScreen(api, ChapterStorage(tmp_path))

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            notes = [n.severity for n in pilot.app._notifications]
            assert notes == ["error"]

    asyncio.run(run())
    assert path.read_text(encoding="utf-8") == "{"


def test_readonly_check_failure_keeps_section(tmp_path):
    """Сбой проверки прав одной работы: раздел виден, работа - по fic.json."""
    api = api_without_fics()
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic("2", "Старая", FIC.chapters, readonly=True), "coauthor")

    async def fake_get_fics(role: str) -> list[Fic]:
        if role != "coauthor":
            return []
        return [Fic("1", "Пустая", []), Fic("2", "Старая", []), FIC]

    async def limited(fic_id: str) -> bool:
        raise SiteError(429)

    api.get_fics = fake_get_fics
    api.is_readonly = limited
    main = MainScreen(api, storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            assert [str(c.title) for c in main.query(Collapsible)] == [
                "Пустая",
                "Старая",
                FIC.title,
            ]
            # без fic.json - без прав, пока не проверим
            assert [
                str(c.title)
                for c in main.query(Collapsible)
                if c.query(".new_chapter_btn")
            ] == [FIC.title]
            assert chapter_titles(main) == ["Глава", "Глава"]
            # непроверенные работы на диск не пишутся
            fics, _ = storage.read_fics()
            assert [f.id for f, _ in fics] == [FIC.id, "2"]

    asyncio.run(run())


def api_without_fics() -> FicbookAPI:
    api = FicbookAPI({})
    api.user = UserInfo("1", "user-1")

    async def no_fics(role: str) -> list[Fic]:
        return []

    api.get_fics = no_fics
    return api


def fake_logins(monkeypatch, results: list[FicbookAPIError | FicbookAPI]) -> None:
    """FicbookAPI.login по очереди отдаёт results: ошибку выбрасывает."""

    async def fake_login(cookies, user_agent):
        result = results.pop(0)
        if isinstance(result, FicbookAPIError):
            raise result
        return result

    monkeypatch.setattr(FicbookAPI, "login", fake_login)


class MainScreenApp(App):
    def __init__(self, main: MainScreen) -> None:
        self.main = main
        super().__init__()

    def on_mount(self) -> None:
        self.push_screen(self.main)


async def open_chapter(pilot, main: MainScreen) -> None:
    """Открывает главу FIC, как клик по ней, и ждёт воркер."""
    main.open_chapter(FIC, FIC.chapters[0], "author")
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def type_in_chapter(pilot, main: MainScreen, keys: str = "!") -> None:
    await open_chapter(pilot, main)
    tab = main.query_one(ChapterTab)
    tab.editor.move_cursor(tab.editor.document.end)
    await pilot.press(*keys)


async def close_tab(pilot, main: MainScreen, button: int = 3) -> None:
    """Клик по вкладке активной главы, по умолчанию правый - закрыть."""
    tabbed = main.query_one(TabbedContent)
    await pilot.click(tabbed.get_tab(tabbed.active), button=button)
    await pilot.pause()


def broken_write_draft(*args) -> None:
    raise OSError("disk full")


def test_chapter_tab_saves_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(ChapterTab, "AUTOSAVE_DELAY", 0.01)
    api = api_without_fics()
    fetched: list[str] = []

    async def fake_get_fics(role: str) -> list[Fic]:
        return [FIC] if role == "author" else []

    async def fake_get_chapter(fic_id: str, chapter_id: str) -> Chapter:
        fetched.append(chapter_id)
        return Chapter("Глава", "Текст.", title_editable=True, status=1, history_url="")

    api.get_fics = fake_get_fics
    api.get_chapter = fake_get_chapter
    storage = ChapterStorage(tmp_path)
    main = MainScreen(api, storage)

    def draft() -> str | None:
        return storage.read_draft("00000000", "00000000")

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            tabbed = main.query_one(TabbedContent)

            async def open_and_type(keys: str) -> ChapterTab:
                # мышью: фокус на нажатой главе не должен возвращать стартовую вкладку
                await pilot.click(".chapter_btn")
                # ChapterButton.Open должен дойти до MainScreen и запустить воркер
                await pilot.pause()
                await pilot.app.workers.wait_for_complete()
                await pilot.pause(0.05)  # подсветка нажатия
                tab = main.query_one(ChapterTab)
                assert tabbed.active == tab.id
                tab.editor.move_cursor(tab.editor.document.end)
                await pilot.press(*keys)
                await pilot.pause(0.05)  # автосохранение
                return tab

            await pilot.app.workers.wait_for_complete()
            await pilot.click(CollapsibleTitle)
            await open_and_type("!")
            assert draft() == "Текст.!"

            # F1 и повторный клик переключают на открытую вкладку
            await pilot.press("f1")
            await pilot.pause()
            assert tabbed.active == "start_page"
            tab = await open_and_type("?")
            assert len(main.query(ChapterTab)) == 1
            assert tabbed.active == tab.id

            # закрытие пишет сразу, не дожидаясь таймера
            await pilot.press("1")
            await close_tab(pilot, main)
            assert not main.query(ChapterTab)
            assert draft() == "Текст.!?1"

            # открытие с диска, без сайта; выход тоже сбрасывает черновик
            await open_and_type("")
            await pilot.press("2")
        assert fetched == ["00000000"]
        assert draft() == "Текст.!?12"

    asyncio.run(run())


def test_readonly_chapter_is_locked(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_draft("00000000", "00000000", "Текст.")
    main = MainScreen(api_without_fics(), storage)
    fic = Fic(FIC.id, FIC.title, FIC.chapters, readonly=True)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            main.open_chapter(fic, fic.chapters[0], "coauthor")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            tab = main.query_one(ChapterTab)
            assert tab.editor.read_only
            assert tab.title_input.disabled

    asyncio.run(run())


def test_chapter_expired_session_opens_nothing(tmp_path):
    api = api_without_fics()
    expired: list[str] = []

    async def fake_get_chapter(fic_id: str, chapter_id: str) -> Chapter:
        raise UserNotLoggedInError("Сессия истекла")

    api.get_chapter = fake_get_chapter
    storage = ChapterStorage(tmp_path)
    main = MainScreen(api, storage)

    class TestApp(MainScreenApp):
        def on_session_expired(self, message: SessionExpired):
            expired.append(message.reason)

    async def run():
        async with TestApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            assert not main.query(ChapterTab)
        assert expired == ["Сессия истекла"]
        assert storage.read_draft("00000000", "00000000") is None

    asyncio.run(run())


def test_click_while_loading_sends_one_request(tmp_path):
    api = api_without_fics()
    response = asyncio.Event()
    fetched: list[str] = []

    async def fake_get_chapter(fic_id: str, chapter_id: str) -> Chapter:
        fetched.append(chapter_id)
        await response.wait()
        return Chapter("Глава", "Текст.", title_editable=True, status=1, history_url="")

    api.get_chapter = fake_get_chapter
    storage = ChapterStorage(tmp_path)
    main = MainScreen(api, storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            main.open_chapter(FIC, FIC.chapters[0], "author")
            await pilot.pause()
            # вкладка открыта сразу, но до конца синхронизации править нельзя
            [tab] = main.query(ChapterTab)
            assert tab.editor.read_only
            # фокус в тексте, иначе TabbedContent вернётся к стартовой вкладке
            assert main.query_one(TabbedContent).active == tab.id
            assert main.focused is tab.editor
            assert tab.title_input.disabled and tab.status.display
            # повторное нажатие переключает на ту же вкладку, закрыть её до конца нельзя
            main.open_chapter(FIC, FIC.chapters[0], "author")
            await close_tab(pilot, main)
            assert len(main.query(ChapterTab)) == 1
            # клик по вкладке забрал фокус
            tab.editor.focus()

            response.set()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert len(main.query(ChapterTab)) == 1
            assert not main.fetching
            assert not tab.editor.read_only and main.focused is tab.editor
            assert not tab.title_input.disabled and not tab.status.display

            # после закрытия глава открывается с диска, без второго запроса
            await pilot.press("!")
            await close_tab(pilot, main)
            await open_chapter(pilot, main)
            assert main.query_one(ChapterTab).editor.text == "!Текст."
        assert fetched == ["00000000"]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("result", "note"),
    [
        (
            Synced(
                "ff",
                "Новое.",
                Version(
                    "v.txt",
                    "Г",
                    "site",
                    "2026-10-27T10:00:00",
                    site_num=2,
                    author_name="user-1",
                ),
            ),
            "Обновлено до правки номер 2 (user-1, 2026-10-27T10:00:00)",
        ),
        (Synced("merge", "Текст."), "На сайте новая правка, слияние пока недоступно"),
        (
            NetworkError("Нет связи с сайтом"),
            "Не удалось обновить главу\nНет связи с сайтом",
        ),
    ],
)
def test_open_chapter_syncs(tmp_path, monkeypatch, result, note: str):
    storage = ChapterStorage(tmp_path)
    storage.write_draft("00000000", "00000000", "Текст.")
    main = MainScreen(api_without_fics(), storage)

    async def synced(*args) -> Synced:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("fwriter.screens.main.sync_chapter", synced)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            # при ошибке сайта открывается локальный черновик
            expected = "Текст." if isinstance(result, Exception) else result.text
            editor = main.query_one(ChapterTab).editor
            assert editor.text == expected and not editor.read_only
            assert note in [n.message for n in pilot.app._notifications]

    asyncio.run(run())


def test_open_chapter_without_draft_closes_on_sync_error(tmp_path, monkeypatch):
    main = MainScreen(api_without_fics(), ChapterStorage(tmp_path))

    async def synced(*args) -> Synced:
        raise NetworkError("Нет связи с сайтом")

    monkeypatch.setattr("fwriter.screens.main.sync_chapter", synced)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            assert not main.query(ChapterTab)
            messages = [n.message for n in pilot.app._notifications]
            assert "Не удалось скачать главу\nНет связи с сайтом" in messages

    asyncio.run(run())


def test_chapter_off_site_is_not_synced(tmp_path, monkeypatch):
    storage = ChapterStorage(tmp_path)
    storage.write_draft("00000000", "00000000", "Текст.")
    main = MainScreen(api_without_fics(), storage)
    calls: list[str] = []

    async def synced(*args) -> Synced:
        calls.append("sync")
        return Synced("same", "Текст.")

    monkeypatch.setattr("fwriter.screens.main.sync_chapter", synced)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            main.open_chapter(FIC, FIC.chapters[0], "author", on_site=False)
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert main.query(ChapterTab)
        assert not calls

    asyncio.run(run())


def test_editor_rewraps_on_screen_after_sync(tmp_path, monkeypatch):
    # без строки "Проверяем сайт…" редактор шире: старый перенос не должен остаться на экране
    # короткие слова: на 2 клетки шире - другой перенос почти в каждой строке
    text = "\n\n".join(" ".join(["ой"] * 150) for _ in range(8))
    storage = ChapterStorage(tmp_path)
    storage.write_draft("00000000", "00000000", text)
    main = MainScreen(api_without_fics(), storage)
    response = asyncio.Event()

    async def synced(*args) -> Synced:
        await response.wait()
        return Synced("same", text)

    monkeypatch.setattr("fwriter.screens.main.sync_chapter", synced)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            main.open_chapter(FIC, FIC.chapters[0], "author")
            # узкий редактор успевает нарисоваться
            await pilot.pause(0.1)
            response.set()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause(0.1)
            editor = main.query_one(ChapterTab).editor

            def shown() -> list[str]:
                return [s.text for s in editor.render_lines(Region(0, 0, *editor.size))]

            before = shown()
            editor.refresh()
            assert shown() == before

    asyncio.run(run())


def test_chapter_info_and_rename(tmp_path, monkeypatch):
    storage = ChapterStorage(tmp_path)
    storage.write_draft("00000000", "00000000", "Текст  главы.")
    v = storage.add_version("00000000", "00000000", "Текст  главы.", "Глава", "site")
    meta = storage.read_chapter("00000000", "00000000")
    storage.write_chapter("00000000", "00000000", replace(meta, base=v.file))
    main = MainScreen(api_without_fics(), storage)

    async def synced(*args) -> Synced:
        return Synced("same", "Текст  главы.", status=2)

    monkeypatch.setattr("fwriter.screens.main.sync_chapter", synced)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            tab = main.query_one(ChapterTab)
            info = main.query_one("#chapter_info", Label)
            assert not tab.title_input.display
            assert str(info.render()) == (
                "опубликована · сохранено · как на сайте · 12 зн., 2 сл."
            )

            # символы и слова пересчитываются при сохранении
            await pilot.press("!")
            assert str(info.render()) == (
                "опубликована · не сохранено · есть правки · 12 зн., 2 сл."
            )
            assert tab.save()
            await pilot.pause()
            assert str(info.render()).endswith(
                "сохранено · есть правки · 13 зн., 2 сл."
            )

            # F2 открывает название, Enter прячет его и возвращает в текст
            await pilot.press("f2")
            assert main.focused is tab.title_input
            await pilot.press("end", "?", "enter")
            assert not tab.title_input.display and main.focused is tab.editor
            assert str(main.query_one(TabbedContent).get_tab(tab).label) == "Глава?"

            # пустое название не остаётся ни на диске, ни в поле
            await pilot.press("f2", "ctrl+u")
            assert tab.save()
            assert storage.read_chapter("00000000", "00000000").title == "Глава"
            await pilot.press("escape")
            assert tab.title_input.value == "Глава?"

            await pilot.press("f1")
            assert str(info.render()) == ""

    asyncio.run(run())


def test_close_chapter_keeps_unsaved_tab(tmp_path, monkeypatch):
    storage = ChapterStorage(tmp_path)
    storage.write_draft("00000000", "00000000", "Текст.")
    main = MainScreen(api_without_fics(), storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            with monkeypatch.context() as m:
                m.setattr(storage, "write_draft", broken_write_draft)
                await type_in_chapter(pilot, main)
                # левый клик вкладку не закрывает
                await close_tab(pilot, main, button=1)
                assert main.query(ChapterTab)
                await close_tab(pilot, main)
                assert main.query(ChapterTab)

            # диск ожил: закрытие пишет черновик и снимает вкладку
            await close_tab(pilot, main)
            assert not main.query(ChapterTab)
        assert storage.read_draft("00000000", "00000000") == "Текст.!"

    asyncio.run(run())


def test_right_click_closes_inactive_tab(tmp_path):
    storage = ChapterStorage(tmp_path)
    fic = Fic(
        "00000000", "Фанфик", [SlimChapterInfo("1", "1"), SlimChapterInfo("2", "2")]
    )
    for chapter in fic.chapters:
        storage.write_draft(fic.id, chapter.id, "Текст.")
    main = MainScreen(api_without_fics(), storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            for chapter in fic.chapters:
                main.open_chapter(fic, chapter, "author")
                await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            tabbed = main.query_one(TabbedContent)
            first, second = main.query(ChapterTab)
            assert tabbed.active == second.id
            await pilot.click(tabbed.get_tab(first), button=3)
            await pilot.pause()
            assert list(main.query(ChapterTab)) == [second]
            # стартовая вкладка правым кликом не закрывается
            await pilot.click(tabbed.get_tab("start_page"), button=3)
            await pilot.pause()
            assert tabbed.active == "start_page"

    asyncio.run(run())


def test_open_close_keeps_mixed_newlines(tmp_path):
    storage = ChapterStorage(tmp_path)
    text = "Текст.\r\nТекст.\nТекст.\u2028Текст."
    storage.write_draft("00000000", "00000000", text)
    main = MainScreen(api_without_fics(), storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            await close_tab(pilot, main)
            assert not main.query(ChapterTab)
        # TextArea привёл бы переводы строк к одному виду
        assert storage.read_draft("00000000", "00000000") == text

    asyncio.run(run())


def test_broken_chapter_json_not_overwritten(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_draft("00000000", "00000000", "Текст.")
    path = tmp_path / "fics" / "00000000" / "00000000" / "chapter.json"
    path.write_text("{", encoding="utf-8")
    main = MainScreen(api_without_fics(), storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            tab = main.query_one(ChapterTab)
            tab.title_input.value = "Новое название"
            await pilot.pause()
            tabbed = main.query_one(TabbedContent)
            assert tabbed.get_tab(tab).label.plain == "Новое название"
            assert tab.save()
        # повреждённый файл с историей версий не затёрт одним названием
        assert path.read_text(encoding="utf-8") == "{"

    asyncio.run(run())


def test_non_utf8_draft_does_not_crash(tmp_path):
    path = tmp_path / "fics" / "00000000" / "00000000" / "draft.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes("Текст.".encode("cp1251"))
    main = MainScreen(api_without_fics(), ChapterStorage(tmp_path))

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            assert not main.query(ChapterTab)
            assert pilot.app.return_code is None

    asyncio.run(run())


def chapter_app(tmp_path, monkeypatch) -> tuple[FWriterApp, FicbookAPI]:
    """FWriterApp со входом без сайта и черновиком 00000000/00000000 в tmp_path."""
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: ({"rme": "1"}, "ua"))
    monkeypatch.setattr("fwriter.screens.main.DATA_DIR", tmp_path)
    api = api_without_fics()

    async def fake_login(cookies, user_agent):
        return api

    monkeypatch.setattr(FicbookAPI, "login", fake_login)
    ChapterStorage(tmp_path).write_draft("00000000", "00000000", "Текст.")
    return FWriterApp(), api


def test_quit_with_unsaved_draft_needs_second_press(tmp_path, monkeypatch):
    app, _ = chapter_app(tmp_path, monkeypatch)

    async def run():
        async with app.run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            assert app.main_screen
            await type_in_chapter(pilot, app.main_screen)
            monkeypatch.setattr(ChapterStorage, "write_draft", broken_write_draft)
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app.return_code is None
            # повторное нажатие выходит без записи
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app.return_code == 0

    asyncio.run(run())
    assert ChapterStorage(tmp_path).read_draft("00000000", "00000000") == "Текст."


def test_expired_session_keeps_unsaved_draft(tmp_path, monkeypatch):
    app, api = chapter_app(tmp_path, monkeypatch)
    storage = ChapterStorage(tmp_path)

    async def run():
        async with app.run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            main = app.main_screen
            assert main
            await type_in_chapter(pilot, main)
            with monkeypatch.context() as m:
                m.setattr(ChapterStorage, "write_draft", broken_write_draft)
                main.post_message(SessionExpired("истекла"))
                await pilot.pause()
            assert isinstance(app.screen, ErrorScreen)
            assert app.main_screen is main
            assert storage.read_draft("00000000", "00000000") == "Текст."

            # повторный вход снимает старый MainScreen и пишет его черновик
            app.open_main(api)
            await pilot.pause()
            assert isinstance(app.screen, MainScreen)
            assert app.main_screen is not main
            assert storage.read_draft("00000000", "00000000") == "Текст.!"

    asyncio.run(run())


def test_relogin_reports_lost_draft(tmp_path, monkeypatch):
    app, api = chapter_app(tmp_path, monkeypatch)
    notes: list[str] = []

    async def run():
        async with app.run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            main = app.main_screen
            assert main
            await type_in_chapter(pilot, main)
            monkeypatch.setattr(ChapterStorage, "write_draft", broken_write_draft)
            monkeypatch.setattr(
                ChapterTab, "notify", lambda self, text, **kw: notes.append(text)
            )
            main.post_message(SessionExpired("истекла"))
            await pilot.pause()
            # диск так и не ожил: снятая вкладка не обещает повторить запись
            app.open_main(api)
            await pilot.pause()
        assert notes == ["Черновик главы не записан на диск, правки потеряны"]

    asyncio.run(run())


def test_report_api_error():
    expired: list[str] = []

    class TestApp(App):
        def on_session_expired(self, message: SessionExpired):
            expired.append(message.reason)

    async def run():
        async with TestApp().run_test() as pilot:
            app = pilot.app
            report_api_error(app.screen, NetworkError("Нет связи с сайтом"), "Не вышло")
            # 429 и 5xx пройдут сами, как и сбой сети
            for status in (503, 429, 404):
                report_api_error(app.screen, SiteError(status), "Не вышло")
            # капча пройдёт сама, отказ проверки - нет
            captcha = SiteRejectedError("К", captcha=True)
            report_api_error(app.screen, captcha, "Не вышло")
            report_api_error(app.screen, SiteRejectedError("Пусто"), "Не вышло")
            await pilot.pause()
            notes = [(n.message, n.severity) for n in app._notifications]
            assert notes == [
                ("Не вышло\nНет связи с сайтом", "warning"),
                ("Не вышло\nСайт временно не работает (ошибка 503)", "warning"),
                (f"Не вышло\n{SiteError.TEXTS[429]}", "warning"),
                (f"Не вышло\n{SiteError.TEXTS[404]}", "error"),
                ("Не вышло\nК", "warning"),
                ("Не вышло\nПусто", "error"),
            ]

            report_api_error(app.screen, UserNotLoggedInError("Сессия истекла"), "-")
            await pilot.pause()
            assert expired == ["Сессия истекла"]
            assert not isinstance(app.screen, ErrorScreen)

            report_api_error(app.screen, APIChangedError("Нет блока"), "Не вышло")
            await pilot.pause()
            assert isinstance(app.screen, ErrorScreen)
            # вторая такая же ошибка не открывает второе окно
            report_api_error(app.screen, APIChangedError("Нет блока"), "Не вышло")
            report_api_error(app.screen, APIChangedError("Нет другого"), "Не вышло")
            await pilot.pause()
            assert len(app.screen_stack) == 2
            assert app.screen.lines == ["Не вышло\nНет блока", "Не вышло\nНет другого"]

    asyncio.run(run())


@pytest.mark.parametrize("beta_fails", [False, True])
def test_gone_fics_marked(tmp_path, beta_fails: bool):
    """Работа из fic.json, которой нет на сайте, - в отдельном разделе, только чтение.

    В разделе только главы с черновиком: остальные с сайта уже не скачать.
    Если какой-то список не загрузился, пропавшие не определить: раздела нет.
    """
    api = api_without_fics()
    storage = ChapterStorage(tmp_path)
    storage.write_fic(FIC, "author")
    two_chapters = [SlimChapterInfo("1", "С черновиком"), SlimChapterInfo("2", "Без")]
    storage.write_fic(Fic("2", "Удалённая", two_chapters), "author")
    storage.write_draft("2", "1", "Текст.")
    storage.write_fic(Fic("3", "Без черновиков", two_chapters), "author")
    storage.write_fic(Fic("4", "Только новая", []), "author")
    storage.write_draft("4", storage.new_chapter_id("4"), "Новая.")

    async def fake_get_fics(role: str) -> list[Fic]:
        if role == "beta" and beta_fails:
            raise SiteError(500)
        return [FIC] if role == "author" else []

    api.get_fics = fake_get_fics
    main = MainScreen(api, storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            titles = [str(c.title) for c in main.query(Collapsible)]
            labels = [str(lb.render()) for lb in main.query(".fics_collapsibles_label")]
            if beta_fails:
                assert titles == [FIC.title]
                return
            assert titles == [FIC.title, "Удалённая", "Только новая"]
            assert labels[-1].startswith("Нет на сайте")
            *_, gone, only_new = main.query(Collapsible)
            assert not gone.query(".new_chapter_btn")
            assert [str(b.render()) for b in gone.query(".chapter_btn")] == [
                "С черновиком"
            ]
            assert [str(b.render()) for b in only_new.query(".chapter_btn")] == [
                "new-1 (не отправлена)"
            ]

    asyncio.run(run())


@pytest.mark.parametrize("readonly", [False, True])
def test_chapter_gone_from_site_marked(tmp_path, readonly: bool):
    """Черновик главы, которой нет в списке с сайта, - с пометкой и правится.

    У работы без прав список глав не с сайта: пометки нет.
    """
    api = api_without_fics()
    storage = ChapterStorage(tmp_path)
    storage.write_fic(Fic(FIC.id, FIC.title, FIC.chapters, readonly), "coauthor")
    storage.write_draft(FIC.id, "1", "Удалённая.")
    storage.set_title(FIC.id, "1", "Удалённая")

    async def fake_get_fics(role: str) -> list[Fic]:
        return [Fic(FIC.id, FIC.title, [])] if role == "coauthor" else []

    async def fake_is_readonly(fic_id: str) -> bool:
        return readonly

    api.get_fics = fake_get_fics
    api.is_readonly = fake_is_readonly
    main = MainScreen(api, storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            if readonly:
                assert chapter_titles(main) == ["Глава"]
                return
            assert chapter_titles(main) == ["Удалённая (нет на сайте)"]
            btn = main.query_one(ChapterButton)
            main.open_chapter(btn.fic, btn.chapter, btn.role)
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            tab = main.query_one(ChapterTab)
            assert tab.editor.text == "Удалённая."
            assert not tab.editor.read_only

    asyncio.run(run())


def offline_storage(tmp_path) -> ChapterStorage:
    """fic.json с прошлого входа: глава с черновиком, глава без него, new-1 и удалённая."""
    storage = ChapterStorage(tmp_path)
    fic = Fic(FIC.id, FIC.title, [*FIC.chapters, SlimChapterInfo("2", "Без копии")])
    storage.write_fic(fic, "author")
    storage.write_draft(FIC.id, FIC.chapters[0].id, "Текст.")
    storage.write_draft(FIC.id, "1", "Удалённая.")
    storage.write_draft(FIC.id, storage.new_chapter_id(FIC.id), "Новая.")
    (tmp_path / "fics" / "8").mkdir()
    (tmp_path / "fics" / "8" / "fic.json").write_text("{")
    return storage


def test_offline_start_page_from_fic_json(tmp_path):
    main = MainScreen(None, offline_storage(tmp_path))

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert main.query(".relogin_btn")
            assert chapter_titles(main) == [
                "Глава",
                "Без копии",
                "1 (нет на сайте)",
                "new-1 (не отправлена)",
            ]
            [note] = pilot.app._notifications
            assert note.title == "Часть работ не показана"
            assert "fics" in note.message and "8" in note.message

            # без связи главу без локальной копии не скачать
            buttons = list(main.query(ChapterButton))
            main.open_chapter(buttons[1].fic, buttons[1].chapter, "author")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert not main.query(ChapterTab)
            assert (
                "локальной копии главы нет"
                in list(pilot.app._notifications)[-1].message
            )

            main.open_chapter(buttons[0].fic, buttons[0].chapter, "author")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert main.query_one(ChapterTab).editor.text == "Текст."

    asyncio.run(run())


def test_new_chapters_shown_online(tmp_path):
    api = api_without_fics()
    storage = ChapterStorage(tmp_path)
    storage.write_draft(FIC.id, storage.new_chapter_id(FIC.id), "Новая.")

    async def fake_get_fics(role: str) -> list[Fic]:
        return [FIC] if role == "author" else []

    api.get_fics = fake_get_fics
    main = MainScreen(api, storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            assert chapter_titles(main) == ["Глава", "new-1 (не отправлена)"]

    asyncio.run(run())


def test_new_chapter_created_on_site(tmp_path, monkeypatch):
    api = api_without_fics()
    storage = ChapterStorage(tmp_path)

    async def fake_get_fics(role: str) -> list[Fic]:
        return [FIC] if role == "author" else []

    api.get_fics = fake_get_fics
    main = MainScreen(api, storage)
    sent: list[tuple[str, bool]] = []

    async def published(api, storage, fic_id, chapter_id, title, create=False):
        sent.append((chapter_id, create))
        storage.rename_chapter(fic_id, chapter_id, "2002", title)
        first = Synced("same", "Текст.")
        return Published(first, None, first, None, created="2002")

    monkeypatch.setattr("fwriter.screens.main.publish_chapter", published)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            main.query_one(NewChapterButton).press()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert chapter_titles(main) == ["Глава", "Глава 2 (не отправлена)"]
            tab = main.query_one(ChapterTab)
            assert (tab.chapter_id, tab.title_input.value) == ("new-1", "Глава 2")
            await pilot.press("f5")
            note = list(pilot.app._notifications)[-1].message
            assert note == "Нажмите F5 ещё раз, чтобы создать главу на сайте"
            await pilot.press("f5")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert sent == [("new-1", True)]
            [tab] = main.query(ChapterTab)
            assert tab.chapter_id == "2002" and not tab.creates
            assert main.query_one(TabbedContent).active == tab.id
            assert chapter_titles(main) == ["Глава", "Глава 2"]
            messages = [n.message for n in pilot.app._notifications]
            assert "Глава создана на сайте как черновик" in messages

    asyncio.run(run())


def test_created_but_not_renamed_blocks_publish(tmp_path, monkeypatch):
    api = api_without_fics()
    storage = ChapterStorage(tmp_path)

    async def fake_get_fics(role: str) -> list[Fic]:
        return [FIC] if role == "author" else []

    api.get_fics = fake_get_fics
    main = MainScreen(api, storage)

    async def published(api, storage, fic_id, chapter_id, title, create=False):
        return Published(Synced("same", ""), created="2002", renamed=False)

    monkeypatch.setattr("fwriter.screens.main.publish_chapter", published)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            # двойной клик: второе нажатие приходит уже от снятой кнопки
            btn = main.query_one(NewChapterButton)
            btn.press()
            btn.press()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert chapter_titles(main) == ["Глава", "Глава 2 (не отправлена)"]
            await pilot.press("f5", "f5")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            tab = main.query_one(ChapterTab)
            assert tab.chapter_id == "new-1" and not tab.publishable
            assert not main.check_action("publish", ())

    asyncio.run(run())


def test_saved_login_without_network_goes_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: ({"rme": "1"}, "ua"))
    offline_storage(tmp_path)
    online = api_without_fics()
    fake_logins(
        monkeypatch, [NetworkError("Нет связи с сайтом"), SiteError(503), online]
    )

    async def run():
        app = FWriterApp()
        async with app.run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            offline = app.main_screen
            assert isinstance(pilot.app.screen, MainScreen)
            assert offline and offline.api is None
            first = next(iter(app._notifications))
            assert (first.message, first.severity) == (
                "Нет связи с сайтом. Работаем офлайн",
                "warning",
            )

            # повтор снова без связи: тот же экран, вкладки не закрываются
            await pilot.click(".relogin_btn")
            await pilot.pause()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert app.main_screen is offline
            assert any(
                n.message == "Сайт временно не работает (ошибка 503). Работаем офлайн"
                for n in app._notifications
            )

            # правка офлайн не теряется при переходе в онлайн
            offline.open_chapter(FIC, FIC.chapters[0], "author")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            tab = offline.query_one(ChapterTab)
            tab.editor.move_cursor(tab.editor.document.end)
            await pilot.press("!")
            offline.action_start_page()
            await pilot.pause(0.05)  # подсветка нажатия
            await pilot.click(".relogin_btn")
            await pilot.pause()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert app.main_screen is not offline
            assert app.main_screen and app.main_screen.api is online

    asyncio.run(run())
    assert ChapterStorage(tmp_path).read_draft(FIC.id, FIC.chapters[0].id) == "Текст.!"


def test_relogin_keeps_offline_if_draft_not_saved(tmp_path, monkeypatch):
    """Черновик не записан: офлайн-экран не снимается, иначе правки пропадут."""
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: ({"rme": "1"}, "ua"))
    online = api_without_fics()
    closed = False

    async def close() -> None:
        nonlocal closed
        closed = True

    online.close = close
    fake_logins(monkeypatch, [NetworkError("Нет связи с сайтом"), online])

    async def run():
        app = FWriterApp()
        async with app.run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            offline = app.main_screen
            assert offline
            monkeypatch.setattr(offline, "save_all", lambda notify=True: False)
            await pilot.click(".relogin_btn")
            await pilot.pause()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert app.main_screen is offline
            assert pilot.app.screen is offline
            assert closed
            assert list(app._notifications)[-1].message == (
                "Не удалось записать черновик на диск, остаёмся офлайн"
            )

    asyncio.run(run())


def test_manual_login_without_network_stays_on_login(monkeypatch):
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: (None, None))

    async def no_network(cookies, user_agent):
        raise NetworkError("Нет связи с сайтом")

    monkeypatch.setattr(FicbookAPI, "login", no_network)
    cookies = '[{"name": "rme", "value": "1"}, {"name": "PHPSESSID", "value": "2"}]'

    async def run():
        async with FWriterApp().run_test() as pilot:
            await pilot.pause()
            pilot.app.screen.query_one("#cookies", Input).value = cookies
            pilot.app.screen.query_one("#user_agent", Input).value = "Mozilla/5.0"
            await pilot.pause()
            await pilot.click("#login")
            await pilot.pause()
            assert isinstance(pilot.app.screen, LoginScreen)
            assert not pilot.app.screen.query_one("#login").disabled
            assert [n.message for n in pilot.app._notifications] == [
                "Не удалось войти\nНет связи с сайтом"
            ]

    asyncio.run(run())


def test_connecting_screen_go_offline(tmp_path, monkeypatch):
    """Пока идёт вход - экран подключения; "Работать офлайн" не ждёт таймаута."""
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: ({"rme": "1"}, "ua"))
    offline_storage(tmp_path)
    cancelled: list[bool] = []

    async def hanging_login(cookies, user_agent):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(FicbookAPI, "login", hanging_login)

    async def run():
        app = FWriterApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ConnectingScreen)
            await pilot.click("#offline")
            await pilot.pause()
            assert isinstance(app.screen, MainScreen)
            assert app.main_screen and app.main_screen.api is None
            assert cancelled

    asyncio.run(run())


def test_relogin_button_shows_attempt(tmp_path, monkeypatch):
    """Во время повтора кнопка неактивна, после неудачи - снова "Повторить вход"."""
    monkeypatch.setattr(SecretStorage, "get_auth", lambda: ({"rme": "1"}, "ua"))
    attempt = asyncio.Event()

    async def no_network(cookies, user_agent):
        await attempt.wait()
        raise NetworkError("Нет связи с сайтом")

    monkeypatch.setattr(FicbookAPI, "login", no_network)

    async def run():
        app = FWriterApp()
        async with app.run_test() as pilot:
            attempt.set()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            btn = app.screen.query_one(".relogin_btn", Label)
            assert str(btn.render()) == "Повторить вход"

            attempt.clear()
            await pilot.click(".relogin_btn")
            await pilot.pause()
            assert str(btn.render()) == "Подключаемся…"
            assert btn.disabled

            attempt.set()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert str(btn.render()) == "Повторить вход"
            assert not btn.disabled

    asyncio.run(run())


def test_trash_local_chapters(tmp_path, monkeypatch):
    """Удаляются только локальные главы, со вторым нажатием и при закрытой вкладке.

    Последняя глава работы из "Нет на сайте" уносит в корзину всю работу.
    """
    monkeypatch.setattr(TrashButton, "CONFIRM_DELAY", 0.3)
    api = api_without_fics()
    storage = ChapterStorage(tmp_path)
    storage.write_fic(FIC, "author")
    storage.write_draft(FIC.id, FIC.chapters[0].id, "С сайта.")
    storage.write_draft(FIC.id, storage.new_chapter_id(FIC.id), "Новая.")
    storage.write_fic(Fic("2", "Удалённая", []), "author")
    storage.write_draft("2", "1", "Раз.")
    storage.write_draft("2", "2", "Два.")

    async def fake_get_fics(role: str) -> list[Fic]:
        return [FIC] if role == "author" else []

    api.get_fics = fake_get_fics
    main = MainScreen(api, storage)

    async def press_twice(pilot, btn: ChapterButton) -> None:
        btn.focus()
        # подсветка нажатия глушит повтор раньше PRESS_DELAY
        await pilot.pause(0.05)
        await pilot.press("x")
        await pilot.pause(0.05)
        await pilot.press("x")
        await pilot.pause()

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            site, new, gone1, gone2 = main.query(ChapterButton)
            assert site.trash is None
            assert not site.check_action("trash", ())
            # стартовая вкладка не закрывается, и F1 на ней некуда вести
            assert not main.check_action("start_page", ())
            assert new.trash and gone2.trash

            # одно нажатие только спрашивает и само сбрасывается
            new.focus()
            await pilot.press("x")
            await pilot.pause()
            assert str(new.trash.render()) == "Удалить? X"
            await pilot.pause(0.4)
            assert str(new.trash.render()) == "X"
            assert storage.new_chapters(FIC.id)

            await press_twice(pilot, new)
            assert not storage.new_chapters(FIC.id)
            assert chapter_titles(main) == ["Глава", "1", "2"]

            # открытая вкладка не даёт удалить
            main.open_chapter(gone1.fic, gone1.chapter, "author")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert main.check_action("start_page", ())
            await pilot.press("f1")
            await press_twice(pilot, gone1)
            assert "закройте вкладку" in list(pilot.app._notifications)[-1].message
            assert storage.read_draft("2", "1") == "Раз."
            await main.query_one(TabbedContent).remove_pane(ChapterTab.tab_id("2", "1"))

            await press_twice(pilot, gone1)
            assert storage.read_draft("2", "1") is None
            assert storage.read_draft("2", "2") == "Два."

            # работу целиком не даёт удалить вкладка любой её главы
            (fic_trash,) = [b for b in main.query(TrashButton) if b.chapter is None]
            main.open_chapter(gone2.fic, gone2.chapter, "author")
            await pilot.app.workers.wait_for_complete()
            await pilot.press("f1")
            fic_trash.action_press()
            await pilot.pause(0.05)
            # глав столько, сколько осталось, а не сколько было при построении
            assert (
                str(fic_trash.render())
                == "Удалить работу и её главы (1)? Нажмите ещё раз"
            )
            fic_trash.action_press()
            await pilot.pause()
            assert "закройте вкладку" in list(pilot.app._notifications)[-1].message
            assert (tmp_path / "fics" / "2" / "fic.json").exists()
            await main.query_one(TabbedContent).remove_pane(ChapterTab.tab_id("2", "2"))

            # мышью по крестику; последняя глава - вся работа
            trash = gone2.trash
            trash.action_press()
            await pilot.pause(0.05)
            trash.action_press()
            await pilot.pause()
            assert [fic.id for fic, _ in storage.read_fics()[0]] == [FIC.id]
            assert len(list((tmp_path / "trash").glob("2-*/fic.json"))) == 1
            assert chapter_titles(main) == ["Глава"]
            assert not main.query(".gone_section")

    asyncio.run(run())


@pytest.mark.parametrize("error", [None, MarkupError(0, 2, "Незакрытый тег <b>")])
def test_f5_publishes_with_tab_locked(tmp_path, monkeypatch, error):
    storage = ChapterStorage(tmp_path)
    storage.write_draft("00000000", "00000000", "Текст.")
    main = MainScreen(api_without_fics(), storage)
    seen: list[tuple[str, bool]] = []

    async def published(
        api, storage, fic_id, chapter_id, title, create=False
    ) -> Published:
        tab = main.query_one(ChapterTab)
        seen.append((storage.read_draft(fic_id, chapter_id), tab.editor.read_only))
        first = Synced("same", "Текст.!")
        if error is not None:
            return Published(first, error)
        rev = Version("v.txt", "Глава", "site", "2026-10-28T12:00:00", site_num=3)
        return Published(first, None, first, rev)

    monkeypatch.setattr("fwriter.screens.main.publish_chapter", published)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await type_in_chapter(pilot, main)
            # первое F5 только спрашивает
            await pilot.press("f5")
            await pilot.app.workers.wait_for_complete()
            assert seen == []
            assert main.publish_armed is not None
            await pilot.press("f5")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            # перед отправкой черновик на диске, правки закрыты
            assert seen == [("Текст.!", True)]
            editor = main.query_one(ChapterTab).editor
            assert not editor.read_only
            messages = [n.message for n in pilot.app._notifications]
            if error is None:
                assert "Отправлено (правка номер 3)" in messages
            else:
                assert editor.cursor_location == (0, 2)
                assert "Ошибка разметки, строка 1: Незакрытый тег <b>" in messages

    asyncio.run(run())


def test_footer_order_in_chapter(tmp_path):
    api = api_without_fics()

    async def fake_get_chapter(fic_id: str, chapter_id: str) -> Chapter:
        return Chapter("Глава", "Текст.", title_editable=True, status=1, history_url="")

    api.get_chapter = fake_get_chapter
    main = MainScreen(api, ChapterStorage(tmp_path))

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            shown = [b.key for _, b, *_ in main.active_bindings.values() if b.show]
            assert shown[:3] == ["f1", "f2", "f5"]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("before", "action", "presses"), [(1, "show_part", 2), (2, "hide_part", 1)]
)
def test_f6_changes_status(tmp_path, monkeypatch, before, action, presses):
    api = api_without_fics()
    status = before
    sent: list[tuple[str, str]] = []

    async def synced(*args) -> Synced:
        return Synced("same", "Текст.", status=status)

    monkeypatch.setattr("fwriter.screens.main.sync_chapter", synced)

    async def fake_get_chapter(fic_id: str, chapter_id: str) -> Chapter:
        return Chapter("Глава", "Текст.", True, status=status, history_url="")

    async def fake_set_status(chapter_id: str, act: str) -> None:
        nonlocal status
        sent.append((chapter_id, act))
        status = 3 - status

    api.get_chapter = fake_get_chapter
    api.set_chapter_status = fake_set_status  # type: ignore[method-assign]
    main = MainScreen(api, ChapterStorage(tmp_path))

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            # фокус в редакторе: у TextArea на f6 своя привязка
            assert isinstance(main.focused, ChapterEditor)
            await pilot.press("f6")
            await pilot.pause()
            assert isinstance(pilot.app.screen, MenuScreen)
            # "Опубликовать" выбирают дважды, первое нажатие только спрашивает
            for _ in range(presses - 1):
                await pilot.press("enter")
                await pilot.pause()
                assert sent == []
            await pilot.press("enter")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert sent == [("00000000", action)]
            assert main.query_one(ChapterTab).site_status == 3 - before
            assert main.check_action("status", ())

    asyncio.run(run())


def test_renamed_chapter_marked_on_start_page(tmp_path):
    api = api_without_fics()
    storage = ChapterStorage(tmp_path)
    storage.write_draft(FIC.id, "00000000", "Текст.")
    meta = storage.load_chapter(FIC.id, "00000000", "Глава")
    storage.write_chapter(FIC.id, "00000000", replace(meta, site_title="Глава"))

    async def fake_get_fics(role: str) -> list[Fic]:
        return [FIC] if role == "author" else []

    api.get_fics = fake_get_fics
    main = MainScreen(api, storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            await pilot.press("f2", "end", "!", "enter")
            assert main.query_one(ChapterTab).save()
            await pilot.pause()
            [btn] = main.query(ChapterButton)
            assert str(btn.render()) == "Глава! (название не отправлено)"
            # пометка серая, как крестик удаления
            [span] = btn.visual.spans  # type: ignore[union-attr]
            assert str(span.style) == "$text-muted"

    asyncio.run(run())


def test_tag_keys_in_editor(tmp_path):
    storage = ChapterStorage(tmp_path)
    storage.write_draft(FIC.id, "00000000", "раз два")
    main = MainScreen(None, storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            editor = main.query_one(ChapterTab).editor
            assert main.focused is editor
            await pilot.press("tab")
            assert editor.text == "<tab>раз два"
            editor.selection = Selection((0, 5), (0, 8))
            await pilot.press("ctrl+b")
            assert editor.text == "<tab><b>раз</b> два"
            await pilot.press("end", "alt+i", "x")
            assert editor.text == "<tab><b>раз</b> два<i>x</i>"
            # каждая вставка тегов - один шаг отмены
            await pilot.press("ctrl+z", "ctrl+z")
            assert editor.text == "<tab><b>раз</b> два"
            await pilot.press("ctrl+z")
            assert editor.text == "<tab>раз два"
            editor.load_text("раз\nдва")
            editor.select_all()
            await pilot.press("ctrl+t")
            assert editor.text == "<s>раз</s>\n<s>два</s>"
            await pilot.press("ctrl+z")
            assert editor.text == "раз\nдва"
            # подсвечены только теги читателя
            editor.load_text("<b>a</b><u>b</u>")
            styled = {editor.text[s.start : s.end] for s in editor.get_line(0).spans}
            assert styled == {"<b>", "</b>"}
            # только для чтения tab переводит фокус
            editor.read_only = True
            await pilot.press("tab")
            assert editor.text == "<b>a</b><u>b</u>"
            assert main.focused is not editor
            # длинное и среднее тире подсвечены разным фоном, дефис - нет
            editor.load_text("a-b—c–d")
            spans = editor.get_line(0).spans
            assert [(s.start, s.end) for s in spans] == [(3, 4), (5, 6)]
            assert spans[0].style != spans[1].style

    asyncio.run(run())
