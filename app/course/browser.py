"""The course browser: one Chromium for the whole run, owned by the orchestrator.

The courier (Codex + Playwright MCP) attaches to it over CDP and only clicks. The code numbers
elements, takes screenshots, keeps the course domain and grants the one-shot submit allowance.
Logins are done before (BrowserSessions): cookies come in from the storage-state file.
"""

import json
import logging
import math
import os
import socket
import time
from pathlib import Path

from app.browser.guard import ALLOW_KEY, SUBMIT_CLASSES, guard_script
from app.config import Settings
from app.course.models import Element, Screen
from app.course.policy import classify, origin_of, same_origin

log = logging.getLogger(__name__)

OVERLAY_JS = (Path(__file__).resolve().parent / "overlay.js").read_text(encoding="utf-8")

_BLOCKED_JS = """() => [...document.querySelectorAll('[data-bitowi-blocked]')].map((el) => {
  const r = [Number(el.getAttribute('data-bitowi-n') || 0), el.getAttribute('data-bitowi-blocked')];
  el.removeAttribute('data-bitowi-blocked');
  return r;
})"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CourseBrowser:
    def __init__(self, settings: Settings, start_url: str, profile_dir: Path, storage: Path | None = None) -> None:
        self.settings = settings
        self.start_url = start_url
        self.origin = origin_of(start_url)
        self.profile_dir = profile_dir
        self.storage = storage
        self.port = 0
        self.last_url = start_url
        self.stamp = ""
        # точка обзора: её двигает только действие scroll; клики и ввод (браузер сам прокручивает к полю) — нет
        self.view_url = ""
        self.view_y = 0
        # заблокированные переходы главной вкладки на чужой домен
        self.offsite_attempts: list[str] = []
        self._pw = None
        self.context = None

    @property
    def cdp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def page(self):
        return next(p for p in self.context.pages if not p.is_closed())

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.profile_dir.chmod(0o700)
        width, height = (int(x) for x in self.settings.course_viewport.split("x"))
        self.port = free_port()
        self._pw = await async_playwright().start()
        # постоянный контекст = контекст по умолчанию: к нему же подключается MCP по CDP
        self.context = await self._pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=True,
            viewport={"width": width, "height": height},
            args=[f"--remote-debugging-port={self.port}", "--remote-debugging-address=127.0.0.1"],
        )
        await self.context.add_init_script(script=guard_script("course"))
        await self.context.route("**/*", self._route)
        if self.storage and self.storage.exists():
            cookies = json.loads(self.storage.read_text(encoding="utf-8")).get("cookies", [])
            await self.context.add_cookies(cookies)
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=self.settings.browser_timeout * 1000)

    async def _route(self, route) -> None:
        req = route.request
        if req.is_navigation_request() and req.frame.parent_frame is None and not same_origin(req.url, self.origin):
            self.offsite_attempts.append(req.url)
            log.warning("offsite navigation blocked: %s", req.url)
            # 204 отменяет навигацию, вкладка остаётся на странице курса (abort увёл бы на chrome-error://)
            await route.fulfill(status=204, body="")
            return
        await route.fallback()

    async def close(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self.settings.browser_timeout * 1000)

    async def check_domain(self) -> str:
        """Close extra tabs; if the main tab left the course domain, roll back and return that URL."""
        pages = [p for p in self.context.pages if not p.is_closed()]
        for extra in pages[1:]:
            await extra.close()
        page = pages[0]
        url = page.url
        if same_origin(url, self.origin):
            self.last_url = url
            return ""
        log.warning("left the course domain: %s", url)
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=self.settings.browser_timeout * 1000)
        except Exception:  # noqa: BLE001 — откат любой ценой
            pass
        if not same_origin(page.url, self.origin):
            await self.goto(self.last_url)
        return url

    async def inspect(self, shots_dir: Path, max_shots: int) -> Screen:
        """Number the elements and screenshot the visible part (several shots for a long page)."""
        page = self.page
        await page.wait_for_load_state("domcontentloaded")
        start = self.view_y if page.url == self.view_url else 0
        self.view_url, self.view_y = page.url, start
        data = await page.evaluate(OVERLAY_JS, SUBMIT_CLASSES)
        self.stamp = data["stamp"]

        shots_dir.mkdir(parents=True, exist_ok=True)
        for old in shots_dir.glob("*.png"):
            old.unlink()
        view = data["viewport"]
        left = max(1, data["height"] - start)
        count = min(max_shots, math.ceil(left / view))
        shots = []
        for k in range(count):
            await page.evaluate("(y) => window.scrollTo(0, y)", start + k * view)
            path = shots_dir / f"{k + 1}.png"
            await page.screenshot(path=str(path))
            shots.append(str(path))
        await page.evaluate("(y) => window.scrollTo(0, y)", start)

        elements = [classify(Element(**e), self.origin) for e in data["elements"]]
        return Screen(
            shots=shots,
            text=data["text"],
            elements=elements,
            messages=data["messages"],
            progress=data["progress"],
            more_below=start + count * view < data["height"],
            url=data["url"],
            title=data["title"],
        )

    async def stamp_ok(self) -> bool:
        """The page still carries the numbers of the last inspection."""
        try:
            return bool(self.stamp) and await self.page.evaluate("window.__bitowiStamp || ''") == self.stamp
        except Exception:  # noqa: BLE001 — страница в процессе перехода
            return False

    async def blocked(self) -> list[tuple[int, str]]:
        """Elements the guard refused to click since the last call: [(n, reason)]."""
        try:
            return [tuple(x) for x in await self.page.evaluate(_BLOCKED_JS)]
        except Exception:  # noqa: BLE001
            return []

    async def scroll(self, direction: str) -> None:
        page = self.page
        y = self.view_y if page.url == self.view_url else 0
        step = int(await page.evaluate("innerHeight") * 0.9)
        limit = max(0, await page.evaluate("document.documentElement.scrollHeight - innerHeight"))
        self.view_url = page.url
        self.view_y = min(limit, max(0, y + (step if direction == "down" else -step)))
        await page.evaluate("(y) => window.scrollTo(0, y)", self.view_y)

    async def allow_submit(self, count: int = 3, seconds: int = 120) -> None:
        """One-shot allowance for the guard: the submit button plus its confirmation."""
        value = json.dumps({"count": count, "until": int((time.time() + seconds) * 1000)})
        await self.page.evaluate("([k, v]) => sessionStorage.setItem(k, v)", [ALLOW_KEY, value])

    async def revoke_submit(self) -> None:
        try:
            await self.page.evaluate("(k) => sessionStorage.removeItem(k)", ALLOW_KEY)
        except Exception:  # noqa: BLE001
            pass
