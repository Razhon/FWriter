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
