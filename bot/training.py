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


def meets(s: WordStats, c: Criteria) -> bool:
    return s.streak >= c.streak and len(s.streak_forms) >= c.forms and s.streak_days >= c.days


def due_for_review(word, now: float) -> bool:
    """Освоенное слово пора повторить в свободном режиме."""
    if word["mastered_at"] is None:
        return False
    stage = min(word["review_stage"], len(REVIEW_INTERVALS_DAYS) - 1)
    last = word["last_review_at"] or word["mastered_at"]
    return now - last >= REVIEW_INTERVALS_DAYS[stage] * DAY


def set_block(title: str, rows: list[tuple], c: Criteria) -> str:
    """Блок системной инструкции для режима набора. rows — (word, WordStats), только неосвоенные."""
    lines = []
    for w, s in sorted(rows, key=lambda r: (r[1].streak, len(r[1].streak_forms))):
        forms = ", ".join(s.streak_forms) or "—"
        lines.append(f"- {w['pl']} ({w['ru']}, {w['pos'] or '—'}): правильно подряд {s.streak}/{c.streak}, "
                     f"формы: {forms}")
    return f"""ТРЕНИРОВКА. Ученик тренирует набор слов «{title}». Цель — чтобы он как можно чаще сам произносил эти слова, органично, в живом разговоре, и в РАЗНЫХ формах.
Слова (сверху — наименее отработанные):
{chr(10).join(lines)}

Как вести тренировку:
- Строй вопросы так, чтобы ученику пришлось ответить, используя 1–2 слова из списка. В первую очередь — наименее отработанные (они сверху).
- Меняй грамматику: спрашивай про него самого (ja), про собеседника (ty), про девушку/друга (ona/on), про «вас» и «их» (my/wy/oni), про прошлое и будущее, «сколько?» (мн. ч.), «где? / о чём? / без чего?» (разные падежи). Добивайся форм, которых ещё нет в списке форм слова.
- Сам тоже используй эти слова в своих репликах — ученик слышит правильную форму.
- Не превращай разговор в список упражнений: одна естественная реплика + один вопрос. Не называй слова «из набора» и не проси «используй слово X», если ученик сам не просит.
- Если ученик явно не знает слово — дай его в new_words и в следующем вопросе дай шанс сказать его самому."""


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
