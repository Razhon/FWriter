import asyncio
import json
import logging
import re
import time
from collections import deque
from typing import ClassVar, Literal

import httpx
from bs4 import BeautifulSoup, Tag

from .errors import *
from .models import (
    Chapter,
    Fic,
    Revision,
    RevisionInfo,
    Role,
    SlimChapterInfo,
    UserInfo,
)

log = logging.getLogger(__name__)


def req[T](value: T | None, what: str) -> T:
    """Требует, чтобы value было не None/не пустым, иначе - APIChangedError."""
    if not value:
        raise APIChangedError(
            f"Не найдено на странице: {what}. Похоже, ficbook изменил разметку."
        )
    return value


class FicbookAPI:
    BASE_URL = "https://ficbook.net"
    # только cookies сессии: полный экспорт из браузера не влезает в keyring на Windows
    AUTH_COOKIES = frozenset({"rme", "PHPSESSID", "cf_clearance"})

    # роль -> страница со списком. Ссылки на главы на всех страницах одинаковые.
    FIC_LISTS: ClassVar[dict[Role, str]] = {
        "author": "/home/myfics",
        # соавтор и сопереводчик на одной странице и оба coauthor, переводчик - author
        "coauthor": "/home/assistant_fanfics/coauthor",
        "beta": "/home/assistant_fanfics/beta",
        "gamma": "/home/assistant_fanfics/gamma",
    }

    # slug, а не id: у новых аккаунтов в ссылке UUID
    _USER_SLUG_RE = re.compile(r"/authors/([^/?#]+)")
    _CHAPTER_RE = re.compile(r"/home/myfics/(\d+)/parts/(\d+)")
    REVERT_URL = "/home/versions/revert_part_version"
    _HISTORY_RE = re.compile(
        r"^/home/versions/(mine|participated)/(\d+)/parts/(\d+)/history$"
    )
    _REVISION_RE = re.compile(
        r"^/home/versions/(?:mine|participated)/\d+/history/(\w+)$"
    )
    _REVISION_DATE_RE = re.compile(r"Изменения от (\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)")
    _LIST_DATE_RE = re.compile(r"(\d{1,2}) (\w+) (\d{4}) г\., (\d\d:\d\d)")
    MONTHS: ClassVar[dict[str, int]] = {
        name: number
        for number, name in enumerate(
            [
                "января",
                "февраля",
                "марта",
                "апреля",
                "мая",
                "июня",
                "июля",
                "августа",
                "сентября",
                "октября",
                "ноября",
                "декабря",
            ],
            start=1,
        )
    }
    # window.ficbook.userInfo есть в любой странице, это JS-литерал, а не JSON
    _USER_INFO_RE = re.compile(r"userInfo:\s*\{([^}]*)\}")
    _LOGGED_IN_RE = re.compile(r"\bisLoggedIn:\s*(true|false)\b")
    _INFO_SLUG_RE = re.compile(r"\bslug:\s*'([^']+)'")

    # Cloudflare: 16-й запрос за 10 с - 429 и блок на 20 с; лимит с запасом
    RATE_LIMIT = 12
    RATE_WINDOW = 10.0

    user: UserInfo | None = None

    def __init__(self, cookies: dict[str, str], user_agent: str | None = None):
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            cookies=cookies,
            headers={"User-Agent": user_agent} if user_agent else None,
            timeout=5.0,
            follow_redirects=False,
        )
        self._start_cookies = cookies
        # сайт выкинул из сессии: её cookies сохранять незачем
        self.logged_out = False
        self._throttle = asyncio.Lock()
        # время начала последних RATE_LIMIT запросов
        self._sent: deque[float] = deque(maxlen=self.RATE_LIMIT)
        # докачка правок вкладок идёт по очереди: см. fetch_revisions()
        self.fetching = asyncio.Lock()

    @classmethod
    def parse_cookies(cls, text: str) -> dict[str, str] | None:
        """Cookies сессии из JSON-экспорта браузера; None, если их не хватает."""
        try:
            cookies = {
                c["name"]: c["value"]
                for c in json.loads(text)
                if c["name"] in cls.AUTH_COOKIES
            }
        except ValueError, KeyError, TypeError:
            return None
        # TODO: проверить какие именно ключи cookie необходимы
        return cookies if len(cookies) >= 2 else None

    @classmethod
    async def login(
        cls, cookies: dict[str, str], user_agent: str | None = None
    ) -> FicbookAPI:
        """Готовый API с проверенными cookies; иначе LoginFailedError.

        Сайт недоступен (NetworkError, временная SiteError) - ошибка как есть.
        """
        api = cls(cookies, user_agent)
        try:
            await api._check_login()
        except BaseException:  # клиент закрывается и при отмене
            await api.close()
            raise
        return api

    async def _check_login(self) -> None:
        """Вход проверяется по главной, заодно пользователь берётся."""
        try:
            response = await self._get("/")
            self.user = self._parse_user(response.text)
        except FicbookAPIError as e:
            # сайт недоступен - не повод входить заново: run.py уходит в офлайн
            if isinstance(e, NetworkError) or (
                isinstance(e, SiteError) and e.temporary
            ):
                raise
            raise LoginFailedError(str(e)) from e

    async def close(self) -> None:
        await self.client.aclose()

    def renewed_cookies(self) -> dict[str, str] | None:
        """Cookies сессии, если сайт их обновил и не выкинул из сессии; иначе None."""
        cookies = {
            c.name: c.value
            for c in self.client.cookies.jar
            if c.name in self.AUTH_COOKIES and c.value
        }
        if self.logged_out or cookies == self._start_cookies:
            return None
        return cookies

    async def get_user_info(self) -> UserInfo:
        if not self.user:  # после login() уже заполнен
            self.user = self._parse_user((await self._get("/")).text)
        return self.user

    @classmethod
    def _parse_user(cls, html: str) -> UserInfo:
        """Slug из userInfo и ник из шапки любой страницы, где пользователь вошёл."""
        # наличие userInfo уже проверил _get
        info = req(cls._USER_INFO_RE.search(html), "window.ficbook.userInfo")
        match = req(cls._INFO_SLUG_RE.search(info.group(1)), "slug в userInfo")
        soup = BeautifulSoup(html, "html.parser")
        avatar = req(
            soup.select_one(".btn-top-bar-profile-menu img[alt]"), "аватар с ником"
        )
        name = req(str(avatar.get("alt", "")).strip(), "ник в аватаре")
        return UserInfo(match.group(1), name)

    async def get_fics(self, role: Role) -> list[Fic]:
        """Список фанфиков с правами role (ключ FIC_LISTS)."""
        response = await self._get(self.FIC_LISTS[role])

        soup = BeautifulSoup(response.text, "html.parser")

        fics_unparsed = soup.find_all("div", class_="myfic")

        fics: list[Fic] = []
        for fic_unparsed in fics_unparsed:
            fic_id_raw = req(
                str(fic_unparsed.get("data-id", "")), "id фанфика (data-id)"
            )
            # isdecimal, а не isdigit: "²" - digit, но не цифра id
            if not fic_id_raw.isdecimal():
                raise APIChangedError(f"Нечисловой id фанфика: {fic_id_raw}")

            title_unparsed = req(
                fic_unparsed.select_one("div.myfic-header.word-break > span > a"),
                "ссылка с названием фанфика",
            )
            title = req(title_unparsed.string, "текст названия фанфика")

            chapters: list[SlimChapterInfo] = []
            content_list_unparsed = fic_unparsed.select_one(
                "div.fanfic-content > div.fanfic-content-list"
            )
            if content_list_unparsed:
                chapters_links_unparsed = content_list_unparsed.find_all("a")
                for ch_link in chapters_links_unparsed:
                    href = str(ch_link.get("href", ""))
                    match = req(
                        self._CHAPTER_RE.search(href), f"ссылка на главу ({href})"
                    )
                    if match.group(1) != fic_id_raw:
                        raise APIChangedError(
                            f"Глава {href} не относится к фанфику {fic_id_raw}"
                        )

                    chapter_id = match.group(2)
                    chapter_title_unparsed = req(
                        ch_link.find("span"), f"название главы {chapter_id}"
                    )

                    # только свой текст: внутри вложен счётчик "Ждут: +N"
                    own_text = chapter_title_unparsed.find_all(
                        string=True, recursive=False
                    )
                    chapter_title = req(
                        " ".join("".join(own_text).split()),
                        f"текст названия главы {chapter_id}",
                    )

                    # классы есть у любой главы, part-draft - только у неопубликованной
                    classes = req(
                        ch_link.get_attribute_list("class"),
                        f"классы ссылки на главу {chapter_id}",
                    )
                    draft = "part-draft" in classes
                    chapters.append(SlimChapterInfo(chapter_id, chapter_title, draft))

            fics.append(Fic(fic_id_raw, title, chapters))
        return fics

    async def is_readonly(self, fic_id: str) -> bool:
        """Список глав работы без права редактирования отвечает 404, с правом - 200.

        Для карточек без глав: работу без прав от работы без глав в карточке
        отличает только текст, код ответа надёжнее.
        """
        try:
            await self._get(f"/home/myfics/{fic_id}/parts")
        except SiteError as e:
            if e.status == 404:
                return True
            raise
        return False

    async def get_chapter(self, fic_id: str, chapter_id: str) -> Chapter:
        """Текст, название и статус главы со страницы её редактирования."""
        soup, form, content = await self._chapter_form(fic_id, chapter_id)
        history = req(soup.find("a", href=self._HISTORY_RE), "ссылка на историю")
        title_input = req(form.find("input", attrs={"name": "title"}), "поле названия")
        # кнопок статуса нет у беты; если они есть, одна обязана быть отмечена
        params = form.find_all("input", attrs={"name": "param"})
        status = None
        if params:
            checked = next((p for p in params if p.has_attr("checked")), None)
            status = str(req(checked, "отмеченный статус главы").get("value", ""))
        if status is not None and not status.isdecimal():
            raise APIChangedError(f"Нечисловой статус главы: {status}")

        return Chapter(
            title=str(title_input.get("value", "")),
            content=content,
            title_editable=not title_input.has_attr("disabled"),
            status=int(status) if status is not None else None,
            history_url=str(history["href"]),
        )

    async def _chapter_form(
        self, fic_id: str, chapter_id: str
    ) -> tuple[BeautifulSoup, Tag, str]:
        """Страница главы, форма с неё и текст главы на сайте."""
        try:
            response = await self._get(f"/home/myfics/{fic_id}/parts/{chapter_id}")
        except SiteError as e:
            # 403 у страницы главы бывает, только если права выключил автор
            if e.status == 403:
                raise EditingDisabledError() from e
            raise
        soup = BeautifulSoup(response.text, "html.parser")
        form = req(soup.find("form", id="savePartForm"), "форма главы (savePartForm)")
        editor = req(form.find("fanfic-editor"), "редактор главы (fanfic-editor)")
        content = editor.get("initial-value")
        if content is None:  # пустой текст допустим, отсутствие атрибута - нет
            raise APIChangedError("Не найдено на странице: текст главы (initial-value)")
        return soup, form, str(content)

    async def save_chapter(
        self, fic_id: str, chapter_id: str, content: str, title: str, base: str
    ) -> None:
        """Текст и название; base - текст сайта, от которого шёл черновик."""
        # свежая форма: старая откатила бы статус, поменянный на сайте
        _, form, site_content = await self._chapter_form(fic_id, chapter_id)
        if site_content != base:
            raise ChapterChangedError()
        url = str(req(form.get("action"), "адрес сохранения (action)"))
        overrides = {"title": title, "change_description": ""}
        fields = [
            (name, overrides.get(name, value))
            for name, value in self._form_fields(form)
        ]
        await self._post_form(url, [*fields, ("content", content)])

    async def create_chapter(self, fic_id: str, title: str, content: str) -> str:
        """Новая глава в конце работы; возвращает её id."""
        response = await self._get(f"/home/myfics/{fic_id}/addpart")
        soup = BeautifulSoup(response.text, "html.parser")
        form = req(soup.find("form", id="savePartForm"), "форма новой главы")
        url = str(req(form.get("action"), "адрес создания (action)"))
        # черновик явно: отмеченный по умолчанию статус - решение сайта
        overrides = {"title": title, "param": "1"}
        fields = [
            (name, overrides.get(name, value))
            for name, value in self._form_fields(form)
        ]
        data = await self._post_form(url, [*fields, ("content", content)])
        redirect = str(data.get("redirect", ""))
        match = req(self._CHAPTER_RE.search(redirect), f"id новой главы ({redirect})")
        return match.group(2)

    async def set_chapter_status(
        self,
        chapter_id: str,
        action: Literal["show_part", "hide_part", "disable_auto_pub"],
    ) -> None:
        """Опубликовать, скрыть в черновики или отменить отложенную публикацию."""
        await self._post_form(f"/home/myfics/{action}", [("part_id", chapter_id)])

    @staticmethod
    def participated_history_url(fic_id: str, chapter_id: str) -> str:
        """История при readonly: страницы главы нет, а mine отвечает 404."""
        return f"/home/versions/participated/{fic_id}/parts/{chapter_id}/history"

    @classmethod
    def last_revision_url(cls, history_url: str) -> str:
        match = req(
            cls._HISTORY_RE.match(history_url), f"адрес истории ({history_url})"
        )
        kind, _, chapter_id = match.groups()
        return f"/home/versions/{kind}/{chapter_id}/history/last"

    async def get_chapter_revisions(self, history_url: str) -> list[RevisionInfo]:
        """Список правок, от новой к старой."""
        response = await self._get(history_url)
        soup = BeautifulSoup(response.text, "html.parser")
        table = req(soup.select_one("main table"), "таблица правок")
        revisions = []
        for row in table.select("tbody tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) != 5:
                raise APIChangedError(f"В строке правки {len(cells)} ячеек, а не 5")
            num = cells[0].get_text(strip=True)
            if not num.isdecimal():
                raise APIChangedError(f"Нечисловой номер правки: {num}")
            date = req(cells[1].find("span", title=True), "дата правки")
            author = req(cells[2].find("a", href=True), "автор правки")
            link = req(cells[4].find("a", href=True), "ссылка на правку")
            url = str(link["href"])
            rev = req(self._REVISION_RE.match(url), f"адрес правки ({url})").group(1)
            if rev != "last" and not rev.isdecimal():
                raise APIChangedError(f"Нечисловой id правки: {rev}")
            revisions.append(
                RevisionInfo(
                    num=int(num),
                    url=url,
                    rev_id=None if rev == "last" else rev,
                    author=self._author_slug(author),
                    author_name=author.get_text(strip=True),
                    comment=cells[3].get_text(strip=True),
                    created=self._parse_list_date(str(date["title"])),
                )
            )
        # у главы всегда есть хотя бы изначальная версия
        if not revisions:
            raise APIChangedError("В истории правок нет строк")
        return revisions

    async def get_revision(self, url: str) -> Revision:
        """Страница правки: дата, автор, пояснение и текст после неё."""
        response = await self._get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        main = req(soup.find("main"), "основная часть страницы правки")
        header = req(main.find("h1"), "заголовок правки").get_text(" ", strip=True)
        created = None
        if match := self._REVISION_DATE_RE.search(header):
            created = f"{match.group(1)}T{match.group(2)}"
        elif "Изначальная версия текста" not in header:
            raise APIChangedError(f"Неизвестный заголовок правки: {header}")
        author = req(main.find("a", href=self._USER_SLUG_RE), "автор правки")
        comment = main.select_one("p.text-muted")
        after = req(main.find("div", id="after"), "текст после правки (#after)")
        return Revision(
            created=created,
            author=self._author_slug(author),
            author_name=author.get_text(strip=True),
            comment=comment.get_text(strip=True) if comment else "",
            content=after.get_text(),
            can_revert=main.find("form", action=self.REVERT_URL) is not None,
        )

    async def revert_revision(self, url: str, expected: str) -> bool:
        """Откат главы на сайте к правке url, если текст после неё - expected.

        False - текст другой, отката нет; иначе сайт создаёт новую правку.
        """
        response = await self._get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        after = req(soup.find("div", id="after"), "текст после правки (#after)")
        if after.get_text() != expected:
            return False
        form = req(soup.find("form", action=self.REVERT_URL), "форма отката")
        response = await self._post(self.REVERT_URL, data=dict(self._form_fields(form)))
        # успех - 302 на главу, ошибки (400, 403, 404) - HTML
        if not response.has_redirect_location:
            raise APIChangedError(f"Откат ответил {response.status_code}, а не 302")
        return True

    @classmethod
    def _author_slug(cls, link: Tag) -> str:
        href = str(link["href"])
        return req(cls._USER_SLUG_RE.search(href), f"slug автора ({href})").group(1)

    @classmethod
    def _parse_list_date(cls, text: str) -> str:
        """'26 октября 2026 г., 02:05' -> '2026-10-26T02:05'."""
        match = req(cls._LIST_DATE_RE.fullmatch(text), f"дата правки ({text})")
        day, month, year, time_ = match.groups()
        number = req(cls.MONTHS.get(month), f"месяц ({month})")
        return f"{year}-{number:02}-{int(day):02}T{time_}"

    @staticmethod
    def _form_fields(form) -> list[tuple[str, str]]:
        """Поля формы, как их отправит браузер."""
        fields = []
        for element in form.find_all(attrs={"name": True}):
            name = str(element["name"])
            if element.name == "common-select":
                value = str(req(element.get(":initial-value"), f"значение поля {name}"))
                # это JS-выражение Vue: строка пришла бы в кавычках или переменной
                if not value.isdecimal():
                    raise APIChangedError(f"Нечисловое значение поля {name}: {value}")
                fields.append((name, value))
                continue
            if element.name not in ("input", "textarea"):
                # не отправив select или Vue-компонент, сбросили бы настройку
                raise APIChangedError(f"Неизвестное поле формы: {element.name} {name}")
            if element.has_attr("disabled"):
                continue
            if element.name == "textarea":
                fields.append((name, element.get_text()))
            elif element.get("type") not in ("radio", "checkbox"):
                fields.append((name, str(element.get("value", ""))))
            elif element.has_attr("checked"):
                fields.append((name, str(element.get("value", "on"))))
        return fields

    async def _post_form(self, url: str, fields: list[tuple[str, str]]) -> dict:
        """POST формы; возвращает data из JSON-ответа."""
        # httpx не берёт список пар
        data: dict[str, list[str]] = {}
        for name, value in fields:
            data.setdefault(name, []).append(value)
        response = await self._post(url, data=data)
        try:
            answer = response.json()
        except ValueError as e:
            raise APIChangedError("Сайт ответил не JSON на отправку формы") from e
        if not isinstance(answer, dict):
            raise APIChangedError("Сайт ответил не объектом на отправку формы")
        # капча тоже с result: false
        if answer.get("captcha_required"):
            raise SiteRejectedError(SiteRejectedError.CAPTCHA_TEXT, captcha=True)
        if answer.get("result") is True:
            return answer.get("data") or {}
        if error := answer.get("error"):
            texts = error.values() if isinstance(error, dict) else [error]
            raise SiteRejectedError("; ".join(str(text) for text in texts))
        raise APIChangedError("В ответе на отправку формы нет ни result, ни error")

    async def _get(self, url: str) -> httpx.Response:
        response = await self._send("GET", url)
        self._raise_for_status(response)
        # открытая страница без сессии отвечает 200 с isLoggedIn: false
        info = req(self._USER_INFO_RE.search(response.text), "window.ficbook.userInfo")
        logged_in = req(
            self._LOGGED_IN_RE.search(info.group(1)), "isLoggedIn в userInfo"
        )
        if logged_in.group(1) != "true":
            self.logged_out = True
            raise UserNotLoggedInError("Сессия истекла: сайт не видит входа")
        return response

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        response = await self._send("POST", url, **kwargs)
        if not response.has_redirect_location:  # откат версии отвечает 302 на главу
            self._raise_for_status(response)
        return response

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Ошибки httpx -> наши, с текстом для человека; UI про httpx не знает."""
        # лок на ожидание, а не на запрос: окно считается по началам запросов
        async with self._throttle:
            if len(self._sent) == self._sent.maxlen:
                delay = self._sent[0] + self.RATE_WINDOW - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            self._sent.append(time.monotonic())
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.TimeoutException as e:
            log.warning("%s %s: нет ответа вовремя", method, url)
            raise NetworkError("Сайт не ответил вовремя") from e
        except httpx.HTTPError as e:
            log.warning("%s %s: нет связи", method, url)
            raise NetworkError("Нет связи с сайтом") from e
        # без заголовков и тела: там cookies и текст глав
        log.debug("%s %s -> %d", method, url, response.status_code)
        self._drop_stale_cookies()
        self._raise_if_logged_out(response)
        return response

    def _drop_stale_cookies(self) -> None:
        # стартовые cookies лежат без домена, cookie от сайта - с доменом рядом, а не
        # вместо; иначе уходят обе, старая первой, и PHP берёт её
        jar = self.client.cookies.jar
        fresh = {c.name for c in jar if c.domain}
        for c in [c for c in jar if not c.domain and c.name in fresh]:
            jar.clear(c.domain, c.path, c.name)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        # как raise_for_status() у httpx: всё, кроме 2xx, включая 3xx
        if not response.is_success:
            request = response.request
            log.warning(
                "%s %s -> %d", request.method, request.url.path, response.status_code
            )
            raise SiteError(response.status_code)

    def _raise_if_logged_out(self, response: httpx.Response) -> None:
        if (
            response.is_redirect
            and httpx.URL(response.headers.get("location", "")).path == "/login"
        ):
            self.logged_out = True
            raise UserNotLoggedInError("Сессия истекла: сайт перенаправил на /login")
