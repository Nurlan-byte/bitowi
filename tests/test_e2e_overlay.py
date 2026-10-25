"""Numbering: a number only for what can be clicked — not under another layer, not outside the page."""

import pytest

pytest.importorskip("playwright")

from playwright.async_api import async_playwright  # noqa: E402

from app.browser.guard import SUBMIT_CLASSES  # noqa: E402
from app.course.browser import OVERLAY_JS  # noqa: E402

pytestmark = pytest.mark.e2e

# курс #12: страница результатов поверх свёрнутого меню курса
COVERED = """<!doctype html><meta charset="utf-8"><title>Результат</title>
<style>body{margin:0;font:16px sans-serif}
#side{position:fixed;left:0;top:80px;width:200px}
#cover{position:fixed;left:0;right:0;top:60px;bottom:0;background:#fff;z-index:10;overflow:auto}</style>
<a href="/home">Домой</a>
<nav id="side"><a href="/m1">Пункт меню под окном</a></nav>
<div id="cover"><h1>Your grade: 95%</h1><a href="/next">Next item</a>
<div style="height:1500px"></div><button>Кнопка внизу окна</button></div>
<a href="/away" style="position:absolute;left:-9999px;top:10px">За краем</a>
"""

# длинная страница: «липкая» шапка, кнопка у края окна под ней, кнопка далеко внизу, оформленный флажок
LONG = """<!doctype html><meta charset="utf-8"><title>Длинная</title>
<style>body{margin:0;font:16px sans-serif}
header{position:sticky;top:0;height:60px;background:#eee;z-index:5}
.box{display:inline-block;width:16px;height:16px;border:1px solid #333}</style>
<header><a href="/home">Шапка</a></header>
<label><input type="checkbox" style="position:absolute;opacity:0"><span class="box"></span> Вариант</label>
<div style="height:850px"></div>
<button style="height:30px">Кнопка под шапкой</button>
<div style="height:2500px"></div>
<button>Кнопка далеко внизу</button>
"""


@pytest.fixture
async def page():
    pages = {"covered": COVERED, "long": LONG}

    async def serve(route):
        path = route.request.url.rsplit("/", 1)[-1]
        await route.fulfill(content_type="text/html; charset=utf-8", body=pages.get(path, "ok"))

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
    await ctx.route("https://lms.test/**", serve)
    p = await ctx.new_page()
    yield p
    await browser.close()
    await pw.stop()


async def numbered(page) -> list[str]:
    data = await page.evaluate(OVERLAY_JS, SUBMIT_CLASSES)
    return [e["label"] for e in data["elements"]]


async def test_covered_and_offscreen_get_no_number(page):
    await page.goto("https://lms.test/covered")
    labels = await numbered(page)
    assert "Next item" in labels and "Домой" in labels
    # за краем прокручиваемого окна — номер есть: кликер прокрутит
    assert "Кнопка внизу окна" in labels
    assert "Пункт меню под окном" not in labels and "За краем" not in labels
    # номера подряд, без пропусков; в aria-label — у пронумерованных
    data = await page.evaluate(OVERLAY_JS, SUBMIT_CLASSES)
    assert [e["n"] for e in data["elements"]] == list(range(1, len(data["elements"]) + 1))
    assert await page.locator("#side a").get_attribute("data-bitowi-n") is None


async def test_long_page_sticky_header_and_styled_checkbox(page):
    await page.goto("https://lms.test/long")
    await page.evaluate("window.scrollTo(0, 400)")
    labels = await numbered(page)
    assert labels == ["Шапка", "Вариант", "Кнопка под шапкой", "Кнопка далеко внизу"]
    # осмотр не сдвигает страницу
    assert await page.evaluate("scrollY") == 400
