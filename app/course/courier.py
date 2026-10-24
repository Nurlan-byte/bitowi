"""Action layer: the courier executes checked actions and describes the screen.

Clicks are done by an executor: CodexExecutor (Codex + Playwright MCP over CDP, production) or
DirectExecutor (plain Playwright, tests without subscriptions). Around every executor call the
code checks the page numbers, the guard's refusals and the course domain.
"""

import json
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel

from app.agents import runner
from app.agents.boss_codex import CodexAgent, _toml
from app.browser.guard import write_guard
from app.config import Settings
from app.course.browser import CourseBrowser
from app.course.models import Action, ActionResult, Screen
from app.course.policy import safe_upload_path

log = logging.getLogger(__name__)

# инструменты MCP курьера: без evaluate/run_code, навигации по адресу, вкладок, клавиш и сети
COURIER_TOOLS = [
    "browser_snapshot",
    "browser_find",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_handle_dialog",
]
# загрузка файла — только в вызове сдачи после «Отправить» владельца
UPLOAD_TOOL = "browser_file_upload"


def upload_dir(run_dir: Path) -> Path:
    return run_dir / "uploads"


class StepReport(BaseModel):
    i: int
    ok: bool
    error: str = ""


class CourierReport(BaseModel):
    """Courier output: per-action result (i — 1-based index in the list) and what the platform said."""

    results: list[StepReport] = []
    messages: list[str] = []
    progress: str = ""


class ExecResult(BaseModel):
    results: list[ActionResult] = []
    messages: list[str] = []
    progress: str = ""
    # главная вкладка ушла с домена курса (откатили) — шаг остановлен
    offsite: str = ""


class Executor(ABC):
    @abstractmethod
    async def run(
        self, browser: CourseBrowser, actions: list[Action], run_dir: Path, allow_upload: bool
    ) -> CourierReport:
        """Perform the actions on elements marked data-bitowi-n."""


class DirectExecutor(Executor):
    """Plain Playwright clicks, real mouse events: the guard sees them as it sees MCP clicks."""

    async def run(
        self, browser: CourseBrowser, actions: list[Action], run_dir: Path, allow_upload: bool
    ) -> CourierReport:
        page = browser.page
        report = CourierReport()
        for i, a in enumerate(actions, 1):
            loc = page.locator(f'[data-bitowi-n="{a.element}"]')
            try:
                if a.action == "click":
                    await loc.click(timeout=5000)
                elif a.action == "type":
                    await loc.fill(a.value, timeout=5000)
                elif a.action == "select":
                    await loc.select_option(label=a.value, timeout=5000)
                elif a.action == "upload":
                    path = safe_upload_path(a.value, upload_dir(run_dir))
                    if not allow_upload or not path:
                        raise ValueError("загрузка файла не разрешена")
                    await loc.set_input_files(str(path), timeout=5000)
                await page.wait_for_timeout(200)
                await page.wait_for_load_state("domcontentloaded")
                report.results.append(StepReport(i=i, ok=True))
            except Exception as exc:  # noqa: BLE001 — результат действия, не авария
                report.results.append(StepReport(i=i, ok=False, error=str(exc).splitlines()[0][:200]))
        return report


class CodexExecutor(CodexAgent, Executor):
    """COURIER_MODE=sudo: codex runs as COURIER_USER through deploy/bitowi-courier-run, in
    COURIER_ROOT/<task_id>, and cannot read the bot's home (.env, state.json, CLI logins)."""

    who = "Курьер"
    out_name = "courier_last.txt"

    @property
    def sudo(self) -> bool:
        return self.settings.courier_mode == "sudo"

    @property
    def timeout(self) -> int:
        # запас поверх таймаута обёртки (timeout --kill-after=10 внутри)
        return self.settings.courier_timeout + (30 if self.sudo else 0)

    def mcp_command(self) -> str:
        return self.settings.courier_mcp_bin if self.sudo else super().mcp_command()

    def lockdown_args(self) -> list[str]:
        # в режиме sudo ограничения зашиты в обёртку bitowi-courier-run, вызывающий их не передаёт
        return [] if self.sudo else super().lockdown_args()

    def codex_command(self, task_dir: Path, mcp: list[str] | None = None) -> list[str]:
        cmd = super().codex_command(task_dir, mcp)
        if not self.sudo:
            return cmd
        if task_dir.parent != self.settings.courier_root:
            raise ValueError(f"папка курьера вне {self.settings.courier_root}: {task_dir}")
        # путь к codex зафиксирован в обёртке
        return [
            runner.resolve_bin("sudo"), "-n", "-u", self.settings.courier_user, self.settings.courier_runner,
            task_dir.name, str(self.settings.courier_timeout), *cmd[1:],
        ]

    def allowed_origins(self, browser: CourseBrowser) -> str:
        extra = [o.strip() for o in self.settings.course_extra_origins.split(";") if o.strip()]
        return ";".join([browser.origin, *extra])

    def mcp_args(self, browser: CourseBrowser, run_dir: Path) -> list[str]:
        return [
            "--cdp-endpoint", browser.cdp_endpoint,
            "--init-script", str(write_guard(run_dir / "guard.js", "course")),
            "--allowed-origins", self.allowed_origins(browser),
            "--no-webmcp",
            "--output-dir", str(run_dir / "mcp"),
            "--timeout-action", "10000",
        ]

    def build_command(self, run_dir: Path, browser: CourseBrowser, allow_upload: bool) -> list[str]:
        tools = COURIER_TOOLS + ([UPLOAD_TOOL] if allow_upload else [])
        upload_dir(run_dir).mkdir(parents=True, exist_ok=True)
        return self.codex_command(run_dir, self.mcp_args(browser, run_dir)) + [
            "-c", f"mcp_servers.playwright.enabled_tools={_toml(tools)}",
            # корень файлов MCP: только файлы, которые код скопировал для одобренной сдачи
            "-c", f"mcp_servers.playwright.cwd={_toml(str(upload_dir(run_dir)))}",
            "-",
        ]

    def prompt(self, actions: list[Action]) -> str:
        items = [{"i": i, **a.model_dump()} for i, a in enumerate(actions, 1)]
        return (
            self.settings.prompt("course_courier.md")
            + "\n\n## Действия\n\n"
            + json.dumps(items, ensure_ascii=False, indent=2)
        )

    async def run(
        self, browser: CourseBrowser, actions: list[Action], run_dir: Path, allow_upload: bool
    ) -> CourierReport:
        cmd = self.build_command(run_dir, browser, allow_upload)
        return await self.ask_json(run_dir, self.prompt(actions), CourierReport, cmd)


class Courier:
    def __init__(
        self, settings: Settings, browser: CourseBrowser, executor: Executor, run_dir: Path, workspace: Path
    ) -> None:
        self.settings = settings
        self.browser = browser
        self.executor = executor
        self.run_dir = run_dir
        self.workspace = workspace

    async def inspect(self, shots_dir: Path) -> Screen:
        return await self.browser.inspect(shots_dir, self.settings.course_max_shots)

    async def execute(self, actions: list[Action], allow_submit: bool = False) -> ExecResult:
        """Run actions in order. Scrolling is done by the code, the rest by the executor in batches."""
        out = ExecResult()
        if allow_submit:
            actions = [self._stage_upload(a) for a in actions]
            await self.browser.allow_submit()
        try:
            batch: list[Action] = []
            for a in [*actions, None]:
                if a is not None and a.action != "scroll":
                    batch.append(a)
                    continue
                if batch:
                    await self._run_batch(batch, allow_submit, out)
                    batch = []
                if a is None:
                    break
                if out.offsite:
                    out.results.append(ActionResult(action=a, ok=False, error="не выполнено: шаг остановлен"))
                else:
                    await self.browser.scroll(a.value)
                    out.results.append(ActionResult(action=a, ok=True))
        finally:
            if allow_submit:
                await self.browser.revoke_submit()
        return out

    async def _run_batch(self, batch: list[Action], allow_submit: bool, out: ExecResult) -> None:
        if out.offsite or not await self.browser.stamp_ok():
            why = "не выполнено: шаг остановлен" if out.offsite else "страница сменилась — номера устарели"
            out.results += [ActionResult(action=a, ok=False, error=why) for a in batch]
            return
        report = await self.executor.run(self.browser, batch, self.run_dir, allow_submit)
        by_i = {r.i: r for r in report.results}
        refused = dict(await self.browser.blocked())
        for i, a in enumerate(batch, 1):
            r = by_i.get(i)
            if a.element in refused:
                out.results.append(ActionResult(action=a, ok=False, error=f"элемент [{a.element}] недоступен"))
            elif r is None:
                out.results.append(ActionResult(action=a, ok=False, error="курьер не сообщил результат"))
            else:
                out.results.append(ActionResult(action=a, ok=r.ok, error=r.error))
        out.messages += report.messages
        out.progress = report.progress or out.progress
        out.offsite = await self.browser.check_domain()

    def _stage_upload(self, action: Action) -> Action:
        """Copy an approved file from the task workspace to the courier's upload dir."""
        src = safe_upload_path(action.value, self.workspace) if action.action == "upload" else None
        if not src:
            return action
        dst = upload_dir(self.run_dir) / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return action.model_copy(update={"value": str(dst)})
