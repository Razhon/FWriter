from dataclasses import dataclass
from typing import Literal

Role = Literal["author", "coauthor", "beta", "gamma"]

CAN_MANAGE: frozenset[Role] = frozenset({"author", "coauthor"})


def can_manage(role: Role, readonly: bool) -> bool:
    """Название главы и "Новая глава"; текст и отправка - при not readonly."""
    return role in CAN_MANAGE and not readonly


@dataclass(frozen=True)
class UserInfo:
    id: int
    name: str


@dataclass
class SlimChapterInfo:
    id: str
    title: str


@dataclass
class Fic:
    id: str
    title: str
    chapters: list[SlimChapterInfo]


@dataclass
class Chapter:
    title: str
    content: str
    title_editable: bool  # у беты поле названия disabled
    status: int | None  # param: 1 черновик, 2 опубликована, 3 позже; None не у автора
