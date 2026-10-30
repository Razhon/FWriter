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
from fwriter.core.api.models import Chapter, Revision, Role, UserInfo

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
    # неопубликованная глава помечена классом part-draft
    assert fics[0].chapters[0].draft
    assert not fics[1].chapters[0].draft


def test_changed_layout_raises():
    broken = BETA_PAGE.replace("myfic-header", "renamed-header")

    with pytest.raises(APIChangedError, match="названием фанфика"):
        parse(broken)


def test_chapter_link_without_class_raises():
    broken = BETA_PAGE.replace('<a class="part word-break" href', "<a href", 1)

    with pytest.raises(APIChangedError, match="классы ссылки на главу"):
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


USER_INFO = "<script>window.ficbook = { userInfo: { slug: '42', isLoggedIn: true, }, };</script>"
LOGGED_OUT = USER_INFO.replace("'42', isLoggedIn: true", "'', isLoggedIn: false")
AVATAR = '<a class="btn-top-bar-profile-menu"><img alt=" Серый – Кот "></a>'
HOME_URL = "https://ficbook.net/"


@pytest.mark.parametrize(
    ("response", "ok"),
    [
        (httpx.Response(200, text=AVATAR + USER_INFO), True),
        (httpx.Response(200, text=AVATAR + LOGGED_OUT), False),  # не вошли
        (httpx.Response(200, text=USER_INFO), False),  # шапка поменялась
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
    logged_out = BETA_PAGE.replace("isLoggedIn: true", "isLoggedIn: false")

    with pytest.raises(UserNotLoggedInError):
        parse(logged_out)


@pytest.mark.parametrize(
    ("old", "new"), [("userInfo", "user"), ("isLoggedIn: true,", "")]
)
def test_get_without_user_info_raises(old: str, new: str):
    with pytest.raises(APIChangedError, match="userInfo"):
        parse(BETA_PAGE.replace(old, new))


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
        history_url="/home/versions/mine/1001/parts/2001/history",
    )


def test_get_chapter_beta():
    chapter = get_chapter(chapter_page("beta"))

    assert chapter.title == "Лорем 1. Ипсум долор"
    assert not chapter.title_editable
    assert chapter.status is None
    assert chapter.history_url == "/home/versions/participated/1001/parts/2001/history"
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
    # 200 у _get требует userInfo с isLoggedIn: true
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


def create(answer: httpx.Response, html: str | None = None) -> tuple[str, list]:
    """create_chapter на фикстуре addpart; возвращает id и отправленные поля."""
    if html is None:
        html = (FIXTURES / "myfics-00000000-addpart.html").read_text(encoding="utf-8")
    sent: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/home/myfics/1/addpart"
            return httpx.Response(200, text=html)
        sent.extend(parse_qsl(request.content.decode(), keep_blank_values=True))
        sent.insert(0, ("<url>", request.url.path))
        return answer

    chapter_id = asyncio.run(mock_api(handler).create_chapter("1", "Т", TEXT))
    return chapter_id, sent


def test_create_chapter():
    # формат redirect проверен на сайте 28.10.2026
    answer = {"result": True, "data": {"redirect": "/home/myfics/1/parts/2002"}}
    chapter_id, sent = create(httpx.Response(200, json=answer))

    assert chapter_id == "2002"
    fields = dict(sent)
    assert fields["<url>"] == "/home/fanfics/partadd_save"
    assert fields["title"] == "Т"
    assert fields["insert_after"] == "3"  # значение common-select по умолчанию
    assert fields["param"] == "1"
    assert fields["content"] == TEXT


def test_create_chapter_sends_draft_status():
    html = (FIXTURES / "myfics-00000000-addpart.html").read_text(encoding="utf-8")
    html = html.replace(
        '<input checked="" name="param" type="radio" value="1"/>',
        '<input name="param" type="radio" value="1"/>',
        1,
    )
    html = html.replace(
        '<input name="param" type="radio" value="2"/>',
        '<input checked="" name="param" type="radio" value="2"/>',
        1,
    )
    answer = {"result": True, "data": {"redirect": "/home/myfics/1/parts/2002"}}
    _, sent = create(httpx.Response(200, json=answer), html)
    assert [value for name, value in sent if name == "param"] == ["1"]


def test_create_chapter_redirect_without_id():
    answer = {"result": True, "data": {"redirect": "/home/myfics/1/parts"}}
    with pytest.raises(APIChangedError, match="id новой главы"):
        create(httpx.Response(200, json=answer))


@pytest.mark.parametrize("attr", ["", ":initial-value=\"'3'\""])
def test_common_select_bad_value_raises(attr: str):
    html = (FIXTURES / "myfics-00000000-addpart.html").read_text(encoding="utf-8")
    html = html.replace(':initial-value="3"', attr, 1)
    with pytest.raises(APIChangedError, match="insert_after"):
        create(OK, html)


@pytest.mark.parametrize("action", ["show_part", "hide_part", "disable_auto_pub"])
def test_set_chapter_status(action):
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, request.content.decode()))
        return httpx.Response(200, json={"result": True})

    asyncio.run(mock_api(handler).set_chapter_status("2001", action))
    assert sent == [(f"/home/myfics/{action}", "part_id=2001")]


def history_page(name: str) -> str:
    return (FIXTURES / f"versions-{name}.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("kind", "count", "last"),
    [("mine", 5, ("user-1", "3001")), ("participated", 32, ("user-2", "3002"))],
)
def test_get_chapter_revisions(kind: str, count: int, last: tuple[str, str]):
    url = f"/home/versions/{kind}/1001/parts/2001/history"
    html = history_page(f"{kind}-00000000-parts-00000000-history")
    revisions = fetch(html, lambda api: api.get_chapter_revisions(url), url)

    assert [r.num for r in revisions] == list(range(count, 0, -1))
    newest, oldest = revisions[0], revisions[-1]
    # у последней правки id не виден, у остальных есть
    assert newest.rev_id is None
    assert newest.url == f"/home/versions/{kind}/2001/history/last"
    assert all(r.rev_id and r.rev_id.isdecimal() for r in revisions[1:])
    assert oldest.url == f"/home/versions/{kind}/2001/history/{oldest.rev_id}"
    assert (newest.author_name, newest.author) == ("user-1", "3001")
    assert (oldest.author_name, oldest.author) == last
    assert newest.comment.startswith("возврат к версии от")
    assert all(len(r.created) == len("2026-10-26T02:05") for r in revisions)


def test_get_chapter_revisions_date_from_title():
    url = "/home/versions/mine/1001/parts/2001/history"
    html = history_page("mine-00000000-parts-00000000-history")
    revisions = fetch(html, lambda api: api.get_chapter_revisions(url), url)
    # в тексте "1 минуту назад", абсолютная дата - только в title
    assert revisions[0].created == "2026-10-26T02:05"


def test_get_chapter_revisions_empty_raises():
    url = "/home/versions/mine/1001/parts/2001/history"
    html = USER_INFO + "<main><table><tbody></tbody></table></main>"
    with pytest.raises(APIChangedError, match="нет строк"):
        fetch(html, lambda api: api.get_chapter_revisions(url), url)


@pytest.mark.parametrize(
    ("text", "iso"),
    [
        ("26 октября 2026 г., 02:05", "2026-10-26T02:05"),
        ("1 мая 2026 г., 14:47", "2026-05-01T14:47"),
    ],
)
def test_parse_list_date(text: str, iso: str):
    assert FicbookAPI._parse_list_date(text) == iso


@pytest.mark.parametrize("text", ["1 минуту назад", "26 октябрь 2026 г., 02:05"])
def test_parse_list_date_unknown_raises(text: str):
    with pytest.raises(APIChangedError):
        FicbookAPI._parse_list_date(text)


@pytest.mark.parametrize(
    ("name", "created", "author", "can_revert"),
    [
        ("mine-00000000-history-00000000", "2026-10-26T00:33:30", "3001", True),
        ("mine-00000000-history-00000000_initial", None, "3001", True),
        ("participated-00000000-history-00000000", "2026-09-12T21:47:07", "3002", True),
        ("participated-00000000-history-last", "2026-10-26T00:32:52", "3001", False),
    ],
)
def test_get_revision(name: str, created: str | None, author: str, can_revert: bool):
    url = "/home/versions/mine/2001/history/9001"
    revision = fetch(history_page(name), lambda api: api.get_revision(url), url)

    assert revision.created == created
    assert revision.author == author
    assert revision.author_name == f"user-{author[-1]}"
    assert revision.can_revert == can_revert


def test_get_revision_text_and_comment():
    url = "/home/versions/participated/2001/history/last"
    html = history_page("participated-00000000-history-last")
    revision = fetch(html, lambda api: api.get_revision(url), url)

    assert revision.comment == "возврат к версии от 24 сентября 2026 г., 14:47"
    # #after - сырая разметка главы, сущности раскрыты
    assert "<center>«Амет консектетур», — адиписцинг элит сед до.</center>" in (
        revision.content
    )
    assert "&lt;" not in revision.content


def test_get_revision_text_matches_chapter():
    # синхронизация сравнивает #after с текстом главы: расхождение даст ложную правку
    url = "/home/versions/mine/2001/history/9001"
    html = history_page("mine-00000000-history-00000000_raw")
    revision = fetch(html, lambda api: api.get_revision(url), url)
    assert revision.content == get_chapter(chapter_page("author_draft")).content


def test_get_revision_initial():
    url = "/home/versions/mine/2001/history/9001"
    html = history_page("mine-00000000-history-00000000_initial")
    revision = fetch(html, lambda api: api.get_revision(url), url)
    assert revision == Revision(
        created=None,
        author="3001",
        author_name="user-1",
        comment="",
        content="лорем",
        can_revert=True,
    )


def test_get_revision_keeps_entities():
    # так сайт отдал "a &lt; b &amp; c & d": каждый & экранирован
    html = history_page("mine-00000000-history-00000000").replace(
        '<div id="after" style="display: none;">лорем</div>',
        '<div id="after" style="display: none;">a &amp;lt; b &amp;amp; c &amp; d</div>',
    )
    url = "/home/versions/mine/2001/history/9001"
    revision = fetch(html, lambda api: api.get_revision(url), url)
    assert revision.content == "a &lt; b &amp; c & d"


def test_get_revision_unknown_header_raises():
    html = history_page("mine-00000000-history-00000000").replace(
        "Изменения от", "Правка от", 1
    )
    url = "/home/versions/mine/2001/history/9001"
    with pytest.raises(APIChangedError, match="заголовок правки"):
        fetch(html, lambda api: api.get_revision(url), url)


def test_history_urls():
    assert (
        FicbookAPI.last_revision_url("/home/versions/mine/1001/parts/2001/history")
        == "/home/versions/mine/2001/history/last"
    )
    participated = FicbookAPI.participated_history_url("1001", "2001")
    assert participated == "/home/versions/participated/1001/parts/2001/history"
    assert FicbookAPI.last_revision_url(participated) == (
        "/home/versions/participated/2001/history/last"
    )
    with pytest.raises(APIChangedError):
        FicbookAPI.last_revision_url("/home/versions")


def revert(
    answer: httpx.Response,
    name: str = "mine-00000000-history-00000000",
    expected: str | None = None,
):
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=history_page(name))
        sent.append((request.url.path, parse_qsl(request.content.decode())))
        return answer

    url = "/home/versions/mine/2001/history/9001"
    api = mock_api(handler)
    if expected is None:
        expected = asyncio.run(api.get_revision(url)).content
    done = asyncio.run(api.revert_revision(url, expected))
    assert done == bool(sent)
    return sent


def test_revert_revision():
    sent = revert(
        httpx.Response(302, headers={"location": "/home/myfics/1001/parts/2001"})
    )
    assert sent == [
        (
            "/home/versions/revert_part_version",
            [("versionId", "9001"), ("do_submit", "Вернуться к этой версии")],
        )
    ]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(404), SiteError),
        (httpx.Response(403), SiteError),
        (httpx.Response(200, text="ok"), APIChangedError),
    ],
)
def test_revert_revision_errors(answer: httpx.Response, error: type[Exception]):
    with pytest.raises(error):
        revert(answer)


def test_revert_skips_other_text():
    # текст правки не тот, что восстановлен: отката нет
    assert revert(httpx.Response(302), expected="Другой текст.") == []


def test_revert_last_revision_raises():
    # у последней правки вместо формы плашка "Это текущая версия"
    with pytest.raises(APIChangedError, match="форма отката"):
        revert(httpx.Response(302), "participated-00000000-history-last")


def cookie_api(handler) -> FicbookAPI:
    start = {"PHPSESSID": "old", "rme": "r"}
    api = FicbookAPI(start)
    api.client = httpx.AsyncClient(
        base_url=FicbookAPI.BASE_URL,
        cookies=start,
        transport=httpx.MockTransport(handler),
    )
    return api


RENEW = {"set-cookie": "PHPSESSID=new; path=/; domain=.ficbook.net"}


def test_renewed_cookie_replaces_start_one():
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers["cookie"])
        return httpx.Response(200, text=BETA_PAGE, headers=RENEW)

    api = cookie_api(handler)
    assert api.renewed_cookies() is None

    async def twice() -> None:
        await api._get("/")
        await api._get("/")

    asyncio.run(twice())
    assert sorted(sent[1].split("; ")) == ["PHPSESSID=new", "rme=r"]
    assert api.renewed_cookies() == {"PHPSESSID": "new", "rme": "r"}


def test_renewed_cookies_dropped_after_logout():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={**RENEW, "location": "/login"})

    api = cookie_api(handler)
    with pytest.raises(UserNotLoggedInError):
        asyncio.run(api._get("/"))
    assert api.renewed_cookies() is None
