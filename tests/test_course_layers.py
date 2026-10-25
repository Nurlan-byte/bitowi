"""Layers of the course mode: the worker never learns that other agents exist.

Code-made text only: page text and the owner's remarks are passed as they are."""

import json
import os
from pathlib import Path

import pytest

from app.agents.runner import RunResult
from app.agents.worker_claude import WORKER_FIX_JSON, ClaudeWorker
from app.course.courier import ERRORS, NO_REPORT, Courier, CourierReport, Executor, StepReport
from app.course.models import Action, ActionResult, Screen
from app.course.worker_input import DEFAULT_RULES, GOAL, StepLog, load_rules, render

AGENT_WORDS = ("курьер", "исполнител", "codex", "claude", "ассистент", "руководител")


def agent_words(text: str) -> list[str]:
    low = text.lower()
    return [w for w in AGENT_WORDS if w in low]


class FakeBrowser:
    async def stamp_ok(self) -> bool:
        return True

    async def blocked(self):
        return []

    async def check_domain(self) -> str:
        return ""


class ChattyExecutor(Executor):
    """Reports in its own words, with an unknown code and a missing result."""

    async def run(self, browser, actions, run_dir, allow_upload) -> CourierReport:
        return CourierReport(results=[
            StepReport(i=1, ok=False, code="not_found", error="Курьер (Codex) не нашёл [3] в снапшоте"),
            StepReport(i=2, ok=False, code="руководитель_ошибся", error="ассистент не понял шаг руководителя"),
        ])


async def test_courier_words_stay_in_journal(settings, tmp_path):
    courier = Courier(settings, FakeBrowser(), ChattyExecutor(), tmp_path / "run", tmp_path / "ws")
    actions = [Action(action="click", element=n) for n in (3, 4, 5)]
    res = await courier.execute(actions)

    assert [r.error for r in res.results] == [ERRORS["not_found"], ERRORS["failed"], NO_REPORT]
    # подробности — для журнала и владельца
    assert res.results[0].detail.startswith("Курьер (Codex)") and res.results[2].detail == ""

    out = render(
        Screen(text="Сколько будет 17·23?"), [], res.results,
        [StepLog(n=1, status="continue", results=res.results, comment="нажал вариант")],
        ["перепроверь второй вопрос"], [], notes=["Запрос к 10.0.0.1:80 заблокирован: локальный адрес."],
    )
    assert agent_words(out) == []
    assert "- click [3] — не выполнено: элемент не найден на странице" in out


def test_code_phrases_have_no_agent_words():
    phrases = [*ERRORS.values(), NO_REPORT, GOAL, DEFAULT_RULES, WORKER_FIX_JSON]
    assert [p for p in phrases if agent_words(p)] == []


def test_unknown_code_is_failed():
    assert StepReport.model_validate({"i": 1, "ok": False, "code": "boom"}).code == "failed"
    assert StepReport.model_validate({"i": 1, "ok": True}).for_worker() == ""


async def test_worker_stdin_and_system_prompt(settings):
    seen = []
    replies = ["не JSON", '{"status": "continue", "actions": []}']

    async def fake_run(cmd, *, cwd, timeout, stdin):
        seen.append(stdin)
        return RunResult(0, '{"result": ' + json.dumps(replies.pop(0)) + ', "is_error": false}', "")

    ws = settings.tasks_dir / "7" / "workspace"
    screen = render(Screen(text="Текст"), [], [ActionResult(action=Action(action="scroll", value="down"), ok=True)],
                    [], [], [])
    await ClaudeWorker(settings, run=fake_run).decide(screen, ws)
    assert len(seen) == 2
    for text in [*seen, (ws.parent / "worker_system.md").read_text(encoding="utf-8")]:
        assert agent_words(text) == [], text


REAL_PROMPTS = Path(os.environ.get("PROMPTS_DIR", "~/bitowi_instructions/prompts")).expanduser()


@pytest.mark.skipif(not (REAL_PROMPTS / "course_worker.md").exists(), reason="нет папки инструкций")
def test_real_worker_prompts():
    # правила — как их видит воркер: комментарии для владельца вырезаны
    texts = {"course_worker.md": (REAL_PROMPTS / "course_worker.md").read_text(encoding="utf-8"),
             "course_rules.md": load_rules(REAL_PROMPTS)}
    assert {name: agent_words(t) for name, t in texts.items() if agent_words(t)} == {}
