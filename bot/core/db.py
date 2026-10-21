"""SQLite: история разговоров, новые слова и ошибки."""
import json
import sqlite3
import time
from pathlib import Path

from ..settings import DICT_SET_SIZE

DICT_OTHER = "Разное"     # тема словаря по умолчанию (слово без темы)

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
-- Темы словаря ⭐: слово словаря лежит в наборе своей темы («⭐ Еда 1», по DICT_SET_SIZE слов)
CREATE TABLE IF NOT EXISTS dict_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
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
-- Упражнения из книги (учебник / рабочая тетрадь): результат по каждому пункту, можно делать частями
CREATE TABLE IF NOT EXISTS book_ex (
    user_id INTEGER NOT NULL,
    unit TEXT NOT NULL,                      -- «kpk:8»
    ex_id TEXT NOT NULL,                     -- id упражнения в файле юнита
    n INTEGER NOT NULL,                      -- номер пункта
    ok INTEGER NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    explanation TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL,
    manual INTEGER NOT NULL DEFAULT 0,       -- ✋ засчитан пользователем вручную
    PRIMARY KEY (user_id, unit, ex_id, n)
);
CREATE TABLE IF NOT EXISTS book_ex_done (    -- упражнение из книги выполнено на 100%: ответы этой попытки
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    unit TEXT NOT NULL,
    ex_id TEXT NOT NULL,
    answers TEXT NOT NULL,                   -- JSON {номер пункта: ответ}
    at REAL NOT NULL,
    manual TEXT NOT NULL DEFAULT '[]'        -- JSON [номера пунктов, засчитанных вручную ✋]
);
CREATE TABLE IF NOT EXISTS read_done (       -- 📚 чтение: показанный абзац главы
    user_id INTEGER NOT NULL,
    unit TEXT NOT NULL,                      -- «leg:1»
    idx INTEGER NOT NULL,                    -- номер абзаца с 0
    at REAL NOT NULL,
    PRIMARY KEY (user_id, unit, idx)
);
CREATE TABLE IF NOT EXISTS read_pos (        -- 📚 где остановился в книге
    user_id INTEGER NOT NULL,
    book TEXT NOT NULL,
    unit TEXT NOT NULL,
    idx INTEGER NOT NULL,
    PRIMARY KEY (user_id, book)
);
CREATE TABLE IF NOT EXISTS zv_status (       -- 🗣 скороговорки: 0 ⚪ не начато, 1 🟡 работаю, 2 ✅ готово
    user_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    status INTEGER NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (user_id, item)
);
CREATE TABLE IF NOT EXISTS rule_cache (       -- 📖 объяснённые правила к упражнениям из книги (ключ — с хэшем текста)
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'free',       -- 'free' | 'set'
    pending TEXT                             -- JSON незавершённого шага (создание набора)
);
"""


def unit_key(unit) -> str:
    """Ключ юнита «книга:номер»; старое «8» (без книги) — Krok po kroku."""
    unit = str(unit)
    return unit if ":" in unit else f"kpk:{unit}"


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
            # familiar_at — 👌 «знакомое»: ученик уже свободно использует, показывать реже (до освоения)
            "set_words": [("kind", "TEXT NOT NULL DEFAULT 'word'"), ("unit", "TEXT"), ("familiar_at", "REAL")],
            "user_state": [("topic", "TEXT")],
            "word_uses": [("weight", "REAL NOT NULL DEFAULT 1"), ("msg_id", "INTEGER")],
            "exercises": [("tg_msg_id", "INTEGER"), ("topics", "TEXT"), ("cfg", "TEXT")],
            # выученность в учебнике в два уровня: test — узнаю (выбор из вариантов), typed — знаю (написал сам)
            "book_stats": [("test_streak", "INTEGER NOT NULL DEFAULT 0"), ("typed_streak", "INTEGER NOT NULL DEFAULT 0"),
                           ("last_ok", "INTEGER NOT NULL DEFAULT 1")],
            # ✋ пункт упражнения из книги засчитан пользователем вручную
            "book_ex": [("manual", "INTEGER NOT NULL DEFAULT 0")],
            "book_ex_done": [("manual", "TEXT NOT NULL DEFAULT '[]'")],
            # ⭐ словарь по темам: тема слова и его строка в наборе темы; набор темы — sets.topic_id
            "dict_items": [("topic_id", "INTEGER"), ("word_id", "INTEGER"), ("src", "TEXT NOT NULL DEFAULT ''")],
            "sets": [("topic_id", "INTEGER")],
            # меню «⭐ В словарь»: мои ошибки, подобрано из ответа бота, отмеченное, своя тема («» — 🤖 сам)
            "dict_offers": [("errs", "TEXT NOT NULL DEFAULT '[]'"), ("sel", "TEXT NOT NULL DEFAULT '[]'"),
                            ("bot_ready", "INTEGER NOT NULL DEFAULT 0"), ("topic", "TEXT NOT NULL DEFAULT ''")],
        }
        two_level = "test_streak" not in self._columns("book_stats")
        for table, cols in adds.items():
            have = self._columns(table)
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # ключи юнитов стали «книга:номер»: старые «8» — это Krok po kroku
        self.conn.execute("UPDATE book_stats SET unit = 'kpk:' || unit WHERE unit NOT LIKE '%:%'")
        self.conn.execute("UPDATE set_words SET unit = 'kpk:' || unit WHERE unit IS NOT NULL AND unit NOT LIKE '%:%'")
        # слова словаря без темы (из старой базы) — в тему «Разное», каждое в набор этой темы
        for r in self.conn.execute("SELECT id, user_id FROM dict_items WHERE word_id IS NULL").fetchall():
            self._dict_place(r["user_id"], r["id"], DICT_OTHER)
        if two_level:  # прошлый прогресс по учебнику засчитываем как «узнаю», «знаю» копится заново
            self.conn.execute("UPDATE book_stats SET test_streak = streak, "
                              "last_ok = CASE WHEN streak = 0 AND wrong > 0 THEN 0 ELSE 1 END")

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
        """Новый активный набор: старый уходит в 📚 архив со всем прогрессом (оттуда его можно вернуть).
        carry_ids — перенести существующие слова в новый набор (сейчас не используется)."""
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

    def sets_archive(self, user_id: int) -> list[dict]:
        """📚 Архив: все наборы, кроме активного, с прогрессом и временем последней тренировки, новые сверху."""
        rows = self.conn.execute(
            "SELECT s.id, s.title, s.created_at, COUNT(w.id) AS total, "
            "SUM(CASE WHEN w.mastered_at IS NOT NULL THEN 1 ELSE 0 END) AS done, "
            "MAX(COALESCE((SELECT MAX(u.created_at) FROM word_uses u WHERE u.word_id = w.id), s.created_at)) AS last_at "
            "FROM sets s LEFT JOIN set_words w ON w.set_id = s.id "
            "WHERE s.user_id=? AND s.status!='active' GROUP BY s.id ORDER BY last_at DESC, s.id DESC", (user_id,)
        ).fetchall()
        return [dict(r, done=r["done"] or 0, last_at=r["last_at"] or r["created_at"]) for r in rows]

    def get_set(self, set_id: int):
        return self.conn.execute("SELECT * FROM sets WHERE id=?", (set_id,)).fetchone()

    def activate_set(self, user_id: int, set_id: int) -> None:
        """Вернуть набор из архива: текущий уходит в архив, ничего никуда не переезжает."""
        self.conn.execute("UPDATE sets SET status='done' WHERE user_id=? AND status='active'", (user_id,))
        self.conn.execute("UPDATE sets SET status='active' WHERE id=? AND user_id=?", (set_id, user_id))
        self.conn.commit()

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

    def msg_set_words(self, msg_id: int) -> list:
        """Слова набора, которые ученик употребил в реплике (кнопка 👌 Знакомые под ответом)."""
        return self.conn.execute("SELECT DISTINCT w.* FROM word_uses u JOIN set_words w ON w.id = u.word_id "
                                 "WHERE u.msg_id=? ORDER BY w.id", (msg_id,)).fetchall()

    def uses_for_msg(self, msg_id: int) -> list:
        return self.conn.execute("SELECT * FROM word_uses WHERE msg_id=? ORDER BY id", (msg_id,)).fetchall()

    def set_use_correct(self, use_id: int, correct: bool) -> None:
        self.conn.execute("UPDATE word_uses SET correct=? WHERE id=?", (int(correct), use_id))
        self.conn.commit()

    def set_familiar(self, word_id: int, on: bool) -> None:
        """👌 Знакомое: вопросы на нём не строим, возвращается по одному раз в пару кругов; из набора — только при освоении."""
        self.conn.execute("UPDATE set_words SET familiar_at=? WHERE id=?", (self.clock() if on else None, word_id))
        self.conn.commit()

    def last_use_at(self, word_id: int) -> float | None:
        r = self.conn.execute("SELECT MAX(created_at) AS t FROM word_uses WHERE word_id=?", (word_id,)).fetchone()
        return r["t"] if r else None

    def user_turns_since(self, user_id: int, since: float) -> int:
        """Сколько реплик ученик сказал после момента since (во всех разговорах)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.user_id=? AND m.role='user' AND m.created_at>?", (user_id, since)).fetchone()[0]

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
            "SELECT * FROM book_stats WHERE user_id=? AND unit=?", (user_id, unit_key(unit)))}

    def book_record(self, user_id: int, unit: str, pl: str, d: str, ok: bool, typed: bool = True) -> None:
        """Ответ по слову юнита. typed — написал сам (карточки, пропуски, разговор), иначе выбрал в тесте.
        Верно написанное засчитывается в обе серии; ошибка в тесте сбрасывает обе (раз не узнал — не знаешь);
        ошибка в написании сбрасывает только «знаю»."""
        t_ok, w_ok = int(ok), int(ok and typed)
        reset_test = int(not ok and not typed)
        self.conn.execute(
            "INSERT INTO book_stats(user_id, unit, pl, dir, streak, right, wrong, last_at, test_streak, typed_streak, "
            "last_ok) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, unit, pl, dir) DO UPDATE SET "
            "test_streak = CASE WHEN ?=1 THEN test_streak+1 WHEN ?=1 THEN 0 ELSE test_streak END, "
            "typed_streak = CASE WHEN ?=1 THEN typed_streak+1 WHEN ?=1 THEN typed_streak ELSE 0 END, "
            "streak = CASE WHEN ?=1 THEN streak+1 ELSE 0 END, "
            "right = right + excluded.right, wrong = wrong + excluded.wrong, last_at = excluded.last_at, "
            "last_ok = excluded.last_ok",
            (user_id, unit_key(unit), pl, d, t_ok, int(ok), int(not ok), self.clock(), t_ok, w_ok, int(ok),
             t_ok, reset_test, w_ok, int(ok and not typed), t_ok))
        self.conn.commit()

    # ---------- 📝 упражнения из книги ----------

    def bex_results(self, user_id: int, unit: str) -> dict:
        """{(ex_id, n): row} — что уже сделано в упражнениях юнита."""
        return {(r["ex_id"], r["n"]): dict(r) for r in self.conn.execute(
            "SELECT * FROM book_ex WHERE user_id=? AND unit=?", (user_id, unit_key(unit)))}

    def bex_save(self, user_id: int, unit: str, ex_id: str, n: int, ok: bool, answer: str, explanation: str,
                 manual: bool = False) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO book_ex(user_id, unit, ex_id, n, ok, answer, explanation, at, manual) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, unit_key(unit), ex_id, n, int(ok), answer, explanation, self.clock(), int(manual)))
        self.conn.commit()

    def bex_mark_manual(self, user_id: int, unit: str, ex_id: str, ns: list[int]) -> list[int]:
        """✋ Засчитать пункты вручную (ответ, если был, сохраняется). Возвращает пункты, которые изменились."""
        have = {(r["ex_id"], r["n"]): r for r in self.bex_results(user_id, unit).values()}
        changed = []
        for n in ns:
            row = have.get((ex_id, n))
            if row and row["ok"]:
                continue
            self.bex_save(user_id, unit, ex_id, n, True, row["answer"] if row else "", "", manual=True)
            changed.append(n)
        return changed

    def bex_done_add(self, user_id: int, unit: str, ex_id: str, answers: dict, manual: list[int] | None = None) -> None:
        self.conn.execute("INSERT INTO book_ex_done(user_id, unit, ex_id, answers, at, manual) VALUES (?,?,?,?,?,?)",
                          (user_id, unit_key(unit), ex_id, json.dumps(answers, ensure_ascii=False), self.clock(),
                           json.dumps(sorted(manual or []))))
        self.conn.commit()

    def bex_done(self, user_id: int, unit: str) -> dict:
        """{ex_id: [попытки на 100%: {answers, at}], от старой к новой}."""
        out: dict = {}
        for r in self.conn.execute("SELECT * FROM book_ex_done WHERE user_id=? AND unit=? ORDER BY id",
                                   (user_id, unit_key(unit))):
            out.setdefault(r["ex_id"], []).append({"answers": json.loads(r["answers"]), "at": r["at"],
                                                   "manual": json.loads(r["manual"] or "[]")})
        return out

    def bex_reset(self, user_id: int, unit: str, ex_id: str) -> None:
        self.conn.execute("DELETE FROM book_ex WHERE user_id=? AND unit=? AND ex_id=?",
                          (user_id, unit_key(unit), ex_id))
        self.conn.commit()

    # ---------- 📚 чтение ----------

    def read_mark(self, user_id: int, unit: str, idx: int) -> None:
        """Абзац прочитан — ученик сам перешёл дальше («➡️ Дальше» / «🏁 Конец главы»)."""
        self.conn.execute("INSERT OR IGNORE INTO read_done(user_id, unit, idx, at) VALUES (?,?,?,?)",
                          (user_id, unit, idx, self.clock()))
        self.conn.commit()

    def read_seen(self, user_id: int, unit: str, idx: int) -> None:
        """Абзац открыт — место в книге здесь (для «▶️ Продолжить»); прочитанным он от этого не становится."""
        self.conn.execute("INSERT OR REPLACE INTO read_pos(user_id, book, unit, idx) VALUES (?,?,?,?)",
                          (user_id, unit.split(":")[0], unit, idx))
        self.conn.commit()

    def read_done(self, user_id: int) -> dict[str, set[int]]:
        """{юнит: {номера прочитанных абзацев}}."""
        out: dict[str, set[int]] = {}
        for r in self.conn.execute("SELECT unit, idx FROM read_done WHERE user_id=?", (user_id,)):
            out.setdefault(r["unit"], set()).add(r["idx"])
        return out

    def read_pos(self, user_id: int, book: str) -> tuple[str, int] | None:
        r = self.conn.execute("SELECT unit, idx FROM read_pos WHERE user_id=? AND book=?", (user_id, book)).fetchone()
        return (r["unit"], r["idx"]) if r else None

    def zv_status(self, user_id: int) -> dict[str, int]:
        return {r["item"]: r["status"] for r in self.conn.execute(
            "SELECT item, status FROM zv_status WHERE user_id=?", (user_id,))}

    def zv_set(self, user_id: int, item: str, status: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO zv_status(user_id, item, status, at) VALUES (?,?,?,?)",
                          (user_id, item, status, self.clock()))
        self.conn.commit()

    def rule_cache_get(self, key: str) -> dict | None:
        r = self.conn.execute("SELECT data FROM rule_cache WHERE key=?", (key,)).fetchone()
        return json.loads(r["data"]) if r else None

    def rule_cache_set(self, key: str, data: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO rule_cache(key, data, at) VALUES (?,?,?)",
                          (key, json.dumps(data, ensure_ascii=False), self.clock()))
        self.conn.commit()

    def dict_has(self, user_id: int, pl: str) -> bool:
        return self.conn.execute("SELECT 1 FROM dict_items WHERE user_id=? AND lower(pl)=lower(?)",
                                 (user_id, pl.strip())).fetchone() is not None

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

    # Слово словаря = строка dict_items (тема, откуда) + строка set_words в наборе своей темы (прогресс, тренировка).
    # Набор темы — обычный набор (sets.topic_id не NULL): «⭐ <тема> <N>», не больше DICT_SET_SIZE слов,
    # создаётся в 📚 архиве (текущий набор не сбивается); из архива — разговор и упражнения, как у любого набора.

    @staticmethod
    def _topic_name(name: str) -> str:
        name = " ".join(str(name or "").split()).strip(" .«»\"'")[:40]
        return (name[:1].upper() + name[1:]) if name else DICT_OTHER

    def topic_get(self, user_id: int, name: str) -> int:
        """id темы словаря (без учёта регистра); нет такой — создаётся."""
        name = self._topic_name(name)
        row = self.conn.execute("SELECT id FROM dict_topics WHERE user_id=? AND lower(name)=lower(?)",
                                (user_id, name)).fetchone()
        if row:
            return row["id"]
        return self.conn.execute("INSERT INTO dict_topics(user_id, name, created_at) VALUES (?,?,?)",
                                 (user_id, name, self.clock())).lastrowid

    def topic(self, topic_id: int):
        return self.conn.execute("SELECT * FROM dict_topics WHERE id=?", (topic_id,)).fetchone()

    def dict_topics(self, user_id: int) -> list[dict]:
        """Темы, где есть слова: [{id, name, n, done}], по алфавиту, «Разное» — в конце."""
        rows = self.conn.execute(
            "SELECT t.id, t.name, COUNT(d.id) AS n, "
            "SUM(CASE WHEN w.mastered_at IS NOT NULL THEN 1 ELSE 0 END) AS done "
            "FROM dict_topics t JOIN dict_items d ON d.topic_id = t.id LEFT JOIN set_words w ON w.id = d.word_id "
            "WHERE t.user_id=? GROUP BY t.id", (user_id,)).fetchall()
        out = [dict(r, done=r["done"] or 0) for r in rows]
        out.sort(key=lambda r: (r["name"] == DICT_OTHER, r["name"].lower()))
        return out

    def topic_names(self, user_id: int) -> list[str]:
        return [t["name"] for t in self.dict_topics(user_id)]

    def _topic_set(self, user_id: int, topic_id: int, size: int = DICT_SET_SIZE) -> int:
        """Набор темы, где есть место; все полные — новый «⭐ <тема> <N>» в архиве."""
        rows = self.conn.execute(
            "SELECT s.id, s.title, COUNT(w.id) AS n FROM sets s LEFT JOIN set_words w ON w.set_id = s.id "
            "WHERE s.user_id=? AND s.topic_id=? GROUP BY s.id ORDER BY s.id", (user_id, topic_id)).fetchall()
        for r in rows:
            if r["n"] < size:
                return r["id"]
        name = self.topic(topic_id)["name"]
        nums = [int(t.rsplit(" ", 1)[1]) for t in (r["title"] for r in rows) if t.rsplit(" ", 1)[-1].isdigit()]
        return self.conn.execute("INSERT INTO sets(user_id, title, status, topic_id, created_at) VALUES (?,?,?,?,?)",
                                 (user_id, f"⭐ {name} {max(nums, default=0) + 1}", "done", topic_id,
                                  self.clock())).lastrowid

    def _drop_empty_topic_set(self, set_id: int | None) -> None:
        """Пустой набор темы (последнее слово ушло) удаляется, если его сейчас не тренируют."""
        if not set_id:
            return
        st = self.get_set(set_id)
        if st and st["topic_id"] and st["status"] != "active" and not self.set_words(set_id):
            self.conn.execute("DELETE FROM sets WHERE id=?", (set_id,))

    def _dict_place(self, user_id: int, item_id: int, topic: str) -> None:
        """Положить слово словаря в набор темы (новая строка set_words) — для слова, у которого строки ещё нет."""
        it = self.conn.execute("SELECT * FROM dict_items WHERE id=?", (item_id,)).fetchone()
        topic_id = self.topic_get(user_id, topic)
        set_id = self._topic_set(user_id, topic_id)
        word_id = self.conn.execute(
            "INSERT INTO set_words(set_id, user_id, pl, translit, ru, pos, kind, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (set_id, user_id, it["pl"], it["translit"], it["ru"], "выраж", "phrase", self.clock())).lastrowid
        self.conn.execute("UPDATE dict_items SET topic_id=?, word_id=?, used_in_set=1 WHERE id=?",
                          (topic_id, word_id, item_id))

    def dict_add(self, user_id: int, pl: str, translit: str, ru: str, topic: str = DICT_OTHER,
                 src: str = "") -> bool:
        """Слово в словарь и сразу в набор своей темы. False — такое выражение уже есть."""
        pl = pl.strip()
        if not pl or self.dict_has(user_id, pl):
            return False
        item_id = self.conn.execute(
            "INSERT INTO dict_items(user_id, pl, translit, ru, src, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, pl, translit, ru, src, self.clock())).lastrowid
        self._dict_place(user_id, item_id, topic)
        self.conn.commit()
        return True

    def dict_item(self, item_id: int):
        return self.conn.execute(
            "SELECT d.*, t.name AS topic, w.set_id, w.mastered_at, s.title AS set_title FROM dict_items d "
            "LEFT JOIN dict_topics t ON t.id = d.topic_id LEFT JOIN set_words w ON w.id = d.word_id "
            "LEFT JOIN sets s ON s.id = w.set_id WHERE d.id=?", (item_id,)).fetchone()

    def dict_remove_id(self, user_id: int, item_id: int) -> None:
        """Убрать слово из словаря и из набора темы (прогресс слова удаляется вместе с ним)."""
        it = self.dict_item(item_id)
        if not it or it["user_id"] != user_id:
            return
        self.conn.execute("DELETE FROM dict_items WHERE id=?", (item_id,))
        if it["word_id"]:
            self.conn.execute("DELETE FROM word_uses WHERE word_id=?", (it["word_id"],))
            self.conn.execute("DELETE FROM set_words WHERE id=?", (it["word_id"],))
            self._drop_empty_topic_set(it["set_id"])
        self.conn.commit()

    def dict_remove(self, user_id: int, pl: str) -> None:
        for r in self.conn.execute("SELECT id FROM dict_items WHERE user_id=? AND lower(pl)=lower(?)",
                                   (user_id, pl.strip())).fetchall():
            self.dict_remove_id(user_id, r["id"])

    def dict_move(self, user_id: int, item_id: int, topic: str, commit: bool = True) -> str | None:
        """📂 Сменить тему: слово переезжает в набор новой темы вместе с прогрессом (та же строка set_words).
        Возвращает название набора, куда попало слово."""
        it = self.dict_item(item_id)
        if not it or it["user_id"] != user_id:
            return None
        topic_id = self.topic_get(user_id, topic)
        if topic_id == it["topic_id"] and it["word_id"]:
            return it["set_title"]
        if it["word_id"]:
            set_id = self._topic_set(user_id, topic_id)
            self.conn.execute("UPDATE set_words SET set_id=? WHERE id=?", (set_id, it["word_id"]))
            self.conn.execute("UPDATE dict_items SET topic_id=? WHERE id=?", (topic_id, item_id))
            self._drop_empty_topic_set(it["set_id"])
        else:
            self._dict_place(user_id, item_id, topic)
            set_id = self.dict_item(item_id)["set_id"]
        if commit:
            self.conn.commit()
        return self.get_set(set_id)["title"]

    def dict_items(self, user_id: int, only_unused: bool = False) -> list:
        """Все слова словаря с темой, набором и отметкой «освоено»: по темам, внутри темы — по порядку добавления."""
        q = ("SELECT d.*, COALESCE(t.name, ?) AS topic, w.set_id, w.mastered_at, s.title AS set_title "
             "FROM dict_items d LEFT JOIN dict_topics t ON t.id = d.topic_id LEFT JOIN set_words w ON w.id = d.word_id "
             "LEFT JOIN sets s ON s.id = w.set_id WHERE d.user_id=?" + (" AND d.used_in_set=0" if only_unused else "")
             + " ORDER BY d.id")
        rows = self.conn.execute(q, (DICT_OTHER, user_id)).fetchall()
        order = {name: i for i, name in enumerate(self.topic_names(user_id))}
        return sorted(rows, key=lambda r: (order.get(r["topic"], len(order)), r["id"]))

    def topic_items(self, user_id: int, topic_id: int) -> list:
        return [r for r in self.dict_items(user_id) if r["topic_id"] == topic_id]

    def dict_sets(self, user_id: int, topic_id: int | None = None) -> list[dict]:
        """Наборы из словаря (по темам) с прогрессом: [{id, title, status, total, done}]."""
        rows = self.conn.execute(
            "SELECT s.id, s.title, s.status, s.topic_id, COUNT(w.id) AS total, "
            "SUM(CASE WHEN w.mastered_at IS NOT NULL THEN 1 ELSE 0 END) AS done "
            "FROM sets s LEFT JOIN set_words w ON w.set_id = s.id WHERE s.user_id=? AND s.topic_id IS NOT NULL"
            + (" AND s.topic_id=?" if topic_id else "") + " GROUP BY s.id ORDER BY s.title",
            (user_id, topic_id) if topic_id else (user_id,)).fetchall()
        return [dict(r, done=r["done"] or 0) for r in rows if r["total"]]

    def dict_since(self, user_id: int, since: float) -> list:
        return self.conn.execute("SELECT * FROM dict_items WHERE user_id=? AND created_at>=? ORDER BY id",
                                 (user_id, since)).fetchall()

    def offer_get(self, msg_id: int):
        """Меню «⭐ В словарь» к ответу бота: errs — мои ошибки, phrases — подобрано из ответа (bot_ready),
        sel — отмеченные ключи («e0», «b3»), saved — уже сохранённые ключи, topic — своя тема («» — 🤖 сам)."""
        row = self.conn.execute("SELECT * FROM dict_offers WHERE msg_id=?", (msg_id,)).fetchone()
        if not row:
            return None
        saved = [f"b{x}" if isinstance(x, int) else x for x in json.loads(row["saved"])]   # старые записи — индексы
        return {"user_id": row["user_id"], "phrases": json.loads(row["phrases"]), "saved": saved,
                "errs": json.loads(row["errs"]), "sel": json.loads(row["sel"]),
                "bot_ready": bool(row["bot_ready"]) or bool(json.loads(row["phrases"])), "topic": row["topic"] or ""}

    def offer_save(self, msg_id: int, user_id: int, phrases: list[dict], saved: list, errs: list[dict] | None = None,
                   sel: list[str] | None = None, bot_ready: bool = False, topic: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO dict_offers(msg_id, user_id, phrases, saved, errs, sel, bot_ready, topic) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (msg_id, user_id, json.dumps(phrases, ensure_ascii=False), json.dumps(saved),
             json.dumps(errs or [], ensure_ascii=False), json.dumps(sel or []), int(bot_ready), topic or ""))
        self.conn.commit()

    def corrections_by(self, user_id: int, key: str, value) -> list[dict]:
        """Ошибки вне разговора (msg_id NULL) с полем data[key] == value, вместе с оспоренными."""
        return [c for c in self._corr_rows("user_id=? AND msg_id IS NULL", (user_id,), with_disputed=True)
                if c.get(key) == value]

    def add_corrections(self, session_id: int, user_id: int, corrections: list[dict]) -> None:
        now = self.clock()
        for k in corrections:
            self.conn.execute("INSERT INTO corrections(session_id, user_id, data, rule, created_at) VALUES (?,?,?,?,?)",
                              (session_id, user_id, json.dumps(k, ensure_ascii=False), k.get("rule"), now))
        self.conn.commit()

    # ---------- упражнения ----------

    def ex_create(self, user_id: int, kind: str, fmt: str, title: str, items: list[dict],
                  topics: list[str] | None = None, cfg: dict | None = None) -> int:
        """cfg — настройки серии (вид, формат, юнит, направление, темы, слова, количество) — для «🔁 Повторить»."""
        now = self.clock()
        ex_id = self.conn.execute(
            "INSERT INTO exercises(user_id, kind, fmt, title, items, created_at, topics, cfg) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, kind, fmt, title, json.dumps(items, ensure_ascii=False), now,
             json.dumps(topics or [], ensure_ascii=False), json.dumps(cfg or {}, ensure_ascii=False))).lastrowid
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
        d["cfg"] = json.loads(d["cfg"]) if d.get("cfg") else {}
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
