"""Глагол в разборе: инфинитив, перевод, тип спряжения, основные формы."""
from html import escape as e

IRREGULAR_CONJ = "неправильный"
CONJ_TYPES = ["-am / -asz", "-ę / -isz / -ysz", "-uję / -ujesz", "-ę / -esz", "-em / -esz", IRREGULAR_CONJ]
# Для этих глаголов тип спряжения ставит код, а не модель.
# Как в каталоге правил: «Неправильные глаголы (być, mieć, iść, jeść, chcieć, móc, wiedzieć)» + их приставочные.
IRREGULAR = {"być", "mieć", "iść", "jeść", "chcieć", "móc", "wiedzieć", "dać", "wziąć",
             "pomóc", "przyjść", "wyjść", "pójść", "zjeść", "powiedzieć"}

PROMPT_RULE = ("если речь о глаголе — verb_inf: инфинитив; verb_translit: его транскрипция русскими буквами, "
               "ударный слог заглавными; verb_ru: перевод инфинитива; verb_conj: тип спряжения — СТРОГО одно из: "
               + ", ".join(f"«{c}»" for c in CONJ_TYPES) + "; verb_forms: формы ja, ty, oni через запятую "
               "(например: płacę, płacisz, płacą). Если это не глагол — все verb_* пустые строки.")

FIELDS = ["verb_inf", "verb_translit", "verb_ru", "verb_conj", "verb_forms"]
SCHEMA_PROPS = {f: {"type": "STRING"} for f in FIELDS}


def conj_type(inf: str, conj: str) -> str:
    """Приводит тип спряжения к одному названию; для известных глаголов решает код."""
    inf = (inf or "").strip().lower()
    c = (conj or "").strip().lower()
    if inf in IRREGULAR:
        return IRREGULAR_CONJ
    if inf.endswith("ować") or inf.endswith("ować się") or "uj" in c:
        return "-uję / -ujesz"
    if "непр" in c:
        return IRREGULAR_CONJ
    if "isz" in c or "ysz" in c:
        return "-ę / -isz / -ysz"
    if "-em" in c or c.startswith("em"):
        return "-em / -esz"
    if "asz" in c:
        return "-am / -asz"
    if "esz" in c:
        return "-ę / -esz"
    return conj.strip()


def verb_line(d: dict) -> str:
    """«🔤 płacić [ПЛА-чичь] — платить · спряжение -ę / -isz / -ysz: płacę, płacisz, płacą» или ''."""
    inf = str(d.get("verb_inf") or "").strip()
    if not inf:
        return ""
    line = f"🔤 <b>{e(inf)}</b>"
    if d.get("verb_translit"):
        line += f" [{e(str(d['verb_translit']))}]"
    if d.get("verb_ru"):
        line += f" — {e(str(d['verb_ru']))}"
    conj = conj_type(inf, str(d.get("verb_conj") or ""))
    if conj:
        line += f" · спряжение {e(conj)}"
    if d.get("verb_forms"):
        line += f": {e(str(d['verb_forms']))}"
    return line
