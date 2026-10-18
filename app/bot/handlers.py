"""Telegram commands, buttons and replies."""

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ForceReply, Message

from app.bot.keyboards import TaskAction
from app.bot.notifier import clip
from app.orchestrator import Orchestrator, State

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
FIX_MARK = re.compile(r"Исправить задачу #(\d+)")

STATE_RU = {
    State.QUEUED: "в очереди",
    State.EXTRACTING: "извлечение задания",
    State.SOLVING: "исполнитель решает",
    State.REVIEWING: "куратор проверяет",
    State.AWAIT_CONFIRM: "ждёт подтверждения",
    State.SUBMITTING: "сдача",
    State.PAUSED: "на паузе",
    State.DONE: "готово",
    State.FAILED: "ошибка",
    State.CANCELLED: "отменена",
}

HELP = (
    "Команды:\n"
    "/text <задание> — новая задача текстом\n"
    "/status — текущая задача\n"
    "/stop — отменить текущую задачу\n"
    "/resume — продолжить после паузы\n"
    "/history — последние 10 задач\n"
    "/id — твой Telegram ID\n\n"
    "Ответ (reply) на моё сообщение — реплика в блокнот текущей задачи."
)


def build_router(orch: Orchestrator, owner_id: int) -> Router:
    router = Router(name="bitowi")

    @router.message(Command("id"))
    async def cmd_id(message: Message) -> None:
        await message.answer(f"Твой Telegram ID: {message.from_user.id}")

    if not owner_id:
        log.warning("OWNER_ID не задан: бот отвечает только на /id")
        return router

    # дальше — только владелец
    owner = Router(name="owner")
    owner.message.filter(F.from_user.id == owner_id)
    owner.callback_query.filter(F.from_user.id == owner_id)
    router.include_router(owner)

    @owner.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        await message.answer("Привет! Пришли задание командой /text.\n\n" + HELP)

    @owner.message(Command("text"))
    async def cmd_text(message: Message, command: CommandObject) -> None:
        text = (command.args or "").strip()
        if not text:
            await message.answer("Напиши задание после команды: /text <задание>")
            return
        task = await orch.submit_text(text)
        if task.state == State.QUEUED:
            await message.answer(f"Задача #{task.id} в очереди — сейчас выполняется другая.")

    @owner.message(Command("task"))
    async def cmd_task(message: Message) -> None:
        await message.answer("Задачи по ссылке пока не поддерживаются. Пришли текст через /text.")

    @owner.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        task = await orch.active_task()
        queued = await orch.queued()
        if not task:
            await message.answer("Сейчас задач нет." + (f" В очереди: {len(queued)}." if queued else ""))
            return
        text = (
            f"Задача #{task.id}: {STATE_RU.get(task.state, task.state)}\n"
            f"Раунд {task.round}/{orch.settings.max_rounds}"
        )
        if queued:
            text += f"\nВ очереди: {len(queued)}"
        await message.answer(text)

    @owner.message(Command("stop"))
    async def cmd_stop(message: Message) -> None:
        task = await orch.cancel()
        if not task:
            await message.answer("Нечего отменять.")

    @owner.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        task = await orch.resume()
        await message.answer(f"Продолжаю задачу #{task.id}." if task else "Задач на паузе нет.")

    @owner.message(Command("history"))
    async def cmd_history(message: Message) -> None:
        tasks = await orch.db.last_tasks(10)
        if not tasks:
            await message.answer("История пуста.")
            return
        lines = []
        for t in tasks:
            head = t.input.replace("\n", " ")[:50]
            tail = t.final_answer or t.error or ""
            lines.append(f"#{t.id} · {STATE_RU.get(t.state, t.state)} · {head}" + (f" → {tail[:60]}" if tail else ""))
        await message.answer(clip("\n".join(lines)))

    @owner.callback_query(TaskAction.filter())
    async def on_action(query: CallbackQuery, callback_data: TaskAction) -> None:
        tid = callback_data.task_id
        if callback_data.action == "submit":
            ok = await orch.confirm(tid)
            await query.answer("Принято" if ok else "Задача уже не ждёт подтверждения")
        elif callback_data.action == "fix":
            task = await orch.db.get_task(tid)
            if not task or task.state != State.AWAIT_CONFIRM:
                await query.answer("Задача уже не ждёт подтверждения")
                return
            await query.answer()
            await query.message.answer(
                f"Исправить задачу #{tid}: напиши ответом на это сообщение, что поправить.",
                reply_markup=ForceReply(selective=True),
            )
        elif callback_data.action == "cancel":
            task = await orch.cancel(tid)
            await query.answer("Отменено" if task else "Задача уже завершена")
        if query.message and callback_data.action in ("submit", "cancel"):
            await query.message.edit_reply_markup(reply_markup=None)

    @owner.message(F.reply_to_message & F.text)
    async def on_reply(message: Message, bot: Bot) -> None:
        replied = message.reply_to_message
        if replied.from_user is None or replied.from_user.id != bot.id:
            return
        mark = FIX_MARK.search(replied.text or "")
        if mark:
            ok = await orch.request_fix(int(mark.group(1)), message.text)
            await message.answer("Передал исполнителю." if ok else "Задача уже не ждёт подтверждения.")
            return
        task = await orch.add_remark(message.text)
        await message.answer(f"Добавил в блокнот задачи #{task.id}." if task else "Сейчас задач нет.")

    @owner.message(F.text)
    async def on_text(message: Message) -> None:
        if URL_RE.search(message.text):
            await message.answer("Задачи по ссылке пока не поддерживаются. Пришли текст через /text.")
        else:
            await message.answer(HELP)

    return router
