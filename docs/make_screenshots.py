"""Скриншоты для README на выдуманных данных: uv run python docs/make_screenshots.py

Снимает экраны через Textual (SVG), оборачивает в рамку Windows Terminal
и рендерит в PNG через headless Edge.
"""

import asyncio
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from textual.app import App

from fwriter.core.api.client import FicbookAPI
from fwriter.core.api.models import Chapter, Fic, SlimChapterInfo, UserInfo
from fwriter.core.storage import ChapterStorage
from fwriter.core.sync import Synced
from fwriter.screens import MainScreen
from fwriter.screens.chapter import ChapterTab

OUT = Path(__file__).parent / "screenshots"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
SIZE = (140, 36)

TEXT = """<tab>Дождь начался ещё до рассвета и к полудню превратил дорогу к маяку в сплошное болото. Мира шла, не разбирая луж, и считала шаги - так было легче не думать о письме в кармане.

<tab>- Ты опоздала, - сказал смотритель, не оборачиваясь. - <i>Опять</i>.

<tab>- Я принесла новости, - ответила она и положила конверт на стол. - <b>Плохие</b>.

<tab>Старик долго смотрел на печать, потом поднял глаза. В них было что-то, чего Мира раньше не видела: не страх, а усталость человека, который слишком давно ждал именно этого<footnote>Печать Северной гавани не ставили на письма уже сорок лет.</footnote>.

<center>* * *</center>

<tab>К вечеру ветер стих. Над морем повисла тяжёлая тишина, и только фонарь наверху продолжал своё медленное вращение, будто ничего не случилось."""

FICS = {
    "author": [
        Fic(
            "10000001",
            "Свет над Северной гаванью",
            [
                SlimChapterInfo("20000001", "Пролог"),
                SlimChapterInfo("20000002", "Глава 1. Письмо"),
                SlimChapterInfo("20000003", "Глава 2. Смотритель"),
                SlimChapterInfo("20000004", "Глава 3. Шторм", draft=True),
            ],
        ),
        Fic(
            "10000002",
            "Сорок дней до зимы",
            [SlimChapterInfo("20000011", "Часть первая")],
        ),
    ],
    "coauthor": [
        Fic(
            "10000003",
            "Город, которого нет",
            [
                SlimChapterInfo("20000021", "Пролог"),
                SlimChapterInfo("20000022", "Глава 1"),
            ],
        ),
    ],
    "beta": [
        Fic(
            "10000004",
            "Песня для чужого моря",
            [SlimChapterInfo("20000031", "Глава 1")],
        ),
        Fic("10000005", "Последний поезд", [SlimChapterInfo("20000041", "Глава 1")]),
    ],
    "gamma": [],
}
FIC = FICS["author"][0]
CHAPTER = FIC.chapters[1]


def fake_api() -> FicbookAPI:
    api = FicbookAPI({})
    api.user = UserInfo("1", "lighthouse_keeper")

    async def get_fics(role):
        return FICS[role]

    async def is_readonly(fic_id):
        return False

    async def get_chapter(fic_id, chapter_id):
        return Chapter(CHAPTER.title, TEXT, True, 2, "")

    api.get_fics = get_fics
    api.is_readonly = is_readonly
    api.get_chapter = get_chapter
    return api


async def fake_sync(api, storage, fic_id, chapter_id, title, readonly):
    return Synced("same", storage.read_draft(fic_id, chapter_id) or TEXT)


def seed(storage: ChapterStorage) -> None:
    """Две правки с сайта и черновик поверх второй - чтобы было что показать в F3."""
    old = TEXT.replace("Плохие", "Странные").replace("медленное вращение", "вращение")
    storage.add_version(
        FIC.id,
        CHAPTER.id,
        old,
        CHAPTER.title,
        "site",
        "первая выкладка",
        created="2026-10-12T21:40:00",
        site_rev="30000001",
        site_num=1,
        author="2",
        author_name="sea_fox",
    )
    base = storage.add_version(
        FIC.id,
        CHAPTER.id,
        TEXT,
        CHAPTER.title,
        "site",
        "опечатки",
        created="2026-10-20T19:05:00",
        site_rev="30000002",
        site_num=2,
        author="1",
        author_name="lighthouse_keeper",
    )
    meta = storage.load_chapter(FIC.id, CHAPTER.id, CHAPTER.title)
    meta.base = base.file
    meta.site_title = CHAPTER.title
    storage.write_chapter(FIC.id, CHAPTER.id, meta)
    draft = TEXT.replace("считала шаги", "считала шаги вслух").replace(
        "К вечеру ветер стих", "К ночи ветер наконец стих"
    )
    storage.write_draft(FIC.id, CHAPTER.id, draft)
    for f in FICS["author"]:
        storage.add_recent(f.id, f.chapters[0].id)
    storage.add_recent(FIC.id, CHAPTER.id)


class ShotApp(App):
    TITLE = "fwriter"

    def __init__(self, main: MainScreen) -> None:
        self.main = main
        super().__init__()

    def on_mount(self) -> None:
        self.push_screen(self.main)


async def shoot(data: Path) -> dict[str, str]:
    import fwriter.screens.chapter as chapter_mod

    chapter_mod.sync_chapter = fake_sync  # type: ignore[assignment]
    FicbookAPI.RATE_WINDOW = 0.0
    storage = ChapterStorage(data)
    seed(storage)
    main = MainScreen(fake_api(), storage)
    shots: dict[str, str] = {}
    async with ShotApp(main).run_test(size=SIZE) as pilot:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause(0.5)
        shots["start"] = pilot.app.export_screenshot()

        main.open_chapter(FIC, CHAPTER, "author")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause(0.5)
        tab = main.query_one(ChapterTab)
        tab.editor.focus()
        await pilot.pause(0.3)
        shots["visual"] = pilot.app.export_screenshot()

        await pilot.press("f7")
        await pilot.pause(0.5)
        shots["markup"] = pilot.app.export_screenshot()

        await pilot.press("f3")
        await pilot.pause(0.8)
        shots["diff"] = pilot.app.export_screenshot()
        await pilot.press("escape")
        await pilot.pause(0.3)
    return shots


# рамка Windows Terminal 11: вкладка, кнопки окна, тема Campbell, Cascadia Mono
FRAME = """<!doctype html><meta charset="utf-8"><style>
html,body{{margin:0;background:transparent}}
.win{{display:inline-block;background:#0c0c0c;border:1px solid #3a3a3a;border-radius:8px;overflow:hidden;font-family:"Segoe UI Variable Text","Segoe UI",sans-serif}}
.bar{{height:40px;background:#202020;display:flex;align-items:flex-end;color:#fff;font-size:12px}}
.tab{{height:32px;margin-left:8px;padding:0 10px;background:#0c0c0c;border-radius:6px 6px 0 0;display:flex;align-items:center;gap:8px;width:220px}}
.tab .t{{flex:1}} .tab .x{{color:#ccc;font-size:11px}}
.icon{{width:14px;height:12px;border:1.5px solid #ccc;border-radius:2px;position:relative;font:9px/12px Consolas;color:#ccc;text-indent:1px}}
.add{{height:32px;display:flex;align-items:center;padding:0 10px;color:#ccc;font-size:16px;gap:14px}}
.add span:last-child{{font-size:10px}}
.ctl{{margin-left:auto;align-self:stretch;display:flex}}
.ctl span{{width:46px;display:flex;align-items:center;justify-content:center;font-family:"Segoe Fluent Icons","Segoe MDL2 Assets";font-size:10px;color:#fff}}
.term{{padding:6px 8px 8px}}
.term svg{{display:block}}
</style><div class="win"><div class="bar"><div class="tab"><div class="icon">&gt;_</div><div class="t">fwriter</div><div class="x">\u2715</div></div>
<div class="add"><span>+</span><span>\u2304</span></div>
<div class="ctl"><span>&#xE921;</span><span>&#xE922;</span><span>&#xE8BB;</span></div></div>
<div class="term">{svg}</div></div>"""


def terminal_only(svg: str) -> tuple[str, float, float]:
    """Из SVG Textual - только сетка терминала, без его рамки с кнопками."""
    svg = re.sub(r"@font-face\s*\{[^}]*\}", "", svg)
    svg = svg.replace(
        "font-family: Fira Code, monospace;",
        'font-family: "Cascadia Mono", Consolas, monospace;',
    )
    # рамка Textual: скруглённый прямоугольник, кружки и заголовок
    svg = re.sub(r'<rect fill="#292929"[^>]*/>', "", svg)
    svg = re.sub(
        r"<g transform=\"translate\(26,22\)\">.*?</g>", "", svg, flags=re.DOTALL
    )
    svg = re.sub(r'<text class="[^"]*-title"[^>]*>.*?</text>', "", svg)
    m = re.search(
        r'<g transform="translate\((\d+(?:\.\d+)?), (\d+(?:\.\d+)?)\)" clip-path="url\(#([^)]*)-clip-terminal\)">',
        svg,
    )
    assert m, "формат SVG Textual сменился"
    x, y = float(m[1]), float(m[2])
    clip = re.search(
        r'<clipPath id="[^"]*-clip-terminal">\s*<rect x="0" y="0" width="([\d.]+)" height="([\d.]+)"',
        svg,
    )
    assert clip
    w, h = float(clip[1]), float(clip[2])
    svg = re.sub(
        r'viewBox="[^"]*"',
        f'viewBox="{x} {y} {w} {h}" width="{w}" height="{h}"',
        svg,
        count=1,
    )
    return svg, w, h


def render(name: str, svg: str, tmp: Path) -> None:
    html = tmp / f"{name}.html"
    svg, w, h = terminal_only(svg)
    html.write_text(FRAME.format(svg=svg), encoding="utf-8")
    png = OUT / f"{name}.png"
    subprocess.run(
        [
            str(EDGE),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--default-background-color=00000000",
            "--force-device-scale-factor=1",
            f"--screenshot={png}",
            f"--window-size={int(w) + 18},{int(h) + 56}",
            html.as_uri(),
        ],
        check=True,
        capture_output=True,
    )


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        shots = asyncio.run(shoot(Path(tmp)))
        for name, svg in shots.items():
            (Path(tmp) / f"{name}.svg").write_text(svg, encoding="utf-8")
            render(name, svg, Path(tmp))
            print(OUT / f"{name}.png", file=sys.stderr)


if __name__ == "__main__":
    main()
