import asyncio
import json
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
