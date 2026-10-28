import pytest

from fwriter.core.api.client import FicbookAPI


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    """Окно лимита запросов нужно сайту, а не MockTransport."""
    monkeypatch.setattr(FicbookAPI, "RATE_WINDOW", 0.0)
