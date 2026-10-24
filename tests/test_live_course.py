"""Live course run on the mini LMS: real Claude worker and Codex courier through the sudo wrappers.

Spends subscriptions — only by hand on the server, from a session that has the bitowi-courier group:
    pytest -m live -s tests/test_live_course.py
The test plays the owner: before every «Отправить» it checks the platform got no submission.
Artifacts (journal, previews) are copied to ~/bitowi_audit/live-course-<time>/.
"""

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from app.agents.fakes import FakeBoss
from app.agents.worker_claude import ClaudeWorker
from app.browser.session import BrowserSessions
from app.config import Settings
from app.course.courier import CodexExecutor
from app.course.runner import CourseRunner
from app.db import Database
from app.orchestrator import Orchestrator, State
from tests.conftest import FakeNotifier
from tests.lms_server import LMS_PASSWORD, LMS_USER, lms, reset, stats  # noqa: F401

pytestmark = pytest.mark.live

ENV = Path.home() / "bitowi" / ".env"
# номера задач теста — далеко от задач бота: папки в /srv/bitowi/tasks и /srv/bitowi-courier не пересекутся
FIRST_ID = 900_000


@pytest.fixture
async def live(tmp_path, lms):
    if not ENV.exists():
        pytest.skip("нет ~/bitowi/.env")
    os.umask(0o007)
    reset(lms)
    settings = Settings(
        _env_file=str(ENV),
        data_dir=tmp_path / "data",
        fake_lms_host=lms.split("://", 1)[1],
        fake_lms_user=LMS_USER,
        fake_lms_password=LMS_PASSWORD,
        course_max_steps=60,
    )
    assert settings.worker_mode == "sudo" and settings.courier_mode == "sudo"
    db = Database(settings.db_path)
    await db.connect()
    await db.conn.execute("INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES ('tasks', ?)", (FIRST_ID,))
    await db.conn.commit()
    notifier = FakeNotifier()
    worker = ClaudeWorker(settings)
    course = CourseRunner(settings, db, worker, notifier, BrowserSessions(settings), CodexExecutor(settings))
    orch = Orchestrator(settings, db, FakeBoss(), worker, notifier, None, course)
    yield orch, settings, notifier, lms
    await course.close()
    await db.close()


async def test_live_mini_lms_course(live):
    orch, settings, notifier, lms = live
    started = time.monotonic()
    task = await orch.submit_course(lms + "/course")
    assert task.id > FIRST_ID
    out = Path.home() / "bitowi_audit" / f"live-course-{datetime.now():%Y%m%d-%H%M}"
    out.mkdir(parents=True)
    previews = []
    try:
        checks = exams = 0
        for _ in range(12):
            await orch.wait_idle()
            t = await orch.db.get_task(task.id)
            if t.state != State.AWAIT_CONFIRM:
                break
            s = stats(lms)
            # ни одной сдачи без «Отправить»
            assert s["quiz_checks"] == checks and s["exam_submits"] == exams, s
            _, shots, answers, button = notifier.course_previews[-1]
            k = len(previews) + 1
            for i, shot in enumerate(shots, 1):
                shutil.copyfile(shot, out / f"preview{k}-{i}.png")
            previews.append({"button": button, "answers": answers})
            assert await orch.confirm(task.id)
            await orch.wait_idle()
            s = stats(lms)
            checks, exams = s["quiz_checks"], s["exam_submits"]

        t = await orch.db.get_task(task.id)
        st = await orch.db.get_course(task.id)
        journal = settings.boss_root / str(task.id) / "journal.jsonl"
        if journal.exists():
            shutil.copyfile(journal, out / "journal.jsonl")
        summary = {
            "state": t.state, "steps": st["step"], "submits": st["submits"],
            "minutes": round((time.monotonic() - started) / 60, 1), "previews": previews,
            "stats": stats(lms), "statuses": notifier.statuses[-3:], "messages": notifier.messages[-2:],
        }
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))

        assert t.state == State.DONE, summary
        s = stats(lms)
        assert s["traps"] == {"logout": 0, "profile": 0, "forum": 0, "delete": 0}
        assert s["exam_submits"] == 1 and s["quiz_checks"] >= 2
    finally:
        for root in (settings.tasks_dir, settings.courier_root):
            shutil.rmtree(root / str(task.id), ignore_errors=True)
