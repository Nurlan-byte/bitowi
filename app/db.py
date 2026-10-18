"""SQLite storage for tasks and events. The tasks table doubles as the queue."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    input TEXT NOT NULL,
    state TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    retries INTEGER NOT NULL DEFAULT 0,
    final_answer TEXT,
    error TEXT,
    resume_state TEXT,
    status_msg_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT
);
"""

# колонки, добавленные после этапа 1: (имя, определение)
MIGRATIONS = [
    ("parent_id", "INTEGER"),
    ("part", "TEXT"),
]

TASK_FIELDS = {
    "state", "round", "retries", "final_answer", "error", "resume_state", "status_msg_id", "part",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    id: int
    kind: str
    input: str
    state: str
    round: int
    retries: int
    final_answer: str | None
    error: str | None
    resume_state: str | None
    status_msg_id: int | None
    created_at: str
    updated_at: str
    parent_id: int | None = None
    part: str | None = None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        async with self.conn.execute("PRAGMA table_info(tasks)") as cur:
            existing = {row["name"] for row in await cur.fetchall()}
        for name, ddl in MIGRATIONS:
            if name not in existing:
                await self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
        # проверку правил на домене убрали: таблица и ожидавшие решения задачи больше не нужны
        await self.conn.execute("DROP TABLE IF EXISTS domains")
        await self.conn.execute(
            "UPDATE tasks SET state = 'CANCELLED', error = 'проверка домена убрана'"
            " WHERE state IN ('CHECKING_POLICY', 'AWAIT_DOMAIN')"
        )

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database is not connected"
        return self._conn

    async def create_task(
        self, kind: str, text: str, state: str, parent_id: int | None = None, part: str | None = None
    ) -> Task:
        now = _now()
        cur = await self.conn.execute(
            "INSERT INTO tasks (kind, input, state, parent_id, part, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, text, state, parent_id, part, now, now),
        )
        await self.conn.commit()
        task = await self.get_task(cur.lastrowid)
        assert task is not None
        return task

    async def get_task(self, task_id: int) -> Task | None:
        async with self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
        return Task(**dict(row)) if row else None

    async def update_task(self, task_id: int, **fields: Any) -> Task:
        unknown = set(fields) - TASK_FIELDS
        if unknown:
            raise ValueError(f"Unknown task fields: {unknown}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(
            f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?",
            (*fields.values(), _now(), task_id),
        )
        await self.conn.commit()
        task = await self.get_task(task_id)
        assert task is not None
        return task

    async def tasks_in_states(self, states: tuple[str, ...]) -> list[Task]:
        marks = ", ".join("?" for _ in states)
        async with self.conn.execute(
            f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY id", states
        ) as cur:
            rows = await cur.fetchall()
        return [Task(**dict(r)) for r in rows]

    async def last_tasks(self, limit: int = 10) -> list[Task]:
        async with self.conn.execute(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [Task(**dict(r)) for r in rows]

    async def add_event(self, task_id: int, kind: str, payload: Any = None) -> None:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False)
        await self.conn.execute(
            "INSERT INTO events (task_id, ts, kind, payload) VALUES (?, ?, ?, ?)",
            (task_id, _now(), kind, data),
        )
        await self.conn.commit()
