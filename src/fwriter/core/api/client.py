import re
from dataclasses import dataclass
from typing import Literal, TypeVar

import httpx
from bs4 import BeautifulSoup

from .errors import *
from .models import Fic, SlimChapterInfo, UserInfo


@dataclass(frozen=True)
class LoginSuccess:
    ok: Literal[True]
    api: FicbookAPI


@dataclass(frozen=True)
class LoginFailure:
    ok: Literal[False]
    error: str


LoginResult = LoginSuccess | LoginFailure

T = TypeVar("T")


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
    async def try_login(
        cls, cookies: dict[str, str], user_agent: str | None = None
    ) -> LoginResult:
        api = cls(cookies, user_agent)
        try:
            response = await api._post("/home/messaging/gettoken")
            
            if bool(response.json().get("result") is True):
                return LoginSuccess(True, api)
        except (httpx.HTTPError, ValueError) as e: 
            return LoginFailure(False, str(e))

        return LoginFailure(False, f"{response.status_code} {response.text}")

    async def get_user_info(self) -> UserInfo:
        if self.user:
            return self.user

        async def get_user_id() -> int:
            response = await self._get("/")

            soup = BeautifulSoup(response.text, "html.parser")
            profile_area = soup.find("div", class_="profile-area")
            if not profile_area:
                raise UserNotLoggedInError(
                    "На главной нет блока профиля: cookies недействительны"
                )

            links = [
                str(a.get("href", "")) for a in profile_area.find_all("a", limit=4)
            ]

            for link in links:
                match = self._USER_ID_RE.search(link)
                if match:
                    return int(match.group(1))
            raise UserNotLoggedInError("Не найден id пользователя в блоке профиля")

        async def get_user_name(user_id: int) -> str:
            response = await self._get(f"/authors/{user_id!s}")

            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title
            if not title or not title.string:
                raise UserNotLoggedInError("Не найдено имя на странице автора")

            return title.string.split()[0]

        user_id = await get_user_id()
        user_name = await get_user_name(user_id)

        self.user = UserInfo(user_id, user_name)

        return self.user

    async def get_author_fics(self) -> list[Fic]:
        response = await self._get("/home/myfics")

        soup = BeautifulSoup(response.text, "html.parser")

        fics_unparsed = soup.find_all("div", class_="myfic")

        fics: list[Fic] = []
        for fic_unparsed in fics_unparsed:
            fic_id_raw = req(str(fic_unparsed.get("data-id", "")), "id фанфика (data-id)")
            id_ = int(fic_id_raw)

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
                    match = req(self._CHAPTER_RE.search(href), f"ссылка на главу ({href})")
                    if match.group(1) != fic_id_raw:
                        raise APIChangedError(
                            f"Глава {href} не относится к фанфику {fic_id_raw}"
                        )

                    chapter_id = int(match.group(2))
                    chapter_title_unparsed = req(
                        ch_link.find("span"), f"название главы {chapter_id}"
                    )

                    chapter_title = req(
                        next(chapter_title_unparsed.stripped_strings, "")
                        .replace("\n", "")
                        .strip(),
                        f"текст названия главы {chapter_id}",
                    )

                    chapters.append(SlimChapterInfo(chapter_id, chapter_title))

            fics.append(Fic(id=id_, title=title, chapters=chapters))
        return fics

    async def get_beta_fics(self) -> list[Fic]:
        response = await self._get("/home/assistant_fanfics/beta")

        soup = BeautifulSoup(response.text, "html.parser")

        fics_unparsed = soup.find_all("div", class_="myfic")

        fics: list[Fic] = []
        for fic_unparsed in fics_unparsed:
            fic_id_raw = req(str(fic_unparsed.get("data-id", "")), "id фанфика (data-id)")
            id_ = int(fic_id_raw)

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
                    match = req(self._CHAPTER_RE.search(href), f"ссылка на главу ({href})")
                    if match.group(1) != fic_id_raw:
                        raise APIChangedError(
                            f"Глава {href} не относится к фанфику {fic_id_raw}"
                        )

                    chapter_id = int(match.group(2))
                    chapter_title_unparsed = req(
                        ch_link.find("span"), f"название главы {chapter_id}"
                    )

                    chapter_title = req(
                        next(chapter_title_unparsed.stripped_strings, "")
                        .replace("\n", "")
                        .strip(),
                        f"текст названия главы {chapter_id}",
                    )

                    chapters.append(SlimChapterInfo(chapter_id, chapter_title))

            fics.append(Fic(id=id_, title=title, chapters=chapters))
        return fics

    async def _get(self, *args, **kwargs) -> httpx.Response:
        response = await self.client.get(*args, **kwargs)
        response.raise_for_status()
        return response

    async def _post(self, *args, **kwargs) -> httpx.Response:
        response = await self.client.post(*args, **kwargs)
        response.raise_for_status()
        return response

    async def get_ficbook_id(self, url: str) -> str:
        """
        Get ficbook id from url
        """
        response = await self.client.get(url)
        response.raise_for_status()
        ficbook_id = response.url.path.split("/")[-1]
        return ficbook_id
