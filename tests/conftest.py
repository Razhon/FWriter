import pytest
from textual.keys import KEY_TO_UNICODE_NAME

from fwriter.core.api.client import FicbookAPI

# pilot шлёт backspace с символом ^H, а терминал - с DEL; ^H у нас ctrl+backspace
KEY_TO_UNICODE_NAME["backspace"] = "DELETE"


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    """Окно лимита запросов нужно сайту, а не MockTransport."""
    monkeypatch.setattr(FicbookAPI, "RATE_WINDOW", 0.0)
