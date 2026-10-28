import asyncio
import json
import logging
import re
import time
from collections import deque
from typing import ClassVar

import httpx
from bs4 import BeautifulSoup, Tag

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

    # slug, а не id: у новых аккаунтов в ссылке UUID
    _USER_SLUG_RE = re.compile(r"/authors/([^/?#]+)")
    _CHAPTER_RE = re.compile(r"/home/myfics/(\d+)/parts/(\d+)")
    # div.profile-area есть в шапке любой страницы, пока сессия жива
    _PROFILE_AREA_RE = re.compile(r'class="(?:[^"]*\s)?profile-area[\s"]')

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
        self._throttle = asyncio.Lock()
        # время начала последних RATE_LIMIT запросов
        self._sent: deque[float] = deque(maxlen=self.RATE_LIMIT)

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

    async def get_user_info(self) -> UserInfo:
        if not self.user:  # после login() уже заполнен
            self.user = self._parse_user((await self._get("/")).text)
        return self.user

    @classmethod
    def _parse_user(cls, html: str) -> UserInfo:
        """Slug и ник из шапки любой страницы, где пользователь вошёл."""
        soup = BeautifulSoup(html, "html.parser")
        # наличие блока уже проверил _get
        profile_area = req(soup.find("div", class_="profile-area"), "блок профиля")
        link = req(profile_area.select_one('a[href*="/authors/"]'), "ссылка на профиль")
        match = req(cls._USER_SLUG_RE.search(str(link.get("href", ""))), "slug автора")
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

                    chapters.append(SlimChapterInfo(chapter_id, chapter_title))

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
        form, content = await self._chapter_form(fic_id, chapter_id)
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
        )

    async def _chapter_form(self, fic_id: str, chapter_id: str) -> tuple[Tag, str]:
        """Форма со страницы главы и текст главы на сайте."""
        response = await self._get(f"/home/myfics/{fic_id}/parts/{chapter_id}")
        soup = BeautifulSoup(response.text, "html.parser")
        form = req(soup.find("form", id="savePartForm"), "форма главы (savePartForm)")
        editor = req(form.find("fanfic-editor"), "редактор главы (fanfic-editor)")
        content = editor.get("initial-value")
        if content is None:  # пустой текст допустим, отсутствие атрибута - нет
            raise APIChangedError("Не найдено на странице: текст главы (initial-value)")
        return form, str(content)

    async def save_chapter(
        self, fic_id: str, chapter_id: str, content: str, title: str, base: str
    ) -> None:
        """Текст и название; base - текст сайта, от которого шёл черновик."""
        # свежая форма: старая откатила бы статус, поменянный на сайте
        form, site_content = await self._chapter_form(fic_id, chapter_id)
        if site_content != base:
            raise ChapterChangedError()
        url = str(req(form.get("action"), "адрес сохранения (action)"))
        overrides = {"title": title, "change_description": ""}
        fields = [
            (name, overrides.get(name, value))
            for name, value in self._form_fields(form)
        ]
        await self._post_form(url, [*fields, ("content", content)])

    @staticmethod
    def _form_fields(form) -> list[tuple[str, str]]:
        """Поля формы, как их отправит браузер."""
        fields = []
        for element in form.find_all(attrs={"name": True}):
            name = str(element["name"])
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
