"""Task state machine and single-slot queue."""

import asyncio
import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from app import sanitize
from app.agents.base import AgentLimit, AgentTimeout, Boss, Worker
from app.config import Settings
from app.db import Database, Task
from app.notebook import CLIENT, EXECUTOR, Notebook

log = logging.getLogger(__name__)


class State(StrEnum):
    QUEUED = "QUEUED"
    EXTRACTING = "EXTRACTING"
    SOLVING = "SOLVING"
    REVIEWING = "REVIEWING"
    AWAIT_CONFIRM = "AWAIT_CONFIRM"
    SUBMITTING = "SUBMITTING"
    PAUSED = "PAUSED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = (State.DONE, State.FAILED, State.CANCELLED)
# задача в этих состояниях занимает слот: следующая из очереди не стартует
BUSY = (State.EXTRACTING, State.SOLVING, State.REVIEWING, State.AWAIT_CONFIRM, State.SUBMITTING, State.PAUSED)
RUNNING = (State.EXTRACTING, State.SOLVING, State.REVIEWING, State.SUBMITTING)


class Notifier(Protocol):
    async def status(self, task: Task, text: str) -> None: ...
    async def preview(self, task: Task, task_text: str, answer: str) -> None: ...
    async def message(self, task: Task, text: str) -> None: ...


class Orchestrator:
    def __init__(self, settings: Settings, db: Database, boss: Boss, worker: Worker, notifier: Notifier) -> None:
        self.settings = settings
        self.db = db
        self.boss = boss
        self.worker = worker
        self.notifier = notifier
        self._lock = asyncio.Lock()
        self._runner: asyncio.Task | None = None

    # --- пути ---
    def task_dir(self, task_id: int) -> Path:
        return self.settings.tasks_dir / str(task_id)

    def notebook(self, task_id: int) -> Notebook:
        return Notebook(self.task_dir(task_id) / "notebook.md")

    def _private_path(self, task_id: int) -> Path:
        return self.task_dir(task_id) / "boss_private.json"

    def load_private(self, task_id: int) -> dict[str, Any]:
        path = self._private_path(task_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    # --- публичные команды ---
    async def submit_text(self, text: str) -> Task:
        task = await self.db.create_task("text", text, State.QUEUED)
        await self.db.add_event(task.id, "created")
        await self._kick()
        return await self.db.get_task(task.id)  # type: ignore[return-value]

    async def active_task(self) -> Task | None:
        busy = await self.db.tasks_in_states(BUSY)
        return busy[0] if busy else None

    async def queued(self) -> list[Task]:
        return await self.db.tasks_in_states((State.QUEUED,))

    async def confirm(self, task_id: int) -> bool:
        task = await self.db.get_task(task_id)
        if not task or task.state != State.AWAIT_CONFIRM:
            return False
        await self._finish(task, State.DONE)
        await self._kick()
        return True

    async def request_fix(self, task_id: int, text: str) -> bool:
        """Owner's correction from Telegram: goes to the notebook as the client's remark."""
        task = await self.db.get_task(task_id)
        if not task or task.state != State.AWAIT_CONFIRM:
            return False
        self.notebook(task_id).append(CLIENT, text)
        await self.db.add_event(task_id, "owner_fix")
        await self.db.update_task(task_id, state=State.SOLVING, round=1, final_answer=None)
        self._start(task_id)
        return True

    async def add_remark(self, text: str) -> Task | None:
        """A reply to the bot: client's remark for the current task."""
        task = await self.active_task()
        if not task:
            return None
        if task.state == State.AWAIT_CONFIRM:
            await self.request_fix(task.id, text)
        else:
            self.notebook(task.id).append(CLIENT, text)
            await self.db.add_event(task.id, "owner_remark")
        return task

    async def cancel(self, task_id: int | None = None) -> Task | None:
        task = await self.db.get_task(task_id) if task_id else await self.active_task()
        if not task or task.state in TERMINAL:
            return None
        if self._runner and not self._runner.done() and getattr(self._runner, "task_id", None) == task.id:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
        task = await self.db.update_task(task.id, state=State.CANCELLED)
        await self.db.add_event(task.id, "cancelled")
        await self.notifier.status(task, f"Задача #{task.id} отменена.")
        await self._kick()
        return task

    async def resume(self) -> Task | None:
        paused = await self.db.tasks_in_states((State.PAUSED,))
        if not paused:
            return None
        task = paused[0]
        await self.db.update_task(task.id, state=task.resume_state or State.SOLVING, resume_state=None)
        await self.db.add_event(task.id, "resumed")
        self._start(task.id)
        return task

    async def recover(self) -> None:
        """On startup: tasks interrupted mid-step become PAUSED, then the queue continues."""
        for task in await self.db.tasks_in_states(RUNNING):
            await self.db.update_task(task.id, state=State.PAUSED, resume_state=task.state)
            await self.db.add_event(task.id, "interrupted")
        await self._kick()

    async def wait_idle(self) -> None:
        """Wait until the current step finishes (for tests)."""
        while self._runner and not self._runner.done():
            await asyncio.wait({self._runner})

    # --- очередь ---
    async def _kick(self) -> None:
        async with self._lock:
            if await self.active_task():
                return
            queued = await self.queued()
            if not queued:
                return
            task = queued[0]
            await self._prepare(task)
            self._start(task.id)

    async def _prepare(self, task: Task) -> None:
        tdir = self.task_dir(task.id)
        (tdir / "workspace").mkdir(parents=True, exist_ok=True)
        self.notebook(task.id).append(CLIENT, task.input)
        self._private_path(task.id).write_text(
            json.dumps({"source": task.kind}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await self.db.update_task(task.id, state=State.SOLVING, round=1)

    def _start(self, task_id: int) -> None:
        runner = asyncio.create_task(self._drive(task_id))
        runner.task_id = task_id  # type: ignore[attr-defined]
        self._runner = runner

    # --- машина состояний ---
    async def _drive(self, task_id: int) -> None:
        cancelled = False
        try:
            await self._loop(task_id)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except AgentLimit as exc:
            task = await self.db.get_task(task_id)
            assert task
            task = await self.db.update_task(task_id, state=State.PAUSED, resume_state=task.state)
            await self.db.add_event(task_id, "limit", str(exc)[:300])
            await self.notifier.status(
                task, f"Задача #{task_id} на паузе: упёрлись в лимит подписки. Продолжить — /resume"
            )
        except AgentTimeout as exc:
            await self._fail(task_id, f"таймаут: {exc}")
        except Exception as exc:  # noqa: BLE001 — любая ошибка шага завершает задачу
            log.exception("task %s failed", task_id)
            await self._fail(task_id, str(exc)[:500])
        finally:
            if not cancelled:
                task = await self.db.get_task(task_id)
                if task and task.state in TERMINAL:
                    asyncio.create_task(self._kick())

    async def _loop(self, task_id: int) -> None:
        max_rounds = self.settings.max_rounds
        while True:
            task = await self.db.get_task(task_id)
            assert task
            if task.state == State.SOLVING:
                await self.notifier.status(task, f"Задача #{task_id} · раунд {task.round}/{max_rounds} · исполнитель решает")
                await self._solve(task)
                await self.db.update_task(task_id, state=State.REVIEWING)
            elif task.state == State.REVIEWING:
                await self.notifier.status(task, f"Задача #{task_id} · раунд {task.round}/{max_rounds} · куратор проверяет")
                result = await self.boss.review(
                    self.task_dir(task_id),
                    self.notebook(task_id).read(),
                    self.load_private(task_id),
                    task.round,
                    max_rounds,
                )
                await self.db.add_event(task_id, "review", result.model_dump())
                if result.verdict == "accept":
                    await self._accepted(task, result.final_answer)
                    return
                if task.round >= max_rounds:
                    await self._fail(task_id, f"превышено число раундов ({max_rounds})")
                    return
                self.notebook(task_id).append(CLIENT, result.comment_for_worker or "Перепроверь решение.")
                task = await self.db.update_task(task_id, state=State.SOLVING, round=task.round + 1)
                await self.notifier.status(
                    task, f"Задача #{task_id} · раунд {task.round}/{max_rounds} · куратор вернул на доработку"
                )
            else:
                return

    async def _solve(self, task: Task) -> None:
        private = self.load_private(task.id)
        banned = [v for k, v in private.items() if k in ("platform", "platform_name") and isinstance(v, str)]
        text = sanitize.for_worker(self.notebook(task.id).read(), banned)
        reply = await self.worker.solve(text, self.task_dir(task.id) / "workspace")
        self.notebook(task.id).append(EXECUTOR, sanitize.for_boss(reply))
        await self.db.add_event(task.id, "worker_reply", {"chars": len(reply)})

    async def _accepted(self, task: Task, final_answer: str) -> None:
        task = await self.db.update_task(task.id, final_answer=final_answer)
        if self.settings.auto_submit:
            await self._finish(task, State.DONE)
            return
        task = await self.db.update_task(task.id, state=State.AWAIT_CONFIRM)
        await self.notifier.status(task, f"Задача #{task.id} · ответ готов, жду подтверждения")
        await self.notifier.preview(task, task.input, final_answer)

    async def _finish(self, task: Task, state: State) -> None:
        # Этап 1: сдачи на платформе нет — «Отправить» просто завершает задачу
        task = await self.db.update_task(task.id, state=state)
        await self.db.add_event(task.id, "done")
        await self.notifier.status(task, f"Задача #{task.id} · готово")
        await self.notifier.message(task, f"Задача #{task.id} готова.\n\nОтвет:\n{task.final_answer or '—'}")

    async def _fail(self, task_id: int, reason: str) -> None:
        task = await self.db.update_task(task_id, state=State.FAILED, error=reason)
        await self.db.add_event(task_id, "failed", reason)
        await self.notifier.status(task, f"Задача #{task_id} не выполнена: {reason}")
