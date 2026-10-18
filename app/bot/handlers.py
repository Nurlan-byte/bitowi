"""Telegram commands, buttons and replies."""

import json
import logging
import os
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ForceReply, Message

from app.bot.keyboards import TaskAction
from app.bot.notifier import clip
from app.browser.sites import auth_file
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
    "ссылка или /task <url> — задача со страницы\n"
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

    async def start_url(message: Message, url: str) -> None:
        task = await orch.submit_url(url)
        if task.state == State.QUEUED:
            await message.answer(f"Задача #{task.id} в очереди — сейчас выполняется другая.")

    @owner.message(Command("task"))
    async def cmd_task(message: Message, command: CommandObject) -> None:
        m = URL_RE.search(command.args or "")
        if not m:
            await message.answer("Пришли ссылку: /task <url>")
            return
        await start_url(message, m.group(0))

    @owner.message(F.document)
    async def on_document(message: Message, bot: Bot) -> None:
        """state.json from the owner: login session for a host."""
        doc = message.document
        if not (doc.file_name or "").endswith(".json") or (doc.file_size or 0) > 2_000_000:
            await message.answer("Жду state.json (до 2 МБ).")
            return
        host = (message.caption or "").strip().lower()
        if not host:
            paused = await orch.db.tasks_in_states((State.PAUSED,))
            private = orch.load_private(paused[0].id) if paused else {}
            host = private.get("host", "")
        if not host:
            await message.answer("Укажи домен в подписи к файлу, например: learn.astanait.edu.kz")
            return
        data = await bot.download(doc)
        try:
            state = json.loads(data.read().decode("utf-8"))
            assert isinstance(state.get("cookies"), list)
        except Exception:  # noqa: BLE001
            await message.answer("Это не state.json Playwright (нет cookies).")
            return
        path = auth_file(orch.settings, host)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        await message.answer(f"Сохранил вход для {host} ({len(state['cookies'])} cookies). Продолжить — /resume")

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
        m = URL_RE.search(message.text)
        if m:
            await start_url(message, m.group(0))
        else:
            await message.answer(HELP)

    return router
