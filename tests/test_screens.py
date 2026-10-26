import asyncio
import logging

from textual.app import App
from textual.widgets import Collapsible, Input, Label, Log, TabbedContent
from textual.widgets._collapsible import CollapsibleTitle

from fwriter.core.api.client import FicbookAPI
from fwriter.core.api.errors import (
    APIChangedError,
    LoginFailedError,
    NetworkError,
    SiteError,
    UserNotLoggedInError,
)
from fwriter.core.api.models import Chapter, Fic, SlimChapterInfo, UserInfo
from fwriter.core.storage import ChapterStorage
from fwriter.run import FWriterApp, setup_logging
from fwriter.screens import ErrorScreen, LoginScreen, MainScreen, SessionExpired
from fwriter.screens.api_errors import report_api_error
from fwriter.screens.chapter import ChapterTab
from fwriter.screens.main import FIC_SECTIONS
from fwriter.secrets import SecretStorage


def test_titles_are_plain_text():
    tricky = "[Kot] a[/]b"

    api = FicbookAPI({})
    api.user = UserInfo(1, tricky)

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
    api.user = UserInfo(1, "Кот")

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
    api.user = UserInfo(1, "Кот")

    async def fake_get_fics(role: str) -> list[Fic]:
        return [Fic("1", role, [])]

    api.get_fics = fake_get_fics
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
    api.user = UserInfo(1, "Кот")

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


def api_without_fics() -> FicbookAPI:
    api = FicbookAPI({})
    api.user = UserInfo(1, "user-1")

    async def no_fics(role: str) -> list[Fic]:
        return []

    api.get_fics = no_fics
    return api


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


def broken_write_draft(*args) -> None:
    raise OSError("disk full")


def test_chapter_tab_saves_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(ChapterTab, "AUTOSAVE_DELAY", 0.1)
    api = api_without_fics()
    fetched: list[str] = []

    async def fake_get_fics(role: str) -> list[Fic]:
        return [FIC] if role == "author" else []

    async def fake_get_chapter(fic_id: str, chapter_id: str) -> Chapter:
        fetched.append(chapter_id)
        return Chapter("Глава", "Текст.", title_editable=True, status=1)

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
                # Pressed должен дойти до MainScreen через OpenChapter и запустить воркер
                await pilot.pause()
                await pilot.app.workers.wait_for_complete()
                await pilot.pause(0.3)  # подсветка нажатия
                tab = main.query_one(ChapterTab)
                assert tabbed.active == tab.id
                tab.editor.move_cursor(tab.editor.document.end)
                await pilot.press(*keys)
                await pilot.pause(0.3)  # автосохранение
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
            await pilot.press("1", "ctrl+w")
            await pilot.pause()
            assert not main.query(ChapterTab)
            assert draft() == "Текст.!?1"

            # открытие с диска, без сайта; выход тоже сбрасывает черновик
            await open_and_type("")
            await pilot.press("2")
        assert fetched == ["00000000"]
        assert draft() == "Текст.!?12"

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
        return Chapter("Глава", "Текст.", title_editable=True, status=1)

    api.get_chapter = fake_get_chapter
    storage = ChapterStorage(tmp_path)
    main = MainScreen(api, storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            main.open_chapter(FIC, FIC.chapters[0], "author")
            await pilot.pause()
            main.open_chapter(FIC, FIC.chapters[0], "author")
            await pilot.pause()
            messages = [n.message for n in pilot.app._notifications]
            assert "Глава загружается…" in messages

            response.set()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert len(main.query(ChapterTab)) == 1
            assert not main.fetching

            # после закрытия глава открывается с диска, без второго запроса
            await pilot.press("!", "ctrl+w")
            await pilot.pause()
            await open_chapter(pilot, main)
            assert main.query_one(ChapterTab).editor.text == "!Текст."
        assert fetched == ["00000000"]

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
                await pilot.press("ctrl+w")
                await pilot.pause()
                assert main.query(ChapterTab)

            # диск ожил: закрытие пишет черновик и снимает вкладку
            await pilot.press("ctrl+w")
            await pilot.pause()
            assert not main.query(ChapterTab)
        assert storage.read_draft("00000000", "00000000") == "Текст.!"

    asyncio.run(run())


def test_open_close_keeps_mixed_newlines(tmp_path):
    storage = ChapterStorage(tmp_path)
    text = "Текст.\r\nТекст.\nТекст. Текст."
    storage.write_draft("00000000", "00000000", text)
    main = MainScreen(api_without_fics(), storage)

    async def run():
        async with MainScreenApp(main).run_test() as pilot:
            await pilot.app.workers.wait_for_complete()
            await open_chapter(pilot, main)
            await pilot.press("ctrl+w")
            await pilot.pause()
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
            report_api_error(app.screen, SiteError(503), "Не вышло")
            await pilot.pause()
            notes = [(n.message, n.severity) for n in app._notifications]
            assert notes == [
                ("Не вышло\nНет связи с сайтом", "warning"),
                ("Не вышло\nСайт ответил ошибкой 503", "error"),
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
