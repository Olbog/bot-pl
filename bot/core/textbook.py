"""Лексика из учебника: юниты лежат файлами bot/textbook/unit_NN.json (их добавляет Claude по скринам).

Формат файла:
{
  "unit": "2",
  "title": "Rodzina",
  "summary": "семья и родственники, mieć, мой/твой/его",
  "words": [
    {"pl": "rodzeństwo", "translit": "ро-ДЗЕНЬ-ство", "ru": "братья и сёстры",
     "ru_alt": ["брат и сестра"], "pl_alt": [], "pos": "сущ., ср. р."}
  ]
}
"""
import json
import random
import re
from pathlib import Path

UNITS_DIR = Path(__file__).resolve().parent.parent / "textbook"  # bot/textbook/unit_NN.json
DIRS = {"pl": "🇵🇱→🇷🇺", "ru": "🇷🇺→🇵🇱", "mix": "🔀 Вперемешку"}
KNOW_STREAK = 3   # слово выучено: столько верных ответов подряд в каждую сторону


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
    """stats: {(pl, dir): {"streak", "right", "wrong"}}; dir — "pl" (PL→RU) или "ru" (RU→PL)."""
    return all(stats.get((pl, d), {}).get("streak", 0) >= KNOW_STREAK for d in ("pl", "ru"))


def pick_words(unit: dict, stats: dict, n: int, direction: str) -> list[tuple[dict, str]]:
    """Слова для упражнения: сначала с последней ошибкой (🔁), потом реже всего тренированные.
    direction: pl / ru / mix / gap (пропуски считаются тренировкой RU→PL — вспомнить польское слово)."""
    out = []
    words = list(unit["words"])
    random.shuffle(words)
    for w in words:
        d = random.choice(("pl", "ru")) if direction == "mix" else ("ru" if direction == "gap" else direction)
        st = stats.get((w["pl"], d), {})
        failed = st.get("wrong", 0) > 0 and st.get("streak", 0) == 0
        out.append((0 if failed else 1, st.get("right", 0) + st.get("wrong", 0), w, d))
    out.sort(key=lambda x: (x[0], x[1]))
    return [(w, d) for _, _, w, d in out[:n]]


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


def unit_progress(unit: dict, stats: dict) -> tuple[int, int]:
    return sum(known(stats, w["pl"]) for w in unit["words"]), len(unit["words"])
