"""Worker backed by Claude Code CLI (`claude -p`).

WORKER_MODE=sudo runs it as a separate Linux user through deploy/bitowi-worker-run,
so the worker cannot read the project, boss files or CLI logins of the bot's user.
"""

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from app.agents import runner
from app.agents.base import AgentError, AgentLimit, Worker, WorkerBadJSON, fill, parse_json
from app.config import Settings
from app.course.models import Decision

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]

ALLOWED_TOOLS = "Read,Write,Edit,Bash(python3:*)"
WEB_TOOLS = "WebSearch,WebFetch"

PROMPT_HEADER = (
    "Заказчик обновил общий рабочий блокнот. Ниже он целиком. "
    "Ответь на последнее задание с учётом всех замечаний.\n\n"
)

# повтор после ответа не по схеме: точная ошибка и как уложиться (большие страницы — частями)
WORKER_FIX_JSON = (
    "\n\nПрошлый ответ не прошёл проверку схемы: {error}\n"
    "Верни ТОЛЬКО JSON по схеме, без markdown и пояснений. Значения value — строки. "
    "Если действий много — верни первые (до 40) со статусом continue, остальные — следующим шагом."
)

# запас поверх таймаута обёртки (timeout --kill-after=10 внутри)
SUDO_GRACE = 30

# системный промпт и настройки — файлами в папке задачи, а не аргументами:
# аргументы процесса видны в ps любому пользователю сервера
SYSTEM_FILE = "worker_system.md"
SETTINGS_FILE = "worker_settings.json"


class ClaudeWorker(Worker):
    def __init__(self, settings: Settings, run=runner.run) -> None:
        self.settings = settings
        self._run = run

    def denied_paths(self) -> list[Path]:
        """Directories the worker must not read: code, instructions, boss files, CLI logins."""
        s = self.settings
        return [PROJECT_DIR, s.prompts_dir.parent, s.data_dir, Path.home() / ".codex"]

    def sandbox_settings(self) -> str:
        deny = []
        for path in self.denied_paths():
            p = path.resolve().as_posix()
            deny += [f"Read(/{p}/**)", f"Edit(/{p}/**)"]
        return json.dumps({
            "sandbox": {
                "enabled": self.settings.worker_sandbox,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
            },
            "permissions": {"deny": deny},
        })

    def write_files(self, task_dir: Path, prompt_name: str = "worker.md") -> None:
        """Put the system prompt and settings next to the workspace (readable by the worker's group)."""
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / SYSTEM_FILE).write_text(self.settings.prompt(prompt_name), encoding="utf-8")
        (task_dir / SETTINGS_FILE).write_text(self.sandbox_settings(), encoding="utf-8")

    def claude_args(self, task_dir: Path) -> list[str]:
        allowed = ALLOWED_TOOLS + ("," + WEB_TOOLS if self.settings.worker_web else "")
        disallowed = "Skill" + ("" if self.settings.worker_web else "," + WEB_TOOLS)
        return [
            "-p",
            "--output-format", "json",
            "--append-system-prompt-file", str(task_dir / SYSTEM_FILE),
            "--allowedTools", allowed,
            "--disallowedTools", disallowed,
            "--strict-mcp-config",
            "--max-turns", "20",
            "--no-session-persistence",
            "--settings", str(task_dir / SETTINGS_FILE),
        ]

    def build_command(self, task_dir: Path) -> list[str]:
        if self.settings.worker_mode == "sudo":
            return [
                runner.resolve_bin("sudo"), "-n", "-u", self.settings.worker_user,
                self.settings.worker_runner, task_dir.name, str(self.settings.worker_timeout),
                *self.claude_args(task_dir),
            ]
        return [runner.resolve_bin(self.settings.claude_bin), *self.claude_args(task_dir)]

    async def _claude(self, workspace: Path, stdin: str) -> str:
        sudo = self.settings.worker_mode == "sudo"
        res = await self._run(
            self.build_command(workspace.parent),
            cwd=workspace,
            timeout=self.settings.worker_timeout + (SUDO_GRACE if sudo else 0),
            stdin=stdin,
        )
        return parse_output(res)

    async def solve(self, notebook_text: str, workspace: Path) -> str:
        workspace.mkdir(parents=True, exist_ok=True)
        self.write_files(workspace.parent)
        return await self._claude(workspace, PROMPT_HEADER + notebook_text)

    async def decide(self, screen_text: str, workspace: Path) -> Decision:
        """Course mode, one step: the screen state in, the next actions out (JSON by schema)."""
        workspace.mkdir(parents=True, exist_ok=True)
        self.write_files(workspace.parent, "course_worker.md")
        replies: list[str] = []
        errors: list[str] = []
        for attempt in range(2):
            stdin = screen_text if attempt == 0 else screen_text + fill(WORKER_FIX_JSON, error=errors[-1])
            reply = await self._claude(workspace, stdin)
            replies.append(reply)
            try:
                return Decision.model_validate(parse_json(reply))
            except (ValueError, ValidationError) as exc:
                errors.append(describe_error(exc, reply))
                log.warning("worker returned bad JSON (attempt %d, %d chars): %s", attempt + 1, len(reply), errors[-1])
        raise WorkerBadJSON(f"Исполнитель дважды вернул ответ не по схеме: {errors[-1]}", replies, errors)


def describe_error(exc: Exception, reply: str) -> str:
    """Which field and what is wrong — for the retry prompt and the journal (no field values)."""
    if isinstance(exc, ValidationError):
        items = [f"{'.'.join(map(str, e['loc'])) or 'ответ'}: {e['msg']}" for e in exc.errors()[:5]]
        more = exc.error_count() - len(items)
        return "; ".join(items) + (f"; и ещё {more}" if more > 0 else "")
    text = str(exc).splitlines()[0][:200]
    tail = reply.rstrip()[-1:] if reply.strip() else ""
    if tail and tail not in "}]`":
        text += " (ответ обрывается — похоже, слишком длинный)"
    return text


def parse_output(res: runner.RunResult) -> str:
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        text = res.stderr + res.stdout
        if "sudo:" in res.stderr or "bitowi-worker-run:" in res.stderr:
            raise AgentError(f"Запуск исполнителя от отдельного пользователя не настроен: {res.stderr.strip()[:300]}")
        if res.returncode in (124, 137):
            raise AgentError("Исполнитель остановлен по таймауту обёртки")
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
