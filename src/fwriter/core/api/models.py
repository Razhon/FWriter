from dataclasses import dataclass


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
