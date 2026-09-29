"""Логика тренировки наборов: прогресс слов, критерий «освоено», повторение."""
from dataclasses import dataclass, field

REVIEW_INTERVALS_DAYS = [3, 7, 14, 30, 60]  # через сколько дней повторять освоенное слово
DAY = 86400


@dataclass
class Criteria:
    streak: int = 20   # правильных употреблений подряд, без ошибок
    forms: int = 3     # разных форм среди них
    days: int = 3      # разных дней среди них


@dataclass
class WordStats:
    total: int = 0                 # всего употреблений
    correct: int = 0               # из них правильно
    errors: int = 0
    streak: int = 0                # правильно подряд после последней ошибки
    streak_forms: list[str] = field(default_factory=list)
    streak_days: int = 0
    all_forms: list[str] = field(default_factory=list)


def stats(uses) -> WordStats:
    """uses — строки word_uses по порядку (form, correct, day)."""
    s = WordStats()
    streak_forms: list[str] = []
    streak_days: set[str] = set()
    all_forms: list[str] = []
    for u in uses:
        s.total += 1
        form = (u["form"] or "").strip().lower()
        if u["correct"]:
            s.correct += 1
            s.streak += 1
            if form and form not in streak_forms:
                streak_forms.append(form)
            streak_days.add(u["day"])
            if form and form not in all_forms:
                all_forms.append(form)
        else:
            s.errors += 1
            s.streak = 0
            streak_forms, streak_days = [], set()
    s.streak_forms = streak_forms
    s.streak_days = len(streak_days)
    s.all_forms = all_forms
    return s


def scaled(c: Criteria, factor: int) -> Criteria:
    return Criteria(c.streak * factor, c.forms * factor, c.days * factor)


def kind_of(word) -> str:
    try:
        return word["kind"] or "word"
    except (IndexError, KeyError):
        return "word"


def meets(s: WordStats, c: Criteria) -> bool:
    return s.streak >= c.streak and len(s.streak_forms) >= c.forms and s.streak_days >= c.days


def due_for_review(word, now: float) -> bool:
    """Освоенное слово пора повторить в свободном режиме."""
    if word["mastered_at"] is None:
        return False
    stage = min(word["review_stage"], len(REVIEW_INTERVALS_DAYS) - 1)
    last = word["last_review_at"] or word["mastered_at"]
    return now - last >= REVIEW_INTERVALS_DAYS[stage] * DAY


def set_block(title: str, rows: list[tuple], c: Criteria, rule_c: Criteria | None = None) -> str:
    """Блок системной инструкции для режима набора. rows — (item, WordStats), только неосвоенные.
    item kind: word/phrase — слово или выражение; rule — правило, на котором ученик ошибался."""
    rule_c = rule_c or c
    words = [(w, s) for w, s in rows if kind_of(w) != "rule"]
    rules = [(w, s) for w, s in rows if kind_of(w) == "rule"]
    parts = [f"ТРЕНИРОВКА. Ученик тренирует набор «{title}». Цель — чтобы он как можно чаще сам говорил "
             f"то, что в наборе, органично, в живом разговоре, и в РАЗНЫХ формах и ситуациях."]
    if words:
        lines = []
        for w, st in sorted(words, key=lambda r: (r[1].streak, len(r[1].streak_forms))):
            forms = ", ".join(st.streak_forms) or "—"
            lines.append(f"- {w['pl']} ({w['ru']}, {w['pos'] or '—'}): правильно подряд {st.streak}/{c.streak}, "
                         f"формы: {forms}")
        parts.append("Слова и выражения (сверху — наименее отработанные):\n" + "\n".join(lines))
    if rules:
        lines = []
        for w, st in sorted(rules, key=lambda r: r[1].streak):
            lines.append(f"- {w['pl']}: правильно подряд {st.streak}/{rule_c.streak}. Прошлые ошибки ученика: {w['ru']}")
        parts.append("Правила, на которых ученик ошибался (сверху — наименее отработанные):\n" + "\n".join(lines)
                     + "\nДля правил: задавай вопросы, ответ на которые требует применить правило (например, для "
                       "«родительный после отрицания» — «czego nie lubisz?», для «местный падеж» — «gdzie byłeś?»). "
                       "Используй ситуации, похожие на прошлые ошибки, но с другими словами. В target_uses для правила "
                       "lemma — название правила ровно как в списке, form — фрагмент ученика, где правило применено.")
    parts.append("""Как вести тренировку:
- Строй вопросы так, чтобы ученику пришлось ответить, используя 1–2 элемента из набора. В первую очередь — наименее отработанные.
- Меняй грамматику: спрашивай про него самого (ja), про собеседника (ty), про девушку/друга (ona/on), про «вас» и «их» (my/wy/oni), про прошлое и будущее, «сколько?» (мн. ч.), «где? / о чём? / без чего?» (разные падежи). Добивайся форм, которых ещё нет в списке форм.
- Сам тоже используй эти слова и конструкции в своих репликах — ученик слышит правильную форму.
- Не превращай разговор в список упражнений: одна естественная реплика + один вопрос. Не называй элементы «из набора» и не проси «используй слово X», если ученик сам не просит.
- Если ученик явно не знает слово — дай его в new_words и в следующем вопросе дай шанс сказать его самому.""")
    return "\n\n".join(parts)


def review_block(words: list) -> str:
    """Блок для свободного режима: освоенные слова, которые пора повторить."""
    if not words:
        return ""
    lst = "\n".join(f"- {w['pl']} ({w['ru']})" for w in words)
    return f"""ПОВТОРЕНИЕ. Свободный разговор. Если получится естественно — вплети в разговор и дай ученику шанс самому употребить эти давно не звучавшие слова (не все сразу, 1 слово за реплику, без нажима):
{lst}"""


def progress_bar(value: int, total: int, width: int = 10) -> str:
    filled = min(width, round(width * value / total)) if total else 0
    return "▰" * filled + "▱" * (width - filled)
