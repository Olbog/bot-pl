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
CREATE TABLE IF NOT EXISTS dict_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    pl TEXT NOT NULL,
    translit TEXT NOT NULL DEFAULT '',
    ru TEXT NOT NULL DEFAULT '',
    used_in_set INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
-- Предложения выражений для ⭐ по конкретному ответу бота
CREATE TABLE IF NOT EXISTS dict_offers (
    msg_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    phrases TEXT NOT NULL,                   -- JSON [{pl, translit, ru}]
    saved TEXT NOT NULL DEFAULT '[]'         -- JSON индексы сохранённых
);
-- Упражнения
CREATE TABLE IF NOT EXISTS exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                      -- words / voice / grammar / rule / errors
    fmt TEXT NOT NULL,                       -- gap / test
    title TEXT NOT NULL,
    items TEXT NOT NULL,                     -- JSON пунктов
    results TEXT,                            -- JSON результатов проверки
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ex_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    exercise_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,                       -- нормализованное предложение — для уникальности
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',      -- new / ok / wrong / unsure
    reused INTEGER NOT NULL DEFAULT 0,       -- 1 — уже снова выдан на повтор
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ex_items_user ON ex_items(user_id, key);
-- Исключения: исправления, которые ученик отметил как неверные («🙅 Не ошибка»)
CREATE TABLE IF NOT EXISTS ignores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    original TEXT NOT NULL,
    correct TEXT NOT NULL,
    created_at REAL NOT NULL
);
-- Лексика из учебника: прогресс по словам юнита (в общий пул ошибок не идёт)
CREATE TABLE IF NOT EXISTS book_stats (
    user_id INTEGER NOT NULL,
    unit TEXT NOT NULL,
    pl TEXT NOT NULL,
    dir TEXT NOT NULL,                       -- pl (PL→RU) / ru (RU→PL)
    streak INTEGER NOT NULL DEFAULT 0,
    right INTEGER NOT NULL DEFAULT 0,
    wrong INTEGER NOT NULL DEFAULT 0,
    last_at REAL,
    PRIMARY KEY (user_id, unit, pl, dir)
);
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'free',       -- 'free' | 'set'
    pending TEXT                             -- JSON незавершённого шага (создание набора)
);
"""


class DB:
    def __init__(self, path: str, clock=time.time):
        self.clock = clock
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Новые колонки для старой базы."""
        adds = {
            "corrections": [("rule", "TEXT"), ("msg_id", "INTEGER"), ("disputed", "INTEGER NOT NULL DEFAULT 0")],
            "set_words": [("kind", "TEXT NOT NULL DEFAULT 'word'"), ("unit", "TEXT")],
            "user_state": [("topic", "TEXT")],
            "word_uses": [("weight", "REAL NOT NULL DEFAULT 1"), ("msg_id", "INTEGER")],
            "exercises": [("tg_msg_id", "INTEGER"), ("topics", "TEXT")],
        }
        for table, cols in adds.items():
            have = self._columns(table)
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def current_session(self, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT id FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        return row["id"] if row else self.new_session(user_id)

    def new_session(self, user_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions(user_id, started_at) VALUES (?, ?)", (user_id, self.clock())
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
                  corrections: list[dict], new_words: list[dict]) -> int:
        """Возвращает id реплики модели — к ней привязаны кнопки под ответом."""
        now = self.clock()
        c = self.conn
        c.execute("INSERT INTO messages(session_id, role, text, created_at) VALUES (?,?,?,?)",
                  (session_id, "user", user_text, now))
        msg_id = c.execute("INSERT INTO messages(session_id, role, text, created_at) VALUES (?,?,?,?)",
                           (session_id, "model", reply_pl, now)).lastrowid
        for w in new_words:
            c.execute("INSERT INTO words(session_id, user_id, ru, pl, translit, created_at) VALUES (?,?,?,?,?,?)",
                      (session_id, user_id, w.get("ru", ""), w.get("pl", ""), w.get("translit", ""), now))
        for k in corrections:
            c.execute("INSERT INTO corrections(session_id, user_id, data, rule, msg_id, created_at) "
                      "VALUES (?,?,?,?,?,?)",
                      (session_id, user_id, json.dumps(k, ensure_ascii=False), k.get("rule"), msg_id, now))
        c.commit()
        return msg_id

    def session_summary(self, session_id: int) -> tuple[list[dict], list[dict]]:
        words = [dict(r) for r in self.conn.execute(
            "SELECT ru, pl, translit FROM words WHERE session_id=? ORDER BY id", (session_id,))]
        corrs = [json.loads(r["data"]) for r in self.conn.execute(
            "SELECT data FROM corrections WHERE session_id=? AND disputed=0 ORDER BY id", (session_id,))]
        return words, corrs

    def message_count(self, session_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='user'", (session_id,)
        ).fetchone()[0]

    # ---------- состояние пользователя ----------

    def get_state(self, user_id: int) -> dict:
        row = self.conn.execute("SELECT mode, pending, topic FROM user_state WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return {"mode": "free", "pending": None, "topic": None}
        return {"mode": row["mode"], "pending": json.loads(row["pending"]) if row["pending"] else None,
                "topic": row["topic"]}

    def _ensure_state(self, user_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO user_state(user_id) VALUES (?)", (user_id,))

    def set_mode(self, user_id: int, mode: str) -> None:
        self._ensure_state(user_id)
        self.conn.execute("UPDATE user_state SET mode=? WHERE user_id=?", (mode, user_id))
        self.conn.commit()

    def set_topic(self, user_id: int, topic: str | None) -> None:
        """Тема свободного разговора (None — без темы)."""
        self._ensure_state(user_id)
        self.conn.execute("UPDATE user_state SET topic=? WHERE user_id=?", (topic, user_id))
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

    def create_set(self, user_id: int, title: str, words: list[dict], carry_ids: list[int] | None = None) -> int:
        """Новый активный набор: старый закрывается (его неосвоенное уходит в «🧳 Недоученные»).
        carry_ids — слова, взятые из «Недоученных»: переезжают в новый набор вместе со своим прогрессом."""
        now = self.clock()
        c = self.conn
        c.execute("UPDATE sets SET status='done' WHERE user_id=? AND status='active'", (user_id,))
        set_id = c.execute("INSERT INTO sets(user_id, title, created_at) VALUES (?,?,?)",
                           (user_id, title, now)).lastrowid
        for w in words:
            c.execute("INSERT INTO set_words(set_id, user_id, pl, translit, ru, pos, kind, unit, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (set_id, user_id, w.get("pl", "").strip(), w.get("translit", ""), w.get("ru", ""),
                       w.get("pos", ""), w.get("kind", "word"), w.get("unit"), now))
        for wid in carry_ids or []:
            c.execute("UPDATE set_words SET set_id=? WHERE id=? AND user_id=?", (set_id, wid, user_id))
        c.commit()
        return set_id

    def leftovers(self, user_id: int) -> list:
        """«🧳 Недоученные»: неосвоенные слова и правила из закрытых наборов, новые сверху."""
        return self.conn.execute(
            "SELECT w.*, s.title AS set_title FROM set_words w JOIN sets s ON s.id = w.set_id "
            "WHERE w.user_id=? AND s.status='done' AND w.mastered_at IS NULL ORDER BY w.id DESC", (user_id,)
        ).fetchall()

    def set_words(self, set_id: int) -> list:
        return self.conn.execute("SELECT * FROM set_words WHERE set_id=? ORDER BY id", (set_id,)).fetchall()

    def word(self, word_id: int):
        return self.conn.execute("SELECT * FROM set_words WHERE id=?", (word_id,)).fetchone()

    def known_words(self, user_id: int) -> list[str]:
        return [r["pl"] for r in self.conn.execute(
            "SELECT pl FROM set_words WHERE user_id=? AND kind!='rule'", (user_id,))]

    def word_uses(self, word_id: int) -> list:
        return self.conn.execute("SELECT * FROM word_uses WHERE word_id=? ORDER BY id", (word_id,)).fetchall()

    def add_use(self, word_id: int, user_id: int, form: str, correct: bool, day: str, weight: float = 1.0,
                msg_id: int | None = None) -> None:
        self.conn.execute("INSERT INTO word_uses(word_id, user_id, form, correct, day, weight, created_at, msg_id) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (word_id, user_id, form, int(correct), day, weight, self.clock(), msg_id))
        self.conn.commit()

    def uses_for_msg(self, msg_id: int) -> list:
        return self.conn.execute("SELECT * FROM word_uses WHERE msg_id=? ORDER BY id", (msg_id,)).fetchall()

    def set_use_correct(self, use_id: int, correct: bool) -> None:
        self.conn.execute("UPDATE word_uses SET correct=? WHERE id=?", (int(correct), use_id))
        self.conn.commit()

    def set_mastered(self, word_id: int, by: str | None) -> None:
        """by=None — снять отметку «освоено»."""
        if by:
            self.conn.execute("UPDATE set_words SET mastered_at=?, mastered_by=?, review_stage=0, "
                              "last_review_at=NULL WHERE id=?", (self.clock(), by, word_id))
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

    # ---------- ошибки ----------

    def _corr_rows(self, where: str, params: tuple, with_disputed: bool = False) -> list[dict]:
        """Ошибки; оспоренные («🙅 Не ошибка») не считаются — только при with_disputed."""
        out = []
        if not with_disputed:
            where = f"({where}) AND disputed=0"
        for r in self.conn.execute(f"SELECT id, data, rule, created_at, user_id, msg_id, disputed FROM corrections "
                                   f"WHERE {where} ORDER BY id", params):
            d = json.loads(r["data"])
            d["rule"] = r["rule"] or d.get("rule")
            d["_id"], d["_at"], d["_user"] = r["id"], r["created_at"], r["user_id"]
            d["_msg"], d["_disputed"] = r["msg_id"], bool(r["disputed"])
            out.append(d)
        return out

    def correction(self, corr_id: int) -> dict | None:
        rows = self._corr_rows("id=?", (corr_id,), with_disputed=True)
        return rows[0] if rows else None

    def corrections_for_msg_all(self, msg_id: int) -> list[dict]:
        return self._corr_rows("msg_id=?", (msg_id,), with_disputed=True)

    def corrections_for_ex(self, user_id: int, ex_id: int, n: int) -> list[dict]:
        return [c for c in self._corr_rows("user_id=? AND msg_id IS NULL", (user_id,), with_disputed=True)
                if c.get("ex_id") == ex_id and c.get("n") == n]

    def set_disputed(self, corr_id: int, disputed: bool, data: dict | None = None) -> None:
        if data is not None:
            clean = {k: v for k, v in data.items() if not k.startswith("_") or k == "_restored"}
            self.conn.execute("UPDATE corrections SET disputed=?, data=? WHERE id=?",
                              (int(disputed), json.dumps(clean, ensure_ascii=False), corr_id))
        else:
            self.conn.execute("UPDATE corrections SET disputed=? WHERE id=?", (int(disputed), corr_id))
        self.conn.commit()

    # ---------- лексика из учебника ----------

    def book_stats(self, user_id: int, unit: str) -> dict:
        return {(r["pl"], r["dir"]): dict(r) for r in self.conn.execute(
            "SELECT * FROM book_stats WHERE user_id=? AND unit=?", (user_id, str(unit)))}

    def book_record(self, user_id: int, unit: str, pl: str, d: str, ok: bool) -> None:
        self.conn.execute(
            "INSERT INTO book_stats(user_id, unit, pl, dir, streak, right, wrong, last_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, unit, pl, dir) DO UPDATE SET "
            "streak = CASE WHEN excluded.right=1 THEN streak+1 ELSE 0 END, "
            "right = right + excluded.right, wrong = wrong + excluded.wrong, last_at = excluded.last_at",
            (user_id, str(unit), pl, d, int(ok), int(ok), int(not ok), self.clock()))
        self.conn.commit()

    # ---------- исключения («🙅 Не ошибка») ----------

    def ignores(self, user_id: int) -> list:
        return self.conn.execute("SELECT * FROM ignores WHERE user_id=? ORDER BY id", (user_id,)).fetchall()

    def ignore_add(self, user_id: int, original: str, correct: str) -> None:
        o, c = original.strip(), correct.strip()
        if not o or not c:
            return
        have = {(r["original"].lower(), r["correct"].lower()) for r in self.ignores(user_id)}
        if (o.lower(), c.lower()) not in have:
            self.conn.execute("INSERT INTO ignores(user_id, original, correct, created_at) VALUES (?,?,?,?)",
                              (user_id, o, c, self.clock()))
            self.conn.commit()

    def ignore_remove(self, user_id: int, ignore_id: int | None = None, pair: tuple[str, str] | None = None) -> None:
        if ignore_id is not None:
            self.conn.execute("DELETE FROM ignores WHERE user_id=? AND id=?", (user_id, ignore_id))
        elif pair:
            self.conn.execute("DELETE FROM ignores WHERE user_id=? AND lower(original)=? AND lower(correct)=?",
                              (user_id, pair[0].strip().lower(), pair[1].strip().lower()))
        self.conn.commit()

    def corrections_since(self, user_id: int, since: float) -> list[dict]:
        return self._corr_rows("user_id=? AND created_at>=?", (user_id, since))

    def corrections_session(self, session_id: int) -> list[dict]:
        return self._corr_rows("session_id=?", (session_id,))

    def corrections_for_msg(self, msg_id: int) -> list[dict]:
        return self._corr_rows("msg_id=?", (msg_id,))

    def corrections_unsorted(self, user_id: int | None = None) -> list[dict]:
        if user_id is None:
            return self._corr_rows("rule IS NULL", ())
        return self._corr_rows("user_id=? AND rule IS NULL", (user_id,))

    def set_correction_rule(self, corr_id: int, rule: str) -> None:
        self.conn.execute("UPDATE corrections SET rule=? WHERE id=?", (rule, corr_id))
        self.conn.commit()

    def words_since(self, user_id: int, since: float) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT ru, pl, translit, created_at FROM words WHERE user_id=? AND created_at>=? ORDER BY id",
            (user_id, since))]

    def words_session(self, session_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT ru, pl, translit, created_at FROM words WHERE session_id=? ORDER BY id", (session_id,))]

    def user_turns_since(self, user_id: int, since: float) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE s.user_id=? AND m.role='user' AND m.created_at>=?", (user_id, since)).fetchone()[0]

    def model_message(self, msg_id: int):
        return self.conn.execute(
            "SELECT m.*, s.user_id FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=?",
            (msg_id,)).fetchone()

    def all_user_ids(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT user_id FROM sessions")]

    # ---------- словарь ⭐ ----------

    def dict_add(self, user_id: int, pl: str, translit: str, ru: str) -> bool:
        """False — такое выражение уже есть."""
        pl = pl.strip()
        if not pl:
            return False
        exists = self.conn.execute("SELECT 1 FROM dict_items WHERE user_id=? AND lower(pl)=lower(?)",
                                   (user_id, pl)).fetchone()
        if exists:
            return False
        self.conn.execute("INSERT INTO dict_items(user_id, pl, translit, ru, created_at) VALUES (?,?,?,?,?)",
                          (user_id, pl, translit, ru, self.clock()))
        self.conn.commit()
        return True

    def dict_remove(self, user_id: int, pl: str) -> None:
        self.conn.execute("DELETE FROM dict_items WHERE user_id=? AND lower(pl)=lower(?)", (user_id, pl.strip()))
        self.conn.commit()

    def dict_items(self, user_id: int, only_unused: bool = False) -> list:
        q = "SELECT * FROM dict_items WHERE user_id=?" + (" AND used_in_set=0" if only_unused else "") + " ORDER BY id"
        return self.conn.execute(q, (user_id,)).fetchall()

    def dict_since(self, user_id: int, since: float) -> list:
        return self.conn.execute("SELECT * FROM dict_items WHERE user_id=? AND created_at>=? ORDER BY id",
                                 (user_id, since)).fetchall()

    def dict_mark_used(self, ids: list[int]) -> None:
        for i in ids:
            self.conn.execute("UPDATE dict_items SET used_in_set=1 WHERE id=?", (i,))
        self.conn.commit()

    def offer_get(self, msg_id: int):
        row = self.conn.execute("SELECT * FROM dict_offers WHERE msg_id=?", (msg_id,)).fetchone()
        if not row:
            return None
        return {"user_id": row["user_id"], "phrases": json.loads(row["phrases"]), "saved": json.loads(row["saved"])}

    def offer_save(self, msg_id: int, user_id: int, phrases: list[dict], saved: list[int]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO dict_offers(msg_id, user_id, phrases, saved) VALUES (?,?,?,?)",
                          (msg_id, user_id, json.dumps(phrases, ensure_ascii=False), json.dumps(saved)))
        self.conn.commit()

    def add_corrections(self, session_id: int, user_id: int, corrections: list[dict]) -> None:
        now = self.clock()
        for k in corrections:
            self.conn.execute("INSERT INTO corrections(session_id, user_id, data, rule, created_at) VALUES (?,?,?,?,?)",
                              (session_id, user_id, json.dumps(k, ensure_ascii=False), k.get("rule"), now))
        self.conn.commit()

    # ---------- упражнения ----------

    def ex_create(self, user_id: int, kind: str, fmt: str, title: str, items: list[dict],
                  topics: list[str] | None = None) -> int:
        now = self.clock()
        ex_id = self.conn.execute(
            "INSERT INTO exercises(user_id, kind, fmt, title, items, created_at, topics) VALUES (?,?,?,?,?,?,?)",
            (user_id, kind, fmt, title, json.dumps(items, ensure_ascii=False), now,
             json.dumps(topics or [], ensure_ascii=False))).lastrowid
        for it in items:
            if it.get("_reuse_id"):
                self.conn.execute("UPDATE ex_items SET reused=1 WHERE id=?", (it["_reuse_id"],))
            self.conn.execute(
                "INSERT INTO ex_items(user_id, exercise_id, kind, key, data, created_at) VALUES (?,?,?,?,?,?)",
                (user_id, ex_id, kind, it["_key"], json.dumps(it, ensure_ascii=False), now))
        self.conn.commit()
        return ex_id

    def ex_get(self, ex_id: int):
        row = self.conn.execute("SELECT * FROM exercises WHERE id=?", (ex_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["items"] = json.loads(d["items"])
        d["results"] = json.loads(d["results"]) if d["results"] else None
        d["topics"] = json.loads(d["topics"]) if d.get("topics") else []
        return d

    def ex_save_results(self, ex_id: int, results: list[dict]) -> None:
        self.conn.execute("UPDATE exercises SET results=? WHERE id=?",
                          (json.dumps(results, ensure_ascii=False), ex_id))
        rows = self.conn.execute("SELECT id FROM ex_items WHERE exercise_id=? ORDER BY id", (ex_id,)).fetchall()
        for row, r in zip(rows, results):
            self.conn.execute("UPDATE ex_items SET status=? WHERE id=?", (r["final"], row["id"]))
        self.conn.commit()

    def ex_set_tg_msg(self, ex_id: int, tg_msg_id: int) -> None:
        self.conn.execute("UPDATE exercises SET tg_msg_id=? WHERE id=?", (tg_msg_id, ex_id))
        self.conn.commit()

    def ex_by_tg_msg(self, user_id: int, tg_msg_id: int):
        row = self.conn.execute("SELECT id FROM exercises WHERE user_id=? AND tg_msg_id=?",
                                (user_id, tg_msg_id)).fetchone()
        return self.ex_get(row["id"]) if row else None

    def ex_seen_keys(self, user_id: int) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT key FROM ex_items WHERE user_id=?", (user_id,))}

    def ex_recent(self, user_id: int, limit: int = 60) -> list[str]:
        return [json.loads(r[0]).get("full_pl", "") for r in self.conn.execute(
            "SELECT data FROM ex_items WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))]

    def ex_recent_topics(self, user_id: int, limit: int = 6, skip: tuple[str, ...] = ()) -> list[tuple[list[str], float]]:
        """Недавние темы: [(темы одного упражнения, когда)] — без повторов, новые первыми.
        У старых упражнений без сохранённых тем берутся правила их пунктов."""
        out, seen = [], set()
        for r in self.conn.execute("SELECT items, topics, created_at FROM exercises WHERE user_id=? "
                                   "ORDER BY id DESC LIMIT 200", (user_id,)):
            topics = json.loads(r["topics"]) if r["topics"] else []
            if not topics:
                counts: dict[str, int] = {}
                for it in json.loads(r["items"]):
                    rule = it.get("rule")
                    if rule and rule not in skip:
                        counts[rule] = counts.get(rule, 0) + 1
                topics = sorted(counts, key=lambda k: -counts[k])[:4]
            key = tuple(sorted(topics))
            if not topics or key in seen:
                continue
            seen.add(key)
            out.append((topics, r["created_at"]))
            if len(out) >= limit:
                break
        return out

    def ex_review_items(self, user_id: int, limit: int, rules_filter: list[str] | None = None) -> list[dict]:
        """Пункты с ошибкой или сомнением, ещё не выданные на повтор."""
        out = []
        for r in self.conn.execute("SELECT id, data FROM ex_items WHERE user_id=? AND status IN ('wrong','unsure') "
                                   "AND reused=0 ORDER BY id", (user_id,)):
            d = json.loads(r["data"])
            if rules_filter and d.get("rule") not in rules_filter:
                continue
            d["_reuse_id"] = r["id"]
            out.append(d)
            if len(out) >= limit:
                break
        return out
