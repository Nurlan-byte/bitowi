"""Entry point: python -m app.main"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher

from app.agents.boss_codex import CodexBoss
from app.agents.worker_claude import ClaudeWorker
from app.bot.handlers import build_router
from app.bot.notifier import TelegramNotifier
from app.browser.session import BrowserSessions
from app.config import get_settings
from app.course.courier import CodexExecutor
from app.course.runner import CourseRunner
from app.db import Database
from app.orchestrator import Orchestrator


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # файлы задач — для группы bitowi-tasks (бот и исполнитель), остальным ничего
    os.umask(0o007)
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN не задан в .env")
    settings.tasks_dir.mkdir(parents=True, exist_ok=True)
    settings.boss_root.mkdir(parents=True, exist_ok=True)

    db = Database(settings.db_path)
    await db.connect()
    bot = Bot(settings.bot_token)
    notifier = TelegramNotifier(bot, settings.owner_id, db)
    worker = ClaudeWorker(settings)
    sessions = BrowserSessions(settings)
    course = CourseRunner(settings, db, worker, notifier, sessions, CodexExecutor(settings))
    orch = Orchestrator(settings, db, CodexBoss(settings), worker, notifier, sessions, course)

    dp = Dispatcher()
    dp.include_router(build_router(orch, settings.owner_id))
    if settings.owner_id:
        await orch.recover()
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
