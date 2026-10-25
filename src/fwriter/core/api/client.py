import json
import re
from typing import ClassVar, Literal

import httpx
from bs4 import BeautifulSoup

from .errors import *
from .models import Fic, SlimChapterInfo, UserInfo

Role = Literal["author", "coauthor", "beta", "gamma"]


def req[T](value: T | None, what: str) -> T:
    """Требует, чтобы value было не None/не пустым, иначе — APIChangedError."""
    if not value:
        raise APIChangedError(
            f"Не найдено на странице: {what}. Похоже, ficbook изменил разметку."
        )
    return value


class FicbookAPI:
    BASE_URL = "https://ficbook.net"
    # Только cookies сессии: полный экспорт из браузера не влезает в keyring на Windows
    AUTH_COOKIES = frozenset({"rme", "PHPSESSID", "cf_clearance"})

    # Роль -> страница со списком. Ссылки на главы на всех страницах одинаковые.
    FIC_LISTS: ClassVar[dict[Role, str]] = {
        "author": "/home/myfics",
        # TODO: соавтор и сопереводчик на одной странице, переводчик в myfics, разделить роли, не на чем проверить
        "coauthor": "/home/assistant_fanfics/coauthor",
        "beta": "/home/assistant_fanfics/beta",
        "gamma": "/home/assistant_fanfics/gamma",
    }

    _USER_ID_RE = re.compile(r"/authors/(\d+)")
    _CHAPTER_RE = re.compile(r"/home/myfics/(\d+)/parts/(\d+)")

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
        # TODO: Проверить какие именно ключи cookie необходимы
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
        except (httpx.HTTPError, FicbookAPIError) as e:
            # str() у таймаутов httpx пустой
            raise LoginFailedError(f"{type(e).__name__}: {e}") from e

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
        profile_area = soup.find("div", class_="profile-area")
        if not profile_area:
            raise UserNotLoggedInError(
                "На странице нет блока профиля: cookies недействительны"
            )
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

                    chapter_title = req(
                        " ".join(chapter_title_unparsed.get_text().split()),
                        f"текст названия главы {chapter_id}",
                    )

                    chapters.append(SlimChapterInfo(chapter_id, chapter_title))

            fics.append(Fic(id=fic_id_raw, title=title, chapters=chapters))
        return fics

    async def _get(self, *args, **kwargs) -> httpx.Response:
        response = await self.client.get(*args, **kwargs)
        response.raise_for_status()
        return response

    async def _post(self, *args, **kwargs) -> httpx.Response:
        response = await self.client.post(*args, **kwargs)
        response.raise_for_status()
        return response
