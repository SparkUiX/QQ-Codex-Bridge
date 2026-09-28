"""Small durable state shared by the QQ receiver and the Stop hook."""

import os
import sqlite3
import time
from pathlib import Path


DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexQQ"
DB_PATH = DATA_DIR / "bridge.sqlite3"


class State:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbound (
                dedup_key TEXT PRIMARY KEY, received_at INTEGER NOT NULL,
                state TEXT NOT NULL, thread_id TEXT
            );
            CREATE TABLE IF NOT EXISTS outbound (
                ref_idx TEXT PRIMARY KEY, qq_id TEXT, thread_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sent_turns (
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                PRIMARY KEY(thread_id, turn_id)
            );
            CREATE TABLE IF NOT EXISTS streaming_turns (
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                PRIMARY KEY(thread_id, turn_id)
            );
            CREATE TABLE IF NOT EXISTS bridge_projects (
                thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thread_aliases (
                old_thread_id TEXT PRIMARY KEY, new_thread_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thread_efforts (
                thread_id TEXT PRIMARY KEY, effort TEXT NOT NULL
            );
        """)

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def claim(self, key: str) -> bool:
        cur = self.db.execute("INSERT OR IGNORE INTO inbound VALUES (?, ?, 'received', NULL)", (key, int(time.time())))
        return cur.rowcount == 1

    def mark_inbound(self, key: str, state: str, thread_id: str | None = None) -> None:
        self.db.execute("UPDATE inbound SET state=?, thread_id=? WHERE dedup_key=?", (state, thread_id, key))

    def bind_outbound(self, response: dict, thread_id: str) -> None:
        ref = (response.get("ext_info") or {}).get("ref_idx")
        if ref:
            self.db.execute(
                "INSERT OR REPLACE INTO outbound VALUES (?, ?, ?, ?)",
                (ref, response.get("id"), thread_id, int(time.time())),
            )

    def thread_for_ref(self, ref: str) -> str | None:
        row = self.db.execute("SELECT thread_id FROM outbound WHERE ref_idx=?", (ref,)).fetchone()
        return row[0] if row else None

    def claim_turn(self, thread_id: str, turn_id: str) -> bool:
        cur = self.db.execute("INSERT OR IGNORE INTO sent_turns VALUES (?, ?)", (thread_id, turn_id))
        return cur.rowcount == 1

    def has_sent_turn(self, thread_id: str, turn_id: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM sent_turns WHERE thread_id=? AND turn_id=?",
                                    (thread_id, turn_id)).fetchone())

    def mark_streaming(self, thread_id: str, turn_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO streaming_turns VALUES (?, ?, ?)",
                        (thread_id, turn_id, int(time.time())))

    def is_streaming(self, thread_id: str, turn_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM streaming_turns WHERE thread_id=? AND turn_id=?",
                              (thread_id, turn_id)).fetchone()
        return bool(row)

    def clear_streaming(self, thread_id: str, turn_id: str) -> None:
        self.db.execute("DELETE FROM streaming_turns WHERE thread_id=? AND turn_id=?",
                        (thread_id, turn_id))

    def release_turn(self, thread_id: str, turn_id: str) -> None:
        self.db.execute("DELETE FROM sent_turns WHERE thread_id=? AND turn_id=?", (thread_id, turn_id))

    def bind_project(self, thread_id: str, project_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO bridge_projects VALUES (?, ?)", (thread_id, project_id))

    def project_for_thread(self, thread_id: str) -> str | None:
        row = self.db.execute("SELECT project_id FROM bridge_projects WHERE thread_id=?", (thread_id,)).fetchone()
        return row[0] if row else None

    def set_effort(self, thread_id: str, effort: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO thread_efforts VALUES (?, ?)", (thread_id, effort))

    def effort_for_thread(self, thread_id: str) -> str | None:
        row = self.db.execute("SELECT effort FROM thread_efforts WHERE thread_id=?", (thread_id,)).fetchone()
        return row[0] if row else None

    def resolve_thread(self, thread_id: str) -> str:
        seen = set()
        while thread_id not in seen:
            seen.add(thread_id)
            row = self.db.execute(
                "SELECT new_thread_id FROM thread_aliases WHERE old_thread_id=?", (thread_id,)
            ).fetchone()
            if not row:
                return thread_id
            thread_id = row[0]
        raise ValueError("任务替换记录存在循环")

    def replace_empty_thread(self, old_id: str, new_id: str, project_id: str | None) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT OR REPLACE INTO thread_aliases VALUES (?, ?)", (old_id, new_id)
            )
            if project_id:
                self.db.execute(
                    "INSERT OR REPLACE INTO bridge_projects VALUES (?, ?)", (new_id, project_id)
                )
            else:
                self.db.execute(
                    "INSERT OR REPLACE INTO bridge_projects SELECT ?, project_id FROM bridge_projects WHERE thread_id=?",
                    (new_id, old_id),
                )
            self.db.execute(
                "INSERT OR REPLACE INTO thread_efforts SELECT ?, effort FROM thread_efforts WHERE thread_id=?",
                (new_id, old_id),
            )
            self.db.execute(
                "UPDATE settings SET value=? WHERE key='selected_thread' AND value=?",
                (new_id, old_id),
            )
            self.db.execute(
                "UPDATE outbound SET thread_id=? WHERE thread_id=?", (new_id, old_id)
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()
