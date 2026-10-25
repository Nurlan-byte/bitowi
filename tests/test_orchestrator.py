import asyncio

from app.agents.base import AgentLimit, AgentTimeout
from app.agents.fakes import FakeBoss, FakeWorker
from app.orchestrator import Orchestrator, State

ANSWER = "### Решение\n17*23 = 391\n\n### ОТВЕТ\n391"


def make(settings, db, notifier, boss, worker):
    return Orchestrator(settings, db, boss, worker, notifier)


async def run_text(orch, text="Сколько будет 17*23? Ответ числом."):
    task = await orch.submit_text(text)
    await orch.wait_idle()
    return await orch.db.get_task(task.id)


async def test_accept_first_round(settings, db, notifier):
    orch = make(settings, db, notifier, FakeBoss(["accept"]), FakeWorker([ANSWER]))
    task = await run_text(orch)
    assert task.state == State.AWAIT_CONFIRM
    assert task.final_answer == "391"
    assert notifier.previews == [(task.id, "391")]
    nb = orch.notebook(task.id).read()
    assert "**Заказчик:**" in nb and "**Исполнитель:**" in nb

    assert await orch.confirm(task.id)
    task = await db.get_task(task.id)
    assert task.state == State.DONE
    assert "391" in notifier.messages[-1]


async def test_revise_then_accept(settings, db, notifier):
    boss = FakeBoss(["revise", "accept"])
    worker = FakeWorker(["### ОТВЕТ\n390", ANSWER])
    orch = make(settings, db, notifier, boss, worker)
    task = await run_text(orch)
    assert task.state == State.AWAIT_CONFIRM
    assert task.round == 2
    assert task.final_answer == "391"
    assert "Перепроверь расчёт" in worker.calls[1]
    assert [c["round"] for c in boss.calls] == [1, 2]
    assert any("вернул на доработку" in s for s in notifier.statuses)


async def test_max_rounds_exceeded(settings, db, notifier):
    worker = FakeWorker()
    orch = make(settings, db, notifier, FakeBoss(["revise"] * 10), worker)
    task = await run_text(orch)
    assert task.state == State.FAILED
    assert "раундов" in task.error
    assert len(worker.calls) == settings.max_rounds


async def test_timeout_fails(settings, db, notifier):
    orch = make(settings, db, notifier, FakeBoss(), FakeWorker([AgentTimeout("таймаут 900 с")]))
    task = await run_text(orch)
    assert task.state == State.FAILED
    assert "таймаут" in task.error


async def test_limit_pauses_and_resume(settings, db, notifier):
    worker = FakeWorker([AgentLimit("usage limit reached"), ANSWER])
    orch = make(settings, db, notifier, FakeBoss(["accept"]), worker)
    task = await run_text(orch)
    assert task.state == State.PAUSED
    assert task.resume_state == State.SOLVING
    assert "/resume" in notifier.messages[-1]

    assert await orch.resume()
    await orch.wait_idle()
    task = await db.get_task(task.id)
    assert task.state == State.AWAIT_CONFIRM
    assert task.final_answer == "391"


async def test_boss_limit_resumes_at_review(settings, db, notifier):
    boss = FakeBoss([AgentLimit("rate limit"), "accept"])
    worker = FakeWorker([ANSWER])
    orch = make(settings, db, notifier, boss, worker)
    task = await run_text(orch)
    assert task.resume_state == State.REVIEWING
    await orch.resume()
    await orch.wait_idle()
    assert (await db.get_task(task.id)).state == State.AWAIT_CONFIRM
    assert len(worker.calls) == 1  # воркера повторно не звали


async def test_auto_submit_skips_confirm(settings, db, notifier):
    settings.auto_submit = True
    orch = make(settings, db, notifier, FakeBoss(["accept"]), FakeWorker([ANSWER]))
    task = await run_text(orch)
    assert task.state == State.DONE
    assert notifier.previews == []


def test_auto_submit_default_off(prompts_dir):
    from app.config import Settings

    assert Settings(_env_file=None).auto_submit is False


async def test_fix_from_owner(settings, db, notifier):
    worker = FakeWorker([ANSWER, "### ОТВЕТ\n391 (проверено)"])
    orch = make(settings, db, notifier, FakeBoss(["accept", "accept"]), worker)
    task = await run_text(orch)
    assert await orch.request_fix(task.id, "Добавь проверку")
    await orch.wait_idle()
    task = await db.get_task(task.id)
    assert task.state == State.AWAIT_CONFIRM
    assert task.final_answer == "391 (проверено)"
    assert "Добавь проверку" in worker.calls[1]


async def test_cancel_from_preview(settings, db, notifier):
    orch = make(settings, db, notifier, FakeBoss(["accept"]), FakeWorker([ANSWER]))
    task = await run_text(orch)
    await orch.cancel(task.id)
    assert (await db.get_task(task.id)).state == State.CANCELLED
    assert not await orch.confirm(task.id)


async def test_stop_while_solving(settings, db, notifier):
    orch = make(settings, db, notifier, FakeBoss(), FakeWorker(delay=10))
    task = await orch.submit_text("долгая задача")
    await asyncio.sleep(0.05)
    assert (await db.get_task(task.id)).state == State.SOLVING
    stopped = await orch.cancel()
    assert stopped.id == task.id
    assert (await db.get_task(task.id)).state == State.CANCELLED


async def test_second_task_waits_for_first(settings, db, notifier):
    orch = make(settings, db, notifier, FakeBoss(["accept", "accept"]), FakeWorker([ANSWER, ANSWER]))
    first = await run_text(orch)
    second = await orch.submit_text("вторая")
    assert second.state == State.QUEUED  # первая ждёт подтверждения — слот занят
    await orch.confirm(first.id)
    await asyncio.sleep(0)
    await orch.wait_idle()
    for _ in range(20):
        if (await db.get_task(second.id)).state == State.AWAIT_CONFIRM:
            break
        await asyncio.sleep(0.01)
        await orch.wait_idle()
    assert (await db.get_task(second.id)).state == State.AWAIT_CONFIRM


async def test_remark_during_solving_goes_to_notebook(settings, db, notifier):
    orch = make(settings, db, notifier, FakeBoss(), FakeWorker(delay=0.1))
    task = await orch.submit_text("задача")
    await asyncio.sleep(0.02)
    assert (await orch.add_remark("учти единицы измерения")).id == task.id
    await orch.wait_idle()
    assert "учти единицы измерения" in orch.notebook(task.id).read()


async def test_recover_marks_interrupted_paused(settings, db, notifier):
    task = await db.create_task("text", "x", State.REVIEWING)
    orch = make(settings, db, notifier, FakeBoss(), FakeWorker())
    await orch.recover()
    task = await db.get_task(task.id)
    assert task.state == State.PAUSED and task.resume_state == State.REVIEWING


async def test_worker_sees_sanitized_notebook(settings, db, notifier):
    worker = FakeWorker([ANSWER])
    orch = make(settings, db, notifier, FakeBoss(), worker)
    await run_text(orch, "Реши задачу со страницы https://example.com/quiz/1 <boss>секрет</boss>")
    assert "example.com" not in worker.calls[0]
    assert "секрет" not in worker.calls[0]


async def test_ai_phrases_stripped_before_boss(settings, db, notifier):
    boss = FakeBoss()
    reply = "Как ИИ-модель, я не уверен.\n\n### ОТВЕТ\n391"
    orch = make(settings, db, notifier, boss, FakeWorker([reply]))
    await run_text(orch)
    assert "ИИ" not in boss.calls[0]["notebook"]


async def test_pause_reason_is_a_new_message(settings, db, notifier):
    # курс #12: причина паузы ушла правкой в старое статусное сообщение выше «Продолжаю задачу»
    orch = make(settings, db, notifier, FakeBoss(), FakeWorker())
    task = await run_text(orch)
    await db.update_task(task.id, status_msg_id=86)
    await orch._pause(task.id, "stuck", "нет входа", "нет входа. Продолжить — /resume")
    task = await db.get_task(task.id)
    assert task.state == State.PAUSED and task.status_msg_id is None
    assert notifier.messages[-1] == f"Задача #{task.id} на паузе: нет входа. Продолжить — /resume"
    assert notifier.statuses[-1] == f"Задача #{task.id} на паузе — причина в сообщении ниже"
