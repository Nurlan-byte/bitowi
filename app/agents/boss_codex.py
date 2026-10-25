"""Boss backed by Codex CLI (`codex exec`), browser via Playwright MCP."""

import json
from pathlib import Path
from typing import Any

from app.agents.base import Boss, BrowserOpts, ExtractResult, ReviewResult, fill
from app.agents.codex import CodexAgent
from app.browser.guard import write_guard


class CodexBoss(CodexAgent, Boss):
    def mcp_args(self, browser: BrowserOpts) -> list[str]:
        args = ["--headless", "--browser", "chromium", "--isolated", "--output-dir", str(browser.output_dir)]
        if browser.storage_state:
            args += ["--storage-state", str(browser.storage_state)]
        if browser.guard:
            # регулярки подставлены в скрипт — кладём готовый файл рядом со скриншотами
            args += ["--init-script", str(write_guard(browser.output_dir / "guard.js", "extract"))]
        return args

    def build_command(self, task_dir: Path, browser: BrowserOpts | None = None) -> list[str]:
        return self.codex_command(task_dir, self.mcp_args(browser) if browser else None) + ["-"]


    async def review(
        self, task_dir: Path, notebook_text: str, private: dict[str, Any], round_no: int, max_rounds: int
    ) -> ReviewResult:
        prompt = (
            fill(self.settings.prompt("boss_review.md"), round=round_no, max_rounds=max_rounds)
            + "\n\n## Общий блокнот\n\n" + notebook_text
            + "\n\n## Твои служебные заметки\n\n" + json.dumps(private, ensure_ascii=False, indent=2)
        )
        return await self.ask_json(task_dir, prompt, ReviewResult, self.build_command(task_dir))

    async def extract(self, task_dir: Path, url: str, browser: BrowserOpts) -> ExtractResult:
        assert browser.guard, "извлечение — только с блокировкой кнопок сдачи"
        prompt = fill(
            self.settings.prompt("boss_extract.md"),
            url=url,
            screens_dir=browser.output_dir,
            max_questions=self.settings.max_questions,
        )
        return await self.ask_json(task_dir, prompt, ExtractResult, self.build_command(task_dir, browser))
