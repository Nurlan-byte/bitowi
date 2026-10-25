"""Agent interfaces and shared models. Boss and worker are swappable behind these ABCs."""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def fill(template: str, **values: Any) -> str:
    """Substitute {name} placeholders without touching JSON braces in the template."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def parse_json(text: str) -> dict[str, Any]:
    text = _FENCE_RE.sub("", text.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("ожидался JSON-объект")
    return data


class AgentError(Exception):
    """Generic agent failure (bad exit code, unparsable output)."""


class AgentTimeout(AgentError):
    """Agent call exceeded its timeout and was killed."""


class AgentLimit(AgentError):
    """Subscription usage / rate limit reached."""


class BossBadJSON(AgentError):
    """Boss returned invalid JSON twice in a row."""


class WorkerBadJSON(AgentError):
    """Worker's course decision did not match the schema twice. Raw replies are kept for the journal."""

    def __init__(self, message: str, replies: list[str], errors: list[str]) -> None:
        super().__init__(message)
        self.replies = replies
        self.errors = errors


class QuestionVerdict(BaseModel):
    n: int
    ok: bool
    comment: str = ""
    answer: str = ""


class ReviewResult(BaseModel):
    verdict: Literal["accept", "revise"]
    questions: list[QuestionVerdict] = []
    comment_for_worker: str = ""
    final_answer: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class Question(BaseModel):
    n: int
    text: str
    answer_type: Literal["single_choice", "multi_choice", "text", "number", "code", "file"] = "text"
    options: list[str] = []
    answer_format: str = ""
    # только для boss_private.json — воркеру не передаются
    already_answered: bool = False
    previous_answer: str | None = None
    visible_correct_answer: str | None = None
    locator_notes: str = ""


class ExtractResult(BaseModel):
    status: Literal["ok", "login_required", "no_task", "error"] = "ok"
    error: str = ""
    platform: str = ""
    page_title: str = ""
    attempt_url: str = ""
    attempts_left: int | None = None
    questions: list[Question] = []
    private_notes: str = ""


@dataclass(frozen=True)
class BrowserOpts:
    """How the boss's browser is launched for one call."""

    storage_state: Path | None
    output_dir: Path
    # блокировка кнопок сдачи: True только на шаге извлечения, при сдаче (этап 3) — False
    guard: bool


class Worker(ABC):
    @abstractmethod
    async def solve(self, notebook_text: str, workspace: Path) -> str:
        """Read the (sanitized) notebook, return the executor's reply."""

    async def decide(self, screen_text: str, workspace: Path):
        """Course mode: screen state (sanitized) → app.course.models.Decision."""
        raise NotImplementedError


class Boss(ABC):
    @abstractmethod
    async def review(
        self, task_dir: Path, notebook_text: str, private: dict[str, Any], round_no: int, max_rounds: int
    ) -> ReviewResult:
        """Check the executor's answers against the task, question by question."""

    async def extract(self, task_dir: Path, url: str, browser: BrowserOpts) -> ExtractResult:
        raise NotImplementedError

    async def submit(
        self, task_dir: Path, private: dict[str, Any], answers: dict[int, str], browser: BrowserOpts
    ) -> dict[str, Any]:
        raise NotImplementedError("Сдача на платформе — этап 3")
