"""Telegram implementation of the orchestrator Notifier."""

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto

from app.bot.keyboards import preview_kb
from app.db import Database, Task

log = logging.getLogger(__name__)

LIMIT = 4096


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

    async def course_preview(self, task: Task, shots: list[Path], answers: list[str], button: str) -> None:
        """Screenshots with the filled answers, then the answers and the buttons [Отправить] [Исправить] [Отмена]."""
        photos = [p for p in shots if p.exists()][:10]
        if len(photos) == 1:
            await self.photo(task, photos[0], f"Курс #{task.id} · перед сдачей")
        elif photos:
            caption = f"Курс #{task.id} · перед сдачей"
            media = [InputMediaPhoto(media=FSInputFile(p), caption=caption if i == 0 else None)
                     for i, p in enumerate(photos)]
            try:
                await self.bot.send_media_group(self.chat_id, media)
            except TelegramBadRequest as exc:
                log.warning("media group failed: %s", exc)
                for p in photos:
                    await self.photo(task, p, f"Курс #{task.id}")
        lines = [f"Курс #{task.id}: готово к сдаче.", f"Будет нажато: {button or '—'}"]
        if answers:
            lines += ["", "Ответы:", *(f"{i}. {a}" for i, a in enumerate(answers, 1))]
        lines += ["", "Отправить — сдать; Исправить — замечание исполнителю; Отмена — курс на паузу."]
        await self.bot.send_message(self.chat_id, clip("\n".join(lines)), reply_markup=preview_kb(task.id))

    async def message(self, task: Task, text: str) -> None:
        await self.bot.send_message(self.chat_id, clip(text))

    async def photo(self, task: Task, path: Path, caption: str) -> None:
        try:
            await self.bot.send_photo(self.chat_id, FSInputFile(path), caption=clip(caption, 1000))
        except TelegramBadRequest:
            # слишком длинная страница для фото — шлём файлом
            await self.bot.send_document(self.chat_id, FSInputFile(path), caption=clip(caption, 1000))

