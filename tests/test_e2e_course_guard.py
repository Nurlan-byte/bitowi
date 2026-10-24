"""guard.js in course mode with a real headless Chromium; pages are served by routes, no LMS needed."""

import json
import time

import pytest

pytest.importorskip("playwright")

from playwright.async_api import async_playwright  # noqa: E402

from app.browser.guard import ALLOW_KEY, DENY_RE, guard_script, is_submit  # noqa: E402
from app.course.netpolicy import NetPolicy  # noqa: E402
from tests.test_course_policy import ALLOWED_LABELS, DENY_LABELS, SUBMIT_LABELS  # noqa: E402

pytestmark = pytest.mark.e2e

COURSE = "https://lms.test"

PAGE = """<!doctype html><meta charset="utf-8"><title>Курс</title>
<a id="next" href="/page2">Далее</a>
<a id="lan" href="http://192.168.0.1/">Роутер</a>
<a id="file" href="file:///etc/passwd">Файл</a>
<a id="dl" href="/report.pdf" download>Отчёт</a>
<a id="ext" href="https://evil.test/form">Внешний сайт</a>
<a id="logout" href="/login/logout.php?sesskey=1">Мой кабинет</a>
<button id="exit" onclick="document.title='вышли'">Выйти</button>
<form method="post" action="/submit">
  <input name="a" value="1">
  <button id="send" type="submit">Отправить</button>
</form>
<form id="f2" method="post" action="/submit"><input name="b"></form>
"""

# внешний сайт: обычная форма «Далее» — на чужом домене тоже только по разрешению
EXTERNAL = """<!doctype html><meta charset="utf-8"><title>Внешний</title>
<form method="post" action="/ext-next"><input name="q" value="1"><button id="extnext" type="submit">Далее</button></form>
"""

NAVFORM = """<!doctype html><meta charset="utf-8"><title>Шаг</title>
<form method="post" action="/page3"><button id="nx" type="submit">Далее</button></form>
"""

CONFIRM = """<!doctype html><meta charset="utf-8"><title>Подтверждение</title>
<form method="post" action="/final"><button id="confirm" type="submit">Отправить всё и завершить</button></form>
"""


@pytest.fixture
async def browser_page():
    hits: list[str] = []

    async def serve(route):
        url = route.request.url
        hits.append(f"{route.request.method} {url}")
        path = url.split("://", 1)[1].split("/", 1)[-1]
        body = {"": PAGE, "submit": CONFIRM, "form": EXTERNAL, "navform": NAVFORM}.get(path, f"<title>{path}</title>ok")
        await route.fulfill(content_type="text/html; charset=utf-8", body=body)

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context()
    await ctx.add_init_script(script=guard_script("course", NetPolicy(COURSE + "/")))
    await ctx.route("https://lms.test/**", serve)
    await ctx.route("https://evil.test/**", serve)
    page = await ctx.new_page()
    await page.goto(COURSE + "/")
    yield page, hits
    await browser.close()
    await pw.stop()


async def allow(page, count: int) -> None:
    value = json.dumps({"count": count, "until": int(time.time() * 1000) + 60_000})
    await page.evaluate("([k, v]) => sessionStorage.setItem(k, v)", [ALLOW_KEY, value])


def posts(hits: list[str]) -> list[str]:
    return [h for h in hits if h.startswith("POST")]


async def test_course_guard_blocks_traps_and_submit(browser_page):
    page, hits = browser_page
    for sel in ("#lan", "#file", "#dl", "#logout", "#exit", "#send"):
        await page.click(sel)
    await page.press("input[name=a]", "Enter")  # неявная отправка формы
    await page.evaluate("document.getElementById('f2').submit()")
    await page.evaluate("window.open('http://127.0.0.1:9/popup')")
    await page.wait_for_timeout(300)

    assert page.url == COURSE + "/" and await page.title() == "Курс"
    blocked = await page.eval_on_selector_all("[data-bitowi-blocked]", "els => els.map(e => [e.id, e.dataset.bitowiBlocked])")
    assert dict(blocked) == {"lan": "private", "file": "scheme", "dl": "download", "logout": "deny", "exit": "deny", "send": "submit"}
    assert not posts(hits) and not [h for h in hits if "report.pdf" in h]


async def test_course_guard_allows_navigation(browser_page):
    page, _ = browser_page
    await page.click("#next")
    await page.wait_for_url(COURSE + "/page2")


async def test_allowance_covers_submit_and_confirmation(browser_page):
    page, hits = browser_page
    await allow(page, 2)
    await page.click("#send")
    await page.wait_for_url(COURSE + "/submit")
    await page.click("#confirm")  # разрешение пережило переход на страницу подтверждения
    await page.wait_for_url(COURSE + "/final")
    assert [h.split(" ")[1] for h in posts(hits)] == [COURSE + "/submit", COURSE + "/final"]


async def test_allowance_is_one_shot_and_expires(browser_page):
    page, hits = browser_page
    await allow(page, 1)
    await page.click("#send")
    await page.wait_for_url(COURSE + "/submit")
    await page.click("#confirm")  # разрешение израсходовано
    await page.wait_for_timeout(300)
    assert len(posts(hits)) == 1

    await page.goto(COURSE + "/")
    value = json.dumps({"count": 5, "until": int(time.time() * 1000) - 1})
    await page.evaluate("([k, v]) => sessionStorage.setItem(k, v)", [ALLOW_KEY, value])
    await page.click("#send")
    await page.wait_for_timeout(300)
    assert len(posts(hits)) == 1


async def test_allowance_never_opens_traps(browser_page):
    page, hits = browser_page
    await allow(page, 5)
    await page.click("#exit")
    await page.click("#lan")
    await page.wait_for_timeout(300)
    assert await page.title() == "Курс" and not [h for h in hits if "192.168" in h]


async def test_external_site_any_form_needs_allowance(browser_page):
    page, hits = browser_page
    await page.click("#ext")  # внешние сайты открыты
    await page.wait_for_url("https://evil.test/form")
    await page.click("#extnext")  # «Далее» — не сдача, но на чужом домене тоже по разрешению
    await page.wait_for_timeout(300)
    assert page.url == "https://evil.test/form" and not posts(hits)
    assert await page.get_attribute("#extnext", "data-bitowi-blocked") == "submit"

    await allow(page, 1)
    await page.click("#extnext")
    await page.wait_for_url("https://evil.test/ext-next")
    assert [h.split(" ")[1] for h in posts(hits)] == ["https://evil.test/ext-next"]


async def test_course_site_navigation_forms_stay_free(browser_page):
    page, hits = browser_page
    await page.goto(COURSE + "/navform")
    await page.click("#nx")  # на сайте курса форма навигации — без подтверждения
    await page.wait_for_url(COURSE + "/page3")


async def test_js_and_python_patterns_agree(browser_page):
    page, _ = browser_page
    labels = [label for label, _ in SUBMIT_LABELS] + DENY_LABELS + ALLOWED_LABELS
    policy = json.loads(guard_script("course").split("const POLICY = ", 1)[1].split(";\n", 1)[0])
    js = await page.evaluate(
        """([p, labels]) => {
            const re = (s) => new RegExp(s, 'i');
            const [S, N, D, X] = [re(p.submit), re(p.nav), re(p.danger), re(p.deny)];
            return labels.map(t => [D.test(t) || (S.test(t) && !N.test(t)), X.test(t)]);
        }""",
        [policy, labels],
    )
    py = [[is_submit(t), bool(DENY_RE.search(t))] for t in labels]
    assert js == py
