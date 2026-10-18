"""Live checks with real codex + Playwright MCP (and claude) on the mini LMS.

Spends subscriptions — run only by hand on the server:  pytest -m live -s
"""

import os
import secrets
import shutil

import pytest

from app.agents.base import BrowserOpts
from app.agents.boss_codex import CodexBoss
from app.agents.worker_claude import ClaudeWorker
from app.answers import render_task
from app.browser.session import BrowserSessions
from app.browser.sites import profile_for
from app.config import Settings
from app.db import Database
from app.orchestrator import Orchestrator, State
from tests.lms_server import LMS_PASSWORD, LMS_USER, lms, reset, stats  # noqa: F401
from tests.test_browser_flow import settle

pytestmark = pytest.mark.live

LEAKS = ("✓", "✗", "Правильный ответ", "Баллов", "Неверно", "Верно", "127.0.0.1", "Мини-LMS", "Осталось попыток")


@pytest.fixture
def live_settings(tmp_path, lms):
    reset(lms)
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        prompts_dir=os.environ.get("PROMPTS_DIR", "~/bitowi_instructions/prompts"),
        fake_lms_host=lms.split("://", 1)[1],
        fake_lms_user=LMS_USER,
        fake_lms_password=LMS_PASSWORD,
        max_rounds=2,
        worker_mode=os.environ.get("WORKER_MODE", "local"),
        tasks_dir=os.environ.get("TASKS_DIR", str(tmp_path / "tasks")),
    )


async def _extract(settings, url):
    storage = await BrowserSessions(settings).ensure_session(profile_for(url, settings), url)
    out = settings.data_dir / "boss" / "live"
    opts = BrowserOpts(storage_state=storage, output_dir=out / "screens", guard=True)
    return await CodexBoss(settings).extract(out, url, opts), out


async def test_live_extract_three_types(live_settings, lms):
    res, out = await _extract(live_settings, lms + "/quiz/1")
    assert res.status == "ok", res.error
    types = [q.answer_type for q in res.questions]
    assert types == ["single_choice", "multi_choice", "text"], types
    assert res.questions[0].options == ["381", "391", "401", "371"]
    assert res.questions[1].options == ["2", "4", "7", "9"]
    assert (out / "screens" / "extract.png").exists()
    s = stats(lms)
    assert s["submit"] == 0 and s["show_answer"] == 0, s
    text = render_task(res.questions)
    assert not [leak for leak in LEAKS if leak in text]


async def test_live_extract_answered_quiz_keeps_grades_private(live_settings, lms):
    res, _ = await _extract(live_settings, lms + "/quiz/2")
    assert res.status == "ok", res.error
    q1 = res.questions[0]
    assert q1.already_answered
    assert "7" in (q1.visible_correct_answer or "")
    assert not [leak for leak in LEAKS if leak in render_task(res.questions)]
    assert stats(lms)["submit"] == 0


async def test_live_full_pipeline(live_settings, lms):
    from tests.conftest import FakeNotifier

    db = Database(live_settings.db_path)
    await db.connect()
    # TASKS_DIR может быть общим с ботом (/srv/bitowi/tasks) — номера задач теста далеко от боевых
    base_id = 900000 + secrets.randbelow(90000)
    await db.conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('tasks', ?)", (base_id,))
    await db.conn.commit()
    orch = Orchestrator(
        live_settings, db, CodexBoss(live_settings), ClaudeWorker(live_settings), FakeNotifier(),
        BrowserSessions(live_settings),
    )
    task = await orch.submit_url(lms + "/quiz/1")
    task = await settle(orch, task.id, (State.AWAIT_CONFIRM, State.FAILED, State.PAUSED), tries=2000)
    await db.close()
    shutil.rmtree(live_settings.tasks_dir / str(task.id), ignore_errors=True)
    assert task.state == State.AWAIT_CONFIRM, task.error
    answers = dict(line.split(":", 1) for line in task.final_answer.splitlines())
    assert answers["1"].strip() == "2"
    assert answers["2"].replace(" ", "") == "1,3"
    assert answers["3"].strip().lower() == "астана"
    assert stats(lms)["submit"] == 0
