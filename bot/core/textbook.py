"""Учебники: bot/textbook/<книга>/book.json + unit_NN.json (их добавляет Claude по страницам учебника и тетради).

book.json: {"id": "kpk", "title": "Krok po kroku. Polski A1", "short": "KpK", "unit_label": "Unit",
            "audio_dir": "Krok_po_kroku/Audio (A1)",   — аудио ищется в BOOKS_DIR/<audio_dir>
            "files": {"tb": "…pdf", "wb": "…pdf"},      — PDF учебника и тетради в BOOKS_DIR
            "page_offset": {"tb": -5, "wb": 0}}        — страница PDF = страница книги + сдвиг

unit_NN.json:
{
  "unit": "8", "title": "Mami, jesteś głodna?", "summary": "суть юнита по-русски",
  "words": [
    {"pl": "rodzeństwo", "translit": "ро-ДЗЕНЬ-ство", "ru": "братья и сёстры", "ru_alt": ["брат и сестра"],
     "pos": "сущ., ср. р.", "src": "wb"},
    {"pl": "mieć rodzeństwo", "translit": "...", "ru": "...", "pos": "🔗 сочетание", "of": "rodzeństwo"}
  ],
  "exercises": [ ... ]   — упражнения из книги, см. exercises/bookex.py
}
"of" — 🔗 сочетание к слову (стоит сразу после него); "src": "wb" — слово из рабочей тетради (иначе учебник).
Ключ юнита везде — «<книга>:<номер>», например «kpk:8»; старое «8» понимается как kpk.

Выученность — два уровня (по каждому направлению PL→RU / RU→PL):
  🟡 узнаю — KNOW_STREAK верных подряд в тесте (выбор из вариантов) хотя бы в одну сторону;
  ✅ знаю  — KNOW_STREAK верных подряд, написанных самому (карточки, пропуски, разговор), в обе стороны.
"""
import json
import random
import re
from pathlib import Path

UNITS_DIR = Path(__file__).resolve().parent.parent / "textbook"  # bot/textbook/<книга>/unit_NN.json
DEFAULT_BOOK = "kpk"     # старые ключи юнитов без книги («8») — это Krok po kroku
DIRS = {"pl": "🇵🇱→🇷🇺", "ru": "🇷🇺→🇵🇱", "mix": "🔀 Вперемешку"}
KNOW_STREAK = 3   # столько верных ответов подряд нужно для «знаю» / «узнаю»


def load_books(path: Path | None = None) -> list[dict]:
    """Книги (папки с book.json) с их юнитами: [{id, title, short, unit_label, audio_dir, units: [...]}]."""
    path = path or UNITS_DIR
    books = []
    for d in sorted(p for p in path.iterdir() if p.is_dir()) if path.exists() else []:
        try:
            meta = json.loads((d / "book.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta.setdefault("id", d.name)
        meta.setdefault("short", meta.get("title", d.name))
        meta.setdefault("unit_label", "Unit")
        units = []
        for f in sorted(d.glob("unit_*.json")):
            try:
                u = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            num = str(u.get("unit") or f.stem.removeprefix("unit_").lstrip("0") or "0")
            u.update(num=num, unit=f"{meta['id']}:{num}", book=meta["id"], book_title=meta.get("title", ""),
                     book_short=meta["short"], name=f"{meta['unit_label']} {num}",
                     audio_dir=meta.get("audio_dir", ""), files=meta.get("files") or {},
                     page_offset=meta.get("page_offset") or {})
            u["words"] = [w for w in u.get("words") or [] if w.get("pl") and w.get("ru")]
            u["exercises"] = [x for x in u.get("exercises") or [] if x.get("id") and x.get("items")]
            if u["words"] or u["exercises"]:
                units.append(u)
        units.sort(key=_order)
        meta["units"] = units
        books.append(meta)
    books.sort(key=lambda b: (b.get("order", 99), b["id"]))
    return books


def load_units(path: Path | None = None, book: str | None = None) -> list[dict]:
    return [u for b in load_books(path) if book in (None, b["id"]) for u in b["units"]]


def _order(u: dict) -> tuple:
    """«7» < «7a» < «8» < «10»: сначала номер урока, потом буква мини-юнита."""
    m = re.match(r"(\d+)(.*)", u["num"])
    return (int(m.group(1)), m.group(2)) if m else (10**6, u["num"])


def get_unit(unit_id: str, path: Path | None = None) -> dict | None:
    """«kpk:8» → юнит; старое «8» (без книги) — юнит Krok po kroku."""
    unit_id = str(unit_id or "")
    if ":" not in unit_id:
        unit_id = f"{DEFAULT_BOOK}:{unit_id}"
    return next((u for u in load_units(path) if u["unit"] == unit_id), None)


def known(stats: dict, pl: str) -> bool:
    """✅ Знаю: написал сам верно KNOW_STREAK раз подряд в обе стороны.
    stats: {(pl, dir): {"typed_streak", "test_streak", ...}}; dir — "pl" (PL→RU) или "ru" (RU→PL)."""
    return all(stats.get((pl, d), {}).get("typed_streak", 0) >= KNOW_STREAK for d in ("pl", "ru"))


def recognized(stats: dict, pl: str) -> bool:
    """🟡 Узнаю: в тесте верно KNOW_STREAK раз подряд хотя бы в одну сторону (и ещё не «знаю»)."""
    return not known(stats, pl) and any(stats.get((pl, d), {}).get("test_streak", 0) >= KNOW_STREAK
                                        for d in ("pl", "ru"))


def pick_words(unit: dict, stats: dict, n: int, direction: str,
               first: list[tuple[str, str]] | None = None) -> list[tuple[dict, str]]:
    """Слова для упражнения: first — обязательно (ошибки прошлого упражнения при «🔁 Повторить»),
    дальше с последней ошибкой, потом ещё не «знаю», потом реже всего тренированные.
    direction: pl / ru / mix / gap (пропуски считаются тренировкой RU→PL — вспомнить польское слово)."""
    by_pl = {w["pl"]: w for w in unit["words"]}
    forced = [(by_pl[pl], d) for pl, d in (first or []) if pl in by_pl][:n]
    taken = {w["pl"] for w, _ in forced}
    out = []
    words = [w for w in unit["words"] if w["pl"] not in taken]
    random.shuffle(words)
    for w in words:
        d = random.choice(("pl", "ru")) if direction == "mix" else ("ru" if direction == "gap" else direction)
        st = stats.get((w["pl"], d), {})
        failed = (st.get("right", 0) + st.get("wrong", 0)) > 0 and not st.get("last_ok", 1)
        out.append((0 if failed else 1, int(known(stats, w["pl"])), st.get("right", 0) + st.get("wrong", 0), w, d))
    out.sort(key=lambda x: (x[0], x[1], x[2]))
    return forced + [(w, d) for *_, w, d in out[:n - len(forced)]]


def card_item(w: dict, d: str) -> dict:
    """Карточка: d=pl — дано польское, пишешь русский; d=ru — наоборот."""
    pl_line = w["pl"] + (f" [{w['translit']}]" if w.get("translit") else "")
    if d == "pl":
        q, answer, accepted = pl_line, w["ru"], list(w.get("ru_alt") or [])
    else:
        q, answer, accepted = w["ru"], w["pl"], list(w.get("pl_alt") or [])
    return {"q": q, "answer": answer, "accepted": accepted, "options": [], "full_pl": w["pl"],
            "translit": w.get("translit", ""), "ru": w["ru"], "grammar": w.get("pos", ""), "rule": "",
            "lemma": w["pl"], "dir": d, "card": True}


def unit_progress(unit: dict, stats: dict) -> tuple[int, int, int]:
    """(✅ знаю, 🟡 узнаю, всего)."""
    return (sum(known(stats, w["pl"]) for w in unit["words"]),
            sum(recognized(stats, w["pl"]) for w in unit["words"]), len(unit["words"]))
