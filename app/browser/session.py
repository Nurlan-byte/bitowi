"""Browser sessions driven by the orchestrator (Python Playwright): form login, session check, screenshots.

Credentials from .env are typed here and never reach the model.
"""

import logging
import os
import re
from pathlib import Path

from app.browser.sites import SiteProfile
from app.config import Settings

log = logging.getLogger(__name__)

_LOGIN_URL_RE = re.compile(
    r"/login\b|/signin\b|/sign-in\b|/auth/|/oauth2?/|/saml|/cas/|login\.microsoftonline\.com|accounts\.google\.com",
    re.IGNORECASE,
)


class SessionExpired(Exception):
    """No valid session for the host: the owner has to provide a fresh state.json."""

    def __init__(self, host: str, reason: str) -> None:
        super().__init__(f"{host}: {reason}")
        self.host = host
        self.reason = reason


class LoginFailed(Exception):
    """Form login did not succeed (wrong credentials or changed page)."""


def is_login_url(url: str) -> bool:
    return bool(_LOGIN_URL_RE.search(url))


async def _is_login_page(page) -> bool:
    if is_login_url(page.url):
        return True
    return await page.locator("input[type=password]:visible").count() > 0


def _save_private(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


class BrowserSessions:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _browser(self, pw):
        return await pw.chromium.launch(headless=True)

    async def ensure_session(self, profile: SiteProfile, url: str) -> Path | None:
        """Return a storage-state file valid for `url` (None if the page needs no login)."""
        from playwright.async_api import async_playwright

        timeout = self.settings.browser_timeout * 1000
        async with async_playwright() as pw:
            browser = await self._browser(pw)
            try:
                if profile.kind == "form":
                    await self._form_login(browser, profile, url, timeout)
                elif not profile.auth_file.exists():
                    # без state.json — может, страница открыта и так
                    ctx = await browser.new_context()
                    page = await ctx.new_page()
                    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                    if await _is_login_page(page):
                        raise SessionExpired(profile.host, "нет state.json, страница требует входа")
                    return None
                ctx = await browser.new_context(storage_state=str(profile.auth_file))
                page = await ctx.new_page()
                await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                if await _is_login_page(page):
                    raise SessionExpired(profile.host, "сессия протухла")
                return profile.auth_file
            finally:
                await browser.close()

    async def _form_login(self, browser, profile: SiteProfile, url: str, timeout: int) -> None:
        form = profile.form
        assert form
        if not form.user or not form.password:
            raise LoginFailed(f"{profile.host}: в .env не заданы логин и пароль")
        scheme = url.split("://", 1)[0]
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(f"{scheme}://{profile.host}{form.login_path}", timeout=timeout, wait_until="domcontentloaded")
        await page.fill(form.user_selector, form.user)
        await page.fill(form.password_selector, form.password)
        await page.click(form.submit_selector)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        if await _is_login_page(page):
            raise LoginFailed(f"{profile.host}: вход по форме не удался")
        profile.auth_file.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(profile.auth_file))
        _save_private(profile.auth_file)
        log.info("form login ok: %s", profile.host)
        await ctx.close()

    async def screenshot(self, url: str, storage: Path | None, path: Path) -> Path | None:
        """Fallback screenshot when the boss did not save one."""
        from playwright.async_api import async_playwright

        try:
            async with async_playwright() as pw:
                browser = await self._browser(pw)
                try:
                    ctx = await browser.new_context(
                        storage_state=str(storage) if storage else None, viewport={"width": 1280, "height": 900}
                    )
                    page = await ctx.new_page()
                    await page.goto(url, timeout=self.settings.browser_timeout * 1000, wait_until="domcontentloaded")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(path), full_page=True)
                    return path
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001 — скриншот не критичен
            log.warning("fallback screenshot failed: %s", exc)
            return None
