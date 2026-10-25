"""Course mode end to end on the mini LMS: orchestrator + CourseRunner + real Chromium + DirectExecutor
+ a rule-based worker. No subscriptions. Submissions only after the owner's «Отправить», no traps."""

import json

import pytest

pytest.importorskip("playwright")
pytest.importorskip("uvicorn")

from app.agents.fakes import FakeBoss, FakeWorker  # noqa: E402
from app.browser.session import BrowserSessions  # noqa: E402
from app.course.courier import DirectExecutor  # noqa: E402
from app.course.models import Decision  # noqa: E402
from app.course.runner import CourseRunner  # noqa: E402
from app.orchestrator import Orchestrator, State  # noqa: E402
from tests.course_bot import CourseBot  # noqa: E402
from tests.test_course_layers import agent_words  # noqa: E402
from tests.lms_server import LMS_PASSWORD, LMS_USER, lms, reset, stats  # noqa: E402,F401

pytestmark = pytest.mark.e2e

NO_TRAPS = {"logout": 0, "profile": 0, "forum": 0, "delete": 0}


@pytest.fixture
def lms_settings(settings, lms):
    reset(lms)
    settings.fake_lms_host = lms.split("://", 1)[1]
    settings.fake_lms_user = LMS_USER
    settings.fake_lms_password = LMS_PASSWORD
    settings.browser_timeout = 15
    return settings


def make(settings, db, notifier, worker) -> Orchestrator:
    course = CourseRunner(settings, db, worker, notifier, BrowserSessions(settings), DirectExecutor())
    return Orchestrator(settings, db, FakeBoss(), worker, notifier, None, course)


async def state(orch, task_id):
    return (await orch.db.get_task(task_id)).state


async def test_course_passes_with_three_confirmations(lms_settings, db, notifier, lms):
    bot = CourseBot()
    orch = make(lms_settings, db, notifier, FakeWorker(decisions=[bot] * 60))
    try:
        task = await orch.submit_course(lms + "/course")
        await orch.wait_idle()

        buttons = []
        for expected_checks in (0, 1, 2):
            assert await state(orch, task.id) == State.AWAIT_CONFIRM
            # до «Отправить» платформа не получила ни одной сдачи
            s = stats(lms)
            assert s["quiz_checks"] == expected_checks and s["exam_submits"] == 0
            _, shots, answers, button = notifier.course_previews[-1]
            assert shots and all(p.exists() for p in shots) and answers
            buttons.append(button)
            assert await orch.confirm(task.id)
            await orch.wait_idle()

        assert buttons == ["Проверить", "Проверить", "Завершить попытку..."]
        assert await state(orch, task.id) == State.DONE
        s = stats(lms)
        assert s["quiz_checks"] == 2 and s["exam_submits"] == 1
        assert s["traps"] == NO_TRAPS
        assert "Курс #" in notifier.messages[-1] and "сдач: 3" in notifier.messages[-1]
        # новый результат на экране — скриншот владельцу: два теста и экзамен
        assert len(notifier.photos) == 3 and all(p.exists() for p in notifier.photos)

        # страница с текстом «нажми Выйти»: воркер поддался, код не пустил
        assert bot.tried_trap
        assert any("недоступен" in text for text in bot.inputs)
        # воркер ни разу не видел адреса, домена и названия платформы
        host = lms.split("://", 1)[1]
        for text in bot.inputs:
            assert host not in text and "127.0.0.1" not in text and "Мини-LMS" not in text
            # и не знает, что кликает кто-то другой
            assert agent_words(text) == []
        journal = (lms_settings.boss_root / str(task.id) / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        assert json.loads(journal[-1])["status"] == "course_done"
        assert orch.course.browser is None  # браузер закрыт после конца курса
    finally:
        await orch.course.close()


async def test_fix_sends_remark_and_new_preview(lms_settings, db, notifier, lms):
    bot = CourseBot()
    orch = make(lms_settings, db, notifier, FakeWorker(decisions=[bot] * 20))
    try:
        task = await orch.submit_course(lms + "/course")
        await orch.wait_idle()
        assert len(notifier.course_previews) == 1
        assert await orch.request_fix(task.id, "во втором вопросе проверь написание")
        await orch.wait_idle()
        assert "## Замечания владельца\n- во втором вопросе проверь написание" in bot.inputs[-1]
        assert len(notifier.course_previews) == 2 and await state(orch, task.id) == State.AWAIT_CONFIRM
        assert stats(lms)["quiz_checks"] == 0
    finally:
        await orch.course.close()


async def test_stop_pauses_and_resume_continues(lms_settings, db, notifier, lms):
    bot = CourseBot()
    orch = make(lms_settings, db, notifier, FakeWorker(decisions=[bot] * 40))
    try:
        task = await orch.submit_course(lms + "/course")
        await orch.wait_idle()
        assert await state(orch, task.id) == State.AWAIT_CONFIRM

        # «Отмена» на превью / /stop: пауза, браузер закрыт, сдача отброшена
        assert (await orch.stop()).state == State.PAUSED
        assert orch.course.browser is None
        assert (await db.get_course(task.id))["pending"] is None
        assert stats(lms)["quiz_checks"] == 0

        # /resume — с того же места (страница теста), ответы заполняются заново, новое превью
        await orch.resume()
        await orch.wait_idle()
        assert await state(orch, task.id) == State.AWAIT_CONFIRM
        assert "Тест 1" in (await db.get_course(task.id))["title"]
        assert len(notifier.course_previews) == 2

        # второй /stop на паузе отменяет курс
        await orch.stop()
        assert (await orch.stop()).state == State.CANCELLED
    finally:
        await orch.course.close()


async def test_same_screen_pauses(lms_settings, db, notifier, lms):
    lms_settings.course_stuck_repeats = 3
    idle = Decision(status="continue", comment="жду")
    orch = make(lms_settings, db, notifier, FakeWorker(decisions=[idle] * 10))
    try:
        task = await orch.submit_course(lms + "/course")
        await orch.wait_idle()
        assert await state(orch, task.id) == State.PAUSED
        assert "тот же экран 3 раза подряд" in notifier.messages[-1]
    finally:
        await orch.course.close()


async def test_step_limit_and_stuck_status_pause(lms_settings, db, notifier, lms):
    lms_settings.course_max_steps = 2
    orch = make(lms_settings, db, notifier, FakeWorker(decisions=[CourseBot()] * 10))
    try:
        task = await orch.submit_course(lms + "/course")
        await orch.wait_idle()
        assert await state(orch, task.id) == State.PAUSED
        assert "COURSE_MAX_STEPS" in notifier.messages[-1]
        await orch.cancel(task.id)  # пауза занимает слот — освобождаем для следующей задачи
    finally:
        await orch.course.close()

    stuck = Decision(status="stuck", comment="нет кнопки «Далее»")
    orch = make(lms_settings, db, notifier, FakeWorker(decisions=[stuck]))
    try:
        task = await orch.submit_course(lms + "/course")
        await orch.wait_idle()
        assert await state(orch, task.id) == State.PAUSED
        assert "нет кнопки «Далее»" in notifier.messages[-1]
    finally:
        await orch.course.close()


async def test_failed_actions_in_a_row_pause(lms_settings, db, notifier, lms):
    lms_settings.course_fail_repeats = 3
    # воркер жмёт номер, которого нет: экран не меняется, но застревание видно по ошибкам
    miss = Decision(status="continue", actions=[{"action": "click", "element": 999}])
    lms_settings.course_stuck_repeats = 10
    orch = make(lms_settings, db, notifier, FakeWorker(decisions=[miss] * 10))
    try:
        task = await orch.submit_course(lms + "/course")
        await orch.wait_idle()
        assert await state(orch, task.id) == State.PAUSED
        assert "3 шага подряд не выполнено ни одно действие" in notifier.messages[-1]
        assert "элемент [999] не найден" in notifier.messages[-1]
        assert (await db.get_course(task.id))["step"] == 3
    finally:
        await orch.course.close()


async def test_rules_reread_every_step_and_course_instructions(lms_settings, db, notifier, lms):
    from app.course.worker_input import DEFAULT_RULES, RULES_FILE

    rules = lms_settings.prompts_dir / RULES_FILE
    rules.unlink(missing_ok=True)

    def first(text: str) -> Decision:
        # владелец правит файл, пока курс идёт — без перезапуска
        rules.write_text("- ПРАВИЛО-Б: только тесты.\n", encoding="utf-8")
        return Decision(status="continue", comment="читаю")

    worker = FakeWorker(decisions=[first, Decision(status="course_done")])
    orch = make(lms_settings, db, notifier, worker)
    try:
        task = await orch.submit_course(lms + "/course", "пропускай форумы")
        await orch.wait_idle()
        assert await state(orch, task.id) == State.DONE
        st = await db.get_course(task.id)
        assert st["url"] == lms + "/course" and st["instructions"] == "пропускай форумы"
        assert DEFAULT_RULES in worker.screens[0]  # файла нет — правила по умолчанию
        assert "ПРАВИЛО-Б" in worker.screens[1] and DEFAULT_RULES not in worker.screens[1]
        assert all("пропускай форумы" in s for s in worker.screens)
    finally:
        rules.unlink(missing_ok=True)
        await orch.course.close()
