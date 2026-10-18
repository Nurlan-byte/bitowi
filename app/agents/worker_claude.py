"""Worker backed by Claude Code CLI (`claude -p`)."""

import json
import logging
from pathlib import Path

from app.agents import runner
from app.agents.base import AgentError, AgentLimit, Worker
from app.config import Settings

log = logging.getLogger(__name__)

ALLOWED_TOOLS = "Read,Write,Edit,Bash(python3:*)"
WEB_TOOLS = "WebSearch,WebFetch"

PROMPT_HEADER = (
    "Заказчик обновил общий рабочий блокнот. Ниже он целиком. "
    "Ответь на последнее задание с учётом всех замечаний.\n\n"
)


class ClaudeWorker(Worker):
    def __init__(self, settings: Settings, run=runner.run) -> None:
        self.settings = settings
        self._run = run

    def build_command(self) -> list[str]:
        allowed = ALLOWED_TOOLS + ("," + WEB_TOOLS if self.settings.worker_web else "")
        disallowed = "Skill" + ("" if self.settings.worker_web else "," + WEB_TOOLS)
        return [
            runner.resolve_bin(self.settings.claude_bin),
            "-p",
            "--output-format", "json",
            "--append-system-prompt", self.settings.prompt("worker.md"),
            "--allowedTools", allowed,
            "--disallowedTools", disallowed,
            "--strict-mcp-config",
            "--max-turns", "20",
            "--no-session-persistence",
        ]

    async def solve(self, notebook_text: str, workspace: Path) -> str:
        workspace.mkdir(parents=True, exist_ok=True)
        res = await self._run(
            self.build_command(),
            cwd=workspace,
            timeout=self.settings.worker_timeout,
            stdin=PROMPT_HEADER + notebook_text,
        )
        return parse_output(res)


def parse_output(res: runner.RunResult) -> str:
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        text = res.stderr + res.stdout
        if runner.looks_like_limit(text):
            raise AgentLimit(text.strip()[:300])
        raise AgentError(f"Исполнитель: код {res.returncode}, вывод не JSON: {text.strip()[:300]}")
    result = str(data.get("result") or "")
    if data.get("is_error") or res.returncode != 0:
        text = result + res.stderr
        if runner.looks_like_limit(text):
            raise AgentLimit(text.strip()[:300])
        raise AgentError(f"Исполнитель вернул ошибку: {text.strip()[:300]}")
    if not result.strip():
        raise AgentError("Исполнитель вернул пустой ответ")
    log.info("worker done, cost=%s", data.get("total_cost_usd"))
    return result
