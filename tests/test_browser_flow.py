"""URL tasks on fake agents: domain rules, extraction, isolation, parts, per-question review."""

import asyncio

from app.agents.base import ExtractResult, PolicyResult, Question
from app.agents.fakes import FakeBoss, FakeWorker
from app.browser.session import SessionExpired
from app.orchestrator import Orchestrator, State

from tests.conftest import FakeSessions

URL = "https://lms.example.kz/course/quiz/7"
HOST = "lms.example.kz"


def questions(n: int = 3) -> list[Question]:
    base = [
        Question(n=1, text="Сколько будет 17·23?", answer_type="single_choice", options=["381", "391", "401"],
                 previous_answer="381", visible_correct_answer="391", already_answered=True,
                 locator_notes="radio name=q1"),
        Question(n=2, text="Какие числа простые?", answer_type="multi_choice", options=["2", "4", "7"]),
        Question(n=3, text="Столица Казахстана?", answer_type="text", answer_format="одно слово"),
    ]
    extra = [Question(n=i, text=f"Сколько будет {i}·2?", answer_type="number") for i in range(4, n + 1)]
    return (base + extra)[:n]


def extracted(n: int = 3) -> ExtractResult:
    return ExtractResult(
        status="ok", platform="SuperLMS", page_title="Тест 7", attempt_url=URL + "?attempt=5",
        attempts_left=2, questions=questions(n), private_notes="кнопка «Отправить» внизу",
    )


ANSWERS_3 = "### Решение\n...\n\n### ОТВЕТ\n1: 2\n2: 1, 3\n3: Астана"


def make(settings, db, notifier, boss, worker, sessions=None):
    return Orchestrator(settings, db, boss, worker, notifier, sessions or FakeSessions())


async def settle(orch, task_id, until, tries=50):
    for _ in range(tries):
        await orch.wait_idle()
        task = await orch.db.get_task(task_id)
        if task.state in until:
            return task
        await asyncio.sleep(0.01)
    return await orch.db.get_task(task_id)


async def test_new_domain_asks_owner_then_extracts(settings, db, notifier):
    boss = FakeBoss(["accept"], extracts=[extracted()])
    orch = make(settings, db, notifier, boss, FakeWorker([ANSWERS_3]))
    task = await orch.submit_url(URL)
    task = await settle(orch, task.id, (State.AWAIT_DOMAIN,))
    assert task.state == State.AWAIT_DOMAIN
    assert notifier.domains == [(task.id, HOST, "allowed")]
    assert (await db.get_domain(HOST)).status == "pending"

    assert await orch.decide_domain(task.id, True)
    task = await settle(orch, task.id, (State.AWAIT_CONFIRM, State.FAILED))
    assert task.state == State.AWAIT_CONFIRM, task.error
    assert (await db.get_domain(HOST)).status == "approved"
    assert task.final_answer == "1: 2\n2: 1, 3\n3: Астана"
    assert notifier.photos and notifier.photos[0].name == "extract.png"
    # блокировка кнопок сдачи — на всех браузерных шагах этапа 2
    assert [k for k, _ in boss.browser_calls] == ["policy", "extract"]
    assert all(b.guard for _, b in boss.browser_calls)


async def test_rejected_domain(settings, db, notifier):
    orch = make(settings, db, notifier, FakeBoss(), FakeWorker())
    task = await orch.submit_url(URL)
    task = await settle(orch, task.id, (State.AWAIT_DOMAIN,))
    await orch.decide_domain(task.id, False)
    assert (await db.get_task(task.id)).state == State.CANCELLED
    assert (await db.get_domain(HOST)).status == "rejected"

    again = await orch.submit_url(URL + "/2")
    again = await settle(orch, again.id, (State.FAILED,))
    assert again.state == State.FAILED and "запрещён" in again.error


async def test_approved_domain_skips_policy(settings, db, notifier):
    await db.set_domain(HOST, "approved", "allowed", "ok", [])
    boss = FakeBoss(["accept"], extracts=[extracted()])
    orch = make(settings, db, notifier, boss, FakeWorker([ANSWERS_3]))
    task = await orch.submit_url(URL)
    task = await settle(orch, task.id, (State.AWAIT_CONFIRM, State.FAILED))
    assert task.state == State.AWAIT_CONFIRM
    assert [k for k, _ in boss.browser_calls] == ["extract"]


async def test_pending_domain_with_verdict_asks_again_without_check(settings, db, notifier):
    await db.set_domain(HOST, "pending", "unclear", "не нашёл правил", [])
    boss = FakeBoss()
    orch = make(settings, db, notifier, boss, FakeWorker())
    task = await orch.submit_url(URL)
    task = await settle(orch, task.id, (State.AWAIT_DOMAIN,))
    assert task.state == State.AWAIT_DOMAIN
    assert boss.browser_calls == []
    assert notifier.domains[-1][2] == "unclear"


async def test_worker_gets_only_condition(settings, db, notifier):
    await db.set_domain(HOST, "approved")
    worker = FakeWorker([ANSWERS_3])
    orch = make(settings, db, notifier, FakeBoss(["accept"], extracts=[extracted()]), worker, FakeSessions())
    task = await orch.submit_url(URL)
    await settle(orch, task.id, (State.AWAIT_CONFIRM,))
    seen = worker.calls[0]
    assert "Сколько будет 17·23?" in seen and "2) 391" in seen
    for leak in ("lms.example", "SuperLMS", "attempt=5", "radio name", "кнопка", "previous", "✓"):
        assert leak not in seen
    # увиденный правильный и прошлый ответы — только в private
    private = orch.load_private(task.id)
    assert private["questions"][0]["visible_correct_answer"] == "391"
    assert private["questions"][0]["previous_answer"] == "381"
    assert "391" not in seen.split("Варианты:")[0]
    # файлы куратора — не в TASKS_DIR
    assert not list(settings.tasks_dir.rglob("boss_private.json"))
    assert (settings.boss_root / str(task.id) / "boss_private.json").exists()


async def test_session_expired_pauses_and_resumes(settings, db, notifier):
    await db.set_domain(HOST, "approved")
    sessions = FakeSessions([SessionExpired(HOST, "сессия протухла")])
    boss = FakeBoss(["accept"], extracts=[extracted()])
    orch = make(settings, db, notifier, boss, FakeWorker([ANSWERS_3]), sessions)
    task = await orch.submit_url(URL)
    task = await settle(orch, task.id, (State.PAUSED,))
    assert task.state == State.PAUSED and task.resume_state == State.EXTRACTING
    assert "state.json" in notifier.statuses[-1] and HOST in notifier.statuses[-1]

    await orch.resume()
    task = await settle(orch, task.id, (State.AWAIT_CONFIRM, State.FAILED))
    assert task.state == State.AWAIT_CONFIRM


async def test_boss_sees_login_page_pauses(settings, db, notifier):
    await db.set_domain(HOST, "approved")
    boss = FakeBoss(extracts=[ExtractResult(status="login_required")])
    orch = make(settings, db, notifier, boss, FakeWorker())
    task = await orch.submit_url(URL)
    task = await settle(orch, task.id, (State.PAUSED, State.FAILED))
    assert task.state == State.PAUSED
    assert "/resume" in notifier.statuses[-1]


async def test_no_task_on_page_fails(settings, db, notifier):
    await db.set_domain(HOST, "approved")
    boss = FakeBoss(extracts=[ExtractResult(status="no_task", error="на странице нет вопросов")])
    orch = make(settings, db, notifier, boss, FakeWorker())
    task = await settle(orch, (await orch.submit_url(URL)).id, (State.FAILED,))
    assert "нет вопросов" in task.error


async def test_many_questions_split_into_parts(settings, db, notifier):
    settings.max_questions = 15
    await db.set_domain(HOST, "approved")
    first = "### ОТВЕТ\n" + "\n".join(f"{i}: x{i}" for i in range(1, 16))
    second = "### ОТВЕТ\n16: 32\n17: 34"
    worker = FakeWorker([first, second])
    orch = make(settings, db, notifier, FakeBoss(["accept", "accept"], extracts=[extracted(17)]), worker)
    task = await orch.submit_url(URL)
    task = await settle(orch, task.id, (State.AWAIT_CONFIRM, State.FAILED))
    assert task.state == State.AWAIT_CONFIRM, task.error
    assert task.part == "1/2"
    assert "Вопрос 15." in worker.calls[0] and "Вопрос 16." not in worker.calls[0]
    child = (await orch.queued())[0]
    assert child.kind == "part" and child.part == "2/2" and child.parent_id == task.id

    await orch.confirm(task.id)
    child = await settle(orch, child.id, (State.AWAIT_CONFIRM, State.FAILED))
    assert child.state == State.AWAIT_CONFIRM, child.error
    assert "Вопрос 16." in worker.calls[1] and "Вопрос 1." not in worker.calls[1]
    assert child.final_answer == "16: 32\n17: 34"


async def test_revise_only_wrong_questions(settings, db, notifier):
    await db.set_domain(HOST, "approved")
    worker = FakeWorker([ANSWERS_3, "### ОТВЕТ\n2: 1, 3"])
    boss = FakeBoss(["revise:2", "accept"], extracts=[extracted()])
    orch = make(settings, db, notifier, boss, worker)
    task = await orch.submit_url(URL)
    task = await settle(orch, task.id, (State.AWAIT_CONFIRM, State.FAILED))
    assert task.state == State.AWAIT_CONFIRM, task.error
    remark = worker.calls[1].rsplit("**Заказчик:**", 1)[-1]
    assert "Вопрос 2" in remark and "Вопрос 1:" not in remark and "Вопрос 3:" not in remark
    assert "только на вопросы 2" in remark
    assert boss.calls[1]["private"]["under_review"] == [2]
    assert task.final_answer == "1: 2\n2: 1, 3\n3: Астана"


async def test_owner_fix_reopens_all_questions(settings, db, notifier):
    await db.set_domain(HOST, "approved")
    worker = FakeWorker([ANSWERS_3, ANSWERS_3])
    boss = FakeBoss(["accept", "accept"], extracts=[extracted()])
    orch = make(settings, db, notifier, boss, worker)
    task = await settle(orch, (await orch.submit_url(URL)).id, (State.AWAIT_CONFIRM,))
    await orch.request_fix(task.id, "Проверь второй вопрос")
    task = await settle(orch, task.id, (State.AWAIT_CONFIRM,))
    assert boss.calls[1]["private"]["under_review"] == [1, 2, 3]


async def test_text_tasks_unaffected_by_browser(settings, db, notifier, sessions):
    orch = make(settings, db, notifier, FakeBoss(["accept"]), FakeWorker(["### ОТВЕТ\n391"]), sessions)
    task = await orch.submit_text("17*23?")
    task = await settle(orch, task.id, (State.AWAIT_CONFIRM,))
    assert task.final_answer == "391" and sessions.calls == []


async def test_policy_result_stored(settings, db, notifier):
    boss = FakeBoss(policies=[PolicyResult(verdict="forbidden", summary="ИИ запрещён", sources=["https://x/rules"])])
    orch = make(settings, db, notifier, boss, FakeWorker())
    task = await settle(orch, (await orch.submit_url(URL)).id, (State.AWAIT_DOMAIN,))
    domain = await db.get_domain(HOST)
    assert domain.verdict == "forbidden" and domain.source_list == ["https://x/rules"]
    assert notifier.domains[-1] == (task.id, HOST, "forbidden")
