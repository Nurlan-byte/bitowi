"""End-to-end on the mini LMS with a real headless Chromium (no agents, no subscriptions)."""

import json
import urllib.request

import pytest

pytest.importorskip("playwright")
pytest.importorskip("uvicorn")

from playwright.async_api import async_playwright  # noqa: E402

from app.agents.boss_codex import GUARD_JS  # noqa: E402
from app.browser.session import BrowserSessions, LoginFailed, SessionExpired  # noqa: E402
from app.browser.sites import SiteProfile, profile_for  # noqa: E402
from tests.lms_server import lms, stats  # noqa: E402,F401

pytestmark = pytest.mark.e2e


@pytest.fixture
def lms_settings(settings, lms):
    urllib.request.urlopen(urllib.request.Request(lms + "/_reset", method="POST"))
    settings.fake_lms_host = lms.split("://", 1)[1]
    settings.browser_timeout = 15
    return settings


async def test_form_login_saves_state(lms_settings, lms):
    url = lms + "/quiz/1"
    profile = profile_for(url, lms_settings)
    assert profile.kind == "form"
    path = await BrowserSessions(lms_settings).ensure_session(profile, url)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert any(c["name"] == "sid" for c in data["cookies"])
    assert stats(lms)["logins"] == 1


async def test_form_login_wrong_password(lms_settings, lms):
    lms_settings.fake_lms_password = "nope"
    url = lms + "/quiz/1"
    with pytest.raises(LoginFailed):
        await BrowserSessions(lms_settings).ensure_session(profile_for(url, lms_settings), url)


async def test_expired_state_detected(lms_settings, lms, tmp_path):
    url = lms + "/quiz/1"
    sessions = BrowserSessions(lms_settings)
    good = await sessions.ensure_session(profile_for(url, lms_settings), url)
    host = lms.split("://", 1)[1]

    # сервер забыл сессию — state.json протух
    urllib.request.urlopen(urllib.request.Request(lms + "/_reset", method="POST"))
    stale = SiteProfile(host, "state", good)
    with pytest.raises(SessionExpired):
        await sessions.ensure_session(stale, url)

    missing = SiteProfile(host, "state", tmp_path / "none.json")
    with pytest.raises(SessionExpired):
        await sessions.ensure_session(missing, url)


async def _page(lms_settings, lms, guard: bool):
    url = lms + "/quiz/1"
    state = await BrowserSessions(lms_settings).ensure_session(profile_for(url, lms_settings), url)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(storage_state=str(state))
    if guard:
        await ctx.add_init_script(path=str(GUARD_JS))
    page = await ctx.new_page()
    await page.goto(url)
    return pw, browser, page


async def test_guard_blocks_submit_and_show_answer(lms_settings, lms):
    pw, browser, page = await _page(lms_settings, lms, guard=True)
    try:
        await page.check("input[name=q1][value='2']")  # выбор варианта не блокируется
        await page.click("button.submit")
        await page.click("button.show")
        await page.evaluate("document.querySelector('form').submit()")
        await page.evaluate("document.querySelector('button.submit').click()")
        await page.wait_for_timeout(300)
        assert page.url.endswith("/quiz/1")
        assert await page.locator("[data-bitowi-blocked]").count() == 2
    finally:
        await browser.close()
        await pw.stop()
    s = stats(lms)
    assert s["submit"] == 0 and s["show_answer"] == 0


async def test_without_guard_submit_works(lms_settings, lms):
    # контроль: при сдаче (этап 3) guard не подключается, и кнопка работает
    pw, browser, page = await _page(lms_settings, lms, guard=False)
    try:
        await page.check("input[name=q1][value='2']")
        await page.click("button.submit")
        await page.wait_for_url("**/submit")
    finally:
        await browser.close()
        await pw.stop()
    assert stats(lms)["submit"] == 1


async def test_guard_allows_navigation_and_login(lms_settings, lms):
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    try:
        ctx = await browser.new_context()
        await ctx.add_init_script(path=str(GUARD_JS))
        page = await ctx.new_page()
        await page.goto(lms + "/quiz/2")
        await page.fill("input[name=username]", "student")
        await page.fill("input[name=password]", "student")
        await page.click("button[type=submit]")  # «Войти» — разрешено
        await page.wait_for_url("**/quiz/2")
        await page.click("text=Правила платформы и академической честности")
        await page.wait_for_url("**/rules")
    finally:
        await browser.close()
        await pw.stop()


async def test_fallback_screenshot(lms_settings, lms, tmp_path):
    url = lms + "/quiz/1"
    sessions = BrowserSessions(lms_settings)
    state = await sessions.ensure_session(profile_for(url, lms_settings), url)
    out = await sessions.screenshot(url, state, tmp_path / "s" / "extract.png")
    assert out and out.read_bytes()[:4] == b"\x89PNG"
