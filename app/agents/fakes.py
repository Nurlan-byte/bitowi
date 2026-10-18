"""Scripted fake agents for tests: no CLI calls, no subscriptions spent."""

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.agents.base import Boss, ReviewResult, Worker

Step = str | BaseException | ReviewResult


def _next(steps: list, default):
    return steps.pop(0) if steps else default


class FakeWorker(Worker):
    """Returns scripted replies; an exception in the script is raised instead."""

    def __init__(self, replies: Iterable[str | BaseException] = (), delay: float = 0.0) -> None:
        self.replies = list(replies)
        self.delay = delay
        self.calls: list[str] = []

    async def solve(self, notebook_text: str, workspace: Path) -> str:
        self.calls.append(notebook_text)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = _next(self.replies, "### Решение\n—\n\n### ОТВЕТ\n42")
        if isinstance(reply, BaseException):
            raise reply
        return reply


class FakeBoss(Boss):
    """Returns scripted verdicts: "accept", "revise", a ReviewResult or an exception."""

    def __init__(self, verdicts: Iterable[Step] = ()) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[dict[str, Any]] = []

    async def review(
        self, task_dir: Path, notebook_text: str, private: dict[str, Any], round_no: int, max_rounds: int
    ) -> ReviewResult:
        self.calls.append({"notebook": notebook_text, "round": round_no, "max_rounds": max_rounds})
        step = _next(self.verdicts, "accept")
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, ReviewResult):
            return step
        if step == "accept":
            return ReviewResult(verdict="accept", final_answer=_last_answer(notebook_text), confidence=0.9)
        return ReviewResult(verdict="revise", comment_for_worker=f"Перепроверь расчёт (раунд {round_no}).")


def _last_answer(notebook_text: str) -> str:
    from app.notebook import extract_answer

    return extract_answer(notebook_text.rsplit("**Исполнитель:**", 1)[-1])
