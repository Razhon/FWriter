from dataclasses import dataclass, replace

from .api.models import Fic


@dataclass(frozen=True)
class FicPlan:
    fic: Fic
    find_gone: bool  # главы с сайта: можно искать удалённые
    save: bool  # записать fic.json
    broken_warning: bool  # битый fic.json, других глав нет


def plan_fic(
    site: Fic, readonly: bool | None, stored: Fic | None, broken: bool
) -> FicPlan:
    """readonly None - права не проверены; stored и broken - fic.json."""
    if readonly is None:
        fic = replace(
            site,
            readonly=stored.readonly if stored else True,
            chapters=stored.chapters if stored else [],
        )
        return FicPlan(fic, find_gone=False, save=False, broken_warning=False)
    fic = replace(site, readonly=readonly)
    # сайт не отдаёт главы работы без прав
    if readonly and stored:
        fic = replace(fic, chapters=stored.chapters)
    # битый fic.json - единственный след списка глав, не затираем
    lost = readonly and broken
    return FicPlan(fic, find_gone=not readonly, save=not lost, broken_warning=lost)
