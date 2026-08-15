from dataclasses import dataclass


@dataclass(frozen=True)
class UserInfo:
    id: int
    name: str


@dataclass
class SlimChapterInfo:
    id: int
    title: str


@dataclass
class Fic:
    id: int
    title: str
    chapters: list[SlimChapterInfo]
