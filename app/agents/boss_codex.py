"""Boss backed by Codex CLI (`codex exec`), browser via Playwright MCP."""

import json
import logging
import re
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.agents import runner
from app.agents.base import (
    AgentError,
    AgentLimit,
    Boss,
    BossBadJSON,
    BrowserOpts,
    ExtractResult,
    ReviewResult,
)
from app.browser.guard import write_guard
from app.config import Settings

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

FIX_JSON = (
    "\n\nТвой прошлый ответ не удалось разобрать: {error}\n"
    "Верни ТОЛЬКО JSON строго по схеме выше, без markdown и пояснений."
)

M = TypeVar("M", bound=BaseModel)


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


def _toml(value: Any) -> str:
    # JSON-строки и массивы строк — валидный TOML
    return json.dumps(value, ensure_ascii=False)


class CodexBoss(Boss):
    def __init__(self, settings: Settings, run=runner.run) -> None:
        self.settings = settings
        self._run = run

    def mcp_args(self, browser: BrowserOpts) -> list[str]:
        args = ["--headless", "--browser", "chromium", "--isolated", "--output-dir", str(browser.output_dir)]
        if browser.storage_state:
            args += ["--storage-state", str(browser.storage_state)]
        if browser.guard:
            # регулярки подставлены в скрипт — кладём готовый файл рядом со скриншотами
            args += ["--init-script", str(write_guard(browser.output_dir / "guard.js", "extract"))]
        return args

    def build_command(self, task_dir: Path, browser: BrowserOpts | None = None) -> list[str]:
        cmd = [
            runner.resolve_bin(self.settings.codex_bin),
            "exec",
            "--skip-git-repo-check",
            "-C", str(task_dir),
            "--sandbox", "read-only",
            "--ephemeral",
            "--color", "never",
            "-o", str(task_dir / "boss_last.txt"),
            "-c", 'web_search="disabled"',
        ]
        if browser:
            cmd += [
                "-c", f"mcp_servers.playwright.command={_toml(runner.resolve_bin(self.settings.playwright_mcp_bin))}",
                "-c", f"mcp_servers.playwright.args={_toml(self.mcp_args(browser))}",
                "-c", 'mcp_servers.playwright.default_tools_approval_mode="approve"',
            ]
        cmd.append("-")
        return cmd

    async def _call(self, task_dir: Path, prompt: str, browser: BrowserOpts | None) -> str:
        task_dir.mkdir(parents=True, exist_ok=True)
        out_file = task_dir / "boss_last.txt"
        out_file.unlink(missing_ok=True)
        res = await self._run(
            self.build_command(task_dir, browser), cwd=task_dir, timeout=self.settings.boss_timeout, stdin=prompt
        )
        if res.returncode != 0:
            text = res.stderr + res.stdout
            if runner.looks_like_limit(text):
                raise AgentLimit(text.strip()[-300:])
            raise AgentError(f"Куратор: код {res.returncode}: {text.strip()[-300:]}")
        if out_file.exists():
            return out_file.read_text(encoding="utf-8")
        return res.stdout

    async def _ask_json(self, task_dir: Path, prompt: str, model: type[M], browser: BrowserOpts | None = None) -> M:
        error = ""
        for attempt in range(2):
            raw = await self._call(task_dir, prompt if attempt == 0 else prompt + fill(FIX_JSON, error=error), browser)
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

    async def extract(self, task_dir: Path, url: str, browser: BrowserOpts) -> ExtractResult:
        assert browser.guard, "извлечение — только с блокировкой кнопок сдачи"
        prompt = fill(
            self.settings.prompt("boss_extract.md"),
            url=url,
            screens_dir=browser.output_dir,
            max_questions=self.settings.max_questions,
        )
        return await self._ask_json(task_dir, prompt, ExtractResult, browser)
