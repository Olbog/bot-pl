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

-- Наборы слов для тренировки
CREATE TABLE IF NOT EXISTS sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'done'
    suggested_next INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS set_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    pl TEXT NOT NULL,
    translit TEXT NOT NULL DEFAULT '',
    ru TEXT NOT NULL DEFAULT '',
    pos TEXT NOT NULL DEFAULT '',
    mastered_at REAL,                        -- NULL — не освоено
    mastered_by TEXT,                        -- 'auto' | 'user'
    review_stage INTEGER NOT NULL DEFAULT 0, -- для повторения в свободном режиме
    last_review_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS word_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    form TEXT NOT NULL,
    correct INTEGER NOT NULL,
    day TEXT NOT NULL,                       -- YYYY-MM-DD по местному времени
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_word_uses_word ON word_uses(word_id, id);
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'free',       -- 'free' | 'set'
    pending TEXT                             -- JSON незавершённого шага (создание набора)
);
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

    # ---------- состояние пользователя ----------

    def get_state(self, user_id: int) -> dict:
        row = self.conn.execute("SELECT mode, pending FROM user_state WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return {"mode": "free", "pending": None}
        return {"mode": row["mode"], "pending": json.loads(row["pending"]) if row["pending"] else None}

    def _ensure_state(self, user_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO user_state(user_id) VALUES (?)", (user_id,))

    def set_mode(self, user_id: int, mode: str) -> None:
        self._ensure_state(user_id)
        self.conn.execute("UPDATE user_state SET mode=? WHERE user_id=?", (mode, user_id))
        self.conn.commit()

    def set_pending(self, user_id: int, pending: dict | None) -> None:
        self._ensure_state(user_id)
        self.conn.execute("UPDATE user_state SET pending=? WHERE user_id=?",
                          (json.dumps(pending, ensure_ascii=False) if pending else None, user_id))
        self.conn.commit()

    # ---------- наборы ----------

    def active_set(self, user_id: int):
        return self.conn.execute(
            "SELECT * FROM sets WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()

    def create_set(self, user_id: int, title: str, words: list[dict], carry_ids: list[int]) -> int:
        """Новый активный набор: старый закрывается, неосвоенные слова carry_ids переезжают в новый."""
        now = time.time()
        c = self.conn
        c.execute("UPDATE sets SET status='done' WHERE user_id=? AND status='active'", (user_id,))
        set_id = c.execute("INSERT INTO sets(user_id, title, created_at) VALUES (?,?,?)",
                           (user_id, title, now)).lastrowid
        for w in words:
            c.execute("INSERT INTO set_words(set_id, user_id, pl, translit, ru, pos, created_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (set_id, user_id, w.get("pl", "").strip(), w.get("translit", ""), w.get("ru", ""),
                       w.get("pos", ""), now))
        for wid in carry_ids:
            c.execute("UPDATE set_words SET set_id=? WHERE id=? AND user_id=?", (set_id, wid, user_id))
        c.commit()
        return set_id

    def set_words(self, set_id: int) -> list:
        return self.conn.execute("SELECT * FROM set_words WHERE set_id=? ORDER BY id", (set_id,)).fetchall()

    def word(self, word_id: int):
        return self.conn.execute("SELECT * FROM set_words WHERE id=?", (word_id,)).fetchone()

    def known_words(self, user_id: int) -> list[str]:
        return [r["pl"] for r in self.conn.execute("SELECT pl FROM set_words WHERE user_id=?", (user_id,))]

    def word_uses(self, word_id: int) -> list:
        return self.conn.execute("SELECT * FROM word_uses WHERE word_id=? ORDER BY id", (word_id,)).fetchall()

    def add_use(self, word_id: int, user_id: int, form: str, correct: bool, day: str) -> None:
        self.conn.execute("INSERT INTO word_uses(word_id, user_id, form, correct, day, created_at) "
                          "VALUES (?,?,?,?,?,?)", (word_id, user_id, form, int(correct), day, time.time()))
        self.conn.commit()

    def set_mastered(self, word_id: int, by: str | None) -> None:
        """by=None — снять отметку «освоено»."""
        if by:
            self.conn.execute("UPDATE set_words SET mastered_at=?, mastered_by=?, review_stage=0, "
                              "last_review_at=NULL WHERE id=?", (time.time(), by, word_id))
        else:
            self.conn.execute("UPDATE set_words SET mastered_at=NULL, mastered_by=NULL WHERE id=?", (word_id,))
        self.conn.commit()

    def mastered_words(self, user_id: int) -> list:
        return self.conn.execute(
            "SELECT * FROM set_words WHERE user_id=? AND mastered_at IS NOT NULL ORDER BY id", (user_id,)
        ).fetchall()

    def advance_review(self, word_id: int, at: float) -> None:
        self.conn.execute("UPDATE set_words SET review_stage=review_stage+1, last_review_at=? WHERE id=?",
                          (at, word_id))
        self.conn.commit()

    def mark_suggested(self, set_id: int, value: int = 1) -> None:
        self.conn.execute("UPDATE sets SET suggested_next=? WHERE id=?", (value, set_id))
        self.conn.commit()
