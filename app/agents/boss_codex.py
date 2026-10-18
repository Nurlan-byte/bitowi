"""Boss backed by Codex CLI (`codex exec`)."""

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agents import runner
from app.agents.base import AgentError, AgentLimit, Boss, BossBadJSON, ReviewResult
from app.config import Settings

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

FIX_JSON = (
    "\n\nТвой прошлый ответ не удалось разобрать: {error}\n"
    "Верни ТОЛЬКО JSON строго по схеме выше, без markdown и пояснений."
)


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


class CodexBoss(Boss):
    def __init__(self, settings: Settings, run=runner.run) -> None:
        self.settings = settings
        self._run = run

    def build_command(self, task_dir: Path) -> list[str]:
        return [
            runner.resolve_bin(self.settings.codex_bin),
            "exec",
            "--skip-git-repo-check",
            "-C", str(task_dir),
            "--sandbox", "read-only",
            "--ephemeral",
            "--color", "never",
            "-o", str(task_dir / "boss_last.txt"),
            "-",
        ]

    async def _call(self, task_dir: Path, prompt: str) -> str:
        out_file = task_dir / "boss_last.txt"
        out_file.unlink(missing_ok=True)
        res = await self._run(
            self.build_command(task_dir), cwd=task_dir, timeout=self.settings.boss_timeout, stdin=prompt
        )
        if res.returncode != 0:
            text = res.stderr + res.stdout
            if runner.looks_like_limit(text):
                raise AgentLimit(text.strip()[-300:])
            raise AgentError(f"Куратор: код {res.returncode}: {text.strip()[-300:]}")
        if out_file.exists():
            return out_file.read_text(encoding="utf-8")
        return res.stdout

    async def _ask_json(self, task_dir: Path, prompt: str, model: type[ReviewResult]) -> ReviewResult:
        error = ""
        for attempt in range(2):
            raw = await self._call(task_dir, prompt if attempt == 0 else prompt + fill(FIX_JSON, error=error))
            try:
                return model.model_validate(parse_json(raw))
            except (ValueError, ValidationError) as exc:
                error = str(exc).splitlines()[0][:200]
                log.warning("boss returned bad JSON (attempt %d): %s", attempt + 1, error)
        raise BossBadJSON(f"Куратор дважды вернул невалидный JSON: {error}")

    async def review(
        self, task_dir: Path, notebook_text: str, private: dict[str, Any], round_no: int, max_rounds: int
    ) -> ReviewResult:
        prompt = (
            fill(self.settings.prompt("boss_review.md"), round=round_no, max_rounds=max_rounds)
            + "\n\n## Общий блокнот\n\n" + notebook_text
            + "\n\n## Твои служебные заметки\n\n" + json.dumps(private, ensure_ascii=False, indent=2)
        )
        return await self._ask_json(task_dir, prompt, ReviewResult)
