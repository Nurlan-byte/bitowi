"""deploy/bitowi-worker-run with a fake claude (Linux only, no sudo)."""

import getpass
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.agents.worker_claude import ClaudeWorker

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "bitowi-worker-run"

linux = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("timeout"), reason="только Linux")


@pytest.fixture
def wrapper(tmp_path):
    tasks = tmp_path / "tasks"
    (tasks / "7" / "workspace").mkdir(parents=True)
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/bin/bash\n"
        'python3 -c \'import json,os,sys; print(json.dumps({"cwd": os.getcwd(), "umask": oct(os.umask(0)),'
        ' "home": os.environ["HOME"], "path": os.environ["PATH"], "args": sys.argv[1:], "stdin": sys.stdin.read()}))\' "$@"\n'
    )
    fake.chmod(0o755)
    text = WRAPPER.read_text()
    text = text.replace("readonly TASKS_DIR=/srv/bitowi/tasks", f"readonly TASKS_DIR={tasks}")
    text = text.replace("readonly WORKER_USER=bitowi-worker", f"readonly WORKER_USER={getpass.getuser()}")
    text = text.replace("readonly CLAUDE=/home/bitowi-worker/.local/bin/claude", f"readonly CLAUDE={fake}")
    script = tmp_path / "bitowi-worker-run"
    script.write_text(text)
    script.chmod(0o755)
    return script, tasks


def run(script, *args, stdin=""):
    return subprocess.run([str(script), *args], input=stdin, capture_output=True, text=True, timeout=30)


@linux
def test_wrapper_runs_in_workspace(wrapper):
    script, tasks = wrapper
    res = run(script, "7", "20", "-p", "--output-format", "json", stdin="блокнот")
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["cwd"] == str(tasks / "7" / "workspace")
    assert data["umask"] == "0o7"
    assert data["home"] == "/home/bitowi-worker"
    assert data["path"].startswith("/home/bitowi-worker/.local/bin:")
    assert data["args"] == ["-p", "--output-format", "json"]
    assert data["stdin"] == "блокнот"


@linux
@pytest.mark.parametrize("task_id", ["../7", "7/../7", "abc", "", "-1", "7 8", "1234567890"])
def test_wrapper_rejects_bad_task_id(wrapper, task_id):
    script, _ = wrapper
    res = run(script, task_id, "20", "-p")
    assert res.returncode == 64 and "bitowi-worker-run:" in res.stderr


@linux
@pytest.mark.parametrize("timeout", ["0", "x", "-5", "999999"])
def test_wrapper_rejects_bad_timeout(wrapper, timeout):
    script, _ = wrapper
    assert run(script, "7", timeout, "-p").returncode == 64


@linux
def test_wrapper_rejects_missing_or_symlinked_workspace(wrapper, tmp_path):
    script, tasks = wrapper
    assert run(script, "8", "20").returncode == 64
    outside = tmp_path / "outside"
    outside.mkdir()
    (tasks / "9").mkdir()
    (tasks / "9" / "workspace").symlink_to(outside)
    assert run(script, "9", "20").returncode == 64


@linux
def test_wrapper_refuses_other_user(wrapper):
    script, _ = wrapper
    script.write_text(script.read_text().replace(f"readonly WORKER_USER={getpass.getuser()}", "readonly WORKER_USER=nobody-else"))
    res = run(script, "7", "20")
    assert res.returncode == 64 and "только от" in res.stderr


@linux
def test_wrapper_timeout_kills(wrapper, tmp_path):
    script, _ = wrapper
    slow = tmp_path / "claude"
    slow.write_text("#!/bin/bash\nsleep 30\n")
    res = run(script, "7", "1")
    assert res.returncode == 124


def test_worker_sudo_command(settings, monkeypatch):
    from app.agents import runner

    monkeypatch.setattr(runner, "resolve_bin", lambda name: f"/usr/bin/{name}")
    settings.worker_mode = "sudo"
    cmd = ClaudeWorker(settings).build_command("42")
    assert cmd[:7] == [
        "/usr/bin/sudo", "-n", "-u", "bitowi-worker", "/usr/local/bin/bitowi-worker-run", "42", str(settings.worker_timeout),
    ]
    assert "claude" not in " ".join(cmd[:7])  # путь к claude фиксирован в обёртке
    assert cmd[7] == "-p" and "--disallowedTools" in cmd


async def test_worker_sudo_error_is_clear(settings):
    from app.agents.base import AgentError
    from app.agents.runner import RunResult
    from app.agents.worker_claude import parse_output

    with pytest.raises(AgentError, match="не настроен"):
        parse_output(RunResult(1, "", "sudo: a password is required\n"))
