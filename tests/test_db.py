import sqlite3

from app.db import Database


async def test_migration_drops_domains_and_cancels_waiting(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, input TEXT NOT NULL,
            state TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 1, retries INTEGER NOT NULL DEFAULT 0,
            final_answer TEXT, error TEXT, resume_state TEXT, status_msg_id INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE domains (domain TEXT PRIMARY KEY, status TEXT NOT NULL, verdict TEXT, summary TEXT,
            sources TEXT, updated_at TEXT NOT NULL);
        INSERT INTO domains VALUES ('x.kz', 'pending', 'unclear', '', '[]', 'now');
        INSERT INTO tasks (kind, input, state, created_at, updated_at) VALUES ('url', 'https://x.kz', 'AWAIT_DOMAIN', 'now', 'now');
        INSERT INTO tasks (kind, input, state, created_at, updated_at) VALUES ('text', '2+2', 'DONE', 'now', 'now');
        """
    )
    con.commit()
    con.close()

    db = Database(path)
    await db.connect()
    waiting, done = await db.get_task(1), await db.get_task(2)
    async with db.conn.execute("SELECT name FROM sqlite_master WHERE name = 'domains'") as cur:
        assert await cur.fetchone() is None
    await db.close()
    assert waiting.state == "CANCELLED" and waiting.parent_id is None
    assert done.state == "DONE"
