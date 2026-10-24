"""Courier isolation: wrapper with a fake codex (Linux, no sudo), codex lockdown flags, sudo command,
and — on the server, once bitowi-courier is installed — what the courier can actually read."""

import getpass
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.agents import runner
from app.agents.boss_codex import CODEX_LOCKDOWN, CodexBoss
from app.course.courier import Courier, CodexExecutor, DirectExecutor, upload_dir
from app.course.models import Action
from app.course.netpolicy import NetPolicy

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "bitowi-courier-run"
INSTALLED = Path("/usr/local/bin/bitowi-courier-run")

linux = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("timeout"), reason="только Linux")


class FakeBrowser:
    origin = "https://lms.test"
    cdp_endpoint = "http://127.0.0.1:9333"
    policy = NetPolicy("https://lms.test")


@pytest.fixture(autouse=True)
def _resolve(monkeypatch):
    monkeypatch.setattr(runner, "resolve_bin", lambda name: f"/usr/bin/{name}")


def flags(cmd: list[str], name: str) -> list[str]:
    return [cmd[i + 1] for i, c in enumerate(cmd[:-1]) if c == name]


def test_codex_lockdown_for_boss_and_courier(settings, tmp_path):
    boss = CodexBoss(settings).build_command(tmp_path)
    courier = CodexExecutor(settings).build_command(tmp_path / "run", FakeBrowser(), allow_upload=False)
    for cmd in (boss, courier):
        assert set(flags(cmd, "--disable")) == set(CODEX_LOCKDOWN)
        for must in ("shell_tool", "unified_exec", "multi_agent", "apps", "view_image"):
            assert "code_mode_host" not in CODEX_LOCKDOWN  # через него вызываются инструменты MCP
            assert must in CODEX_LOCKDOWN
        assert "--ignore-user-config" in cmd and flags(cmd, "--sandbox") == ["read-only"]


def test_courier_sudo_command(settings):
    settings.courier_mode = "sudo"
    run_dir = settings.courier_dir(42)
    cmd = CodexExecutor(settings).build_command(run_dir, FakeBrowser(), allow_upload=False)
    assert cmd[:8] == [
        "/usr/bin/sudo", "-n", "-u", "bitowi-courier", "/usr/local/bin/bitowi-courier-run", "42",
        str(settings.courier_timeout), "exec",
    ]
    assert "codex" not in " ".join(cmd[:8])  # путь к codex зафиксирован в обёртке
    conf = {c.split("=", 1)[0]: c.split("=", 1)[1] for c in cmd if c.startswith("mcp_servers.")}
    assert json.loads(conf["mcp_servers.playwright.command"]) == settings.courier_mcp_bin
    assert json.loads(conf["mcp_servers.playwright.cwd"]) == str(run_dir / "uploads")
    # ограничения добавляет обёртка — вызывающий их не передаёт
    assert not flags(cmd, "--disable") and "--sandbox" not in cmd and "--ignore-user-config" not in cmd
    with pytest.raises(ValueError):
        CodexExecutor(settings).build_command(settings.tasks_dir / "42", FakeBrowser(), allow_upload=False)


def test_wrapper_lockdown_matches_code():
    text = WRAPPER.read_text()
    block = text.split("readonly LOCKDOWN_FEATURES=(", 1)[1].split(")", 1)[0]
    assert block.split() == list(CODEX_LOCKDOWN)
    assert "--ignore-user-config --sandbox read-only" in text


def test_upload_is_copied_only_for_approved_submit(settings, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "essay.txt").write_text("текст")
    run_dir = tmp_path / "run"
    courier = Courier(settings, None, DirectExecutor(), run_dir, ws)
    staged = courier._stage_upload(Action(action="upload", element=3, value="essay.txt"))
    assert staged.value == str(upload_dir(run_dir) / "essay.txt")
    assert (upload_dir(run_dir) / "essay.txt").read_text() == "текст"
    outside = courier._stage_upload(Action(action="upload", element=3, value="../../etc/passwd"))
    assert outside.value == "../../etc/passwd" and not (upload_dir(run_dir) / "passwd").exists()


# --- обёртка с фейковым codex ---

@pytest.fixture
def wrapper(tmp_path):
    root = tmp_path / "courier"
    (root / "7").mkdir(parents=True)
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/bin/bash\n"
        'python3 -c \'import json,os,sys; print(json.dumps({"cwd": os.getcwd(), "umask": oct(os.umask(0)),'
        ' "home": os.environ["HOME"], "codex_home": os.environ["CODEX_HOME"], "args": sys.argv[1:],'
        ' "stdin": sys.stdin.read()}))\' "$@"\n'
    )
    fake.chmod(0o755)
    text = WRAPPER.read_text()
    text = text.replace("readonly RUN_ROOT=/srv/bitowi-courier", f"readonly RUN_ROOT={root}")
    text = text.replace("readonly COURIER_USER=bitowi-courier", f"readonly COURIER_USER={getpass.getuser()}")
    text = text.replace("readonly CODEX=/home/bitowi-courier/.local/bin/codex", f"readonly CODEX={fake}")
    script = tmp_path / "bitowi-courier-run"
    script.write_text(text)
    script.chmod(0o755)
    return script, root


def run(script, *args, stdin=""):
    return subprocess.run([str(script), *args], input=stdin, capture_output=True, text=True, timeout=30)


@linux
def test_wrapper_adds_lockdown_and_accepts_code_command(wrapper, settings):
    """The command built by CodexExecutor (sudo mode) passes the wrapper; lockdown comes from the wrapper."""
    script, root = wrapper
    settings.courier_mode = "sudo"
    settings.courier_root = root
    cmd = CodexExecutor(settings).build_command(settings.courier_dir(7), FakeBrowser(), allow_upload=False)
    res = run(script, *cmd[5:], stdin="действия")
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["cwd"] == str(root / "7") and data["umask"] == "0o7"
    assert data["home"] == "/home/bitowi-courier" and data["codex_home"] == "/home/bitowi-courier/.codex"
    args = data["args"]
    assert args[:6] == ["exec", "--ignore-user-config", "--sandbox", "read-only", "-c", 'web_search="disabled"']
    assert flags(args, "--disable") == list(CODEX_LOCKDOWN)
    assert args[-1] == "-" and data["stdin"] == "действия"


@linux
@pytest.mark.parametrize("extra", [
    ["--dangerously-bypass-approvals-and-sandbox"], ["--sandbox", "danger-full-access"], ["--enable", "shell_tool"],
    ["-c", "features.shell_tool=true"], ["-c", 'sandbox_mode="danger-full-access"'],
    ["-c", 'mcp_servers.playwright.command="/bin/sh"'], ["-c", 'mcp_servers.evil.command="/bin/sh"'],
    ["-c", 'mcp_servers.playwright.enabled_tools=["browser_click","browser_evaluate"]'],
    ["-c", 'web_search="live"'], ["-C", "/tmp"], ["-o", "/tmp/out.txt"], ["--profile", "x"], ["--full-auto"],
])
def test_wrapper_rejects_weakening(wrapper, extra):
    script, root = wrapper
    res = run(script, "7", "20", "exec", "-C", str(root / "7"), *extra, "-")
    assert res.returncode == 64 and "bitowi-courier-run:" in res.stderr


@linux
@pytest.mark.parametrize("args", [
    ["../7", "20", "exec"], ["abc", "20", "exec"], ["7", "0", "exec"], ["7", "20", "login"],
    ["8", "20", "exec", "-"], ["7", "20"],
])
def test_wrapper_rejects(wrapper, args):
    script, _ = wrapper
    res = run(script, *args)
    assert res.returncode == 64 and "bitowi-courier-run:" in res.stderr


@linux
def test_wrapper_rejects_output_outside_run_dir(wrapper):
    script, root = wrapper
    res = run(script, "7", "20", "exec", "-o", f"{root}/7/../8/out.txt", "-")
    assert res.returncode == 64


@linux
def test_wrapper_check_mode(wrapper, tmp_path):
    script, _ = wrapper
    secret = tmp_path / "secret"
    secret.write_text("x")
    secret.chmod(0o000)
    res = run(script, "check", str(tmp_path / "missing"), str(secret))
    assert res.returncode == 0
    assert f"denied {tmp_path / 'missing'}" in res.stdout
    assert f"denied {secret}" in res.stdout


# --- на сервере: что курьер реально может прочитать ---

def _installed() -> bool:
    if sys.platform == "win32" or not INSTALLED.exists():
        return False
    res = subprocess.run(["sudo", "-n", "-u", "bitowi-courier", str(INSTALLED), "check", "/"],
                         capture_output=True, text=True, timeout=30)
    return res.returncode == 0


@pytest.mark.e2e
@pytest.mark.skipif(not _installed(), reason="bitowi-courier не установлен")
def test_courier_cannot_read_bot_secrets():
    home = Path.home()
    targets = [
        home, home / "bitowi" / ".env", home / "bitowi" / "data" / "auth", home / "bitowi" / "data" / "bitowi.db",
        home / ".codex" / "auth.json", home / ".claude" / ".credentials.json", home / "bitowi_instructions",
        *sorted((home / "bitowi" / "data" / "auth").glob("*.json")),
    ]
    res = subprocess.run(["sudo", "-n", "-u", "bitowi-courier", str(INSTALLED), "check", *map(str, targets)],
                         capture_output=True, text=True, timeout=30)
    lines = res.stdout.splitlines()
    assert len(lines) == len(targets)
    assert all(line.startswith("denied ") for line in lines), lines
