"""The course browser: one Chromium for the whole run, owned by the orchestrator.

The courier (Codex + Playwright MCP) attaches to it over CDP and only clicks. The code numbers
elements, takes screenshots, enforces the network policy and grants the one-shot submit allowance.

Network policy (app/course/netpolicy.py):
- all traffic goes through the filtering proxy (app/course/proxy.py): private and local addresses
  are closed by the IP a host resolves to — navigation, subresources, fetch/XHR, WebSocket, redirects;
- other public domains are allowed; the tab's moves to them are counted (limit, «домены:») and a form
  sent to an external domain needs the owner's «Отправить»;
- cookies of the storage file are loaded for the course domain only; downloads and service workers are off.
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
from app.course.netpolicy import NetPolicy, host_port
from app.course.policy import classify, origin_of
from app.course.proxy import FilteringProxy, Resolver, system_resolve

log = logging.getLogger(__name__)

OVERLAY_JS = (Path(__file__).resolve().parent / "overlay.js").read_text(encoding="utf-8")

_BLOCKED_JS = """() => [...document.querySelectorAll('[data-bitowi-blocked]')].map((el) => {
  const r = [Number(el.getAttribute('data-bitowi-n') || 0), el.getAttribute('data-bitowi-blocked')];
  el.removeAttribute('data-bitowi-blocked');
  return r;
})"""

# дословный текст, рядом с которым элемент: его окно (диалог) или ближайший блок с абзацем текста
_CONTEXT_JS = """(n) => {
  const el = document.querySelector(`[data-bitowi-n="${n}"]`);
  if (!el) return '';
  let p = el.closest('dialog, [role=dialog], [role=alertdialog], [aria-modal=true], .modal');
  if (!p) {
    p = el;
    while (p.parentElement && p.parentElement !== document.body && (p.innerText || '').trim().length < 300) {
      p = p.parentElement;
    }
  }
  return (p.innerText || '').trim();
}"""

# страница отрисована: есть текст или хотя бы один видимый интерактивный элемент
_RENDERED_JS = """() => {
  if (!document.body) return false;
  if (document.body.innerText.trim().length > 0) return true;
  return [...document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, [role=button]')]
    .some((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
}"""

NETWORK_IDLE_MS = 8000
RENDER_WAIT_S = 10


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CourseBrowser:
    def __init__(
        self,
        settings: Settings,
        start_url: str,
        profile_dir: Path,
        storage: Path | None = None,
        *,
        course_url: str = "",
        domains: list[str] | None = None,
        visited: list[str] | None = None,
        resolver: Resolver = system_resolve,
    ) -> None:
        self.settings = settings
        self.start_url = start_url
        # домен курса — из ссылки курса, а не со стартовой страницы (после /resume она может быть внешней)
        course_url = course_url or start_url
        self.origin = origin_of(course_url)
        self.policy = NetPolicy(course_url, settings.course_private_allow, domains)
        self.proxy = FilteringProxy(self.policy, resolver)
        self.profile_dir = profile_dir
        self.storage = storage
        self.port = 0
        self.last_url = start_url
        self.stamp = ""
        # точка обзора: её двигает только действие scroll; клики и ввод (браузер сам прокручивает к полю) — нет
        self.view_url = ""
        self.view_y = 0
        # внешние домены, куда переходила вкладка (по порядку); превышение лимита — домен, на котором остановились
        self.external_domains: list[str] = list(visited or [])
        self.limit_hit = ""
        # отправка формы на внешний домен — только пока действует разрешение владельца
        self.submit_allowed = False
        self._notes: list[str] = []
        self._pw = None
        self.context = None

    @property
    def cdp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def page(self):
        return next(p for p in self.context.pages if not p.is_closed())

    def note(self, text: str) -> None:
        if text not in self._notes:
            self._notes.append(text)

    def take_notes(self) -> list[str]:
        """Refusals of the network policy since the last call — for the worker's next input."""
        notes, self._notes = self._notes, []
        for host, port, why in self.proxy.take_blocked():
            notes.append(f"Запрос к {host}:{port} заблокирован: {why}.")
        return list(dict.fromkeys(notes))

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.profile_dir.chmod(0o700)
        width, height = (int(x) for x in self.settings.course_viewport.split("x"))
        await self.proxy.start()
        self.port = free_port()
        self._pw = await async_playwright().start()
        # постоянный контекст = контекст по умолчанию: к нему же подключается MCP по CDP
        self.context = await self._pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=True,
            viewport={"width": width, "height": height},
            accept_downloads=False,
            service_workers="block",
            args=[
                f"--remote-debugging-port={self.port}", "--remote-debugging-address=127.0.0.1",
                # весь трафик — через фильтрующий прокси, включая loopback (иначе Chromium ходит туда напрямую)
                f"--proxy-server=http://127.0.0.1:{self.proxy.port}", "--proxy-bypass-list=<-loopback>",
                "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ],
        )
        await self.context.add_init_script(script=guard_script("course", self.policy))
        await self.context.route("**/*", self._route)
        await self.context.route_web_socket("**/*", self._websocket)
        await self._load_cookies()
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=self.settings.browser_timeout * 1000)

    async def _load_cookies(self) -> None:
        """Only the course domain's cookies: on other domains the bot is not logged in."""
        if not (self.storage and self.storage.exists()):
            return
        cookies = json.loads(self.storage.read_text(encoding="utf-8")).get("cookies", [])
        own = [c for c in cookies if self.policy.is_course("https://" + str(c.get("domain", "")).lstrip("."))]
        if len(own) < len(cookies):
            log.info("cookies of other domains are not loaded: %d", len(cookies) - len(own))
        if own:
            await self.context.add_cookies(own)

    async def _route(self, route) -> None:
        req = route.request
        url = req.url
        nav = req.is_navigation_request() and req.frame.parent_frame is None
        why = self.policy.literal_block(url)
        if why:
            host = host_port(url)[0] or url.split(":", 1)[0]
            self.note(f"{'Переход' if nav else 'Запрос'} на {host} заблокирован: {why}.")
            if nav:
                # 204 отменяет навигацию, вкладка остаётся на месте (abort увёл бы на chrome-error://)
                await route.fulfill(status=204, body="")
            else:
                await route.abort("blockedbyclient")
            return
        if nav and url.startswith(("http://", "https://")) and not self.policy.is_course(url):
            host = host_port(url)[0]
            if not self.policy.navigation_allowed(url):
                self.note(f"Переход на {host} заблокирован: домена нет в списке «домены:» этого курса.")
                await route.fulfill(status=204, body="")
                return
            if req.method != "GET" and not self.submit_allowed:
                self.note(f"Отправка формы на внешний сайт {host} — только после подтверждения владельца.")
                await route.fulfill(status=204, body="")
                return
            if host not in self.external_domains:
                if len(self.external_domains) >= self.settings.course_max_external_domains:
                    self.limit_hit = host
                    await route.fulfill(status=204, body="")
                    return
                self.external_domains.append(host)
                log.info("new external domain: %s", host)
        await route.fallback()

    async def _websocket(self, ws) -> None:
        why = self.policy.literal_block(ws.url)
        if why:
            self.note(f"WebSocket на {host_port(ws.url)[0]} заблокирован: {why}.")
            await ws.close(code=1008, reason="blocked")
            return
        # соединение идёт из страницы — через прокси, с проверкой IP
        ws.connect_to_server()

    async def close(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self._pw:
            await self._pw.stop()
            self._pw = None
        await self.proxy.close()

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self.settings.browser_timeout * 1000)

    async def check_domain(self) -> str:
        """After the courier: a link opened in a new tab moves to the main tab; a forbidden page is rolled back.

        Returns the forbidden URL the tab was on (the step stops), or ''."""
        pages = [p for p in self.context.pages if not p.is_closed()]
        page, moved = pages[0], ""
        for extra in pages[1:]:
            url = extra.url
            await extra.close()
            if url.startswith(("http://", "https://")) and not self.policy.literal_block(url):
                moved = url
        if moved:
            self.note("Ссылка открылась в новой вкладке — открыта в основной.")
            try:
                await self.goto(moved)
            except Exception:  # noqa: BLE001 — переход мог быть отклонён политикой
                pass
        url = page.url
        if not self.policy.literal_block(url) and not url.startswith("chrome-error:"):
            if url.startswith(("http://", "https://")):
                self.last_url = url
            return ""
        log.warning("forbidden page in the tab: %s", url)
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=self.settings.browser_timeout * 1000)
        except Exception:  # noqa: BLE001 — откат любой ценой
            pass
        if self.policy.literal_block(page.url) or page.url.startswith("chrome-error:"):
            await self.goto(self.last_url)
        return url

    async def settle(self) -> bool:
        """Wait for a SPA to draw: networkidle up to 8 s, then up to 10 s for text or an interactive element."""
        page = self.page
        try:
            await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
        except Exception:  # noqa: BLE001 — болтливые страницы не затихают; это не ошибка
            pass
        deadline = time.monotonic() + RENDER_WAIT_S
        while True:
            try:
                if await page.evaluate(_RENDERED_JS):
                    return True
            except Exception:  # noqa: BLE001 — страница в процессе перехода
                pass
            if time.monotonic() >= deadline:
                return False
            await page.wait_for_timeout(250)

    async def inspect(self, shots_dir: Path, max_shots: int) -> Screen:
        """Number the elements and screenshot the visible part (several shots for a long page)."""
        rendered = await self.settle()
        page = self.page
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

        elements = [classify(Element(**e), self.policy) for e in data["elements"]]
        return Screen(
            shots=shots,
            text=data["text"],
            elements=elements,
            messages=data["messages"],
            progress=data["progress"],
            more_below=start + count * view < data["height"],
            rendered=rendered,
            external=not self.policy.is_course(data["url"]),
            url=data["url"],
            title=data["title"],
        )

    async def stamp_ok(self) -> bool:
        """The page still carries the numbers of the last inspection."""
        try:
            return bool(self.stamp) and await self.page.evaluate("window.__bitowiStamp || ''") == self.stamp
        except Exception:  # noqa: BLE001 — страница в процессе перехода
            return False

    async def context_text(self, n: int) -> str:
        """Verbatim text around element [n] (its dialog or paragraph) — for the owner, never for the worker."""
        try:
            return await self.page.evaluate(_CONTEXT_JS, n)
        except Exception:  # noqa: BLE001 — страница в процессе перехода
            return ""

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
        self.submit_allowed = True
        value = json.dumps({"count": count, "until": int((time.time() + seconds) * 1000)})
        await self.page.evaluate("([k, v]) => sessionStorage.setItem(k, v)", [ALLOW_KEY, value])

    async def revoke_submit(self) -> None:
        self.submit_allowed = False
        try:
            await self.page.evaluate("(k) => sessionStorage.removeItem(k)", ALLOW_KEY)
        except Exception:  # noqa: BLE001
            pass
