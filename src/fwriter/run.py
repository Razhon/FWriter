import logging
import os
import sys
import time
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import ClassVar

from platformdirs import user_log_dir
from textual import work
from textual.app import App
from textual.screen import Screen

from .core.api.client import FicbookAPI
from .core.api.errors import LoginFailedError, NetworkError, SiteError
from .core.storage import DATA_DIR, lock_data_dir
from .screens import *
from .screens.start_page import StartPageTab
from .secrets import *

log = logging.getLogger(__name__)


class FWriterApp(App):
    MODES: ClassVar[dict[str, str | Callable[[], Screen]]] = {
        "login": LoginScreen,
        "connecting": ConnectingScreen,
    }

    main_screen: MainScreen | None = None
    force_quit_until = 0.0

    # Textual печатает traceback только в терминал после выхода; без locals
    def _handle_exception(self, error: Exception) -> None:
        log.critical("программа упала", exc_info=error)
        super()._handle_exception(error)

    # ctrl+q; повторное нажатие, пока видна подсказка, выходит без записи
    async def action_quit(self) -> None:
        main = self.main_screen
        if (
            not main
            or main.save_all(notify=False)
            or time.monotonic() < self.force_quit_until
        ):
            self.exit()
            return
        self.force_quit_until = time.monotonic() + self.NOTIFICATION_TIMEOUT
        self.notify(
            "Не удалось записать черновик на диск.\n"
            "Нажмите Ctrl+Q ещё раз, чтобы выйти без сохранения",
            severity="error",
        )

    # ctrl+c у Textual вместо выхода; в TextArea это копирование
    def action_help_quit(self) -> None:
        self.notify("Выход - Ctrl+Q")

    def on_mount(self) -> None:
        self.login_saved_or_ask()

    def on_start_page_tab_relogin(self) -> None:
        self.login_saved_or_ask()

    def login_saved_or_ask(self) -> None:
        try:
            cookies, user_agent = SecretStorage.get_auth()
        except Exception:  # у бэкендов keyring свои типы ошибок
            log.warning("сохранённый вход не прочитан", exc_info=True)
            self.notify(
                "Не удалось прочитать сохранённый вход, войдите снова",
                severity="warning",
                timeout=6,
            )
            cookies, user_agent = None, None

        if not cookies or not user_agent:
            self.relogin_done()
            self.switch_mode("login")
            return
        # при запуске; повтор из офлайна показывает это на кнопке
        if not self.main_screen:
            self.switch_mode("connecting")
        self.login_saved(cookies, user_agent)

    def on_connecting_screen_go_offline(self) -> None:
        if self.current_mode != "connecting":  # вход уже закончился
            return
        self.workers.cancel_group(self, "login")
        self.open_main(None)

    def relogin_done(self) -> None:
        """Попытка входа из офлайна закончилась: кнопку повтора - обратно."""
        if self.main_screen and self.main_screen.api is None:
            self.main_screen.query_one(StartPageTab).set_connecting(False)

    # в воркере, чтобы приложение отвечало на клавиши, пока идёт запрос
    @work(exclusive=True, group="login")
    async def login_saved(self, cookies: dict[str, str], user_agent: str) -> None:
        try:
            api = await FicbookAPI.login(cookies, user_agent)
        except LoginFailedError as e:
            log.warning("вход по сохранённым cookies не удался", exc_info=True)
            self.relogin_done()
            self.switch_mode("login")
            self.push_screen(ErrorScreen(f"Не удалось войти: {e}"))
            return
        except (NetworkError, SiteError) as e:  # login() пропускает только временные
            log.warning("сайт недоступен, работаем офлайн", exc_info=True)
            self.notify(f"{e}. Работаем офлайн", severity="warning")
            # повтор из офлайна: открытые вкладки не закрываем
            if self.main_screen and self.main_screen.api is None:
                self.relogin_done()
            else:
                self.open_main(None)
            return
        # здесь main_screen - только офлайн; как при истёкшей сессии, без записи
        # черновиков экран не снимаем, иначе правки пропадут вместе с вкладками
        if self.main_screen and not self.main_screen.save_all(notify=False):
            await api.close()
            self.relogin_done()
            self.notify(
                "Не удалось записать черновик на диск, остаёмся офлайн",
                severity="error",
            )
            return
        self.open_main(api)

    def open_main(self, api: FicbookAPI | None) -> None:
        """api None - офлайн: список работ из fic.json."""
        # режим, а не install_screen: после повторного входа нужен новый MainScreen
        # остался после истёкшей сессии с незаписанным черновиком или офлайн;
        # вкладки глав пишут черновик на диск при снятии экрана
        if self.main_screen:
            # shortcut: если запись не удастся и здесь, правки пропадут без вопроса;
            # спрашивать, как при выходе, если такой сбой случится на деле
            if self.current_mode == "main":  # повтор входа из офлайна
                self.switch_mode("login")
            self.remove_mode("main")
        main = self.main_screen = MainScreen(api)
        self.add_mode("main", lambda: main)
        self.switch_mode("main")

    def on_login_screen_logged_in(self, message: LoginScreen.LoggedIn) -> None:
        self.open_main(message.api)

    def on_session_expired(self, message: SessionExpired) -> None:
        """На LoginScreen; режим main удаляется, MainScreen закрывает клиент API."""
        if self.current_mode != "main":  # второй воркер уже переключил
            return
        self.switch_mode("login")
        text = f"Войдите снова. {message.reason}"
        # иначе черновик, не записанный на диск, пропал бы вместе с вкладкой
        if not self.main_screen or self.main_screen.save_all(notify=False):
            self.remove_mode("main")
            self.main_screen = None
        else:
            text += (
                "\nЧерновик не записан на диск: запись повторится при входе и выходе"
            )
        self.push_screen(ErrorScreen(text))


def setup_logging(log_dir: Path) -> None:
    """Лог в файл; DEBUG только у fwriter и только при FWRITER_DEBUG=1."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "fwriter.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError as e:
        # без лога работать можно; терминал ещё не занят Textual
        print(f"Лог не будет вестись: {e}", file=sys.stderr)
        return
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    if os.environ.get("FWRITER_DEBUG") == "1":
        logging.getLogger("fwriter").setLevel(logging.DEBUG)


def main():
    try:
        _lock = lock_data_dir(DATA_DIR)  # живёт до выхода из main, то есть процесса
    except OSError:
        sys.exit("FWriter уже запущен")
    # после блокировки: две копии не делят один файл при ротации
    setup_logging(Path(user_log_dir("fwriter", appauthor=False)))
    app = FWriterApp()
    app.run()


if __name__ == "__main__":
    main()
