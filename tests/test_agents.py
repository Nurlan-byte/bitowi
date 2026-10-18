import json
import sys
from pathlib import Path

import pytest

from app.agents import runner
from app.agents.base import AgentError, AgentLimit, AgentTimeout, BossBadJSON
from app.agents.boss_codex import CodexBoss, fill, parse_json
from app.agents.runner import RunResult
from app.agents.worker_claude import ClaudeWorker, parse_output


@pytest.fixture(autouse=True)
def _resolve(monkeypatch):
    monkeypatch.setattr(runner, "resolve_bin", lambda name: name)


def test_worker_command_flags(settings):
    cmd = ClaudeWorker(settings).build_command()
    assert cmd[:2] == ["claude", "-p"]
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    assert "Skill" in disallowed.split(",")
    assert "WebSearch" in disallowed and "WebFetch" in disallowed
    assert "--strict-mcp-config" in cmd
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--append-system-prompt") + 1] == "Ты — исполнитель."
    assert "--dangerously-skip-permissions" not in cmd


def test_worker_web_flag(settings):
    settings.worker_web = True
    cmd = ClaudeWorker(settings).build_command()
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
    assert cmd[:3] == ["codex", "exec", "--skip-git-repo-check"]
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
