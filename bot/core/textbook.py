"""Учебники: bot/textbook/<книга>/book.json + unit_NN.json (их добавляет Claude по страницам учебника и тетради).

book.json: {"id": "kpk", "title": "Krok po kroku. Polski A1", "short": "KpK", "unit_label": "Unit",
            "audio_dir": {"tb": "…", "wb": "…"},       — аудио учебника и тетради в BOOKS_DIR (или одна папка строкой)
            "files": {"tb": "…pdf", "wb": "…pdf"},      — PDF учебника и тетради в BOOKS_DIR
                     пути сверяются без учёта регистра, пробелов, «_» и польских букв: «Krok po Kroku» = «Krok_po_kroku»
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
📚 Чтение (book.json: "reading": true) — юнит = глава: "paragraphs": [{"pl", "ru", "words": [{"pl", "ru", "rule"}]}];
слова главы собираются из абзацев, упражнения («вопросы к главе») — как у учебника.
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

from .readability import tr

UNITS_DIR = Path(__file__).resolve().parent.parent / "textbook"  # bot/textbook/<книга>/unit_NN.json
PHRASES_BOOK = "phr"     # 💬 полезные выражения: «книга» вне учебников (standalone в book.json)
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
                     phrases=bool(meta.get("phrases")), reading=bool(meta.get("reading")),
                     unit_of=meta.get("unit_of", "юнита"), item_word=meta.get("item_word", "слов"),
                     audio_dir=meta.get("audio_dir", ""), files=meta.get("files") or {},
                     page_offset=meta.get("page_offset") or {})
            if u.get("paragraphs") and not u.get("words"):   # 📚 чтение: слова главы — из её абзацев
                u["words"] = reading_words(u["paragraphs"])
            u["words"] = group_words([w for w in u.get("words") or [] if w.get("pl") and w.get("ru")],
                                     u.get("topics") or [])
            u["exercises"] = [x for x in u.get("exercises") or [] if x.get("id") and x.get("items")]
            if u["words"] or u["exercises"]:
                units.append(u)
        units.sort(key=_order)
        meta["units"] = units
        books.append(meta)
    books.sort(key=lambda b: (b.get("order", 99), b["id"]))
    return books


MIN_TOPIC = 3            # в теме меньше слов (без сочетаний) — они уходят в «Разное», чтобы не плодить мелкие темы
OTHER_TOPIC = "Разное"


def group_words(words: list[dict], order: list[str]) -> list[dict]:
    """Слова юнита по темам (поле topic; сочетание — в теме своего слова, сразу под ним): темы в порядке
    unit["topics"], «Разное» — в конце. Ничего не выбрасывается — только порядок и поле group.
    Если тем в юните нет — порядок как в файле, без групп."""
    if not any(w.get("topic") for w in words):
        return words
    base_topic: dict[str, str] = {}
    for w in words:
        if not w.get("of"):
            base_topic[w["pl"]] = w.get("topic") or OTHER_TOPIC
    for w in words:
        w["group"] = base_topic.get(w["of"], w.get("topic") or OTHER_TOPIC) if w.get("of") else base_topic[w["pl"]]
    sizes: dict[str, int] = {}
    for w in words:
        if not w.get("of"):
            sizes[w["group"]] = sizes.get(w["group"], 0) + 1
    for w in words:
        if sizes.get(w["group"], 0) < MIN_TOPIC:
            w["group"] = OTHER_TOPIC
    seen = list(dict.fromkeys(order + [w["group"] for w in words]))
    rank = {t: i for i, t in enumerate(t for t in seen if t != OTHER_TOPIC)}
    rank[OTHER_TOPIC] = len(rank)
    return sorted(words, key=lambda w: rank[w["group"]])   # sorted устойчив: внутри темы — порядок файла


def topic_parts(words: list[dict], size: int) -> list[tuple[str | None, int, list[tuple[int, dict]]]]:
    """[(тема, номер части, [(номер слова, слово)…])] — куски не больше size, не через границу темы."""
    out: list[tuple[str | None, int, list[tuple[int, dict]]]] = []
    for n, w in enumerate(words, 1):
        g = w.get("group")
        if out and out[-1][0] == g and len(out[-1][2]) < size:
            out[-1][2].append((n, w))
        elif out and out[-1][0] == g:
            out.append((g, out[-1][1] + 1, [(n, w)]))
        else:
            out.append((g, 1, [(n, w)]))
    return out


def reading_words(paragraphs: list[dict]) -> list[dict]:
    """Слова главы для чтения: слова всех абзацев по порядку, без повторов."""
    seen, out = set(), []
    for p in paragraphs:
        for w in p.get("words") or []:
            k = str(w.get("pl", "")).strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(dict(w))
    return out


def load_units(path: Path | None = None, book: str | None = None) -> list[dict]:
    return [u for b in load_books(path) if book in (None, b["id"]) for u in b["units"]]


def _order(u: dict) -> tuple:
    """«7» < «7a» < «8» < «10»: сначала номер урока, потом буква мини-юнита."""
    m = re.match(r"(\d+)(.*)", u["num"])
    return (int(m.group(1)), m.group(2)) if m else (10**6, u["num"])


REVIEW = "rev:"   # 🔁 повторение: «rev:kpk:10» — юнит с отдельным прогрессом; «rev:kpk:9,10;hurra:3@tb,wb» — слова юнитов


def review_id(units: list[str], parts: list[str] | None = None) -> str:
    """Ключ повторения: юниты [«kpk:9», «kpk:10»] (+ части) → «rev:kpk:9,10@tb,wb». Один юнит без частей — «rev:kpk:9»."""
    books: dict[str, list[str]] = {}
    for u in units:
        b, _, n = u.partition(":")
        books.setdefault(b, []).append(n)
    spec = ";".join(f"{b}:{','.join(ns)}" for b, ns in books.items())
    return REVIEW + spec + (f"@{','.join(parts)}" if parts else "")


def review_units(rid: str, path: Path | None = None) -> tuple[list[dict], list[str]]:
    """«rev:kpk:9,10@tb» → ([юниты], [части]); части по умолчанию — все."""
    spec, _, parts = str(rid).removeprefix(REVIEW).partition("@")
    units = []
    for chunk in spec.split(";"):
        b, _, nums = chunk.partition(":")
        for n in nums.split(","):
            u = get_unit(f"{b}:{n}", path) if b and n else None
            if u:
                units.append(u)
    return units, [p for p in parts.split(",") if p] or ["tb", "wb"]


def review_unit(rid: str, path: Path | None = None) -> dict | None:
    """Юнит повторения. Один юнит без частей («rev:kpk:10») — тот же юнит (упражнения из книги), но прогресс
    отдельный: ключ в базе — «rev:kpk:10». Несколько юнитов / с частями — слова выбранных юнитов и частей вместе
    (для карточек, теста и пропусков), упражнений нет."""
    units, parts = review_units(rid, path)
    if not units:
        return None
    if len(units) == 1 and "@" not in rid:
        return {**units[0], "unit": rid, "rev": True, "base": units[0]["unit"]}
    seen, words = set(), []
    for u in units:
        for w in u["words"]:
            if w.get("src", "tb") in parts and w["pl"].lower() not in seen:
                seen.add(w["pl"].lower())
                words.append(w)
    nums = ", ".join(f"{u['book_short']} {u['num']}" for u in units)
    return {**units[0], "unit": rid, "rev": True, "base": None, "words": words, "exercises": [],
            "num": nums, "name": "🔁 Повторение", "title": nums, "units_list": [u["unit"] for u in units]}


LESSON = "les:"   # 🎓 урок по юниту: «les:kpk:12» — тот же юнит, но прогресс упражнений из книги — отдельный


def lesson_unit(lid: str, path: Path | None = None) -> dict | None:
    base = get_unit(str(lid).removeprefix(LESSON), path)
    return {**base, "unit": LESSON + base["unit"], "les": True, "base": base["unit"]} if base else None


def lessons(unit_id: str, path: Path | None = None) -> list[dict]:
    """План уроков юнита: <книга>/lessons_<номер>.json → [{id, title, …}] (пишет Claude). Нет файла — []."""
    unit = get_unit(str(unit_id).removeprefix(LESSON), path)
    if not unit:
        return []
    num = unit["num"].zfill(2) if unit["num"].isdigit() else unit["num"]
    try:
        data = json.loads(((path or UNITS_DIR) / unit["book"] / f"lessons_{num}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [x for x in data.get("lessons") or [] if isinstance(x, dict) and x.get("id")]


def lesson_units(path: Path | None = None) -> list[dict]:
    """Юниты, для которых есть план уроков."""
    return [u for u in load_units(path) if lessons(u["unit"], path)]


def get_unit(unit_id: str, path: Path | None = None) -> dict | None:
    """«kpk:8» → юнит; старое «8» (без книги) — юнит Krok po kroku; «rev:…» — юнит повторения; «les:…» — урока."""
    unit_id = str(unit_id or "")
    if unit_id.startswith(REVIEW):
        return review_unit(unit_id, path)
    if unit_id.startswith(LESSON):
        return lesson_unit(unit_id, path)
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


def _dirs(direction: str) -> tuple[str, ...]:
    return ("pl", "ru") if direction == "mix" else ("ru",) if direction == "gap" else (direction,)


def fresh_dirs(stats: dict, pl: str, direction: str) -> list[str]:
    """🆕 Направления, в которых слово ещё ни разу не отвечено верно."""
    return [d for d in _dirs(direction) if not stats.get((pl, d), {}).get("right", 0)]


def fresh_count(unit: dict, stats: dict, direction: str) -> int:
    """Сколько слов юнита ещё ни разу не отвечено верно (в направлении упражнения; mix — хоть в одну сторону)."""
    return sum(1 for w in unit["words"] if fresh_dirs(stats, w["pl"], direction))


def pick_words(unit: dict, stats: dict, n: int, direction: str,
               first: list[tuple[str, str]] | None = None, fresh: bool = False) -> list[tuple[dict, str]]:
    """Слова для упражнения: first — обязательно (ошибки прошлого упражнения при «🔁 Повторить»),
    дальше с последней ошибкой, потом ещё не «знаю», потом реже всего тренированные.
    direction: pl / ru / mix / gap (пропуски считаются тренировкой RU→PL — вспомнить польское слово).
    fresh — «🆕 Новые слова»: только слова, ещё ни разу не отвеченные верно (в этом направлении)."""
    by_pl = {w["pl"]: w for w in unit["words"]}
    forced = [(by_pl[pl], d) for pl, d in (first or []) if pl in by_pl][:n]
    taken = {w["pl"] for w, _ in forced}
    out = []
    words = [w for w in unit["words"] if w["pl"] not in taken]
    random.shuffle(words)
    for w in words:
        if fresh:
            dirs = fresh_dirs(stats, w["pl"], direction)
            if not dirs:
                continue
            d = random.choice(dirs)
        else:
            d = random.choice(("pl", "ru")) if direction == "mix" else ("ru" if direction == "gap" else direction)
        st = stats.get((w["pl"], d), {})
        failed = (st.get("right", 0) + st.get("wrong", 0)) > 0 and not st.get("last_ok", 1)
        out.append((0 if failed else 1, int(known(stats, w["pl"])), st.get("right", 0) + st.get("wrong", 0), w, d))
    out.sort(key=lambda x: (x[0], x[1], x[2]))
    return forced + [(w, d) for *_, w, d in out[:n - len(forced)]]


def card_item(w: dict, d: str) -> dict:
    """Карточка: d=pl — дано польское, пишешь русский; d=ru — наоборот."""
    t = tr(w["pl"], w.get("translit", ""))
    pl_line = w["pl"] + (f" [{t}]" if t else "")
    if d == "pl":
        q, answer, accepted = pl_line, w["ru"], list(w.get("ru_alt") or [])
    else:
        q, answer, accepted = w["ru"], w["pl"], list(w.get("pl_alt") or [])
    return {"q": q, "answer": answer, "accepted": accepted, "options": [], "full_pl": w["pl"],
            "translit": w.get("translit", ""), "ru": w["ru"], "grammar": w.get("pos", ""), "rule": w.get("rule", ""),
            "lemma": w["pl"], "dir": d, "card": True}


def unit_progress(unit: dict, stats: dict) -> tuple[int, int, int]:
    """(✅ знаю, 🟡 узнаю, всего)."""
    return (sum(known(stats, w["pl"]) for w in unit["words"]),
            sum(recognized(stats, w["pl"]) for w in unit["words"]), len(unit["words"]))
