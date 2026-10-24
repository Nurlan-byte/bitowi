"""A big quiz page (25 questions × 2 numeric fields, like «Balancing Chemical Equations» in Moodle):
the real ClaudeWorker.decide parsing with a scripted CLI, the real browser and DirectExecutor."""

import json

import pytest

pytest.importorskip("playwright")
pytest.importorskip("uvicorn")

from app.agents.fakes import FakeBoss  # noqa: E402
from app.agents.runner import RunResult  # noqa: E402
from app.agents.worker_claude import ClaudeWorker  # noqa: E402
from app.browser.session import BrowserSessions  # noqa: E402
from app.course.courier import DirectExecutor  # noqa: E402
from app.course.models import Decision  # noqa: E402
from app.course.runner import MAX_ACTIONS_PER_STEP, CourseRunner  # noqa: E402
from app.orchestrator import Orchestrator, State  # noqa: E402
from tests.course_bot import page_text, parse  # noqa: E402
from tests.lms_server import LMS_PASSWORD, LMS_USER, lms, reset, stats  # noqa: E402,F401

pytestmark = pytest.mark.e2e

BIG = 25


@pytest.fixture
def lms_settings(settings, lms):
    reset(lms)
    settings.fake_lms_host = lms.split("://", 1)[1]
    settings.fake_lms_user = LMS_USER
    settings.fake_lms_password = LMS_PASSWORD
    settings.browser_timeout = 15
    return settings


def natural_reply(screen: str, limit: int | None = None) -> str:
    """What a model naturally writes: numbers as values, "[N]" as element, answers as an object."""
    els = parse(screen)
    if "Баллы:" in page_text(screen):
        return json.dumps({"status": "course_done", "answers": None, "actions": [], "comment": None})
    fields = [n for n, kind, _, _ in els if kind == "text"]
    check = next(n for n, _, label, _ in els if label == "Проверить")
    actions = []
    for i in range(1, BIG + 1):
        actions.append({"action": "type", "element": fields[2 * i - 2], "value": i + 1})
        actions.append({"action": "type", "element": f"[{fields[2 * i - 1]}]", "value": float(i)})
    actions.append({"action": "click", "element": check})
    status = "submit_ready"
    if limit:
        actions, status = actions[:limit], "continue"
    answers = {f"Вопрос {i}": {"x": i + 1, "y": i} for i in range(1, BIG + 1)}
    return "```json\n" + json.dumps({"status": status, "answers": answers, "actions": actions}, ensure_ascii=False) + "\n```"


class ScriptedClaude:
    """Stands in for runner.run of the claude CLI: each call answers with the next script item."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.stdins: list[str] = []

    async def __call__(self, cmd, *, cwd, timeout, stdin):
        self.stdins.append(stdin)
        step = self.script.pop(0) if self.script else natural_reply
        result = step(stdin) if callable(step) else step
        return RunResult(0, json.dumps({"result": result, "is_error": False}), "")


def make(settings, db, notifier, claude) -> Orchestrator:
    worker = ClaudeWorker(settings, run=claude)
    course = CourseRunner(settings, db, worker, notifier, BrowserSessions(settings), DirectExecutor())
    return Orchestrator(settings, db, FakeBoss(), worker, notifier, None, course)


def test_decision_accepts_natural_output():
    d = Decision.model_validate({
        "status": " Submit_Ready ", "comment": None, "answers": {"x": 2, "y": [1, 2]},
        "actions": [{"action": "TYPE", "element": "[12]", "value": 3.0}, {"action": "click", "element": "7"},
                    {"action": "type", "element": 5, "value": 2.5}, {"action": "select", "element": 9, "value": None}],
    })
    assert d.status == "submit_ready" and d.comment == ""
    assert d.answers == ["x — 2", "y — 1, 2"]
    assert [(a.action, a.element, a.value) for a in d.actions] == [
        ("type", 12, "3"), ("click", 7, ""), ("type", 5, "2.5"), ("select", 9, ""),
    ]
    assert len(Decision(status="continue", actions=[{"action": "click", "element": 1}] * 80).actions) == 80


async def test_big_page_filled_and_submitted_after_confirm(lms_settings, db, notifier, lms):
    claude = ScriptedClaude([natural_reply])
    orch = make(lms_settings, db, notifier, claude)
    try:
        task = await orch.submit_course(lms + "/course/big")
        await orch.wait_idle()
        assert (await db.get_task(task.id)).state == State.AWAIT_CONFIRM
        assert stats(lms)["big_checks"] == 0
        _, shots, answers, button = notifier.course_previews[-1]
        assert button == "Проверить" and len(answers) == BIG and answers[0] == "Вопрос 1 — x: 2; y: 1"
        assert len(shots) >= 2  # длинная страница — несколько скриншотов, с начала формы

        assert await orch.confirm(task.id)
        await orch.wait_idle()
        assert stats(lms)["big_checks"] == 1
        assert (await db.get_task(task.id)).state == State.DONE
        assert "Баллы: 25 из 25" in (await db.get_course(task.id))["score"]
        assert stats(lms)["traps"] == {"logout": 0, "profile": 0, "forum": 0, "delete": 0}
    finally:
        await orch.course.close()


async def test_bad_json_twice_pauses_and_resume_continues(lms_settings, db, notifier, lms):
    truncated = natural_reply  # полный ответ, обрезанный посередине
    claude = ScriptedClaude([
        lambda s: truncated(s)[:1500],
        lambda s: '{"status": "continue", "actions": [{"action": "press", "element": 3}]}',
        natural_reply,
    ])
    orch = make(lms_settings, db, notifier, claude)
    try:
        task = await orch.submit_course(lms + "/course/big")
        await orch.wait_idle()
        task = await db.get_task(task.id)
        assert task.state == State.PAUSED and task.resume_state == State.COURSE  # пауза, а не провал
        assert "/resume" in notifier.statuses[-1] and "actions.0.action" in notifier.statuses[-1]
        # повтор получил понятную ошибку, а не «1 validation error»
        assert "ответ обрывается" in claude.stdins[1] and "Значения value — строки" in claude.stdins[1]

        boss = lms_settings.boss_root / str(task.id)
        raw = sorted(p.name for p in (boss / "bad_json").iterdir())
        assert raw == ["step1-1.txt", "step1-2.txt"]
        assert (boss / "bad_json" / "step1-1.txt").read_text(encoding="utf-8") == natural_reply(claude.stdins[0])[:1500]
        entry = json.loads((boss / "journal.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert entry["status"] == "bad_json" and len(entry["errors"]) == 2 and "action" in entry["errors"][1]

        await orch.resume()
        await orch.wait_idle()
        assert (await db.get_task(task.id)).state == State.AWAIT_CONFIRM
        assert stats(lms)["big_checks"] == 0
    finally:
        await orch.course.close()


async def test_too_many_actions_are_split(lms_settings, db, notifier, lms):
    def seventy(screen: str) -> str:
        data = json.loads(natural_reply(screen).strip("`\njson"))
        data["actions"] = data["actions"][:-1] * 2 + [data["actions"][-1]]  # 100 вводов + «Проверить»
        return json.dumps(data)

    claude = ScriptedClaude([seventy, natural_reply])
    orch = make(lms_settings, db, notifier, claude)
    try:
        task = await orch.submit_course(lms + "/course/big")
        await orch.wait_idle()
        # первый шаг: выполнены первые 60, сдача из хвоста не нажата; воркер узнал об этом
        assert f"не больше {MAX_ACTIONS_PER_STEP} действий" in claude.stdins[1]
        assert stats(lms)["big_checks"] == 0
        assert (await db.get_task(task.id)).state == State.AWAIT_CONFIRM
    finally:
        await orch.course.close()
