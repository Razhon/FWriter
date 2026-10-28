import logging

from textual.message import Message
from textual.widget import Widget

from ..core.api.errors import (
    FicbookAPIError,
    NetworkError,
    SiteError,
    UserNotLoggedInError,
)
from .error import ErrorScreen

log = logging.getLogger(__name__)


class SessionExpired(Message):
    """Сессия истекла; обрабатывает App."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__()


def report_api_error(widget: Widget, e: FicbookAPIError, action: str) -> None:
    """Ошибку API - человеку; action - что не вышло: "Не удалось скачать главу".

    Адрес запроса пишет в лог клиент API, здесь - что делал человек.
    """
    log.warning("%s", action, exc_info=e)
    text = f"{action}\n{e}"
    match e:
        case UserNotLoggedInError():
            widget.post_message(SessionExpired(str(e)))
        # временная: повторит сам человек
        case NetworkError() | SiteError(temporary=True):
            widget.notify(text, severity="warning")
        case SiteError():
            widget.notify(text, severity="error")
        # APIChangedError и прочее: само не пройдёт, нужна новая версия
        case _ if isinstance(widget.app.screen, ErrorScreen):
            # серия ошибок - в одно окно, а не стопкой
            widget.app.screen.add(text)
        case _:
            widget.app.push_screen(ErrorScreen(text))
