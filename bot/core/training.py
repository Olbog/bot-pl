"""Логика тренировки наборов: прогресс слов, критерий «освоено», повторение."""
from dataclasses import dataclass, field

REVIEW_INTERVALS_DAYS = [3, 7, 14, 30, 60]  # через сколько дней повторять освоенное слово
DAY = 86400


@dataclass
class Criteria:
    streak: int = 20   # правильных употреблений (ошибка не засчитывается, но и не обнуляет)
    forms: int = 3     # разных форм среди них
    days: int = 3      # разных дней среди них


@dataclass
class WordStats:
    total: int = 0                 # всего употреблений
    correct: int = 0               # из них правильно
    errors: int = 0
    streak: float = 0              # правильных употреблений всего (упражнения — с весом 0.1); ошибка не обнуляет
    streak_forms: list[str] = field(default_factory=list)   # разные формы среди правильных (живая речь)
    streak_days: int = 0                                     # разные дни среди правильных (живая речь)
    all_forms: list[str] = field(default_factory=list)


def stats(uses) -> WordStats:
    """uses — строки word_uses по порядку (form, correct, day). Ошибочное употребление не засчитывается,
    но и не обнуляет набранное: прогресс — все правильные употребления, их формы и дни."""
    s = WordStats()
    streak_forms: list[str] = []
    streak_days: set[str] = set()
    all_forms: list[str] = []
    for u in uses:
        s.total += 1
        form = (u["form"] or "").strip().lower()
        try:
            weight = float(u["weight"])
        except (IndexError, KeyError, TypeError):
            weight = 1.0
        full = weight >= 1
        if u["correct"]:
            s.correct += 1
            s.streak = round(s.streak + weight, 2)
            if full:  # формы и дни засчитываются только за живую речь
                if form and form not in streak_forms:
                    streak_forms.append(form)
                streak_days.add(u["day"])
            if form and form not in all_forms:
                all_forms.append(form)
        else:
            s.errors += 1
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


def set_block(title: str, rows: list[tuple], c: Criteria, rule_c: Criteria | None = None,
              familiar_now=None) -> str:
    """Блок системной инструкции для режима набора. rows — (item, WordStats), только неосвоенные.
    item kind: word/phrase — слово или выражение; rule — правило, на котором ученик ошибался.
    👌 Знакомые (familiar_at) — ученик их уже свободно использует: вопросы на них не строим, в списке они
    только для отметки в target_uses; familiar_now — одно знакомое, которому подошла очередь (вставить в реплику).
    Если незнакомых не осталось — все идут в обычном режиме."""
    rule_c = rule_c or c
    fam = [(w, s) for w, s in rows if is_familiar(w)]
    active = [(w, s) for w, s in rows if not is_familiar(w)]
    if not active:
        active, fam, familiar_now = rows, [], None
    words = [(w, s) for w, s in active if kind_of(w) != "rule"]
    rules = [(w, s) for w, s in active if kind_of(w) == "rule"]
    parts = [f"ТРЕНИРОВКА. Ученик тренирует набор «{title}». Цель — чтобы он как можно чаще сам говорил "
             f"то, что в наборе, органично, в живом разговоре, и в РАЗНЫХ формах и ситуациях."]
    if words:
        lines = []
        for w, st in sorted(words, key=lambda r: (r[1].streak, len(r[1].streak_forms))):
            forms = ", ".join(st.streak_forms) or "—"
            lines.append(f"- {w['pl']} ({w['ru']}, {w['pos'] or '—'}): правильно {num(st.streak)}/{c.streak}, "
                         f"формы: {forms}")
        parts.append("Слова и выражения (сверху — наименее отработанные):\n" + "\n".join(lines))
    if rules:
        lines = []
        for w, st in sorted(rules, key=lambda r: r[1].streak):
            lines.append(f"- {w['pl']}: правильно {num(st.streak)}/{rule_c.streak}. Прошлые ошибки ученика: {w['ru']}")
        parts.append("Правила, на которых ученик ошибался (сверху — наименее отработанные):\n" + "\n".join(lines)
                     + "\nДля правил: задавай вопросы, ответ на которые требует применить правило (например, для "
                       "«родительный после отрицания» — «czego nie lubisz?», для «местный падеж» — «gdzie byłeś?»). "
                       "Используй ситуации, похожие на прошлые ошибки, но с другими словами. В target_uses для правила "
                       "lemma — название правила ровно как в списке, form — фрагмент ученика, где правило применено.")
    if fam:
        lst = "\n".join(f"- {w['pl']} ({w['ru']})" for w, _ in fam)
        parts.append("ЗНАКОМЫЕ — ученик их уже свободно использует. Вопросы на них НЕ строй и сам их не вставляй "
                     "(кроме одного, указанного ниже). Они здесь только для target_uses — если ученик сам их скажет, "
                     "отметь как обычно:\n" + lst)
        if familiar_now is not None:
            w = familiar_now
            parts.append(f"В ЭТОЙ реплике можно вернуть одно знакомое — {w['pl']} ({w['ru']}): вплети его естественно "
                         "в свою фразу или вопрос, только его одного из знакомых, основной фокус — на словах выше.")
    parts.append("""Как вести тренировку:
- Строй вопросы так, чтобы ученику пришлось ответить, используя 1–2 элемента из набора. В первую очередь — наименее отработанные.
- Меняй грамматику: спрашивай про него самого (ja), про собеседника (ty), про девушку/друга (ona/on), про «вас» и «их» (my/wy/oni), про прошлое и будущее, «сколько?» (мн. ч.), «где? / о чём? / без чего?» (разные падежи). Добивайся форм, которых ещё нет в списке форм.
- Сам тоже используй эти слова и конструкции в своих репликах — ученик слышит правильную форму.
- Не превращай разговор в список упражнений: одна естественная реплика + один вопрос. Не называй элементы «из набора» и не проси «используй слово X», если ученик сам не просит.
- Если ученик явно не знает слово — дай его в new_words и в следующем вопросе дай шанс сказать его самому.""")
    return "\n\n".join(parts)


FAMILIAR_MIN_TURNS = 6    # 👌 знакомое возвращается не раньше, чем через столько ответов ученика…
FAMILIAR_ROUNDS = 2       # …и не раньше, чем через 2 круга по незнакомым (2 × их число ответов)


def is_familiar(w) -> bool:
    try:
        return w["familiar_at"] is not None
    except (IndexError, KeyError):
        return False


def familiar_gap(n_active: int) -> int:
    """Сколько ответов ученика должно пройти, прежде чем 👌 знакомое снова прозвучит."""
    return max(FAMILIAR_MIN_TURNS, FAMILIAR_ROUNDS * n_active)


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


def num(x: float) -> str:
    """20 → «20», 3.4 → «3.4»."""
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"
