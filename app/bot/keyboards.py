"""Inline keyboards."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class TaskAction(CallbackData, prefix="t"):
    action: str  # submit | fix | cancel | consent_yes | consent_no | consent_pause
    task_id: int


def preview_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Отправить", callback_data=TaskAction(action="submit", task_id=task_id))
    kb.button(text="Исправить", callback_data=TaskAction(action="fix", task_id=task_id))
    kb.button(text="Отмена", callback_data=TaskAction(action="cancel", task_id=task_id))
    kb.adjust(3)
    return kb.as_markup()


def consent_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Нажать", callback_data=TaskAction(action="consent_yes", task_id=task_id))
    kb.button(text="Не нажимать", callback_data=TaskAction(action="consent_no", task_id=task_id))
    kb.button(text="Пауза", callback_data=TaskAction(action="consent_pause", task_id=task_id))
    kb.adjust(3)
    return kb.as_markup()
