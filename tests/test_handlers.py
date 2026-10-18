"""Telegram routing through a real Dispatcher; Bot API calls are recorded, not sent."""

from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.agents.fakes import FakeBoss, FakeWorker
from app.bot.handlers import HELP, build_router
from app.bot.keyboards import TaskAction
from app.orchestrator import Orchestrator, State

OWNER = 1
ANSWER = "### ОТВЕТ\n391"


class RecordingBot(Bot):
    def __init__(self) -> None:
        super().__init__("123456:TEST")
        self.sent: list = []

    async def __call__(self, method, request_timeout=None):
        self.sent.append(method)
        return True


@pytest.fixture
async def env(settings, db, notifier):
    orch = Orchestrator(settings, db, FakeBoss(["accept"] * 3), FakeWorker([ANSWER] * 3), notifier)
    dp = Dispatcher()
    dp.include_router(build_router(orch, OWNER))
    bot = RecordingBot()
    yield orch, dp, bot
    await bot.session.close()


def user(uid=OWNER) -> User:
    return User(id=uid, is_bot=False, first_name="u")


def message(text: str, uid=OWNER, mid=10) -> Message:
    return Message(message_id=mid, date=datetime.now(), chat=Chat(id=uid, type="private"), from_user=user(uid), text=text)


async def send(env, text: str, uid=OWNER) -> list[str]:
    orch, dp, bot = env
    bot.sent.clear()
    await dp.feed_update(bot, Update(update_id=1, message=message(text, uid)))
    await orch.wait_idle()
    return [m.text for m in bot.sent if isinstance(m, SendMessage)]


async def press(env, action: str, task_id: int) -> list:
    orch, dp, bot = env
    bot.sent.clear()
    query = CallbackQuery(
        id="q", from_user=user(), chat_instance="c", message=message("превью", mid=20),
        data=TaskAction(action=action, task_id=task_id).pack(),
    )
    await dp.feed_update(bot, Update(update_id=2, callback_query=query))
    await orch.wait_idle()
    return bot.sent


async def waiting_task(orch):
    task = await orch.submit_text("17*23?")
    await orch.wait_idle()
    task = await orch.db.get_task(task.id)
    assert task.state == State.AWAIT_CONFIRM
    return task


@pytest.mark.parametrize("cmd", ["/status", "/stop", "/resume", "/history"])
async def test_commands_are_not_help(env, cmd):
    replies = await send(env, cmd)
    assert replies and HELP not in replies


async def test_status_shows_waiting_task(env):
    task = await waiting_task(env[0])
    replies = await send(env, "/status")
    assert f"Задача #{task.id}: ждёт подтверждения" in replies[0]


async def test_stop_cancels_task(env):
    orch = env[0]
    task = await waiting_task(orch)
    await send(env, "/stop")
    assert (await orch.db.get_task(task.id)).state == State.CANCELLED


async def test_submit_button(env):
    orch = env[0]
    task = await waiting_task(orch)
    sent = await press(env, "submit", task.id)
    assert (await orch.db.get_task(task.id)).state == State.DONE
    assert any(isinstance(m, AnswerCallbackQuery) and m.text == "Принято" for m in sent)
    assert any(isinstance(m, EditMessageReplyMarkup) for m in sent)


async def test_cancel_button(env):
    orch = env[0]
    task = await waiting_task(orch)
    sent = await press(env, "cancel", task.id)
    assert (await orch.db.get_task(task.id)).state == State.CANCELLED
    assert any(isinstance(m, AnswerCallbackQuery) and m.text == "Отменено" for m in sent)


async def test_fix_button_asks_reply(env):
    task = await waiting_task(env[0])
    sent = await press(env, "fix", task.id)
    assert any(isinstance(m, SendMessage) and f"Исправить задачу #{task.id}" in m.text for m in sent)


async def test_stranger_gets_nothing(env):
    assert await send(env, "/status", uid=999) == []


async def test_plain_text_is_help(env):
    assert await send(env, "привет") == [HELP]


async def test_bare_link_starts_task(env):
    orch = env[0]
    await send(env, "https://lms.example.kz/quiz/7.")
    tasks = await orch.db.last_tasks(10)
    assert [t.input for t in tasks] == ["https://lms.example.kz/quiz/7"]


async def test_link_inside_text_does_not_start_task(env):
    orch = env[0]
    replies = await send(env, 'Вот платформа "https://learn.astanait.edu.kz", посмотри потом')
    assert await orch.db.last_tasks(10) == []
    assert "/task" in replies[0]


async def test_task_command_trims_quotes(env):
    orch = env[0]
    await send(env, '/task "https://learn.astanait.edu.kz"')
    assert [t.input for t in await orch.db.last_tasks(10)] == ["https://learn.astanait.edu.kz"]
