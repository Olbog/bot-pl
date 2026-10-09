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
NUM_RE = re.compile(r"(?:(?<=[\s;,])|^)(\d{1,2})\s*[.):\-]?\s*")

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


# «4-» — пункт 4 не знаю; «4-8-» — с 4 по 8 не знаю (дальше — конец, запятая, «;», «.», цифра или перевод строки)
SKIP_RANGE_RE = re.compile(r"(?:(?<=[\s,;.])|^)(\d{1,2})\s*-\s*(\d{1,2})\s*-(?=\s*(?:$|[,;.\n]|\d))")
SKIP_ONE_RE = re.compile(r"(?:(?<=[\s,;.])|^)(\d{1,2})\s*-(?=\s*(?:$|[,;.\n]|\d))")


# «5+» — пункт 5 засчитать вручную (✋); «4+8+» или «4-8+» — с 4 по 8
DONE_RANGE_RE = re.compile(r"(?:(?<=[\s,;.])|^)(\d{1,2})\s*[-+]\s*(\d{1,2})\s*\+(?=\s*(?:$|[,;.\n]|\d))")
DONE_ONE_RE = re.compile(r"(?:(?<=[\s,;.])|^)(\d{1,2})\s*\+(?=\s*(?:$|[,;.\n]|\d))")


def _marks(text: str, n: int, one_re, range_re) -> tuple[str, set[int]]:
    found: set[int] = set()

    def rng(m):
        a, b = sorted((int(m.group(1)), int(m.group(2))))
        found.update(k for k in range(a, b + 1) if 1 <= k <= n)
        return " ; "

    def one(m):
        if 1 <= int(m.group(1)) <= n:
            found.add(int(m.group(1)))
        return " ; "
    text = range_re.sub(rng, text)
    return one_re.sub(one, text), found


def _skips(text: str, n: int) -> tuple[str, set[int]]:
    skipped: set[int] = set()

    def rng(m):
        a, b = sorted((int(m.group(1)), int(m.group(2))))
        skipped.update(k for k in range(a, b + 1) if 1 <= k <= n)
        return " ; "

    def one(m):
        if 1 <= int(m.group(1)) <= n:
            skipped.add(int(m.group(1)))
        return " ; "
    text = SKIP_RANGE_RE.sub(rng, text)
    return SKIP_ONE_RE.sub(one, text), skipped


def _boundary(before: str) -> bool:
    """Перед номером — начало, новая строка, «;» или «.»: тогда номер точно начинает новый пункт."""
    tail = before.rstrip(" \t")
    return not tail or tail[-1] in "\n;."


def parse_answers(text: str, n: int, numeric: bool = False) -> dict[int, dict]:
    """«1 piję (ja → -ę) 2 lubi (почему не lubią?) 3 mam!» → {1: {answer, unsure, sure, note}, ...}.
    Номера — в любом порядке (каждый пункт один раз, в пределах 1..n); всё до следующего номера — ответ,
    в скобках — уточнение. «4-» или «4-8-» — «не знаю»: пункт с пустым ответом и skip=True;
    «5+» или «4+8+» — засчитать вручную: пункт с пустым ответом и done=True.
    numeric — в правильных ответах есть цифры: новый пункт начинается только с новой строки, после «;» или «.»."""
    text, notes = _hide_notes((text or "").replace("\n", " \n "))
    text, done = _marks(text, n, DONE_ONE_RE, DONE_RANGE_RE)
    text, skipped = _skips(text, n)
    skipped -= done
    marks = []
    used: set[int] = set(skipped) | done
    for m in NUM_RE.finditer(text):
        num = int(m.group(1))
        if not 1 <= num <= n or num in used:
            continue
        if numeric and marks and not _boundary(text[:m.start()]):
            continue
        marks.append((num, m.start(), m.end()))
        used.add(num)
    out: dict[int, dict] = {k: {"answer": "", "unsure": False, "sure": False, "note": "", "skip": True}
                            for k in skipped}
    out.update({k: {"answer": "", "unsure": False, "sure": False, "note": "", "done": True} for k in done})
    if not marks:  # без номеров: по строкам или через запятую, если ответов ровно n
        parts = [p.strip() for p in re.split(r"\n|,|;", text) if p.strip()]
        if not skipped and not done and len(parts) == n:
            for i, raw in enumerate(parts, 1):
                out[i] = _one(raw, notes)
        return out
    for i, (num, _, end) in enumerate(marks):
        stop = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        out[num] = _one(re.sub(r"[\s,;]+$", "", text[end:stop].strip()), notes)
    return out


CHOICE_TYPES = ("choice", "match", "tf")   # упражнения из книги с ответом кнопками


def book_options(ex: dict, item: dict) -> list[str]:
    """Варианты ответа пункта упражнения из книги: свои, общие («соедини») или prawda / nieprawda."""
    return item.get("options") or ex.get("options") or (["prawda", "nieprawda"] if ex.get("type") == "tf" else [])


def book_pages(ex: dict) -> list[int]:
    """Страницы книги упражнения: поле pages или «s. 54» / «s. 54–55» из ref."""
    if ex.get("pages"):
        return [int(p) for p in ex["pages"]]
    out: list[int] = []
    for a, b in re.findall(r"s\.\s*(\d+)(?:\s*[–-]\s*(\d+))?", ex.get("ref") or ""):
        out += list(range(int(a), int(b or a) + 1))
    return out[:4]


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
