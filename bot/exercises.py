"""Упражнения: разбор ответов, быстрая проверка кодом, уникальность пунктов."""
import re
import unicodedata

LETTERS = "abcd"
UNSURE_RE = re.compile(r"(?<![A-Za-zА-Яа-яЁё])НУ(?![A-Za-zА-Яа-яЁё])")
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


def parse_answers(text: str, n: int) -> dict[int, dict]:
    """«1 piję 2 lubi НУ 3 kupuje?» → {1: {answer, unsure}, ...}.
    Номера должны идти по возрастанию и быть в пределах 1..n; всё между номерами — ответ."""
    text = (text or "").replace("\n", " \n ")
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
                unsure = bool(UNSURE_RE.search(raw)) or raw.endswith("?")
                out[i] = {"answer": re.sub(r"\s+", " ", UNSURE_RE.sub(" ", raw)).strip().rstrip("?").strip(),
                          "unsure": unsure}
        return out
    for i, (num, _, end) in enumerate(marks):
        stop = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        raw = text[end:stop].strip()
        unsure = bool(UNSURE_RE.search(raw)) or raw.rstrip().endswith("?")
        ans = UNSURE_RE.sub(" ", raw).strip().rstrip("?").strip()
        out[num] = {"answer": re.sub(r"\s+", " ", ans), "unsure": unsure}
    return out


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
        res = {"n": i, "user": "", "unsure": False, "status": "missing"}
        if a and a["answer"]:
            user = option_text(it, a["answer"]) if fmt == "test" else a["answer"]
            res.update(user=user, unsure=a["unsure"])
            accepted = [it.get("answer", "")] + list(it.get("accepted") or [])
            if any(norm(user) == norm(x) for x in accepted if x):
                res["status"] = "ok"
            elif any(diacritics_only(user, x) for x in accepted if x):
                res["status"] = "diacritics"
            elif fmt == "test":
                res["status"] = "wrong"            # в тесте других правильных вариантов нет
            else:
                res["status"] = "check"            # возможно, другой верный вариант — решит модель
        out.append(res)
    return out


def needs_model(results: list[dict]) -> list[dict]:
    """Пункты, которым нужно объяснение моделью: ошибки, сомнения, неоднозначные."""
    return [r for r in results if r["status"] in ("wrong", "diacritics", "check", "missing") or r["unsure"]]
