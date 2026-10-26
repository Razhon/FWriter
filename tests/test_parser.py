import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest

from fwriter.core.api.client import FicbookAPI, Role
from fwriter.core.api.errors import (
    APIChangedError,
    LoginFailedError,
    UserNotLoggedInError,
)
from fwriter.core.api.models import Chapter, UserInfo

FIXTURES = Path(__file__).parent / "fixtures"
BETA_PAGE = (FIXTURES / "assistant_fanfics-beta.html").read_text(encoding="utf-8")


def mock_api(handler) -> FicbookAPI:
    api = FicbookAPI({})
    api.client = httpx.AsyncClient(
        base_url=FicbookAPI.BASE_URL, transport=httpx.MockTransport(handler)
    )
    return api


def fetch[T](
    html: str, call: Callable[[FicbookAPI], Coroutine[Any, Any, T]], path: str
) -> T:
    """Вызов API на странице html с проверкой, что запрошен ровно path."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, text=html)

    result = asyncio.run(call(mock_api(handler)))
    assert requested == [path]
    return result


def parse(html: str, role: Role = "beta"):
    return fetch(html, lambda api: api.get_fics(role), FicbookAPI.FIC_LISTS[role])


def test_beta_page():
    fics = parse(BETA_PAGE)

    assert [(f.id, len(f.chapters)) for f in fics] == [
        ("1001", 1),
        ("1002", 9),
        ("1003", 9),
    ]
    assert fics[0].title == "Фанфик 1"
    assert fics[1].chapters[0].id == "2002"
    assert fics[1].chapters[0].title == "Глава 1. Название 1"


def test_changed_layout_raises():
    broken = BETA_PAGE.replace("myfic-header", "renamed-header")

    with pytest.raises(APIChangedError, match="названием фанфика"):
        parse(broken)


def test_non_numeric_fic_id_raises():
    broken = BETA_PAGE.replace('data-id="1001"', 'data-id="fic-1001"')

    with pytest.raises(APIChangedError, match="Нечисловой id"):
        parse(broken)


GOOD = {"rme": "1", "PHPSESSID": "2", "cf_clearance": "3"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            json.dumps(
                [{"name": k, "value": v} for k, v in {**GOOD, "_ga": "x"}.items()]
            ),
            GOOD,
        ),
        ("мусор", None),
        ("{}", None),
        ('{"rme": "1", "PHPSESSID": "2"}', None),
        ("[1]", None),
        ('[{"name": "rme"}]', None),
        ('[{"name": "rme", "value": "1"}]', None),
    ],
)
def test_parse_cookies(text: str, expected: dict[str, str] | None):
    assert FicbookAPI.parse_cookies(text) == expected


PROFILE = '<div class="profile-area"><a href="/home/news">x</a><a href="/authors/42">x</a></div>'
AVATAR = '<a class="btn-top-bar-profile-menu"><img alt=" Серый – Кот "></a>'
HOME_URL = "https://ficbook.net/"


@pytest.mark.parametrize(
    ("response", "ok"),
    [
        (httpx.Response(200, text=AVATAR + PROFILE), True),
        (httpx.Response(200, text=AVATAR), False),  # не вошли
        (httpx.Response(200, text=PROFILE), False),  # шапка поменялась
        (httpx.Response(200, text="<html>Cloudflare</html>"), False),
        (httpx.Response(403, request=httpx.Request("GET", HOME_URL)), False),
    ],
)
def test_login(monkeypatch, response: httpx.Response, ok: bool):
    closed: list[bool] = []

    async def fake_get(self, path: str) -> httpx.Response:
        assert path == "/"
        if response.status_code != 200:
            response.raise_for_status()
        return response

    async def fake_close(self) -> None:
        closed.append(True)

    monkeypatch.setattr(FicbookAPI, "_get", fake_get)
    monkeypatch.setattr(FicbookAPI, "close", fake_close)
    login = FicbookAPI.login({})

    if ok:
        api = asyncio.run(login)
        assert api.user == UserInfo(42, "Серый – Кот")
        assert not closed
    else:
        with pytest.raises(LoginFailedError):
            asyncio.run(login)
        assert closed


def test_expired_session_get_raises():
    logged_out = BETA_PAGE.replace("profile-area", "login-area")

    with pytest.raises(UserNotLoggedInError):
        parse(logged_out)


@pytest.mark.parametrize(
    ("headers", "error"),
    [
        ({"location": "https://ficbook.net/login"}, UserNotLoggedInError),
        ({"location": "/home/myfics/1/parts/2"}, None),  # откат отвечает 302 на главу
        ({}, httpx.HTTPStatusError),  # 3xx без Location
    ],
)
def test_post_redirect(headers: dict[str, str], error: type[Exception] | None):
    api = mock_api(lambda _: httpx.Response(302, headers=headers))
    post = api._post("/home/versions/revert_part_version")

    if error:
        with pytest.raises(error):
            asyncio.run(post)
    else:
        assert asyncio.run(post).status_code == 302


def get_chapter(html: str) -> Chapter:
    return fetch(html, lambda api: api.get_chapter("1", "2"), "/home/myfics/1/parts/2")


def chapter_page(name: str) -> str:
    return (FIXTURES / f"myfics-00000000-parts-00000000_{name}_raw.html").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("name", "status", "last_line"),
    [("author_draft", 1, "<tab>Долор 2"), ("author_published", 2, "<tab>Долор 4")],
)
def test_get_chapter_author(name: str, status: int, last_line: str):
    chapter = get_chapter(chapter_page(name))

    assert chapter == Chapter(
        title="Лорем ипсум",
        content=f"Лорем <b>ипсум</b>\n{last_line}",
        title_editable=True,
        status=status,
    )


def test_get_chapter_beta():
    chapter = get_chapter(chapter_page("beta"))

    assert chapter.title == "Лорем 1. Ипсум долор"
    assert not chapter.title_editable
    assert chapter.status is None
    assert chapter.content.startswith("Лорем ипсум долор, сит амет")
    assert chapter.content.endswith("долор сит амет консектетур адиписцинг.")


@pytest.mark.parametrize(
    ("old", "new", "what"),
    [
        ('id="savePartForm"', 'id="renamedForm"', "savePartForm"),
        ("initial-value=", "data-value=", "initial-value"),
        ('checked="" name="param"', 'name="param"', "статус"),
    ],
)
def test_get_chapter_changed_layout_raises(old: str, new: str, what: str):
    with pytest.raises(APIChangedError, match=what):
        get_chapter(chapter_page("author_draft").replace(old, new, 1))
