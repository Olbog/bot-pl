"""Транскрипция — только там, где польское слово трудно прочитать (ученик уже читает сам).

Трудно: много шипящих подряд или в одном слове (szcz, chrz, żdż, Szczebrzeszyn), очень длинное слово (≥ 12 букв),
нестандартное чтение — заимствования (q, x, v, ee, oo: quiz, weekend, Javier). Длинный текст (больше max_words слов)
транскрипцию не получает: это уже не «трудное слово», а текст целиком."""
import re

_WORD = re.compile(r"[^\W\d_]+")
_SIB = re.compile(r"dż|dź|dz|sz|cz|rz|ż|ź|ś|ć")
_SEQ = ("szcz", "chrz", "trzc", "żdż", "źdź", "dżdż", "rzcz", "strz", "wszcz", "zdrz", "pszcz")
_FOREIGN = re.compile(r"[qxv]|ee|oo")
LONG = 12
MAX_WORDS = 12


def hard_word(word: str) -> bool:
    w = word.lower()
    return (len(w) >= LONG or any(s in w for s in _SEQ) or len(_SIB.findall(w)) >= 4
            or bool(_FOREIGN.search(w)))


def hard(text: str, max_words: int = MAX_WORDS) -> bool:
    words = _WORD.findall(str(text or ""))
    return 0 < len(words) <= max_words and any(hard_word(w) for w in words)


def tr(pl: str, translit: str, max_words: int = MAX_WORDS) -> str:
    """Транскрипция, если её стоит показать; иначе пусто."""
    translit = str(translit or "").strip()
    return translit if translit and hard(pl, max_words) else ""


# для запросов к Gemini: поле translit заполнять только у трудных слов — меньше токенов и быстрее ответ
PROMPT_RULE = ("translit — транскрипция русскими буквами (ударный слог ЗАГЛАВНЫМИ) ТОЛЬКО если польское трудно прочитать: "
               "много шипящих подряд (szcz, chrz, żdż), очень длинное слово, нестандартное чтение (заимствования: weekend, "
               "quiz); иначе translit — пустая строка «». Ученик уже читает по-польски сам.")
