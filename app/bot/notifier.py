"""Telegram implementation of the orchestrator Notifier."""

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

from app.agents.base import PolicyResult
from app.bot.keyboards import domain_kb, preview_kb
from app.db import Database, Task

log = logging.getLogger(__name__)

LIMIT = 4096

POLICY_RU = {"allowed": "разрешено", "forbidden": "запрещено", "unclear": "неясно"}


def clip(text: str, limit: int = LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n…(обрезано)"


class TelegramNotifier:
    def __init__(self, bot: Bot, chat_id: int, db: Database) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.db = db

    async def status(self, task: Task, text: str) -> None:
        """Keep one status message per task and edit it instead of sending new ones."""
        fresh = await self.db.get_task(task.id)
        msg_id = fresh.status_msg_id if fresh else None
        if msg_id:
            try:
                await self.bot.edit_message_text(clip(text), chat_id=self.chat_id, message_id=msg_id)
                return
            except TelegramBadRequest as exc:
                if "not modified" in str(exc):
                    return
                log.warning("status edit failed: %s", exc)
        msg = await self.bot.send_message(self.chat_id, clip(text))
        await self.db.update_task(task.id, status_msg_id=msg.message_id)

    async def preview(self, task: Task, task_text: str, answer: str) -> None:
        body = f"Задача #{task.id}\n\nЗадание:\n{clip(task_text, 1500)}\n\nОтвет:\n{answer}"
        await self.bot.send_message(self.chat_id, clip(body), reply_markup=preview_kb(task.id))

    async def message(self, task: Task, text: str) -> None:
        await self.bot.send_message(self.chat_id, clip(text))

    async def photo(self, task: Task, path: Path, caption: str) -> None:
        try:
            await self.bot.send_photo(self.chat_id, FSInputFile(path), caption=clip(caption, 1000))
        except TelegramBadRequest:
            # слишком длинная страница для фото — шлём файлом
            await self.bot.send_document(self.chat_id, FSInputFile(path), caption=clip(caption, 1000))

    async def domain_request(self, task: Task, host: str, policy: PolicyResult) -> None:
        verdict = POLICY_RU.get(policy.verdict, policy.verdict)
        sources = "\n".join(f"• {s}" for s in policy.sources[:8]) or "• не найдены"
        text = (
            f"Задача #{task.id}: новый домен {host}\n\n"
            f"Правила об ИИ: {verdict}\n{policy.summary}\n\nИсточники:\n{sources}\n\n"
            "Разрешить работу на этом домене?"
        )
        await self.bot.send_message(self.chat_id, clip(text), reply_markup=domain_kb(task.id))
