"""Каталог правил: к какому правилу относится ошибка. Нужен, чтобы ошибки группировались стабильно."""

CATALOG = [
    "Родительный падеж после отрицания",
    "Родительный падеж (после do, bez, od, z, u, dla, ilości)",
    "Винительный падеж (прямое дополнение)",
    "Местный падеж после w / na / o / przy / po",
    "Творительный падеж (z kim, być kim)",
    "Дательный падеж",
    "Звательный падеж, обращение",
    "Множественное число существительных",
    "Род существительных",
    "Согласование прилагательных (род, число, падеж)",
    "Прилагательное или наречие",
    "Спряжение -am / -asz",
    "Спряжение -ę / -isz / -ysz",
    "Спряжение -uję / -ujesz (-ować)",
    "Спряжение -ę / -esz и чередования",
    "Неправильные глаголы (być, mieć, iść, jeść, chcieć, móc, wiedzieć)",
    "Прошедшее время",
    "Будущее время",
    "Вид глагола (совершенный / несовершенный)",
    "Возвратные глаголы (się)",
    "Управление глаголов и предлоги",
    "Предлоги места и направления (do, na, w)",
    "Личные местоимения (mnie, mi, go, jej, niej…)",
    "Притяжательные местоимения, swój",
    "Числительные и сочетание с существительными",
    "Порядок слов",
    "Вопросительные слова и союзы (który, jaki, że, żeby)",
    "Конструкция «X to Y», to jest",
    "Лексика: неверное слово или калька с русского",
    "Произношение: смягчение (ki, gi, ni, si, ci, li)",
    "Произношение: cz / sz / rz / ż и ć / ś / ź",
    "Произношение: y / i, ą / ę, ó / u",
    "Произношение: ударение и прочее",
    "Другое",
]

OTHER = "Другое"
UNSORTED = "Без категории"
_BY_KEY = {r.lower(): r for r in CATALOG}


def normalize(rule: str | None) -> str:
    """Правило из каталога; всё неизвестное — «Другое»."""
    if not rule:
        return OTHER
    r = rule.strip().lower()
    if r in _BY_KEY:
        return _BY_KEY[r]
    for key, name in _BY_KEY.items():  # модель могла чуть сократить название
        if len(r) >= 8 and (r in key or key in r):
            return name
    return OTHER


def catalog_text() -> str:
    return "\n".join(f"- {r}" for r in CATALOG)


def group(corrections: list[dict]) -> list[tuple[str, int, list[dict]]]:
    """[(правило, число ошибок, уникальные примеры)] — по убыванию числа ошибок."""
    buckets: dict[str, list[dict]] = {}
    for c in corrections:
        buckets.setdefault(c.get("rule") or UNSORTED, []).append(c)
    out = []
    for rule, items in buckets.items():
        seen, uniq = set(), []
        for c in items:
            key = (str(c.get("original", "")).strip().lower(), str(c.get("correct", "")).strip().lower())
            if key not in seen:
                seen.add(key)
                uniq.append(c)
        out.append((rule, len(items), uniq))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out
