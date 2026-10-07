"""Лексика из учебника: юниты лежат файлами bot/textbook/unit_NN.json (их добавляет Claude по скринам).

Формат файла:
{
  "unit": "2",
  "title": "Rodzina",
  "summary": "семья и родственники, mieć, мой/твой/его",
  "words": [
    {"pl": "rodzeństwo", "translit": "ро-ДЗЕНЬ-ство", "ru": "братья и сёстры",
     "ru_alt": ["брат и сестра"], "pl_alt": [], "pos": "сущ., ср. р."},
    {"pl": "mieć rodzeństwo", "translit": "...", "ru": "иметь братьев и сестёр", "pos": "🔗 сочетание",
     "of": "rodzeństwo"}
  ]
}
"of" — 🔗 сочетание (с предлогом или коллокация) к слову; стоит сразу после него и тренируется как отдельный пункт.

Выученность — два уровня (по каждому направлению PL→RU / RU→PL):
  🟡 узнаю — KNOW_STREAK верных подряд в тесте (выбор из вариантов) хотя бы в одну сторону;
  ✅ знаю  — KNOW_STREAK верных подряд, написанных самому (карточки, пропуски, разговор), в обе стороны.
"""
import json
import random
import re
from pathlib import Path

UNITS_DIR = Path(__file__).resolve().parent.parent / "textbook"  # bot/textbook/unit_NN.json
DIRS = {"pl": "🇵🇱→🇷🇺", "ru": "🇷🇺→🇵🇱", "mix": "🔀 Вперемешку"}
KNOW_STREAK = 3   # столько верных ответов подряд нужно для «знаю» / «узнаю»


def load_units(path: Path | None = None) -> list[dict]:
    path = path or UNITS_DIR
    units = []
    for f in sorted(path.glob("unit_*.json")):
        try:
            u = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        u["unit"] = str(u.get("unit") or f.stem.removeprefix("unit_").lstrip("0") or "0")
        u["words"] = [w for w in u.get("words") or [] if w.get("pl") and w.get("ru")]
        if u["words"]:
            units.append(u)
    units.sort(key=_order)
    return units


def _order(u: dict) -> tuple:
    """«7» < «7a» < «8» < «10»: сначала номер урока, потом буква мини-юнита."""
    m = re.match(r"(\d+)(.*)", u["unit"])
    return (int(m.group(1)), m.group(2)) if m else (10**6, u["unit"])


def get_unit(unit_id: str, path: Path | None = None) -> dict | None:
    return next((u for u in load_units(path) if u["unit"] == str(unit_id)), None)


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
