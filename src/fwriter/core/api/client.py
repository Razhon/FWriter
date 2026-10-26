import json
import logging
import re
from typing import ClassVar

import httpx
from bs4 import BeautifulSoup

from .errors import *
from .models import Chapter, Fic, Role, SlimChapterInfo, UserInfo

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

    _USER_ID_RE = re.compile(r"/authors/(\d+)")
    _CHAPTER_RE = re.compile(r"/home/myfics/(\d+)/parts/(\d+)")
    # div.profile-area есть в шапке любой страницы, пока сессия жива
    _PROFILE_AREA_RE = re.compile(r'class="(?:[^"]*\s)?profile-area[\s"]')

    user: UserInfo | None = None

    def __init__(self, cookies: dict[str, str], user_agent: str | None = None):
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            cookies=cookies,
            headers={"User-Agent": user_agent} if user_agent else None,
            timeout=5.0,
            follow_redirects=False,
        )

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
        """Готовый API с проверенными cookies; иначе LoginFailedError."""
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
        # shortcut: без связи тоже LoginFailedError, и run.py ведёт на вход, а не в
        # офлайн по fic.json; пропускать NetworkError наверх вместе с офлайн-режимом
        except FicbookAPIError as e:
            raise LoginFailedError(str(e)) from e

    async def close(self) -> None:
        await self.client.aclose()

    async def get_user_info(self) -> UserInfo:
        if not self.user:  # после login() уже заполнен
            self.user = self._parse_user((await self._get("/")).text)
        return self.user

    @classmethod
    def _parse_user(cls, html: str) -> UserInfo:
        """Id и ник из шапки любой страницы, где пользователь вошёл."""
        soup = BeautifulSoup(html, "html.parser")
        # наличие блока уже проверил _get
        profile_area = req(soup.find("div", class_="profile-area"), "блок профиля")
        link = req(profile_area.select_one('a[href*="/authors/"]'), "ссылка на профиль")
        match = req(cls._USER_ID_RE.search(str(link.get("href", ""))), "id автора")
        avatar = req(
            soup.select_one(".btn-top-bar-profile-menu img[alt]"), "аватар с ником"
        )
        name = req(str(avatar.get("alt", "")).strip(), "ник в аватаре")
        return UserInfo(int(match.group(1)), name)

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

                    chapters.append(SlimChapterInfo(chapter_id, chapter_title))

            fics.append(Fic(id=fic_id_raw, title=title, chapters=chapters))
        return fics

    async def get_chapter(self, fic_id: str, chapter_id: str) -> Chapter:
        """Текст, название и статус главы со страницы её редактирования."""
        response = await self._get(f"/home/myfics/{fic_id}/parts/{chapter_id}")
        soup = BeautifulSoup(response.text, "html.parser")

        form = req(soup.find("form", id="savePartForm"), "форма главы (savePartForm)")
        editor = req(form.find("fanfic-editor"), "редактор главы (fanfic-editor)")
        content = editor.get("initial-value")
        if content is None:  # пустой текст допустим, отсутствие атрибута - нет
            raise APIChangedError("Не найдено на странице: текст главы (initial-value)")
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
            content=str(content),
            title_editable=not title_input.has_attr("disabled"),
            status=int(status) if status is not None else None,
        )

    async def _get(self, url: str) -> httpx.Response:
        response = await self._send("GET", url)
        self._raise_for_status(response)
        # GET защищённой страницы без сессии отвечает 200, но без блока профиля
        if not self._PROFILE_AREA_RE.search(response.text):
            raise UserNotLoggedInError("Сессия истекла: на странице нет блока профиля")
        return response

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        response = await self._send("POST", url, **kwargs)
        if not response.has_redirect_location:  # откат версии отвечает 302 на главу
            self._raise_for_status(response)
        return response

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Ошибки httpx -> наши, с текстом для человека; UI про httpx не знает."""
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
        self._raise_if_logged_out(response)
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        # как raise_for_status() у httpx: всё, кроме 2xx, включая 3xx
        if not response.is_success:
            request = response.request
            log.warning(
                "%s %s -> %d", request.method, request.url.path, response.status_code
            )
            raise SiteError(response.status_code)

    @staticmethod
    def _raise_if_logged_out(response: httpx.Response) -> None:
        if (
            response.is_redirect
            and httpx.URL(response.headers.get("location", "")).path == "/login"
        ):
            raise UserNotLoggedInError("Сессия истекла: сайт перенаправил на /login")
