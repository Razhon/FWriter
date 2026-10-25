import asyncio
from pathlib import Path

import httpx
import pytest

from fwriter.core.api.client import FicbookAPI
from fwriter.core.api.errors import APIChangedError

BETA_PAGE = (
    Path(__file__).parent / "fixtures" / "assistant_fanfics-beta.html"
).read_text(encoding="utf-8")


def parse(html: str, role: str = "beta"):
    api = FicbookAPI({})
    requested: list[str] = []

    async def fake_get(path: str) -> httpx.Response:
        requested.append(path)
        return httpx.Response(200, text=html)

    api._get = fake_get
    fics = asyncio.run(api.get_fics(role))
    assert requested == [FicbookAPI.FIC_LISTS[role]]
    return fics


def test_beta_page():
    fics = parse(BETA_PAGE)

    assert [(f.id, len(f.chapters)) for f in fics] == [
        (1001, 1),
        (1002, 9),
        (1003, 9),
    ]
    assert fics[0].title == "Фанфик 1"
    assert fics[1].chapters[0].id == 2002
    assert fics[1].chapters[0].title == "Глава 1. Название 1"


def test_user_name_with_spaces():
    pages = {
        "/": '<div class="profile-area"><a href="/authors/42">x</a></div>',
        "/authors/42": "<title>Серый – Кот – профиль автора фанфиков и ориджиналов"
        " – Книга Фанфиков</title>",
    }
    api = FicbookAPI({})

    async def fake_get(path: str) -> httpx.Response:
        return httpx.Response(200, text=pages[path])

    api._get = fake_get
    user = asyncio.run(api.get_user_info())
    assert (user.id, user.name) == (42, "Серый – Кот")


def test_changed_layout_raises():
    broken = BETA_PAGE.replace("myfic-header", "renamed-header")

    with pytest.raises(APIChangedError, match="названием фанфика"):
        parse(broken)
