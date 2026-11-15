"""🔊 Озвучка упражнений: текст для edge-tts.

Задание — польские слова / предложения по порядку, перед каждым номер по-польски («jeden», «dwa»…),
пропуск ___ — пауза (ответ не звучит), варианты ответа — «a, …; b, …». Русские пункты (карточки ru→pl,
вопросы по-русски) пропускаются.
Правильные ответы — польское слово карточки или предложение целиком с заполненным пропуском."""
import re

_ONES = ["", "jeden", "dwa", "trzy", "cztery", "pięć", "sześć", "siedem", "osiem", "dziewięć", "dziesięć",
         "jedenaście", "dwanaście", "trzynaście", "czternaście", "piętnaście", "szesnaście", "siedemnaście",
         "osiemnaście", "dziewiętnaście"]
_TENS = {2: "dwadzieścia", 3: "trzydzieści", 4: "czterdzieści", 5: "pięćdziesiąt"}
PAUSE = " … "
_MASK = re.compile(r"[^\W\d_]_|_[^\W\d_]")      # пропущенные буквы внутри слова: j_d_n, _wa

_CYR = re.compile(r"[а-яёА-ЯЁ]")
_LAT = re.compile(r"[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
_BRACKETS = re.compile(r"\[[^\]]*\]")                  # транскрипция [ЛЮ-бье]
_CYR_PARENS = re.compile(r"\([^)]*[а-яёА-ЯЁ][^)]*\)")  # пояснения по-русски в скобках
_LEAD = re.compile(r"^\s*[a-hA-H]\)\s*")              # «a) …» — буква пункта в книге
_JUNK = re.compile(r"[^\w\s.,!?;:…'’\-–—/]", re.UNICODE)  # эмодзи, звёздочки, стрелки


def num(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return (_TENS.get(t) or str(n)) + (f" {_ONES[o]}" if o else "")


def is_pl(s: str) -> bool:
    """Польский текст: латинских букв больше, чем кириллицы."""
    return len(_LAT.findall(s)) > len(_CYR.findall(s))


def clean(s: str) -> str:
    s = _BRACKETS.sub("", str(s or ""))
    s = _CYR_PARENS.sub("", s)
    s = _LEAD.sub("", s)
    s = s.replace("___", PAUSE).replace("—", ",").replace("–", ",")
    s = _JUNK.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;:")
    return s


def _end(s: str) -> str:
    return s if s.endswith(("…", ".", "!", "?")) else s + "."


def task_text(items: list[dict]) -> str:
    """Задание целиком; пусто — польского текста в задании нет (карточки ru→pl)."""
    parts = []
    for n, it in enumerate(items, 1):
        if it.get("card"):
            s = it.get("lemma", "") if it.get("dir") == "pl" else ""
        else:
            s = it.get("q", "")
        if _MASK.search(str(s).replace("___", " ")):   # «j_d_n» — пропущенные буквы: вслух бессмысленно
            continue
        s = clean(s)
        if not s or not is_pl(s):
            continue
        opts = [clean(o) for o in it.get("options") or []]
        if opts and all(o and is_pl(o) for o in opts):
            s = _end(s) + " " + "; ".join(f"{'abcd'[j]}, {o}" for j, o in enumerate(opts[:4]))
        parts.append(f"{num(n)}. {_end(s)}")
    return " ".join(parts)


def _fill(q: str, answer: str) -> str:
    """Предложение с ответом на месте пропуска; ответ — уже полное предложение — как есть."""
    answer = re.sub(r"\s+\d+$", "", answer.strip()) if _LAT.search(answer) else answer.strip()
    gaps = q.count("___")
    if not gaps:
        return answer
    if len(answer.split()) >= 3 and answer[:1].isupper() and answer.rstrip()[-1:] in ".!?":
        return answer
    parts = [p.strip() for p in re.split(r"\s*[,/]\s*", answer)] if gaps > 1 else [answer]
    if len(parts) != gaps:
        return q.replace("___", PAUSE) + ". " + answer
    for p in parts:
        q = q.replace("___", p, 1)
    return q


def fill(q: str, answer: str) -> str:
    return _fill(q, answer)


def phrase(it: dict) -> str:
    """Правильный ответ пункта фразой целиком (для дневника ошибок урока); пусто — фразы нет или она не польская."""
    if it.get("card"):
        s = it.get("lemma", "")
    elif it.get("full_pl"):
        s = it["full_pl"]
    else:
        s = _fill(str(it.get("q", "")), str(it.get("answer", "")))
    if _MASK.search(str(s).replace("___", " ")):
        return ""
    s = clean(re.sub(r"\([^)]*\)", "", str(s)))
    return s if s and is_pl(s) else ""


def answers_text(items: list[dict]) -> str:
    """Правильные ответы по порядку (только польские)."""
    parts = []
    for n, it in enumerate(items, 1):
        if it.get("card"):
            s = it.get("lemma", "")
        elif it.get("full_pl"):
            s = it["full_pl"]
        else:
            s = _fill(str(it.get("q", "")), str(it.get("answer", "")))
        s = clean(re.sub(r"\([^)]*\)", "", s))   # подсказки в скобках — «Ja ___ (mieć) brata» → «Ja mam brata»
        if not s or not is_pl(s):
            continue
        parts.append(f"{num(n)}. {_end(s)}")
    return " ".join(parts)
