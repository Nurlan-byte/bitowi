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
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from app.agents import runner
from app.agents.base import AgentError, AgentLimit, AgentTimeout, BossBadJSON
from app.agents.codex import CodexAgent, toml_value
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
# действий на один вызов курьера; перед каждой пачкой — проверка, что страница та же
COURIER_BATCH = 20


StepCode = Literal["", "not_found", "disabled", "page_changed", "failed"]

# воркеру — только эти фразы: свои слова курьера (error) остаются во внутреннем журнале
ERRORS = {
    "not_found": "элемент не найден на странице",
    "disabled": "элемент неактивен",
    "page_changed": "страница сменилась — номера устарели",
    "failed": "действие не удалось",
}
NO_REPORT = "результат действия неизвестен"


def upload_dir(run_dir: Path) -> Path:
    return run_dir / "uploads"


class StepReport(BaseModel):
    i: int
    ok: bool
    code: StepCode = ""
    error: str = ""

    @field_validator("code", mode="before")
    @classmethod
    def _code(cls, v: Any) -> Any:
        # незнакомый код — не повод отвергать весь отчёт
        return v if v in ERRORS or v == "" else "failed"

    def for_worker(self) -> str:
        return "" if self.ok else ERRORS.get(self.code, ERRORS["failed"])


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
    # курьер не отработал (таймаут, ошибка, лимит): причина — владельцу, detail — в журнал;
    # у невыполненных действий — NO_REPORT
    failed: str = ""
    failed_detail: str = ""
    limit: bool = False


def failure_reason(exc: AgentError, timeout: int) -> str:
    """For the owner: what happened to the courier, without its output."""
    if isinstance(exc, AgentTimeout):
        return f"курьер не ответил за {timeout} с"
    if isinstance(exc, AgentLimit):
        return "у курьера закончился лимит подписки"
    if isinstance(exc, BossBadJSON):
        return "курьер дважды вернул ответ не по схеме"
    return "курьер завершился с ошибкой"


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
            if not await loc.count():
                report.results.append(StepReport(i=i, ok=False, code="not_found", error="нет элемента с номером"))
                continue
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
                report.results.append(StepReport(i=i, ok=False, code="failed", error=str(exc).splitlines()[0][:200]))
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

    @property
    def wrapper_timeout(self) -> int:
        return self.settings.courier_timeout

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

    def mcp_args(self, browser: CourseBrowser, run_dir: Path) -> list[str]:
        # --allowed-origins нет: он режет подресурсы с CDN (белый экран SPA). Сетевую политику держат
        # маршрут и прокси браузера курса — они действуют и на клики курьера
        return [
            "--cdp-endpoint", browser.cdp_endpoint,
            "--init-script", str(write_guard(run_dir / "guard.js", "course", browser.policy)),
            "--no-webmcp",
            "--output-dir", str(run_dir / "mcp"),
            "--timeout-action", "10000",
        ]

    def build_command(self, run_dir: Path, browser: CourseBrowser, allow_upload: bool) -> list[str]:
        tools = COURIER_TOOLS + ([UPLOAD_TOOL] if allow_upload else [])
        upload_dir(run_dir).mkdir(parents=True, exist_ok=True)
        return self.codex_command(run_dir, self.mcp_args(browser, run_dir)) + [
            "-c", f"mcp_servers.playwright.enabled_tools={toml_value(tools)}",
            # корень файлов MCP: только файлы, которые код скопировал для одобренной сдачи
            "-c", f"mcp_servers.playwright.cwd={toml_value(str(upload_dir(run_dir)))}",
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
                # длинный список — пачками: один вызов курьера не должен упираться в таймаут
                for i in range(0, len(batch), COURIER_BATCH):
                    await self._run_batch(batch[i : i + COURIER_BATCH], allow_submit, out)
                batch = []
                if a is None:
                    break
                if out.offsite:
                    out.results.append(ActionResult(action=a, ok=False, error="не выполнено: шаг остановлен"))
                else:
                    await self.browser.scroll(a.value)
                    out.results.append(ActionResult(action=a, ok=True))
        except AgentError as exc:
            # что успели — с результатами, остальное неизвестно: клик мог пройти до сбоя
            out.failed = failure_reason(exc, self.settings.courier_timeout)
            out.failed_detail = f"{exc}\n{exc.detail}".strip()
            out.limit = isinstance(exc, AgentLimit)
            log.warning("courier failed: %s", exc)
            out.results += [ActionResult(action=a, ok=False, error=NO_REPORT, detail=out.failed)
                            for a in actions[len(out.results):]]
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
                out.results.append(ActionResult(action=a, ok=False, error=NO_REPORT))
            else:
                out.results.append(ActionResult(action=a, ok=r.ok, error=r.for_worker(), detail=r.error))
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
