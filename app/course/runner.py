"""Course mode loop: inspect → decide → act → inspect.

The runner owns the course browser while the task is active (filled answers live in it until the
owner confirms). Position, counters and the pending submission are stored in the DB (courses);
the step journal is internal (boss dir, journal.jsonl) and never reaches the worker as a file.
"""

import hashlib
import json
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.agents.base import Worker, WorkerBadJSON
from app.browser.sites import profile_for
from app.config import Settings
from app.course.browser import CourseBrowser
from app.course.courier import Courier, Executor
from app.course.models import Action, ActionResult, Screen
from app.browser.session import is_login_url
from app.course.netpolicy import host_port, split_course_input
from app.course.policy import plan_actions
from app.course.worker_input import StepLog, banned_words, copy_shots, load_rules, render
from app.db import Database, Task

log = logging.getLogger(__name__)

# после «Отправить» владельца: столько следующих шагов, состоящих только из нажатий кнопок сдачи,
# идут без нового подтверждения — это сводка и окно подтверждения той же сдачи
SUBMIT_FLOW_STEPS = 2
# больше за шаг не выполняем: остальное — следующим шагом (большие тесты на 25+ вопросов)
MAX_ACTIONS_PER_STEP = 60

_SCORE_RE = re.compile(r"(?:балл\w*|оценка|сдан\w*|итог\w*|mark|grade|score)\s*:?\s*\d+(?:[.,]\d+)?\s*(?:из|/|out of)\s*\d+(?:[.,]\d+)?",
                       re.IGNORECASE)


class CoursePause(Exception):
    """Stop the loop: the task goes to PAUSED, the owner gets `text`."""

    def __init__(self, event: str, text: str) -> None:
        super().__init__(text)
        self.event = event
        self.text = text


class CourseNotifier(Protocol):
    async def status(self, task: Task, text: str) -> None: ...
    async def message(self, task: Task, text: str) -> None: ...
    async def photo(self, task: Task, path: Path, caption: str) -> None: ...
    async def course_preview(self, task: Task, shots: list[Path], answers: list[str], button: str) -> None: ...


class Sessions(Protocol):
    async def ensure_session(self, profile, url: str) -> Path | None: ...


def new_state(url: str, instructions: str = "", domains: list[str] | None = None) -> dict[str, Any]:
    return {
        "url": url, "last_url": url, "instructions": instructions,
        # «домены:» из команды — куда можно переходить; external — где вкладка уже была
        "domains": domains or [], "external": [], "title": "", "progress": "", "score": "",
        "step": 0, "submits": 0, "runtime": 0.0,
        "page_url": "", "page_steps": 0, "screen_hash": "", "same_screen": 0,
        "remarks": [], "history": [], "last_results": [], "notes": [],
        "pending": None, "grant": 0, "fail_steps": 0,
    }


def screen_hash(screen: Screen) -> str:
    parts = [screen.url, screen.text, *(f"{e.n}|{e.label}|{e.value}|{e.checked}" for e in screen.elements)]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


class CourseRunner:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        worker: Worker,
        notifier: CourseNotifier,
        sessions: Sessions | None,
        executor: Executor,
    ) -> None:
        self.settings = settings
        self.db = db
        self.worker = worker
        self.notifier = notifier
        self.sessions = sessions
        self.executor = executor
        self.task_id: int | None = None
        self.browser: CourseBrowser | None = None
        self.courier: Courier | None = None

    # --- пути ---
    def boss_dir(self, task_id: int) -> Path:
        return self.settings.boss_root / str(task_id)

    def workspace(self, task_id: int) -> Path:
        return self.settings.tasks_dir / str(task_id) / "workspace"

    def run_dir(self, task_id: int) -> Path:
        # sudo: папка курьера в COURIER_ROOT (группа bitowi-courier); local — рядом с файлами задачи
        if self.settings.courier_mode == "sudo":
            return self.settings.courier_dir(task_id)
        return self.boss_dir(task_id) / "courier"

    # --- жизненный цикл ---
    async def start(self, task: Task) -> None:
        self.workspace(task.id).mkdir(parents=True, exist_ok=True)
        self.boss_dir(task.id).mkdir(parents=True, exist_ok=True)
        await self.db.save_course(task.id, new_state(*split_course_input(task.input)))

    async def close(self) -> None:
        if self.browser:
            try:
                await self.browser.close()
            except Exception:  # noqa: BLE001 — браузер мог уже умереть
                log.warning("course browser close failed", exc_info=True)
        self.browser = None
        self.courier = None
        self.task_id = None

    async def _open(self, task: Task, st: dict[str, Any]) -> bool:
        """Make sure the browser for this task is up. True if it was (re)opened just now."""
        # курсы, начатые до политики доменов
        st.setdefault("domains", [])
        st.setdefault("external", [])
        if self.browser and self.task_id == task.id:
            return False
        await self.close()
        url = st["last_url"] or st["url"]
        storage = None
        if self.sessions:
            storage = await self.sessions.ensure_session(profile_for(st["url"], self.settings), st["url"])
        browser = CourseBrowser(
            self.settings, url, self.boss_dir(task.id) / "profile", storage,
            course_url=st["url"], domains=st.get("domains", []), visited=st.get("external", []),
        )
        await browser.start()
        run_dir = self.run_dir(task.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.browser, self.task_id = browser, task.id
        self.courier = Courier(self.settings, browser, self.executor, run_dir, self.workspace(task.id))
        return True

    # --- владелец ---
    async def add_remark(self, task: Task, text: str) -> None:
        st = await self.db.get_course(task.id)
        st["remarks"].append(text)
        await self.db.save_course(task.id, st)

    async def fix(self, task: Task, text: str) -> None:
        """«Исправить»: the owner's text goes to the worker, the pending submission is dropped."""
        st = await self.db.get_course(task.id)
        st["remarks"].append(text)
        st["pending"] = None
        st["same_screen"] = 0
        await self.db.save_course(task.id, st)

    async def drop_pending(self, task: Task) -> None:
        st = await self.db.get_course(task.id)
        st["pending"] = None
        await self.db.save_course(task.id, st)

    # --- цикл ---
    async def run(self, task: Task) -> str:
        """Steps until the course is done ("done") or a submission waits for the owner ("await_confirm")."""
        st = await self.db.get_course(task.id)
        await self._open(task, st)
        assert self.courier and self.browser
        started = time.monotonic()
        base_runtime = st["runtime"]
        run_steps = 0
        banned = banned_words(self.browser.origin, self.settings.course_banned_words)
        ws = self.workspace(task.id)
        # новый запуск (/resume после подсказки) — счёт неудачных шагов заново
        st["fail_steps"] = 0

        while True:
            st["runtime"] = base_runtime + time.monotonic() - started
            if run_steps >= self.settings.course_max_steps:
                await self._save(task, st)
                raise CoursePause("limit_steps", f"сделано {run_steps} шагов (COURSE_MAX_STEPS). Продолжить — /resume")
            if time.monotonic() - started >= self.settings.course_max_runtime:
                await self._save(task, st)
                raise CoursePause("limit_runtime", "истекло время запуска (COURSE_MAX_RUNTIME). Продолжить — /resume")

            screen = await self.courier.inspect(self.boss_dir(task.id) / "screens")
            await self._track(task, st, screen)
            await self._login_wall(task, st, screen)
            shots = copy_shots(screen, ws)
            results = [ActionResult.model_validate(r) for r in st["last_results"]]
            history = [StepLog.model_validate(h) for h in st["history"]]
            # правила владельца перечитываются на каждом шаге — правки файла действуют без перезапуска
            text = render(screen, shots, results, history, st["remarks"], banned, st["notes"], workspace=ws,
                          rules=load_rules(self.settings.prompts_dir), instructions=st.get("instructions", ""))
            try:
                decision = await self.worker.decide(text, ws)
            except WorkerBadJSON as exc:
                self._save_bad_json(task, st, screen, exc)
                await self._save(task, st)
                raise CoursePause("bad_json", f"ответ исполнителя дважды не прошёл проверку ({exc.errors[-1][:200]}). "
                                              "Сырые ответы — в журнале задачи. Продолжить — /resume") from exc
            st["step"] += 1
            run_steps += 1
            await self.db.add_event(task.id, "course_step", {"step": st["step"], "status": decision.status,
                                                            "actions": len(decision.actions)})

            if decision.status == "course_done":
                self._journal(task, st, screen, decision, [])
                await self._save(task, st)
                return "done"
            if decision.status == "stuck":
                self._journal(task, st, screen, decision, [])
                await self._save(task, st)
                raise CoursePause("stuck", f"исполнитель не может продолжить: {decision.comment or 'без пояснения'}. "
                                           "Ответь на это сообщение подсказкой и /resume")

            notes: list[str] = []
            actions = decision.actions
            if len(actions) > MAX_ACTIONS_PER_STEP:
                # сдача в хвосте не нажимается, пока не заполнено остальное
                actions = actions[:MAX_ACTIONS_PER_STEP]
                notes.append(f"За шаг выполняется не больше {MAX_ACTIONS_PER_STEP} действий — выполнены первые "
                             f"{MAX_ACTIONS_PER_STEP} из {len(decision.actions)}; остальные верни следующим шагом.")
            plan = plan_actions(actions, screen.elements, ws)
            auto = (
                st["grant"] > 0
                and plan.pending_submit
                and all(a.action == "click" for a in plan.pending_submit)
                and not any(a.action in ("type", "select", "upload") for a in plan.run)
            )
            res = await self.courier.execute(plan.run)
            results = plan.rejected + res.results
            await self._net(task, st, notes)
            if res.offsite:
                await self._after(task, st, screen, decision, results, notes)
                raise CoursePause("forbidden", "вкладка попала на запрещённый адрес (локальная сеть) — вернул назад. Продолжить — /resume")

            if plan.pending_submit and (auto or self.settings.auto_submit):
                sub = await self.courier.execute(plan.pending_submit, allow_submit=True)
                results += sub.results
                await self._net(task, st, notes)
                if auto:
                    st["grant"] -= 1
                else:
                    st["submits"] += 1
                    st["grant"] = SUBMIT_FLOW_STEPS
                if sub.offsite:
                    await self._after(task, st, screen, decision, results, notes)
                    raise CoursePause("forbidden", "вкладка попала на запрещённый адрес (локальная сеть) — вернул назад. Продолжить — /resume")
            elif plan.pending_submit:
                await self._after(task, st, screen, decision, results, notes)
                await self._preview(task, st, screen, plan.pending_submit, decision.answers)
                return "await_confirm"
            else:
                st["grant"] = 0
                if decision.status == "submit_ready" and len(actions) == len(decision.actions):
                    notes.append("Статус submit_ready, но среди действий нет кнопки сдачи — "
                                 "добавь её нажатие последним действием.")
            await self._after(task, st, screen, decision, results, notes)
            await self._check_failures(task, st, results)

    async def _net(self, task: Task, st: dict[str, Any], notes: list[str]) -> None:
        """Network policy after the courier: refusals → worker notes, new external domains, the limit."""
        notes += self.browser.take_notes()
        new = [d for d in self.browser.external_domains if d not in st["external"]]
        if new:
            st["external"] += new
            await self.db.add_event(task.id, "course_domains", new)
            self._journal_line(task, {"step": st["step"], "status": "domains", "new": new})
        if self.browser.limit_hit:
            host, self.browser.limit_hit = self.browser.limit_hit, ""
            await self._save(task, st)
            raise CoursePause("domain_limit", f"курс ведёт на {host} — сверх лимита внешних доменов "
                                              f"({self.settings.course_max_external_domains}, уже: {', '.join(st['external'])}). "
                                              "Подскажи ответом, как продолжить, и /resume")

    async def _login_wall(self, task: Task, st: dict[str, Any], screen: Screen) -> None:
        """A login page: outside the course domain the bot is never logged in; on it — the session expired."""
        if not (is_login_url(screen.url) or any(e.kind == "password" for e in screen.elements)):
            return
        await self._save(task, st)
        host = host_port(screen.url)[0]
        if screen.external:
            raise CoursePause("login_external", f"на {host} нужен вход — бот там не залогинен (вход только на "
                                                "домене курса). Подскажи ответом, как продолжить, и /resume")
        raise CoursePause("login_course", f"на {host} просит вход — сессия курса истекла. Пришли state.json "
                                          "(подпись — домен) и /resume")

    async def _check_failures(self, task: Task, st: dict[str, Any], results: list[ActionResult]) -> None:
        """Stuck by errors: steps in a row where not a single action worked (the screen may still change)."""
        if not results:
            return
        if any(r.ok for r in results):
            st["fail_steps"] = 0
            return
        st["fail_steps"] = st.get("fail_steps", 0) + 1
        if st["fail_steps"] >= self.settings.course_fail_repeats:
            await self._save(task, st)
            # владельцу — подробность (слова курьера), воркеру она не идёт
            why = results[0].detail or results[0].error
            raise CoursePause("fail_steps", f"{st['fail_steps']} шага подряд не выполнено ни одно действие "
                                            f"(«{why[:120]}»). Подскажи ответом на это сообщение и /resume")

    async def submit(self, task: Task) -> None:
        """After the owner's «Отправить»: the courier presses the pending submit buttons."""
        st = await self.db.get_course(task.id)
        pending = st.get("pending")
        if not pending:
            return
        fresh = await self._open(task, st)
        assert self.courier
        screen = await self.courier.inspect(self.boss_dir(task.id) / "screens")
        actions = [Action.model_validate(p["action"]) for p in pending["actions"]]
        by_n = {e.n: e for e in screen.elements}
        same = all(
            p["action"].get("element") is None
            or (by_n.get(p["action"]["element"]) is not None and by_n[p["action"]["element"]].label == p["label"])
            for p in pending["actions"]
        )
        st["pending"] = None
        if fresh or not same:
            # браузер перезапускался или страница другая — заполненные ответы не те, что видел владелец
            st["notes"] = ["Ответы перед сдачей не сохранились (страница перезагрузилась) — "
                           "заполни их заново и снова верни submit_ready."]
            await self.db.add_event(task.id, "course_submit_stale")
            await self._save(task, st)
            return
        res = await self.courier.execute(actions, allow_submit=True)
        st["submits"] += 1
        st["grant"] = SUBMIT_FLOW_STEPS
        st["remarks"] = []
        st["last_results"] = [r.model_dump() for r in res.results]
        st["notes"] = []
        await self._net(task, st, st["notes"])
        await self.db.add_event(task.id, "course_submit", {"ok": all(r.ok for r in res.results)})
        await self._save(task, st)
        if res.offsite:
            raise CoursePause("forbidden", "вкладка попала на запрещённый адрес (локальная сеть) — вернул назад. Продолжить — /resume")

    # --- помощники ---
    async def _track(self, task: Task, st: dict[str, Any], screen: Screen) -> None:
        """Stuck detection: the same screen N times in a row, too many steps on one page."""
        h = screen_hash(screen)
        st["same_screen"] = st["same_screen"] + 1 if h == st["screen_hash"] else 1
        st["screen_hash"] = h
        st["page_steps"] = st["page_steps"] + 1 if screen.url == st["page_url"] else 1
        st["page_url"] = screen.url
        st["last_url"] = self.browser.last_url if self.browser else st["last_url"]
        st["title"] = screen.title
        st["progress"] = screen.progress or st["progress"]
        score = next((m.group(0) for m in map(_SCORE_RE.search, [*screen.messages, screen.text]) if m), "")
        key = f"{screen.url}|{score}"
        if score and key != st.get("score_key") and screen.shots:
            # новый результат на экране — скриншот владельцу и в папку задачи (разбор оценок)
            saved = self.boss_dir(task.id) / "results" / f"step{st['step'] + 1}.png"
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(screen.shots[0], saved)
            await self.notifier.photo(task, saved, f"Курс #{task.id} · результат: {score}")
            st["score_key"] = key
        st["score"] = score or st["score"]
        if st["same_screen"] >= self.settings.course_stuck_repeats:
            await self._save(task, st)
            raise CoursePause("stuck_screen", f"тот же экран {st['same_screen']} раза подряд. "
                                              "Подскажи ответом на это сообщение и /resume")
        if st["page_steps"] > self.settings.course_max_steps_per_page:
            await self._save(task, st)
            raise CoursePause("limit_page", f"больше {self.settings.course_max_steps_per_page} шагов на одной странице "
                                            "(COURSE_MAX_STEPS_PER_PAGE). Продолжить — /resume")
        await self.notifier.status(task, self.status_line(task, st))

    def status_line(self, task: Task, st: dict[str, Any]) -> str:
        parts = [f"Курс #{task.id}", f"шаг {st['step'] + 1}"]
        if st["title"]:
            parts.append(st["title"][:80])
        if st["progress"]:
            parts.append(f"прогресс {st['progress']}")
        parts.append(f"сдач: {st['submits']}")
        if st.get("external"):
            parts.append("внешние: " + ", ".join(st["external"]))
        if st["score"]:
            parts.append(f"последний результат: {st['score']}")
        return " · ".join(parts)

    def report(self, task: Task, st: dict[str, Any]) -> str:
        minutes = round(st["runtime"] / 60)
        lines = [
            f"Курс #{task.id} завершён.",
            f"Шагов: {st['step']}, время: {minutes} мин, сдач: {st['submits']}.",
        ]
        if st["progress"]:
            lines.append(f"Прогресс: {st['progress']}")
        if st["score"]:
            lines.append(f"Последний результат: {st['score']}")
        if st.get("external"):
            lines.append("Внешние сайты: " + ", ".join(st["external"]))
        return "\n".join(lines)

    async def _preview(
        self, task: Task, st: dict[str, Any], screen: Screen, pending: list[Action], answers: list[str]
    ) -> None:
        """Screenshots with filled answers + answers + the button — for the owner's decision."""
        assert self.courier
        labels = {e.n: e.label for e in screen.elements}
        st["pending"] = {
            "actions": [{"action": a.model_dump(), "label": labels.get(a.element, "")} for a in pending],
            "answers": answers,
        }
        await self._save(task, st)
        shown = await self.courier.inspect(self.boss_dir(task.id) / "preview")
        button = ", ".join(dict.fromkeys(labels.get(a.element, "") for a in pending if a.element is not None))
        await self.db.add_event(task.id, "course_preview", {"button": button, "answers": len(answers)})
        await self.notifier.course_preview(task, [Path(p) for p in shown.shots], answers, button)

    async def _after(self, task, st, screen, decision, results: list[ActionResult], notes: list[str]) -> None:
        st["last_results"] = [r.model_dump() for r in results]
        st["notes"] = notes
        st["history"] = (st["history"] + [StepLog(n=st["step"], status=decision.status, results=results,
                                                  comment=decision.comment).model_dump()])[-20:]
        self._journal(task, st, screen, decision, results)
        await self._save(task, st)

    async def _save(self, task: Task, st: dict[str, Any]) -> None:
        await self.db.save_course(task.id, st)

    def _save_bad_json(self, task: Task, st: dict[str, Any], screen: Screen, exc: WorkerBadJSON) -> None:
        """Raw replies → boss dir (not seen by the worker), the errors → journal."""
        folder = self.boss_dir(task.id) / "bad_json"
        folder.mkdir(parents=True, exist_ok=True)
        files = []
        for i, reply in enumerate(exc.replies, 1):
            path = folder / f"step{st['step'] + 1}-{i}.txt"
            path.write_text(reply, encoding="utf-8")
            files.append(path.name)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "step": st["step"] + 1, "url": screen.url, "title": screen.title, "status": "bad_json",
            "errors": exc.errors, "replies": files, "reply_chars": [len(r) for r in exc.replies],
            "elements": len(screen.elements),
        }
        with (self.boss_dir(task.id) / "journal.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _journal_line(self, task: Task, entry: dict[str, Any]) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        with (self.boss_dir(task.id) / "journal.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _journal(self, task: Task, st: dict[str, Any], screen: Screen, decision, results: list[ActionResult]) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "step": st["step"], "url": screen.url, "title": screen.title,
            "status": decision.status, "comment": decision.comment, "answers": decision.answers,
            "actions": [a.model_dump() for a in decision.actions],
            "results": [r.model_dump() for r in results],
        }
        path = self.boss_dir(task.id) / "journal.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
