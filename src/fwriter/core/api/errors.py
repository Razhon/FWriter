class FicbookAPIError(Exception):
    pass


class APIChangedError(FicbookAPIError):
    pass


class UserNotLoggedInError(FicbookAPIError):
    pass
