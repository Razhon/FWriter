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
from textual.binding import BindingType
from textual.screen import Screen

from .core.api.client import FicbookAPI
from .core.api.errors import LoginFailedError
from .core.storage import DATA_DIR, lock_data_dir
from .screens import *
from .secrets import *

log = logging.getLogger(__name__)


class FWriterApp(App):
    BINDINGS: ClassVar[list[BindingType]] = [("ctrl+c", "quit", "Exit app")]

    MODES: ClassVar[dict[str, str | Callable[[], Screen]]] = {
        "login": LoginScreen,
    }

    main_screen: MainScreen | None = None
    force_quit_until = 0.0

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

    def on_mount(self) -> None:
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
            self.switch_mode("login")
            return
        self.login_saved(cookies, user_agent)

    # в воркере, чтобы приложение отвечало на клавиши, пока идёт запрос
    @work(exclusive=True)
    async def login_saved(self, cookies: dict[str, str], user_agent: str) -> None:
        try:
            api = await FicbookAPI.login(cookies, user_agent)
        except LoginFailedError as e:
            log.warning("вход по сохранённым cookies не удался", exc_info=True)
            self.switch_mode("login")
            self.push_screen(ErrorScreen(f"Не удалось войти: {e}"))
            return
        self.open_main(api)

    def open_main(self, api: FicbookAPI) -> None:
        # режим, а не install_screen: после повторного входа нужен новый MainScreen
        if self.main_screen:  # остался после истёкшей сессии с незаписанным черновиком
            # shortcut: если запись не удастся и здесь, правки пропадут без вопроса;
            # спрашивать, как при выходе, если такой сбой случится на деле
            self.remove_mode("main")
        main = self.main_screen = MainScreen(api)
        self.add_mode("main", lambda: main)
        self.switch_mode("main")

    def on_login_screen_logged_in(self, message: LoginScreen.LoggedIn) -> None:
        self.open_main(message.api)

    def on_main_screen_session_expired(
        self, message: MainScreen.SessionExpired
    ) -> None:
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
