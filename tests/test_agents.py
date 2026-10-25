import json
import sys
from pathlib import Path

import pytest

from app.agents import runner
from app.agents.base import AgentError, AgentLimit, AgentTimeout, BossBadJSON
from app.agents.base import fill, parse_json
from app.agents.boss_codex import CodexBoss
from app.agents.runner import RunResult
from app.agents.worker_claude import ClaudeWorker, parse_output


@pytest.fixture(autouse=True)
def _resolve(monkeypatch):
    monkeypatch.setattr(runner, "resolve_bin", lambda name: name)


def test_worker_command_flags(settings, tmp_path):
    cmd = ClaudeWorker(settings).build_command(tmp_path / "7")
    assert cmd[:2] == ["claude", "-p"]
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    assert "Skill" in disallowed.split(",")
    assert "WebSearch" in disallowed and "WebFetch" in disallowed
    assert "--strict-mcp-config" in cmd
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--append-system-prompt-file") + 1] == str(tmp_path / "7" / "worker_system.md")
    assert cmd[cmd.index("--settings") + 1] == str(tmp_path / "7" / "worker_settings.json")
    # в аргументах процесса (видны в ps) нет ни текста промпта, ни списка путей
    joined = " ".join(cmd)
    assert "исполнитель" not in joined and "deny" not in joined
    assert "--dangerously-skip-permissions" not in cmd


def test_worker_web_flag(settings, tmp_path):
    settings.worker_web = True
    cmd = ClaudeWorker(settings).build_command(tmp_path / "7")
    assert "WebSearch" in cmd[cmd.index("--allowedTools") + 1]
    assert cmd[cmd.index("--disallowedTools") + 1] == "Skill"


async def test_worker_runs_in_workspace(settings, tmp_path):
    seen = {}

    async def fake_run(cmd, *, cwd, timeout, stdin):
        seen.update(cwd=cwd, timeout=timeout, stdin=stdin)
        return RunResult(0, json.dumps({"result": "### ОТВЕТ\n1", "is_error": False}), "")

    ws = settings.tasks_dir / "7" / "workspace"
    out = await ClaudeWorker(settings, run=fake_run).solve("блокнот", ws)
    assert out.endswith("1")
    assert seen["cwd"] == ws and ws.is_dir()
    assert seen["timeout"] == settings.worker_timeout
    assert "блокнот" in seen["stdin"]
    # промпт и настройки — файлами рядом с workspace, не внутри него
    assert (ws.parent / "worker_system.md").read_text(encoding="utf-8") == "Ты — исполнитель."
    assert json.loads((ws.parent / "worker_settings.json").read_text(encoding="utf-8"))["permissions"]["deny"]
    assert not (ws / "worker_system.md").exists()
    # TASKS_DIR вне проекта
    project = Path(__file__).resolve().parents[1]
    assert project not in ws.resolve().parents


def test_worker_parse_errors():
    with pytest.raises(AgentLimit):
        parse_output(RunResult(1, json.dumps({"result": "Claude usage limit reached", "is_error": True}), ""))
    with pytest.raises(AgentLimit):
        parse_output(RunResult(1, "", "Error: rate limit"))
    with pytest.raises(AgentError):
        parse_output(RunResult(1, "garbage", "boom"))
    # ответ про лимиты в самом решении — не лимит
    assert "rate limit" in parse_output(RunResult(0, json.dumps({"result": "rate limit = 5", "is_error": False}), ""))


def test_boss_command_flags(settings, tmp_path):
    cmd = CodexBoss(settings).build_command(tmp_path)
    assert cmd[:2] == ["codex", "exec"] and "--skip-git-repo-check" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("-C") + 1] == str(tmp_path)
    assert cmd[-1] == "-"


def test_fill_keeps_json_braces():
    out = fill('Раунд {round} из {max_rounds}. {"verdict": "accept"}', round=2, max_rounds=5)
    assert out == 'Раунд 2 из 5. {"verdict": "accept"}'


def test_parse_json_fenced():
    assert parse_json('```json\n{"verdict": "accept"}\n```') == {"verdict": "accept"}
    assert parse_json('Вот: {"a": 1} готово') == {"a": 1}


def _boss_with(outputs, settings, calls):
    async def fake_run(cmd, *, cwd, timeout, stdin):
        calls.append(stdin)
        out = outputs.pop(0)
        if isinstance(out, RunResult):
            return out
        (Path(cwd) / "boss_last.txt").write_text(out, encoding="utf-8")
        return RunResult(0, "", "")

    return CodexBoss(settings, run=fake_run)


async def test_boss_review_ok(settings, tmp_path):
    calls = []
    boss = _boss_with(['{"verdict": "accept", "final_answer": "391", "confidence": 0.9}'], settings, calls)
    res = await boss.review(tmp_path, "**Заказчик:** x", {"source": "text"}, 2, 5)
    assert res.verdict == "accept" and res.final_answer == "391"
    assert "Раунд 2 из 5" in calls[0]
    assert '"source": "text"' in calls[0]


async def test_boss_bad_json_retry_then_ok(settings, tmp_path):
    calls = []
    boss = _boss_with(["не json", '{"verdict": "revise", "comment_for_worker": "ещё"}'], settings, calls)
    res = await boss.review(tmp_path, "nb", {}, 1, 5)
    assert res.verdict == "revise"
    assert len(calls) == 2 and "не удалось разобрать" in calls[1]


async def test_boss_bad_json_twice(settings, tmp_path):
    boss = _boss_with(["нет", '{"verdict": "maybe"}'], settings, [])
    with pytest.raises(BossBadJSON):
        await boss.review(tmp_path, "nb", {}, 1, 5)


async def test_boss_limit(settings, tmp_path):
    boss = _boss_with([RunResult(1, "", "You've hit your usage limit")], settings, [])
    with pytest.raises(AgentLimit):
        await boss.review(tmp_path, "nb", {}, 1, 5)


async def test_runner_timeout_kills(tmp_path):
    with pytest.raises(AgentTimeout):
        await runner.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout=0.5)


async def test_runner_stdin_and_output(tmp_path):
    res = await runner.run(
        [sys.executable, "-X", "utf8", "-c", "import sys; print(sys.stdin.read().upper())"], cwd=tmp_path, timeout=10, stdin="ок"
    )
    assert res.returncode == 0 and "ОК" in res.stdout


def test_looks_like_limit():
    assert runner.looks_like_limit("Error 429 Too Many Requests")
    assert not runner.looks_like_limit("ответ: 391")


# --- этап 2: браузер куратора ---
from app.agents.base import BrowserOpts, ExtractResult  # noqa: E402


def test_boss_review_has_no_browser(settings, tmp_path):
    cmd = CodexBoss(settings).build_command(tmp_path)
    assert 'web_search="disabled"' in cmd
    assert not any("mcp_servers" in c for c in cmd)


def test_boss_browser_extract_mode_has_guard(settings, tmp_path):
    opts = BrowserOpts(storage_state=tmp_path / "auth.json", output_dir=tmp_path / "screens", guard=True)
    cmd = CodexBoss(settings).build_command(tmp_path, opts)
    joined = " ".join(cmd)
    assert 'default_tools_approval_mode="approve"' in joined
    args = json.loads(next(c for c in cmd if c.startswith("mcp_servers.playwright.args=")).split("=", 1)[1])
    assert "--isolated" in args and "--headless" in args
    assert args[args.index("--storage-state") + 1] == str(tmp_path / "auth.json")
    assert args[args.index("--init-script") + 1].endswith("guard.js")


def test_boss_browser_submit_mode_has_no_guard(settings, tmp_path):
    # этап 3: при сдаче блокировка кнопок отключается
    opts = BrowserOpts(storage_state=None, output_dir=tmp_path, guard=False)
    args = CodexBoss(settings).mcp_args(opts)
    assert "--init-script" not in args and "--storage-state" not in args


async def test_boss_extract_refuses_without_guard(settings, tmp_path):
    boss = _boss_with(['{"status": "ok"}'], settings, [])
    with pytest.raises(AssertionError):
        await boss.extract(tmp_path, "http://x", BrowserOpts(None, tmp_path, guard=False))


async def test_boss_extract_parses(settings, tmp_path):
    calls = []
    raw = json.dumps({
        "status": "ok", "platform": "Moodle",
        "questions": [{"n": 1, "text": "2+2?", "answer_type": "number"}],
    })
    boss = _boss_with([raw], settings, calls)
    res = await boss.extract(tmp_path, "http://x/q", BrowserOpts(None, tmp_path / "s", guard=True))
    assert isinstance(res, ExtractResult) and res.questions[0].text == "2+2?"
    assert "http://x/q" in calls[0] and str(tmp_path / "s") in calls[0]


def test_worker_denies_project_and_boss_files(settings):
    data = json.loads(ClaudeWorker(settings).sandbox_settings())
    deny = data["permissions"]["deny"]
    assert any(str(settings.data_dir.as_posix()) in d for d in deny)
    assert any(Path(__file__).resolve().parents[1].as_posix() in d for d in deny)
    assert data["sandbox"]["allowUnsandboxedCommands"] is False
    assert data["sandbox"]["enabled"] is settings.worker_sandbox
