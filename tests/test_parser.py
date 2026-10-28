import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from fwriter.core.api.client import FicbookAPI
from fwriter.core.api.errors import (
    APIChangedError,
    ChapterChangedError,
    LoginFailedError,
    NetworkError,
    SiteError,
    SiteRejectedError,
    UserNotLoggedInError,
)
from fwriter.core.api.models import Chapter, Role, UserInfo

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
    # счётчик "Ждут: +N" вложен в заголовок главы и в название не входит
    assert [ch.title for ch in fics[1].chapters[:3]] == [
        "Глава 1. Название 1",
        "Глава 2. Название 2",
        "Глава 3. Название 3",
    ]


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
            raise SiteError(response.status_code)
        return response

    async def fake_close(self) -> None:
        closed.append(True)

    monkeypatch.setattr(FicbookAPI, "_get", fake_get)
    monkeypatch.setattr(FicbookAPI, "close", fake_close)
    login = FicbookAPI.login({})

    if ok:
        api = asyncio.run(login)
        assert api.user == UserInfo("42", "Серый – Кот")
        assert not closed
    else:
        with pytest.raises(LoginFailedError):
            asyncio.run(login)
        assert closed


@pytest.mark.parametrize(
    "error",
    [NetworkError("Нет связи с сайтом"), SiteError(503), SiteError(429)],
)
def test_login_site_unavailable_passes_through(monkeypatch, error):
    """Недоступный сайт - не LoginFailedError: run.py уходит в офлайн, а не на вход."""

    async def fake_get(self, path: str) -> httpx.Response:
        raise error

    monkeypatch.setattr(FicbookAPI, "_get", fake_get)
    with pytest.raises(type(error)):
        asyncio.run(FicbookAPI.login({}))


def test_expired_session_get_raises():
    logged_out = BETA_PAGE.replace("profile-area", "login-area")

    with pytest.raises(UserNotLoggedInError):
        parse(logged_out)


@pytest.mark.parametrize(
    ("headers", "error"),
    [
        ({"location": "https://ficbook.net/login"}, UserNotLoggedInError),
        ({"location": "/home/myfics/1/parts/2"}, None),  # откат отвечает 302 на главу
        ({}, SiteError),  # 3xx без Location
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


def raise_(error: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


@pytest.mark.parametrize(
    ("handler", "text"),
    [
        (raise_(httpx.ConnectError("getaddrinfo failed")), "Нет связи с сайтом"),
        (raise_(httpx.ReadTimeout("")), "Сайт не ответил вовремя"),
        (lambda _: httpx.Response(503), "Сайт временно не работает (ошибка 503)"),
        (lambda _: httpx.Response(400), "Сайт ответил ошибкой 400"),
        (lambda _: httpx.Response(403), SiteError.TEXTS[403]),
        (lambda _: httpx.Response(404), SiteError.TEXTS[404]),
        (lambda _: httpx.Response(429), SiteError.TEXTS[429]),
    ],
)
def test_http_errors_become_api_errors(handler, text: str):
    # UI показывает str(e), поэтому текст для человека, а не сообщение httpx
    with pytest.raises((NetworkError, SiteError)) as e:
        asyncio.run(mock_api(handler)._get("/"))
    assert str(e.value) == text


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


@pytest.mark.parametrize(
    ("name", "slug"),
    [("old", "3001"), ("new", "00000000-0000-7000-8000-000000003002")],
)
def test_parse_user_slug(name: str, slug: str):
    """У нового аккаунта в ссылке на профиль UUID, а не числовой id."""
    html = (FIXTURES / f"index_{name}_account_raw.html").read_text(encoding="utf-8")
    assert FicbookAPI._parse_user(html).slug == slug


@pytest.mark.parametrize(("status", "readonly"), [(200, False), (404, True)])
def test_is_readonly(status: int, readonly: bool):
    """Работу без прав от работы без глав различает код ответа /parts."""
    # 200 у _get требует блок профиля
    page = (FIXTURES / "index_old_account_raw.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/home/myfics/1001/parts"
        return httpx.Response(status, text=page)

    assert asyncio.run(mock_api(handler).is_readonly("1001")) is readonly


def test_is_readonly_keeps_other_errors():
    """Не 404 на /parts - не readonly, а ошибка: иначе 429 спрятал бы главы."""
    with pytest.raises(SiteError) as e:
        asyncio.run(mock_api(lambda _: httpx.Response(429)).is_readonly("1001"))
    assert e.value.status == 429


def test_coauthor_list_without_chapters():
    """Карточки без прав и без глав обе приходят без глав и без запросов /parts."""
    html = (FIXTURES / "assistant_fanfics-coauthor_raw.html").read_text(
        encoding="utf-8"
    )
    fics = asyncio.run(
        mock_api(lambda _: httpx.Response(200, text=html)).get_fics("coauthor")
    )
    assert [(f.id, f.chapters) for f in fics] == [("1002", []), ("1001", [])]


def test_rate_limit_window(monkeypatch):
    """Не больше RATE_LIMIT запросов за RATE_WINDOW, и при параллельных вызовах."""
    monkeypatch.setattr(FicbookAPI, "RATE_LIMIT", 2)
    monkeypatch.setattr(FicbookAPI, "RATE_WINDOW", 0.3)
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(time.monotonic())
        return httpx.Response(200, text=BETA_PAGE)

    async def five_parallel():
        api = mock_api(handler)
        await asyncio.gather(*(api.get_fics("beta") for _ in range(5)))

    asyncio.run(five_parallel())
    assert len(starts) == 5
    # первые два сразу, дальше каждый ждёт, пока из окна выйдет запрос на 2 раньше
    assert starts[1] - starts[0] < 0.1
    assert all(b - a >= 0.29 for a, b in zip(starts, starts[2:], strict=False))


TEXT = "Новый\n<tab>текст"


def save(
    html: str, answer: httpx.Response, base: str | None = None
) -> list[tuple[str, str]]:
    """save_chapter на странице главы html; возвращает отправленные поля.

    base по умолчанию - текст главы на странице, то есть сайт не менялся.
    """
    if base is None:
        base = get_chapter(html).content
    sent: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/home/myfics/1/parts/2"
            return httpx.Response(200, text=html)
        sent.extend(parse_qsl(request.content.decode(), keep_blank_values=True))
        sent.insert(0, ("<url>", request.url.path))
        return answer

    asyncio.run(mock_api(handler).save_chapter("1", "2", TEXT, "Т", base))
    return sent


def test_save_chapter_changed_on_site():
    """Главу изменили на сайте после синхронизации: до POST не доходит."""
    post = httpx.Response(500)  # дошло бы - был бы SiteError
    with pytest.raises(ChapterChangedError):
        save(chapter_page("author_draft"), post, base="Старый текст")


OK = httpx.Response(200, json={"result": True, "data": {"redirect": "/"}})


def test_save_chapter_author():
    sent = save(chapter_page("author_draft"), OK)

    assert sent == [
        ("<url>", "/home/fanfics/partauthoredit_save"),
        ("part_id", "2001"),
        ("title", "Т"),
        ("comment_before", ""),
        ("comment_after", ""),
        ("param", "1"),
        ("auto_pub", "1"),
        ("part_auto_pub_date", "2026-11-02 02:06"),
        ("continue_required_votes", "0"),
        ("content", TEXT),
    ]


def test_save_chapter_coauthor_keeps_deferred():
    """Статус и дата отложенной главы уходят как есть, пояснение - пустым."""
    sent = dict(save(chapter_page("coauthor_deferred"), OK))

    assert sent["param"] == "3"
    assert sent["part_auto_pub_date"] == "2026-12-30 12:00"
    assert sent["change_description"] == ""
    assert sent["title"] == "Т"


def test_save_chapter_beta():
    """Название у беты disabled и не уходит; примечания - как есть."""
    sent = dict(save(chapter_page("beta"), OK))

    assert sent["<url>"] == "/home/fanfics/partbetaedit_save"
    assert "title" not in sent
    assert "param" not in sent
    assert sent["change_description"] == ""
    assert sent["comment_before"] != ""


def test_save_chapter_unknown_field_raises():
    html = chapter_page("author_draft").replace(
        "<textarea class=", '<select name="x"></select><textarea class=', 1
    )
    with pytest.raises(APIChangedError, match="select x"):
        save(html, OK)


@pytest.mark.parametrize(
    ("answer", "error", "text"),
    [
        (
            {"result": False, "error": {"title": "Пусто.", "content": "Нет текста."}},
            SiteRejectedError,
            "Пусто.; Нет текста.",
        ),
        ({"error": {"title": "Длинно"}}, SiteRejectedError, "Длинно"),
        ({"result": False, "error": "Строкой"}, SiteRejectedError, "Строкой"),
        # капча раньше error
        (
            {"result": False, "captcha_required": True, "error": "x"},
            SiteRejectedError,
            SiteRejectedError.CAPTCHA_TEXT,
        ),
        ({"result": False}, APIChangedError, "ни result, ни error"),
        ([], APIChangedError, "не объектом"),
    ],
)
def test_post_form_answers(answer, error: type[Exception], text: str):
    with pytest.raises(error) as e:
        save(chapter_page("author_draft"), httpx.Response(200, json=answer))
    assert text in str(e.value)
    if isinstance(e.value, SiteRejectedError):
        assert e.value.temporary == ("captcha_required" in answer)


def test_post_form_not_json():
    with pytest.raises(APIChangedError, match="не JSON"):
        save(chapter_page("author_draft"), httpx.Response(200, text="<html>"))
