import pytest

from fwriter.core.api.models import Fic, SlimChapterInfo
from fwriter.core.catalog import FicPlan, plan_fic

SITE_CH = [SlimChapterInfo("1", "С сайта")]
STORED_CH = [SlimChapterInfo("2", "Из fic.json")]


def site(chapters: list[SlimChapterInfo] | None = None) -> Fic:
    return Fic("00000000", "Работа", chapters or [])


def stored(readonly: bool) -> Fic:
    return Fic("00000000", "Работа", STORED_CH, readonly)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        # главы в карточке
        (
            (site(SITE_CH), False, None, False),
            FicPlan(Fic("00000000", "Работа", SITE_CH, False), True, True, False),
        ),
        # глав нет, права есть
        (
            (site(), False, stored(True), False),
            FicPlan(Fic("00000000", "Работа", [], False), True, True, False),
        ),
        # без прав, есть fic.json
        (
            (site(), True, stored(True), False),
            FicPlan(Fic("00000000", "Работа", STORED_CH, True), False, True, False),
        ),
        # без прав, fic.json нет
        (
            (site(), True, None, False),
            FicPlan(Fic("00000000", "Работа", [], True), False, True, False),
        ),
        # без прав, fic.json битый: не затираем
        (
            (site(), True, None, True),
            FicPlan(Fic("00000000", "Работа", [], True), False, False, True),
        ),
        # права не проверены: всё из fic.json
        (
            (site(), None, stored(False), False),
            FicPlan(Fic("00000000", "Работа", STORED_CH, False), False, False, False),
        ),
        # права не проверены, fic.json нет: без прав
        (
            (site(), None, None, True),
            FicPlan(Fic("00000000", "Работа", [], True), False, False, False),
        ),
    ],
)
def test_plan_fic(args, expected):
    assert plan_fic(*args) == expected


def test_plan_fic_keeps_site_fic():
    fic = site()
    plan_fic(fic, True, stored(True), False)
    assert fic == site()
