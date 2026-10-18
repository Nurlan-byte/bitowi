"""Agent interfaces and shared models. Boss and worker are swappable behind these ABCs."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentError(Exception):
    """Generic agent failure (bad exit code, unparsable output)."""


class AgentTimeout(AgentError):
    """Agent call exceeded its timeout and was killed."""


class AgentLimit(AgentError):
    """Subscription usage / rate limit reached."""


class BossBadJSON(AgentError):
    """Boss returned invalid JSON twice in a row."""


class ReviewResult(BaseModel):
    verdict: Literal["accept", "revise"]
    comment_for_worker: str = ""
    final_answer: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class Worker(ABC):
    @abstractmethod
    async def solve(self, notebook_text: str, workspace: Path) -> str:
        """Read the (sanitized) notebook, return the executor's reply."""


class Boss(ABC):
    @abstractmethod
    async def review(
        self, task_dir: Path, notebook_text: str, private: dict[str, Any], round_no: int, max_rounds: int
    ) -> ReviewResult:
        """Check the last executor reply against the task."""

    async def extract(self, task_dir: Path, url: str) -> dict[str, Any]:
        raise NotImplementedError("Извлечение из браузера — этап 2")

    async def submit(self, task_dir: Path, private: dict[str, Any], final_answer: str) -> dict[str, Any]:
        raise NotImplementedError("Сдача на платформе — этап 3")
