"""Task state machine and single-slot queue."""

import asyncio
import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from app import sanitize
from app.agents.base import (
    AgentLimit,
    AgentTimeout,
    Boss,
    BrowserOpts,
    Question,
    QuestionVerdict,
    Worker,
)
from app.answers import chunk, parse_answers, render_answers, render_task
from app.browser.session import LoginFailed, SessionExpired
from app.browser.sites import host_of, profile_for
from app.config import Settings
from app.db import Database, Task
from app.course.runner import CoursePause, CourseRunner
from app.notebook import CLIENT, EXECUTOR, Notebook

log = logging.getLogger(__name__)


class State(StrEnum):
    QUEUED = "QUEUED"
    EXTRACTING = "EXTRACTING"
    SOLVING = "SOLVING"
    REVIEWING = "REVIEWING"
    AWAIT_CONFIRM = "AWAIT_CONFIRM"
    SUBMITTING = "SUBMITTING"
    # режим курса: цикл осмотр → решение → действия
    COURSE = "COURSE"
    PAUSED = "PAUSED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = (State.DONE, State.FAILED, State.CANCELLED)
# задача в этих состояниях занимает слот: следующая из очереди не стартует
BUSY = (
    State.EXTRACTING, State.SOLVING, State.REVIEWING, State.AWAIT_CONFIRM, State.SUBMITTING, State.COURSE, State.PAUSED,
)
RUNNING = (State.EXTRACTING, State.SOLVING, State.REVIEWING, State.SUBMITTING, State.COURSE)


class Notifier(Protocol):
    async def status(self, task: Task, text: str) -> None: ...
    async def preview(self, task: Task, task_text: str, answer: str) -> None: ...
    async def message(self, task: Task, text: str) -> None: ...
    async def photo(self, task: Task, path: Path, caption: str) -> None: ...
    async def course_preview(self, task: Task, shots: list[Path], answers: list[str], button: str) -> None: ...


class Sessions(Protocol):
    async def ensure_session(self, profile, url: str) -> Path | None: ...
    async def screenshot(self, url: str, storage: Path | None, path: Path) -> Path | None: ...


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        boss: Boss,
        worker: Worker,
        notifier: Notifier,
        sessions: Sessions | None = None,
        course: CourseRunner | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.boss = boss
        self.worker = worker
        self.notifier = notifier
        self.sessions = sessions
        self.course = course
        self._lock = asyncio.Lock()
        self._runner: asyncio.Task | None = None

    # --- пути ---
    def task_dir(self, task_id: int) -> Path:
        """Notebook and the worker's workspace (TASKS_DIR, outside the project)."""
        return self.settings.tasks_dir / str(task_id)

    def boss_dir(self, task_id: int) -> Path:
        """Boss-only files: private notes, answers, screenshots (DATA_DIR)."""
        return self.settings.boss_root / str(task_id)

    def notebook(self, task_id: int) -> Notebook:
        return Notebook(self.task_dir(task_id) / "notebook.md")

    def _json(self, task_id: int, name: str) -> Path:
        return self.boss_dir(task_id) / name

    def load_private(self, task_id: int) -> dict[str, Any]:
        return self._read_json(self._json(task_id, "boss_private.json"))

    def save_private(self, task_id: int, data: dict[str, Any]) -> None:
        self._write_json(self._json(task_id, "boss_private.json"), data)

    def load_answers(self, task_id: int) -> dict[str, Any]:
        data = self._read_json(self._json(task_id, "answers.json"))
        data.setdefault("expected", [1])
        data.setdefault("pending", list(data["expected"]))
        data.setdefault("answers", {})
        data["answers"] = {int(k): v for k, v in data["answers"].items()}
        return data

    def save_answers(self, task_id: int, data: dict[str, Any]) -> None:
        self._write_json(self._json(task_id, "answers.json"), data)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- публичные команды ---
    async def submit_text(self, text: str) -> Task:
        return await self._submit("text", text)

    async def submit_url(self, url: str) -> Task:
        return await self._submit("url", url)

    async def submit_course(self, url: str, instructions: str = "") -> Task:
        """/course url | указания: указания на этот курс уходят воркеру вместе с правилами."""
        if not self.course:
            raise RuntimeError("режим курса не настроен")
        return await self._submit("course", url + (f" | {instructions.strip()}" if instructions.strip() else ""))

    async def _submit(self, kind: str, text: str) -> Task:
        task = await self.db.create_task(kind, text, State.QUEUED)
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
        if task.kind == "course":
            # курьер нажимает отложенные кнопки сдачи, курс идёт дальше
            await self.db.add_event(task_id, "owner_submit")
            await self.db.update_task(task_id, state=State.SUBMITTING)
            self._start(task_id)
            return True
        # Этап 2: сдачи на платформе нет — «Отправить» просто завершает задачу
        await self._finish(task, State.DONE)
        await self._kick()
        return True

    async def request_fix(self, task_id: int, text: str) -> bool:
        """Owner's correction from Telegram: goes to the notebook as the client's remark."""
        task = await self.db.get_task(task_id)
        if not task or task.state != State.AWAIT_CONFIRM:
            return False
        if task.kind == "course":
            # замечания владельца — воркеру в следующий вход, потом новое превью
            await self.course.fix(task, text)
            await self.db.add_event(task_id, "owner_fix")
            await self.db.update_task(task_id, state=State.COURSE)
            self._start(task_id)
            return True
        self.notebook(task_id).append(CLIENT, text)
        answers = self.load_answers(task_id)
        answers["pending"] = list(answers["expected"])
        self.save_answers(task_id, answers)
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
        elif task.kind == "course":
            await self.course.add_remark(task, text)
            await self.db.add_event(task.id, "owner_remark")
        else:
            self.notebook(task.id).append(CLIENT, text)
            await self.db.add_event(task.id, "owner_remark")
        return task

    async def cancel(self, task_id: int | None = None) -> Task | None:
        task = await self.db.get_task(task_id) if task_id else await self.active_task()
        if not task or task.state in TERMINAL:
            return None
        await self._halt(task)
        task = await self.db.update_task(task.id, state=State.CANCELLED)
        await self.db.add_event(task.id, "cancelled")
        await self.notifier.status(task, f"Задача #{task.id} отменена.")
        await self._kick()
        return task

    async def _halt(self, task: Task) -> None:
        """Stop the running step right away (agent processes are killed) and close the course browser."""
        if self._runner and not self._runner.done() and getattr(self._runner, "task_id", None) == task.id:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
        if self.course and self.course.task_id == task.id:
            await self.course.close()

    async def stop(self) -> Task | None:
        """/stop: a course goes to pause (/resume continues from the same place); other tasks are cancelled."""
        task = await self.active_task()
        if not task:
            return None
        if task.kind != "course":
            return await self.cancel(task.id)
        if task.state == State.PAUSED:
            # уже на паузе — второй /stop отменяет курс совсем
            return await self.cancel(task.id)
        return await self.pause_course(task.id, "по команде /stop")

    async def pause_course(self, task_id: int, why: str) -> Task | None:
        """Pause a course (/stop, «Отмена» on the preview): the pending submission is dropped."""
        task = await self.db.get_task(task_id)
        if not task or task.kind != "course" or task.state in (*TERMINAL, State.PAUSED):
            return None
        await self._halt(task)
        await self.course.drop_pending(task)
        task = await self.db.update_task(task_id, state=State.PAUSED, resume_state=State.COURSE)
        await self.db.add_event(task_id, "paused", why)
        await self.notifier.status(task, f"Курс #{task_id} на паузе ({why}). Продолжить — /resume, отменить — /stop")
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
        (self.task_dir(task.id) / "workspace").mkdir(parents=True, exist_ok=True)
        self.boss_dir(task.id).mkdir(parents=True, exist_ok=True)
        if task.kind == "course":
            await self.course.start(task)
            await self.db.update_task(task.id, state=State.COURSE)
            return
        if task.kind == "url":
            self.save_private(task.id, {"source": "url", "url": task.input, "host": host_of(task.input)})
            await self.db.update_task(task.id, state=State.EXTRACTING, round=1)
            return
        if task.kind == "part":
            # часть большой страницы: private и текст подготовил родитель
            text = self._json(task.id, "worker_task.md").read_text(encoding="utf-8")
            self.notebook(task.id).append(CLIENT, text)
        else:
            self.notebook(task.id).append(CLIENT, task.input)
            self.save_private(task.id, {"source": task.kind})
            self.save_answers(task.id, {"expected": [1], "pending": [1], "answers": {}})
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
        except CoursePause as exc:
            await self._pause(task_id, exc.event, exc.text, exc.text)
        except AgentLimit as exc:
            await self._pause(task_id, "limit", str(exc), "упёрлись в лимит подписки. Продолжить — /resume")
        except SessionExpired as exc:
            url = self.load_private(task_id).get("url", "")
            await self._pause(
                task_id,
                "session_expired",
                exc.reason,
                f"нет входа на {exc.host} ({exc.reason}).\n\n"
                f"Сделай state.json на Windows:\n  npx playwright open --save-storage=state.json {url}\n"
                f"Залогинься, закрой окно и пришли файл state.json сюда (подпись: {exc.host}). Потом /resume.",
            )
        except LoginFailed as exc:
            await self._fail(task_id, f"вход не удался: {exc}")
        except AgentTimeout as exc:
            await self._fail(task_id, f"таймаут: {exc}")
        except Exception as exc:  # noqa: BLE001 — любая ошибка шага завершает задачу
            log.exception("task %s failed", task_id)
            await self._fail(task_id, str(exc)[:500])
        finally:
            if not cancelled:
                task = await self.db.get_task(task_id)
                # браузер курса живёт, только пока курс идёт или ждёт «Отправить»
                if self.course and self.course.task_id == task_id and (
                    not task or task.state not in (State.COURSE, State.SUBMITTING, State.AWAIT_CONFIRM)
                ):
                    await self.course.close()
                if task and task.state in TERMINAL:
                    asyncio.create_task(self._kick())

    async def _pause(self, task_id: int, event: str, detail: str, text: str) -> None:
        task = await self.db.get_task(task_id)
        assert task
        task = await self.db.update_task(task_id, state=State.PAUSED, resume_state=task.state)
        await self.db.add_event(task_id, event, detail[:300])
        await self.notifier.status(task, f"Задача #{task_id} на паузе: {text}")

    async def _loop(self, task_id: int) -> None:
        max_rounds = self.settings.max_rounds
        while True:
            task = await self.db.get_task(task_id)
            assert task
            if task.state == State.EXTRACTING:
                if not await self._extract(task):
                    return
            elif task.state == State.SOLVING:
                await self.notifier.status(task, f"{self._title(task)} · раунд {task.round}/{max_rounds} · исполнитель решает")
                await self._solve(task)
                await self.db.update_task(task_id, state=State.REVIEWING)
            elif task.state == State.REVIEWING:
                await self.notifier.status(task, f"{self._title(task)} · раунд {task.round}/{max_rounds} · куратор проверяет")
                if await self._review(task):
                    return
            elif task.state == State.COURSE:
                outcome = await self.course.run(task)
                if outcome == "done":
                    await self._course_done(task)
                    return
                await self.db.update_task(task_id, state=State.AWAIT_CONFIRM)
                return
            elif task.state == State.SUBMITTING and task.kind == "course":
                await self.course.submit(task)
                await self.db.update_task(task_id, state=State.COURSE)
            else:
                return

    async def _course_done(self, task: Task) -> None:
        st = await self.db.get_course(task.id)
        task = await self.db.update_task(task.id, state=State.DONE)
        await self.db.add_event(task.id, "done")
        await self.notifier.status(task, f"Курс #{task.id} · пройден")
        await self.notifier.message(task, self.course.report(task, st))

    @staticmethod
    def _title(task: Task) -> str:
        return f"Задача #{task.id}" + (f" (часть {task.part})" if task.part else "")

    # --- браузер: вход и извлечение ---
    async def _session(self, task: Task) -> Path | None:
        if not self.sessions:
            raise RuntimeError("браузер не настроен")
        url = self.load_private(task.id)["url"]
        return await self.sessions.ensure_session(profile_for(url, self.settings), url)

    def _browser(self, task_id: int, storage: Path | None) -> BrowserOpts:
        # guard=True: на извлечении кнопки сдачи заблокированы
        return BrowserOpts(storage_state=storage, output_dir=self.boss_dir(task_id) / "screens", guard=True)

    async def _extract(self, task: Task) -> bool:
        """EXTRACTING. Returns False when the task failed here."""
        private = self.load_private(task.id)
        url, host = private["url"], private["host"]
        await self.notifier.status(task, f"Задача #{task.id} · открываю страницу")
        storage = await self._session(task)
        await self.notifier.status(task, f"Задача #{task.id} · куратор извлекает задание")
        result = await self.boss.extract(self.boss_dir(task.id), url, self._browser(task.id, storage))
        await self.db.add_event(task.id, "extract", {"status": result.status, "questions": len(result.questions)})
        if result.status == "login_required":
            if profile_for(url, self.settings).kind == "state":
                raise SessionExpired(host, "куратор попал на страницу входа")
            raise LoginFailed(f"{host}: после входа по форме куратор видит страницу входа")
        if result.status != "ok" or not result.questions:
            await self._fail(task.id, f"задание не извлечено: {result.error or result.status}")
            return False

        await self._send_screenshot(task, result.attempt_url or url, storage)
        parts = chunk(result.questions, self.settings.max_questions)
        base = {
            **private,
            "platform": result.platform,
            "page_title": result.page_title,
            "attempt_url": result.attempt_url,
            "attempts_left": result.attempts_left,
            "private_notes": result.private_notes,
            "parts": len(parts),
        }
        total = len(parts)
        for i, qs in enumerate(parts[1:], 2):
            child = await self.db.create_task("part", url, State.QUEUED, parent_id=task.id, part=f"{i}/{total}")
            self._init_part(child.id, base, qs, f"{i} из {total}")
        part = f"1/{total}" if total > 1 else None
        if part:
            task = await self.db.update_task(task.id, part=part)
        self._init_part(task.id, base, parts[0], f"1 из {total}" if part else None)
        self.notebook(task.id).append(CLIENT, self._json(task.id, "worker_task.md").read_text(encoding="utf-8"))
        await self.db.update_task(task.id, state=State.SOLVING)
        if total > 1:
            await self.notifier.message(task, f"На странице {len(result.questions)} вопросов — делю на {total} части.")
        return True

    def _init_part(self, task_id: int, base: dict[str, Any], questions: list[Question], part: str | None) -> None:
        # в private — всё, включая увиденные правильные и прошлые ответы; воркеру — только условие
        self.save_private(task_id, {**base, "part": part, "questions": [q.model_dump() for q in questions]})
        nums = [q.n for q in questions]
        self.save_answers(task_id, {"expected": nums, "pending": nums, "answers": {}})
        self._json(task_id, "worker_task.md").write_text(render_task(questions, part), encoding="utf-8")

    async def _send_screenshot(self, task: Task, url: str, storage: Path | None) -> None:
        path = self.boss_dir(task.id) / "screens" / "extract.png"
        if not path.exists() and self.sessions:
            await self.sessions.screenshot(url, storage, path)
        if path.exists():
            await self.notifier.photo(task, path, f"Задача #{task.id} · страница задания")

    # --- решение и проверка ---
    def _banned(self, task_id: int) -> list[str]:
        private = self.load_private(task_id)
        words = [private.get("platform") or "", private.get("host") or ""]
        host = private.get("host") or ""
        if ":" in host:
            words.append(host.split(":")[0])
        return [w for w in words if w]

    async def _solve(self, task: Task) -> None:
        text = sanitize.for_worker(self.notebook(task.id).read(), self._banned(task.id))
        reply = await self.worker.solve(text, self.task_dir(task.id) / "workspace")
        self.notebook(task.id).append(EXECUTOR, sanitize.for_boss(reply))
        answers = self.load_answers(task.id)
        answers["answers"].update(parse_answers(reply, answers["pending"]))
        self.save_answers(task.id, answers)
        await self.db.add_event(task.id, "worker_reply", {"chars": len(reply)})

    async def _review(self, task: Task) -> bool:
        """REVIEWING. Returns True when the task leaves the loop (accepted or failed)."""
        max_rounds = self.settings.max_rounds
        answers = self.load_answers(task.id)
        pending = answers["pending"]
        private = {
            **self.load_private(task.id),
            "under_review": pending,
            "current_answers": {str(n): answers["answers"].get(n, "") for n in answers["expected"]},
        }
        result = await self.boss.review(
            self.boss_dir(task.id), self.notebook(task.id).read(), private, task.round, max_rounds
        )
        await self.db.add_event(task.id, "review", result.model_dump())

        verdicts = {v.n: v for v in result.questions if v.n in pending}
        for n in pending:
            if n not in verdicts:
                verdicts[n] = QuestionVerdict(n=n, ok=result.verdict == "accept", comment=result.comment_for_worker)
        for v in verdicts.values():
            if v.ok and v.answer:
                answers["answers"][v.n] = v.answer
        if len(answers["expected"]) == 1 and result.verdict == "accept" and result.final_answer:
            answers["answers"][answers["expected"][0]] = result.final_answer
        wrong = sorted(n for n, v in verdicts.items() if not v.ok)

        if not wrong:
            answers["pending"] = []
            self.save_answers(task.id, answers)
            final = render_answers({n: answers["answers"].get(n, "") for n in answers["expected"]})
            await self._accepted(task, final)
            return True
        if task.round >= max_rounds:
            await self._fail(task.id, f"превышено число раундов ({max_rounds})")
            return True

        answers["pending"] = wrong
        self.save_answers(task.id, answers)
        self.notebook(task.id).append(CLIENT, self._remark(answers["expected"], wrong, verdicts, result.comment_for_worker))
        task = await self.db.update_task(task.id, state=State.SOLVING, round=task.round + 1)
        await self.notifier.status(
            task, f"{self._title(task)} · раунд {task.round}/{max_rounds} · куратор вернул на доработку: {len(wrong)}"
        )
        return False

    @staticmethod
    def _remark(expected: list[int], wrong: list[int], verdicts: dict[int, QuestionVerdict], general: str) -> str:
        if len(expected) == 1:
            return verdicts[wrong[0]].comment or general or "Перепроверь решение."
        lines = ["Замечания по вопросам:"]
        for n in wrong:
            comment = verdicts[n].comment or general or "перепроверь."
            lines.append(comment if comment.lower().startswith(f"вопрос {n}") else f"Вопрос {n}: {comment}")
        nums = ", ".join(map(str, wrong))
        lines.append(
            f"\nОстальные ответы приняты. Ответь заново только на вопросы {nums} — "
            "в блоке «ОТВЕТ» в формате «номер: ответ»."
        )
        return "\n".join(lines)

    async def _accepted(self, task: Task, final_answer: str) -> None:
        task = await self.db.update_task(task.id, final_answer=final_answer)
        if self.settings.auto_submit:
            await self._finish(task, State.DONE)
            return
        task = await self.db.update_task(task.id, state=State.AWAIT_CONFIRM)
        await self.notifier.status(task, f"{self._title(task)} · ответ готов, жду подтверждения")
        await self.notifier.preview(task, self._summary(task), final_answer)

    def _summary(self, task: Task) -> str:
        if task.kind == "text":
            return task.input
        private = self.load_private(task.id)
        n = len(self.load_answers(task.id)["expected"])
        title = private.get("page_title") or ""
        return f"{private.get('url', task.input)}\n{title}\nВопросов: {n}".strip()

    async def _finish(self, task: Task, state: State) -> None:
        # Этап 2: сдачи на платформе нет — «Отправить» просто завершает задачу
        task = await self.db.update_task(task.id, state=state)
        await self.db.add_event(task.id, "done")
        await self.notifier.status(task, f"{self._title(task)} · готово")
        await self.notifier.message(task, f"{self._title(task)} готова.\n\nОтвет:\n{task.final_answer or '—'}")

    async def _fail(self, task_id: int, reason: str) -> None:
        task = await self.db.update_task(task_id, state=State.FAILED, error=reason)
        await self.db.add_event(task_id, "failed", reason)
        await self.notifier.status(task, f"{self._title(task)} не выполнена: {reason}")
