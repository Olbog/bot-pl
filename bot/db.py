"""SQLite: история разговоров, новые слова и ошибки."""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    started_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL,          -- 'user' | 'model'
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    ru TEXT NOT NULL,
    pl TEXT NOT NULL,
    translit TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    data TEXT NOT NULL,          -- JSON: original, correct, translit, ru, why
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, id);
"""


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def current_session(self, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT id FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        return row["id"] if row else self.new_session(user_id)

    def new_session(self, user_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions(user_id, started_at) VALUES (?, ?)", (user_id, time.time())
        )
        self.conn.commit()
        return cur.lastrowid

    def history(self, session_id: int, limit: int) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT role, text FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        hist = [(r["role"], r["text"]) for r in reversed(rows)]
        # Gemini требует, чтобы история начиналась с реплики пользователя.
        while hist and hist[0][0] != "user":
            hist.pop(0)
        return hist

    def save_turn(self, session_id: int, user_id: int, user_text: str, reply_pl: str,
                  corrections: list[dict], new_words: list[dict]) -> None:
        now = time.time()
        c = self.conn
        c.execute("INSERT INTO messages(session_id, role, text, created_at) VALUES (?,?,?,?)",
                  (session_id, "user", user_text, now))
        c.execute("INSERT INTO messages(session_id, role, text, created_at) VALUES (?,?,?,?)",
                  (session_id, "model", reply_pl, now))
        for w in new_words:
            c.execute("INSERT INTO words(session_id, user_id, ru, pl, translit, created_at) VALUES (?,?,?,?,?,?)",
                      (session_id, user_id, w.get("ru", ""), w.get("pl", ""), w.get("translit", ""), now))
        for k in corrections:
            c.execute("INSERT INTO corrections(session_id, user_id, data, created_at) VALUES (?,?,?,?)",
                      (session_id, user_id, json.dumps(k, ensure_ascii=False), now))
        c.commit()

    def session_summary(self, session_id: int) -> tuple[list[dict], list[dict]]:
        words = [dict(r) for r in self.conn.execute(
            "SELECT ru, pl, translit FROM words WHERE session_id=? ORDER BY id", (session_id,))]
        corrs = [json.loads(r["data"]) for r in self.conn.execute(
            "SELECT data FROM corrections WHERE session_id=? ORDER BY id", (session_id,))]
        return words, corrs

    def message_count(self, session_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='user'", (session_id,)
        ).fetchone()[0]
