"""The courier's real browser path without a model: playwright-mcp with the courier's arguments,
attached over CDP to the orchestrator's browser, driven by plain JSON-RPC."""

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from app.agents import runner
from app.course.courier import COURIER_TOOLS, UPLOAD_TOOL, CodexExecutor
from tests.lms_server import lms  # noqa: F401 — фикстура мини-LMS

# строка снапшота MCP: link "[3] Выйти" [ref=e12]
REF_RE = re.compile(r'"\[(\d+)\][^"]*" \[ref=(e\d+)\]')

MCP_BIN = shutil.which("playwright-mcp", path=os.environ.get("PATH", "") + os.pathsep + str(Path.home() / ".local/bin"))


class FakeBrowser:
    origin = "https://lms.test"
    cdp_endpoint = "http://127.0.0.1:9333"


def test_courier_command(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "resolve_bin", lambda name: name)
    settings.course_extra_origins = "https://cdn.test; "
    ex = CodexExecutor(settings)
    cmd = ex.build_command(tmp_path / "run", FakeBrowser(), allow_upload=False)
    conf = {c.split("=", 1)[0]: c.split("=", 1)[1] for c in cmd if c.startswith("mcp_servers.")}
    tools = json.loads(conf["mcp_servers.playwright.enabled_tools"])
    assert tools == COURIER_TOOLS
    for banned in ("browser_evaluate", "browser_run_code_unsafe", "browser_navigate", "browser_tabs",
                   "browser_press_key", UPLOAD_TOOL):
        assert banned not in tools
    args = json.loads(conf["mcp_servers.playwright.args"])
    assert args[args.index("--cdp-endpoint") + 1] == "http://127.0.0.1:9333"
    assert args[args.index("--allowed-origins") + 1] == "https://lms.test;https://cdn.test"
    assert "--no-webmcp" in args and (tmp_path / "run" / "guard.js").exists()
    assert json.loads(conf["mcp_servers.playwright.cwd"]) == str(tmp_path / "run" / "uploads")
    assert cmd[-1] == "-" and 'web_search="disabled"' in cmd

    up = ex.build_command(tmp_path / "run", FakeBrowser(), allow_upload=True)
    conf = {c.split("=", 1)[0]: c.split("=", 1)[1] for c in up if c.startswith("mcp_servers.")}
    assert UPLOAD_TOOL in json.loads(conf["mcp_servers.playwright.enabled_tools"])


async def test_courier_report_parsed(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "resolve_bin", lambda name: name)
    seen = {}

    async def fake_run(cmd, *, cwd, timeout, stdin):
        seen.update(stdin=stdin, timeout=timeout)
        (Path(cwd) / "courier_last.txt").write_text(
            '{"results": [{"i": 1, "ok": true}], "messages": ["Верно"], "progress": "40%"}', encoding="utf-8"
        )
        return runner.RunResult(0, "", "")

    from app.course.models import Action

    ex = CodexExecutor(settings, run=fake_run)
    report = await ex.run(FakeBrowser(), [Action(action="click", element=3)], tmp_path / "run", False)
    assert report.results[0].ok and report.progress == "40%"
    assert '"element": 3' in seen["stdin"] and seen["timeout"] == settings.courier_timeout
    assert "lms.test" not in seen["stdin"]  # в промпте курьера — только действия


@pytest.mark.e2e
@pytest.mark.skipif(not MCP_BIN, reason="playwright-mcp не установлен")
async def test_mcp_over_cdp_sees_numbers_and_guard_holds(settings, tmp_path, lms):
    pytest.importorskip("playwright")
    pytest.importorskip("uvicorn")
    from app.browser.session import BrowserSessions
    from app.browser.sites import profile_for
    from app.course.browser import CourseBrowser
    from tests.lms_server import LMS_PASSWORD, LMS_USER, reset, stats

    reset(lms)
    settings.fake_lms_host = lms.split("://", 1)[1]
    settings.fake_lms_user, settings.fake_lms_password = LMS_USER, LMS_PASSWORD
    url = lms + "/course"
    storage = await BrowserSessions(settings).ensure_session(profile_for(url, settings), url)
    browser = CourseBrowser(settings, url, tmp_path / "profile", storage)
    await browser.start()
    try:
        screen = await browser.inspect(tmp_path / "shots", 2)
        n_exit = next(e.n for e in screen.elements if e.label == "Выйти")
        n_next = next(e.n for e in screen.elements if e.label == "Продолжить курс")
        args = CodexExecutor(settings).mcp_args(browser, tmp_path / "run")
        snap, = await mcp_session(args, [("browser_snapshot", {})])
        # MCP видит номера оверлея в доступных именах — по ним курьер находит элементы
        refs = dict(REF_RE.findall(snap))
        assert str(n_exit) in refs and str(n_next) in refs
        # бейджи — картинки, в дереве доступности их нет
        assert not re.search(r'generic "\[\d+\]"', snap)

        await mcp_session(args, [("browser_snapshot", {}), ("browser_click", click(n_exit))])
        await asyncio.sleep(0.3)
        assert browser.page.url == url and stats(lms)["traps"]["logout"] == 0
        assert (n_exit, "deny") in await browser.blocked()

        await mcp_session(args, [("browser_snapshot", {}), ("browser_click", click(n_next))])
        await asyncio.sleep(0.3)
        assert browser.page.url.endswith("/course/item/r1")
        assert await browser.check_domain() == ""
    finally:
        await browser.close()


def click(n: int):
    """browser_click on element [n]: the ref comes from the snapshot taken in the same MCP session."""

    def args(outputs: list[str]) -> dict:
        refs = dict(REF_RE.findall(outputs[-1]))
        return {"element": f"[{n}]", "target": refs[str(n)]}

    return args


async def mcp_session(args: list[str], calls: list[tuple[str, dict]]) -> list[str]:
    """One MCP process (as within one Codex call): refs are valid only inside it."""
    proc = await asyncio.create_subprocess_exec(
        MCP_BIN, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "PATH": os.environ.get("PATH", "") + os.pathsep + str(Path(MCP_BIN).parent)},
    )

    async def send(msg):
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

    async def recv(i):
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), 60)
            if not line:
                raise RuntimeError("playwright-mcp завершился")
            msg = json.loads(line)
            if msg.get("id") == i:
                return msg

    try:
        await send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        await recv(0)
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        out = []
        for i, (name, arguments) in enumerate(calls, 1):
            if callable(arguments):
                arguments = arguments(out)
            await send({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            msg = await recv(i)
            out.append("".join(c.get("text", "") for c in msg["result"]["content"]))
        return out
    finally:
        proc.terminate()
        await proc.wait()


@pytest.mark.e2e
@pytest.mark.skipif(not MCP_BIN, reason="playwright-mcp не установлен")
async def test_mcp_clicks_hidden_and_labelledby_radios(settings, tmp_path, lms):
    pytest.importorskip("playwright")
    from app.browser.session import BrowserSessions
    from app.browser.sites import profile_for
    from app.course.browser import CourseBrowser
    from tests.lms_server import LMS_PASSWORD, LMS_USER, reset

    reset(lms)
    settings.fake_lms_host = lms.split("://", 1)[1]
    settings.fake_lms_user, settings.fake_lms_password = LMS_USER, LMS_PASSWORD
    url = lms + "/course/radios"
    storage = await BrowserSessions(settings).ensure_session(profile_for(url, settings), url)
    browser = CourseBrowser(settings, url, tmp_path / "profile", storage)
    await browser.start()
    try:
        screen = await browser.inspect(tmp_path / "shots", 1)
        switches = [e for e in screen.elements if e.kind in ("radio", "checkbox")]
        args = CodexExecutor(settings).mcp_args(browser, tmp_path / "run")
        snap, = await mcp_session(args, [("browser_snapshot", {})])
        refs = dict(REF_RE.findall(snap))
        # как в Moodle: без правки aria-labelledby номера в снапшоте не было — курьер писал «элемент не найден»
        assert all(str(e.n) in refs for e in switches), [e.n for e in switches if str(e.n) not in refs]

        m2 = next(e for e in switches if e.label == "b. Обмен")
        h3 = [e for e in switches if e.kind == "radio" and e.label == "Соединение"][0]
        await mcp_session(args, [("browser_snapshot", {}), ("browser_click", click(m2.n)),
                                 ("browser_snapshot", {}), ("browser_click", click(h3.n))])
        checked = await browser.page.evaluate(
            "() => [document.getElementById('m2').checked, document.querySelector('input[name=h][value=\"3\"]').checked]"
        )
        assert checked == [True, True]
    finally:
        await browser.close()
