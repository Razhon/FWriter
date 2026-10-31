from typing import ClassVar


class FicbookAPIError(Exception):
    pass


class APIChangedError(FicbookAPIError):
    pass


class UserNotLoggedInError(FicbookAPIError):
    pass


class LoginFailedError(FicbookAPIError):
    pass


class NetworkError(FicbookAPIError):
    """Нет связи с сайтом или он не ответил вовремя."""


class SiteError(FicbookAPIError):
    """Сайт ответил кодом ошибки."""

    # общие: что именно не вышло, говорит action в report_api_error
    TEXTS: ClassVar[dict[int, str]] = {
        403: "Нет доступа: у вас нет прав на это действие",
        404: "Сайт не нашёл страницу: её удалили или у вас нет прав на это действие",
        429: "Сайт ограничил частоту запросов, повторите через полминуты",
    }

    def __init__(self, status: int) -> None:
        self.status = status
        if status in self.TEXTS:
            text = self.TEXTS[status]
        elif self.temporary:
            text = f"Сайт временно не работает (ошибка {status})"
        else:
            text = f"Сайт ответил ошибкой {status}"
        super().__init__(text)

    @property
    def temporary(self) -> bool:
        """Пройдёт само: лимит запросов или сбой сервера."""
        return self.status == 429 or self.status >= 500


class EditingDisabledError(SiteError):
    """Страница главы ответила 403: автор выключил помощнику редактирование."""

    TEXTS: ClassVar[dict[int, str]] = {
        **SiteError.TEXTS,
        403: "Автор выключил вам редактирование работы",
    }

    def __init__(self) -> None:
        super().__init__(403)


class SiteRejectedError(FicbookAPIError):
    """Сайт ответил на POST отказом: текст из error или капча."""

    CAPTCHA_TEXT = "Сайт требует капчу. Подождите и отправьте позже"

    def __init__(self, text: str, captcha: bool = False) -> None:
        # капча пройдёт сама, а отказ проверки - нет
        self.temporary = captcha
        super().__init__(text)


class ChapterChangedError(SiteRejectedError):
    """Текст главы на сайте не тот, от которого шёл черновик."""

    def __init__(self) -> None:
        super().__init__(
            "Пока вы готовили отправку, главу изменили на сайте. "
            "Обновитесь и отправьте снова"
        )
