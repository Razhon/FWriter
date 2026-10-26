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

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Сайт ответил ошибкой {status}")
