"""Codex CLI (`codex exec`) with a JSON answer: shared by the boss and the course courier."""

import json
import logging
import re
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.agents import runner
from app.agents.base import AgentError, AgentLimit, AgentTimeout, BossBadJSON, fill, parse_json
from app.config import Settings

log = logging.getLogger(__name__)

FIX_JSON = (
    "\n\nТвой прошлый ответ не удалось разобрать: {error}\n"
    "Верни ТОЛЬКО JSON строго по схеме выше, без markdown и пояснений."
)

M = TypeVar("M", bound=BaseModel)

# код выхода `timeout` (coreutils): обёртка курьера сняла codex по своему таймауту
TIMEOUT_EXIT = 124
# хвост вывода CLI — во внутренний журнал
DETAIL_CHARS = 1000
_ERROR_LINE_RE = re.compile(r"^\s*error\b[:\s]", re.IGNORECASE)


def error_line(text: str) -> str:
    """The last «ERROR: …» line of the CLI output, if any."""
    lines = [ln.strip() for ln in text.splitlines() if _ERROR_LINE_RE.match(ln)]
    return lines[-1][:200] if lines else ""


def toml_value(value: Any) -> str:
    # JSON-строки и массивы строк — валидный TOML
    return json.dumps(value, ensure_ascii=False)


# функции Codex, дающие модели доступ к машине или к приложениям аккаунта (Gmail, GitHub, Drive…):
# shell, дочерние агенты, просмотр файлов-картинок. Проверено на codex 0.163: без них модель
# не читает файлы владельца. Это второй слой — курьер ещё и работает от отдельного пользователя.
# code_mode_host не выключаем: через него вызываются инструменты MCP (браузер); JS в нём без импортов —
# ни файлов, ни shell (проверено приманкой).
CODEX_LOCKDOWN = (
    "shell_tool", "unified_exec", "view_image", "multi_agent", "apps", "plugins",
    "image_generation", "browser_use", "browser_use_external", "computer_use", "in_app_browser",
    "sleep_tool", "tool_suggest", "skill_search", "skill_mcp_dependency_install", "goals",
    "workspace_dependencies",
)


class CodexAgent:
    """`codex exec` with a JSON answer: shared by the boss and the course courier."""

    who = "Куратор"
    out_name = "boss_last.txt"

    def __init__(self, settings: Settings, run=runner.run) -> None:
        self.settings = settings
        self._run = run

    @property
    def timeout(self) -> int:
        return self.settings.boss_timeout

    def mcp_command(self) -> str:
        return runner.resolve_bin(self.settings.playwright_mcp_bin)

    def lockdown_args(self) -> list[str]:
        """Read-only sandbox, no user config, no shell/exec/sub-agents/apps."""
        args = ["--sandbox", "read-only", "--ignore-user-config"]
        for feature in CODEX_LOCKDOWN:
            args += ["--disable", feature]
        return args

    def codex_command(self, task_dir: Path, mcp: list[str] | None = None) -> list[str]:
        cmd = [
            runner.resolve_bin(self.settings.codex_bin),
            "exec",
            *self.lockdown_args(),
            "--skip-git-repo-check",
            "-C", str(task_dir),
            "--ephemeral",
            "--color", "never",
            "-o", str(task_dir / self.out_name),
            "-c", 'web_search="disabled"',
        ]
        if mcp is not None:
            cmd += [
                "-c", f"mcp_servers.playwright.command={toml_value(self.mcp_command())}",
                "-c", f"mcp_servers.playwright.args={toml_value(mcp)}",
                "-c", 'mcp_servers.playwright.default_tools_approval_mode="approve"',
            ]
        return cmd

    @property
    def wrapper_timeout(self) -> int:
        """The limit the owner sees in «не ответил за N с» (the courier's wrapper has its own `timeout`)."""
        return self.timeout

    async def _call(self, task_dir: Path, prompt: str, cmd: list[str]) -> str:
        task_dir.mkdir(parents=True, exist_ok=True)
        out_file = task_dir / self.out_name
        out_file.unlink(missing_ok=True)
        try:
            res = await self._run(cmd, cwd=task_dir, timeout=self.timeout, stdin=prompt)
        except AgentTimeout as exc:
            raise AgentTimeout(f"{self.who}: не ответил за {self.wrapper_timeout} с", detail=str(exc)) from exc
        if res.returncode != 0:
            # codex повторяет промпт в stderr: ни в сообщение, ни в поиск лимита он не идёт
            text = (res.stderr + res.stdout).replace(prompt, "").strip()
            detail = text[-DETAIL_CHARS:]
            if res.returncode == TIMEOUT_EXIT:
                raise AgentTimeout(f"{self.who}: не ответил за {self.wrapper_timeout} с", detail=detail)
            if runner.looks_like_limit(text):
                raise AgentLimit(text[-300:], detail=detail)
            line = error_line(text)
            raise AgentError(f"{self.who}: код {res.returncode}" + (f": {line}" if line else ""), detail=detail)
        if out_file.exists():
            return out_file.read_text(encoding="utf-8")
        return res.stdout

    async def ask_json(self, task_dir: Path, prompt: str, model: type[M], cmd: list[str]) -> M:
        error = ""
        for attempt in range(2):
            raw = await self._call(task_dir, prompt if attempt == 0 else prompt + fill(FIX_JSON, error=error), cmd)
            try:
                return model.model_validate(parse_json(raw))
            except (ValueError, ValidationError) as exc:
                error = str(exc).splitlines()[0][:200]
                log.warning("%s returned bad JSON (attempt %d): %s", self.who, attempt + 1, error)
        raise BossBadJSON(f"{self.who} дважды вернул невалидный JSON: {error}")
