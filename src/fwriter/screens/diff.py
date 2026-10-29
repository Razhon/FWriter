import re
from difflib import SequenceMatcher
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

# слова, пробелы (только U+0020 и \t) и переводы строк
_TOKEN = re.compile(r"[ \t]+|\n|[^ \t\n]+")


def diff_text(old: list[str], new: list[str], mark: Style | str = "reverse") -> Text:
    """Строки new, слова, которых нет в old, - стилем mark."""
    out = Text()
    # сначала по абзацам: пословно по всему блоку - минуты на сотне абзацев
    sm = SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if j1 and j2 > j1:
            out.append("\n", mark if i1 == i2 else None)
        b = _TOKEN.findall("\n".join(new[j1:j2]))
        if tag == "equal":
            out.append("".join(b))
            continue
        a = _TOKEN.findall("\n".join(old[i1:i2]))
        words = SequenceMatcher(None, a, b, autojunk=False)
        for t, _, _, k1, k2 in words.get_opcodes():
            out.append("".join(b[k1:k2]), None if t == "equal" else mark)
    return out


class ParagraphDiff(Static):
    """Блок абзацев с подписью в рамке; изменённые слова подсвечены относительно old."""

    # цвет подсветки задаёт CSS экрана
    COMPONENT_CLASSES: ClassVar[set[str]] = {"paragraph-diff--mark"}

    DEFAULT_CSS = """
    ParagraphDiff > .paragraph-diff--mark {
        text-style: reverse;
    }
    """

    def show(self, label: str, old: list[str] | None, new: list[str]) -> None:
        """old None - без подсветки."""
        self.border_title = label
        if not any(new):
            text = Text("(пусто)", "dim italic")
        elif old is None:
            text = Text("\n".join(new))
        else:
            mark = self.get_component_rich_style("paragraph-diff--mark", partial=True)
            text = diff_text(old, new, mark)
        self.update(text)


__all__ = ["ParagraphDiff", "diff_text"]
