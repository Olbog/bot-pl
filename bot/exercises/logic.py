"""Упражнения: разбор ответов, быстрая проверка кодом, уникальность пунктов."""
import re
import unicodedata

LETTERS = "abcdefgh"   # варианты ответа; больше четырёх — в упражнениях «соедини» из книги
UNSURE_RE = re.compile(r"(?<![A-Za-zА-Яа-яЁё])НУ(?![A-Za-zА-Яа-яЁё])")
UNSURE_MARKERS = False   # НУ / ? — «не уверен». Выключено: объясняются все пункты, кроме помеченных «!»
SURE_MARK = "!"   # «уверен» — пункт можно не объяснять
NOTE_RE = re.compile(r"\(([^()]*)\)")   # уточнение или вопрос к пункту — в скобках
NOTE_TOKEN_RE = re.compile(r"⟦(\d+)⟧")
# Номер пункта в начале строки или после пробела: «1 », «1.», «1)», «1:», «1-»
NUM_RE = re.compile(r"(?:(?<=\s)|^)(\d{1,2})\s*[.):\-]?\s*")

POLISH_STRIP = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def norm(s: str) -> str:
    """Для сравнения ответов: регистр, пробелы, знаки препинания по краям — не важны; польские буквы — важны."""
    s = unicodedata.normalize("NFC", s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .,!?;:«»\"'")


def norm_sentence(s: str) -> str:
    """Ключ уникальности предложения."""
    s = norm(s).replace("___", "_").replace("…", "_")
    return re.sub(r"[^\w_ ]", "", s)


def diacritics_only(user: str, right: str) -> bool:
    """Ответ отличается только польскими буквами (pije вместо piję)."""
    u, r = norm(user), norm(right)
    return u != r and u.translate(POLISH_STRIP) == r.translate(POLISH_STRIP)


def unclosed_note(text: str) -> bool:
    """Открыта скобка, но не закрыта — всё до конца сообщения стало уточнением."""
    return (text or "").count("(") > (text or "").count(")")


def _hide_notes(text: str) -> tuple[str, list[str]]:
    """Скобки → метки ⟦k⟧, чтобы числа внутри уточнений («3 л. ед. ч.») не считались номерами пунктов."""
    notes: list[str] = []

    def keep(m):
        notes.append(m.group(1).strip())
        return f" ⟦{len(notes) - 1}⟧ "
    text = NOTE_RE.sub(keep, text)
    if "(" in text:  # незакрытая скобка — до конца сообщения
        head, _, tail = text.partition("(")
        notes.append(tail.replace(")", "").strip())
        text = f"{head} ⟦{len(notes) - 1}⟧ "
    return text, notes


def _one(raw: str, notes: list[str]) -> dict:
    """Ответ на пункт: вне скобок — ответ и маркер «!» (уверен); в скобках — уточнение."""
    note = "; ".join(notes[int(k)] for k in NOTE_TOKEN_RE.findall(raw) if notes[int(k)])
    raw = NOTE_TOKEN_RE.sub(" ", raw)
    unsure = UNSURE_MARKERS and (bool(UNSURE_RE.search(raw)) or raw.rstrip().endswith("?"))
    sure = SURE_MARK in raw and not unsure
    ans = raw.replace(SURE_MARK, " ")
    if UNSURE_MARKERS:
        ans = UNSURE_RE.sub(" ", ans).strip().rstrip("?")
    return {"answer": re.sub(r"\s+", " ", ans).strip(), "unsure": unsure, "sure": sure, "note": note}


def parse_answers(text: str, n: int) -> dict[int, dict]:
    """«1 piję (ja → -ę) 2 lubi (почему не lubią?) 3 mam!» → {1: {answer, unsure, sure, note}, ...}.
    Номера должны идти по возрастанию и быть в пределах 1..n; всё между номерами — ответ, в скобках — уточнение."""
    text, notes = _hide_notes((text or "").replace("\n", " \n "))
    marks = []
    last = 0
    for m in NUM_RE.finditer(text):
        num = int(m.group(1))
        if last < num <= n:
            marks.append((num, m.start(), m.end()))
            last = num
    out: dict[int, dict] = {}
    if not marks:  # без номеров: по строкам или через запятую, если ответов ровно n
        parts = [p.strip() for p in re.split(r"\n|,|;", text) if p.strip()]
        if len(parts) == n:
            for i, raw in enumerate(parts, 1):
                out[i] = _one(raw, notes)
        return out
    for i, (num, _, end) in enumerate(marks):
        stop = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        out[num] = _one(text[end:stop].strip(), notes)
    return out


CHOICE_TYPES = ("choice", "match", "tf")   # упражнения из книги с ответом кнопками


def book_options(ex: dict, item: dict) -> list[str]:
    """Варианты ответа пункта упражнения из книги: свои, общие («соедини») или prawda / nieprawda."""
    return item.get("options") or ex.get("options") or (["prawda", "nieprawda"] if ex.get("type") == "tf" else [])


def option_text(item: dict, answer: str) -> str:
    """Для теста: «b» → текст варианта b."""
    a = norm(answer)
    opts = item.get("options") or []
    if len(a) == 1 and a in LETTERS and LETTERS.index(a) < len(opts):
        return opts[LETTERS.index(a)]
    return answer


def quick_check(items: list[dict], answers: dict[int, dict], fmt: str) -> list[dict]:
    """Проверка кодом. status: ok / wrong / diacritics / missing / check (нужна проверка моделью)."""
    out = []
    for i, it in enumerate(items, 1):
        a = answers.get(i)
        res = {"n": i, "user": "", "unsure": False, "sure": False, "note": "", "status": "missing"}
        if a and a["answer"]:
            user = option_text(it, a["answer"]) if fmt == "test" else a["answer"]
            res.update(user=user, unsure=a["unsure"], sure=a.get("sure", False))
            accepted = [it.get("answer", "")] + list(it.get("accepted") or [])
            if any(norm(user) == norm(x) for x in accepted if x):
                res["status"] = "ok"
            elif any(diacritics_only(user, x) for x in accepted if x):
                res["status"] = "diacritics"
            elif fmt == "test":
                res["status"] = "wrong"            # в тесте других правильных вариантов нет
            else:
                res["status"] = "check"            # возможно, другой верный вариант — решит модель
        if a and a.get("note"):
            res["note"] = a["note"]
        out.append(res)
    return out


def needs_model(results: list[dict], explain_all: bool = True) -> list[dict]:
    """Пункты, которым нужно объяснение моделью.
    explain_all — объяснять и верные ответы, кроме помеченных «!»; иначе только ошибки, сомнения, неоднозначные."""
    def need(r: dict) -> bool:
        if r["status"] != "ok" or r["unsure"] or r.get("note"):
            return True
        return explain_all and not r.get("sure")
    return [r for r in results if need(r)]
