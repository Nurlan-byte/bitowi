"""Scripted fake agents for tests: no CLI calls, no subscriptions spent."""

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.agents.base import Boss, BrowserOpts, ExtractResult, QuestionVerdict, ReviewResult, Worker

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
    """Scripted boss.

    verdicts: "accept", "revise", "revise:2,3" (only these questions wrong), a ReviewResult or an exception.
    """

    def __init__(
        self,
        verdicts: Iterable[Step] = (),
        extracts: Iterable[ExtractResult | BaseException] = (),
    ) -> None:
        self.verdicts = list(verdicts)
        self.extracts = list(extracts)
        self.calls: list[dict[str, Any]] = []
        self.browser_calls: list[tuple[str, BrowserOpts]] = []

    async def review(
        self, task_dir: Path, notebook_text: str, private: dict[str, Any], round_no: int, max_rounds: int
    ) -> ReviewResult:
        self.calls.append({"notebook": notebook_text, "round": round_no, "max_rounds": max_rounds, "private": private})
        step = _next(self.verdicts, "accept")
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, ReviewResult):
            return step
        under_review = private.get("under_review", [1])
        if step == "accept":
            return ReviewResult(
                verdict="accept",
                questions=[QuestionVerdict(n=n, ok=True) for n in under_review],
                confidence=0.9,
            )
        if step.startswith("revise:"):
            wrong = {int(x) for x in step.split(":", 1)[1].split(",")}
            return ReviewResult(
                verdict="revise",
                questions=[
                    QuestionVerdict(n=n, ok=n not in wrong, comment="" if n not in wrong else f"Вопрос {n}: проверь ещё раз.")
                    for n in under_review
                ],
            )
        return ReviewResult(verdict="revise", comment_for_worker=f"Перепроверь расчёт (раунд {round_no}).")

    async def extract(self, task_dir: Path, url: str, browser: BrowserOpts) -> ExtractResult:
        self.browser_calls.append(("extract", browser))
        step = _next(self.extracts, ExtractResult(status="no_task"))
        if isinstance(step, BaseException):
            raise step
        return step
