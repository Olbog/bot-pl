"""Оформление сообщений (Telegram HTML)."""
from html import escape as e

from .gemini import Turn
from .config import local_dt

from .rules import group
from .verbs import verb_line
from .training import Criteria, WordStats, kind_of, num, progress_bar

KIND_ICON = {"word": "🔤", "grammar": "📝", "pronunciation": "🗣"}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def turn_message(t: Turn, from_voice: bool, show_model: bool = False, target_line: str = "") -> str:
    out: list[str] = []
    if from_voice and t.user_text:
        out.append(f"🎙 <i>{e(t.user_text)}</i>")

    if t.corrections:
        lines = ["✏️ <b>Исправления</b>"]
        for c in t.corrections:
            icon = KIND_ICON.get(str(c.get("kind", "")).lower(), "•")
            line = f"{icon} {e(c.get('original', ''))} → <b>{e(c.get('correct', ''))}</b>"
            if c.get("translit"):
                line += f" [{e(c['translit'])}]"
            if c.get("ru"):
                line += f" — {e(c['ru'])}"
            if c.get("why"):
                line += f"\n   <i>{e(c['why'])}</i>"
            if verb_line(c):
                line += f"\n   {verb_line(c)}"
            lines.append(line)
        out.append("\n".join(lines))

    differs = t.corrected_pl and _norm(t.corrected_pl) != _norm(t.user_text)
    if t.corrections or differs:
        if t.corrected_pl:
            block = f"✔️ <b>Правильно:</b> {e(t.corrected_pl)}"
            if t.corrected_translit:
                block += f"\n[{e(t.corrected_translit)}]"
            if t.corrected_ru:
                block += f"\n— {e(t.corrected_ru)}"
            out.append(block)
    elif t.user_text and not t.new_words:
        out.append("✅ Без явных ошибок")

    if t.new_words:
        lines = ["🆕 <b>Новые слова</b>"]
        for w in t.new_words:
            lines.append(f"• {e(w.get('ru', ''))} → <b>{e(w.get('pl', ''))}</b> [{e(w.get('translit', ''))}]")
        out.append("\n".join(lines))

    reply = f"💬 <b>{e(t.reply_pl)}</b>"
    if t.reply_translit:
        reply += f"\n[{e(t.reply_translit)}]"
    if t.reply_ru:
        reply += f"\n— {e(t.reply_ru)}"
    out.append(reply)
    if target_line:
        out.append(target_line)
    if show_model and t.model:
        out.append(f"<i>· {e(t.model.removeprefix('gemini-'))}</i>")
    return "\n\n".join(out)


def summary_message(words: list[dict], corrections: list[dict], turns: int) -> str:
    if not turns:
        return "В этом разговоре пока ничего не было. Напиши или скажи что-нибудь по-польски."
    out = [f"📋 <b>Итог разговора</b> — реплик: {turns}"]

    seen: set[str] = set()
    wl = []
    for w in words:
        key = w["pl"].lower()
        if key in seen:
            continue
        seen.add(key)
        wl.append(f"• <b>{e(w['pl'])}</b> [{e(w['translit'])}] — {e(w['ru'])}")
    out.append("🆕 <b>Новые слова</b>\n" + ("\n".join(wl) if wl else "—"))

    if corrections:
        cl = []
        for c in corrections:
            line = f"• {e(c.get('original', ''))} → <b>{e(c.get('correct', ''))}</b>"
            if c.get("translit"):
                line += f" [{e(c['translit'])}]"
            if c.get("why"):
                line += f" — <i>{e(c['why'])}</i>"
            cl.append(line)
        out.append("✏️ <b>Ошибки</b>\n" + "\n".join(cl))
    else:
        out.append("✏️ <b>Ошибки</b>\n—")
    return "\n\n".join(out)


HELP = (
    "Пиши или говори голосом по-польски. Не знаешь слово — вставь его по-русски.\n\n"
    "Я исправлю ошибки, подскажу польские слова и отвечу — текстом и голосом.\n\n"
    "<b>Режимы</b>\n"
    "🎯 Набор — тренируем 10 слов, пока не освоишь: я строю разговор так, чтобы ты говорил их "
    "в разных формах.\n"
    "🏁 Свободный — разговор на любую тему, иногда подмешиваю давно не звучавшие освоенные слова.\n\n"
    "/set — набор: прогресс, новый набор, отметить освоенные\n"
    "/free — свободный разговор\n"
    "/ex — упражнения: слова, грамматика, правила, мои ошибки, голосом\n"
    "/new — новая тема (начать разговор заново)\n"
    "/itog — итог: ошибки по правилам и новые слова за час / сутки / разговор, всё время — файлом\n"
    "/dict — словарь ⭐: выражения, которые ты сохранил, чтобы ввести в речь\n"
    "/rule вопрос — объяснить правило своими словами\n"
    "/export — выгрузить всё файлом: ошибки по правилам, слова и словарь (сам — раз в неделю, пн 04:00)\n\n"
    "Под ответами: 📖 Правило — разбор ошибок этого сообщения, ⭐ В словарь — сохранить выражения.\n\n"
    "/cancel — отменить создание набора\n"
    "/help — эта подсказка"
)


# ---------- наборы ----------

def word_line(w, st: WordStats, c: Criteria) -> str:
    if w["mastered_at"] is not None:
        icon = "✅"
    elif st.streak > 0:
        icon = "🟡"
    elif st.errors:
        icon = "🔴"
    else:
        icon = "⚪"
    if kind_of(w) == "rule":
        line = f"{icon} 📐 <b>{e(w['pl'])}</b>"
    else:
        line = f"{icon} <b>{e(w['pl'])}</b>"
        if w["translit"]:
            line += f" [{e(w['translit'])}]"
        line += f" — {e(w['ru'])}"
    if w["mastered_at"] is None:
        line += (f"\n     {progress_bar(st.streak, c.streak)} {num(st.streak)}/{c.streak}"
                 f" · формы {len(st.streak_forms)}/{c.forms} · дни {st.streak_days}/{c.days}")
        if st.streak_forms:
            line += f"\n     <i>{e(', '.join(st.streak_forms[:8]))}</i>"
    return line


def set_progress(title: str, rows: list[tuple], c: Criteria, mode: str, rule_c: Criteria | None = None) -> str:
    rule_c = rule_c or c
    done = sum(1 for w, _ in rows if w["mastered_at"] is not None)
    head = f"📚 <b>Набор «{e(title)}»</b> — освоено {done} из {len(rows)}"
    mode_line = "Режим: 🎯 тренировка набора" if mode == "set" else "Режим: 🏁 свободный разговор"
    legend = (f"<i>Освоено: слово — {c.streak} раз подряд без ошибок, {c.forms} формы, {c.days} дня"
              + (f"; 📐 правило — {rule_c.streak} раз, {rule_c.forms} ситуаций, {rule_c.days} дней"
                 if any(kind_of(w) == "rule" for w, _ in rows) else "")
              + ". Или отметь сам.</i>")
    lines = [word_line(w, st, rule_c if kind_of(w) == "rule" else c) for w, st in rows]
    return "\n".join([head, mode_line, ""] + lines + ["", legend])


def set_buttons(mode: str, has_set: bool) -> list[list[tuple[str, str]]]:
    rows = []
    if has_set:
        rows.append([("🏁 Свободный разговор", "mode:free")] if mode == "set"
                    else [("🎯 Тренировать набор", "mode:set")])
    rows.append([("➕ Набор по теме", "s:topic"), ("✍️ Свои слова", "s:own")])
    rows.append([("🧩 Из ошибок", "e:start"), ("⭐ Из словаря", "s:dict")])
    if has_set:
        rows.append([("✅ Отметить освоенные", "m:list")])
    return rows


def preview_message(p: dict, carry: list) -> str:
    lines = [f"📝 <b>Новый набор «{e(p['title'])}»</b>", ""]
    off = set(p.get("off", []))
    for i, w in enumerate(p["words"]):
        mark = "❌" if i in off else "•"
        tr = f" [{e(w.get('translit', ''))}]" if w.get("translit") else ""
        txt = f"{mark} <b>{e(w['pl'])}</b>{tr} — {e(w.get('ru', ''))}"
        lines.append(f"<s>{txt}</s>" if i in off else txt)
    if carry:
        lines += ["", "<b>Переходят из прошлого набора:</b>"]
        lines += [f"↪ <b>{e(w['pl'])}</b> — {e(w['ru'])}" for w in carry]
    lines += ["", "<i>Нажми на слово, чтобы убрать или вернуть его.</i>"]
    return "\n".join(lines)


def preview_buttons(p: dict) -> list[list[tuple[str, str]]]:
    off = set(p.get("off", []))
    word_btns = [((("❌ " if i in off else "") + w["pl"])[:30], f"p:{i}") for i, w in enumerate(p["words"])]
    rows = [word_btns[i:i + 3] for i in range(0, len(word_btns), 3)]
    extra = [("🔄 Другие слова", "p:regen")] if p.get("topic") else []
    rows.append([("✅ Начать", "p:ok")] + extra)
    rows.append([("✍️ Добавить свои", "p:add")])
    return rows


def nav_row(step: str, back: bool) -> list[tuple[str, str]]:
    """Ряд «⬅️ Назад / ✖️ Отмена» под шагом выбора; шаг в данных — чтобы старые кнопки не срабатывали."""
    return ([("⬅️ Назад", f"nav:b:{step}")] if back else []) + [("✖️ Отмена", f"nav:c:{step}")]


def mastered_message(title: str) -> str:
    return (f"✅ <b>Отметить освоенные — «{e(title)}»</b>\n\n"
            "Нажми на слово, которое уже знаешь уверенно — оно уйдёт из тренировки в повторение. "
            "Нажми ещё раз, чтобы вернуть.")


def mastered_buttons(words: list) -> list[list[tuple[str, str]]]:
    btns = [((("✅ " if w["mastered_at"] is not None else "⚪ ") + w["pl"])[:30], f"m:{w['id']}") for w in words]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([("↩️ К прогрессу", "s:show")])
    return rows


def target_line(uses: list[tuple[str, str, bool]]) -> str:
    """uses — (lemma, form, correct) для слов набора в этой реплике."""
    if not uses:
        return ""
    parts = [f"{e(form or lemma)} {'✓' if ok else '✗'}" for lemma, form, ok in uses]
    return "🎯 " + " · ".join(parts)


# ---------- правила ----------

def rules_message(data: dict, header: str = "📖") -> str:
    blocks = []
    for r in data.get("rules") or []:
        if not isinstance(r, dict):
            continue
        lines = [f"{header} <b>{e(r.get('title', ''))}</b>", e(r.get("explanation", ""))]
        for ex in r.get("examples") or []:
            if isinstance(ex, dict):
                lines.append(f"• <b>{e(ex.get('pl', ''))}</b> [{e(ex.get('translit', ''))}] — {e(ex.get('ru', ''))}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "Не получилось объяснить правило, попробуй ещё раз."


def reply_buttons(msg_id: int, has_corrections: bool) -> list[list[tuple[str, str]]]:
    row = [("📖 Правило", f"r:{msg_id}")] if has_corrections else []
    row.append(("⭐ В словарь", f"d:{msg_id}"))
    return [row]


# ---------- словарь ⭐ ----------

def offer_message(phrases: list[dict], saved: list[int]) -> str:
    lines = ["⭐ <b>Что сохранить в словарь?</b> Нажимай на выражения.", ""]
    for i, p in enumerate(phrases):
        mark = "✅" if i in saved else "•"
        lines.append(f"{mark} <b>{e(p.get('pl', ''))}</b> [{e(p.get('translit', ''))}] — {e(p.get('ru', ''))}")
    return "\n".join(lines)


def offer_buttons(msg_id: int, phrases: list[dict], saved: list[int]) -> list[list[tuple[str, str]]]:
    rows = [[((("✅ " if i in saved else "") + p.get("pl", ""))[:40], f"ds:{msg_id}:{i}")] for i, p in enumerate(phrases)]
    rows.append([("✍️ Своё", "dn:own"), ("✅ Готово", f"dx:{msg_id}")])
    return rows


def dict_message(items: list, limit: int = 40) -> str:
    if not items:
        return ("⭐ Словарь пока пуст.\n\nНажимай «⭐ В словарь» под ответами бота или пришли своё: "
                "/dict add najbardziej lubię, szczerze mówiąc")
    unused = sum(1 for i in items if not i["used_in_set"])
    lines = [f"⭐ <b>Словарь</b> — {len(items)} выражений, ещё не тренировалось: {unused}", ""]
    for i in items[-limit:]:
        mark = "•" if not i["used_in_set"] else "✓"
        tr = f" [{e(i['translit'])}]" if i["translit"] else ""
        lines.append(f"{mark} <b>{e(i['pl'])}</b>{tr} — {e(i['ru'])}")
    if len(items) > limit:
        lines.append(f"\n<i>Показаны последние {limit}. Полный список — файлом.</i>")
    lines.append("\n<i>✓ — уже было в наборе. Добавить своё: /dict add выражение</i>")
    return "\n".join(lines)


def dict_buttons() -> list[list[tuple[str, str]]]:
    return [[("🎯 Набор из словаря", "s:dict"), ("📄 Файлом", "i:dict")]]


# ---------- итоги ----------

PERIODS = {"h": ("последний час", 3600), "d": ("сутки", 86400), "w": ("неделю", 7 * 86400),
           "a": ("всё время", None), "s": ("этот разговор", None)}


def itog_choice() -> tuple[str, list[list[tuple[str, str]]]]:
    return ("📋 <b>Итог за…</b>",
            [[("Последний час", "i:h"), ("Сутки", "i:d"), ("Этот разговор", "i:s")],
             [("📄 Всё время — файлом", "i:fa")]])


def itog_message(period: str, turns: int, corrections: list[dict], words: list[dict], dict_new: int,
                 max_rules: int = 10, max_examples: int = 4) -> str:
    label = PERIODS[period][0]
    if not turns and not corrections:
        return f"📋 За {label} разговоров не было."
    out = [f"📋 <b>Итог за {label}</b> — реплик: {turns}"]
    groups = group(corrections)
    if groups:
        lines = [f"✏️ <b>Ошибки — {len(corrections)}</b>, по правилам (сначала самые частые):"]
        for rule, n, ex in groups[:max_rules]:
            lines.append(f"• <b>{e(rule)}</b> ×{n}")
            sample = " · ".join(f"{e(c.get('original', ''))}→{e(c.get('correct', ''))}" for c in ex[:max_examples])
            more = f" <i>+{len(ex) - max_examples}</i>" if len(ex) > max_examples else ""
            lines.append(f"   {sample}{more}")
        if len(groups) > max_rules:
            lines.append(f"<i>…и ещё правил: {len(groups) - max_rules} — полный список файлом</i>")
        out.append("\n".join(lines))
    else:
        out.append("✏️ Ошибок нет 👍")
    seen, uniq = set(), []
    for w in words:
        if w["pl"].lower() not in seen:
            seen.add(w["pl"].lower())
            uniq.append(w)
    if uniq:
        out.append(f"🆕 <b>Слова, которые ты сказал по-русски — {len(uniq)}</b>\n"
                   + " · ".join(f"{e(w['pl'])} ({e(w['ru'])})" for w in uniq))
    if dict_new:
        out.append(f"⭐ Сохранено в словарь — {dict_new}")
    return "\n\n".join(out)


def itog_buttons(period: str, has_errors: bool) -> list[list[tuple[str, str]]]:
    row = [("📄 Файлом", f"i:f{period}")]
    if has_errors:
        row.append(("🎯 Тренировать эти ошибки", f"e:p:{period}"))
    return [row]


def _dt(ts: float) -> str:
    return local_dt(ts).strftime("%d.%m %H:%M")


def export_text(title: str, corrections: list[dict], words: list[dict], dict_items: list) -> str:
    """Выгрузка в текстовый файл — без HTML."""
    out = [title, "=" * len(title), ""]
    groups = group(corrections)
    out.append(f"ОШИБКИ — {len(corrections)}, по правилам (сначала самые частые)")
    out.append("")
    for rule, n, ex in groups:
        out.append(f"■ {rule} — ×{n}")
        for c in ex:
            line = f"   {c.get('original', '')} → {c.get('correct', '')}"
            if c.get("translit"):
                line += f" [{c['translit']}]"
            if c.get("ru"):
                line += f" — {c['ru']}"
            if c.get("_at"):
                line += f"   ({_dt(c['_at'])})"
            out.append(line)
            if c.get("why"):
                out.append(f"      {c['why']}")
        out.append("")
    if not groups:
        out += ["   нет", ""]
    seen, uniq = set(), []
    for w in words:
        if w["pl"].lower() not in seen:
            seen.add(w["pl"].lower())
            uniq.append(w)
    out.append(f"СЛОВА, СКАЗАННЫЕ ПО-РУССКИ — {len(uniq)}")
    out += [f"   {w['pl']} [{w.get('translit', '')}] — {w['ru']}" for w in uniq] or ["   нет"]
    out.append("")
    out.append(f"СЛОВАРЬ ⭐ — {len(dict_items)}")
    out += [f"   {d['pl']} [{d['translit']}] — {d['ru']}" for d in dict_items] or ["   пусто"]
    return "\n".join(out) + "\n"


# ---------- набор из ошибок ----------

def errsel_message(p: dict) -> str:
    label = PERIODS[p["period"]][0]
    if not p["rules"]:
        return f"🧩 За {label} ошибок нет. Выбери другой период."
    lines = [f"🧩 <b>Набор из ошибок — за {label}</b>",
             "Отметь правила для тренировки (сверху — где больше всего ошибок):", ""]
    on = set(p.get("on", []))
    for i, r in enumerate(p["rules"]):
        lines.append(f"{'✅' if i in on else '⚪'} {e(r['rule'])} ×{r['n']}")
        lines.append(f"     <i>{e(r['examples'])}</i>")
    lines.append("")
    lines.append(f"📖 Правило перед тренировкой: {'вкл' if p.get('rule_first', True) else 'выкл'}")
    return "\n".join(lines)


def errsel_buttons(p: dict) -> list[list[tuple[str, str]]]:
    rows = [[("Час", "e:p:h"), ("Сутки", "e:p:d"), ("Неделя", "e:p:w"), ("Всё", "e:p:a")]]
    on = set(p.get("on", []))
    btns = [((("✅ " if i in on else "⚪ ") + r["rule"])[:40], f"e:t:{i}") for i, r in enumerate(p["rules"])]
    rows += [[b] for b in btns]
    if p["rules"]:
        rows.append([("🎲 Случайные 3", "e:rand"),
                     (f"📖 Правило: {'вкл' if p.get('rule_first', True) else 'выкл'}", "e:rule")])
        rows.append([("▶️ Начать", "e:go")])
    return rows


# ---------- упражнения ----------

EX_KINDS = {
    "words": "📝 Слова: пропуски",
    "voice": "🎙 Упражнения голосом",
    "grammar": "🧩 Грамматика",
    "errors": "🔁 Мои частые ошибки",
}


def ex_menu() -> tuple[str, list[list[tuple[str, str]]]]:
    return ("🏋️ <b>Упражнения</b> — что тренируем?\n\n"
            "<i>В каждом упражнении 10 пунктов. Отвечаешь одним сообщением: «1 piję 2 lubi 3 kupuje». "
            "Каждый пункт бот объясняет; уверен — поставь ! («3 kupuje!»), и этот пункт объяснять не будет. "
            "Уточнение или вопрос к пункту — в скобках: «2 lubi (3 л. ед. ч., почему не lubią?)» — "
            "бот проверит и рассуждение.</i>",
            [[(EX_KINDS["words"], "x:k:words"), (EX_KINDS["voice"], "x:k:voice")],
             [(EX_KINDS["grammar"], "x:k:grammar"), (EX_KINDS["errors"], "x:k:errors")]])


def ex_source_buttons() -> list[list[tuple[str, str]]]:
    return [[("🎯 Текущий набор", "x:s:set"), ("⭐ Словарь", "x:s:dict")],
            [("✍️ Свои слова", "x:s:own"), ("🗂 Тема", "x:s:topic")]]


def ex_format_buttons(kind: str = "") -> list[list[tuple[str, str]]]:
    return [[("🔘 Тест (варианты)", "x:f:test"), ("⌨️ Свой ввод", "x:f:gap")]]


def ex_topic_sources() -> tuple[str, list[list[tuple[str, str]]]]:
    return ("🧩 <b>Что тренируем?</b>\n<i>Одна тема — все пункты на неё. Несколько — пункты вперемешку, "
            "и в каждом нужно понять, какое правило работает.</i>",
            [[("🔁 Из моих ошибок", "x:t:err")],
             [("🕘 Недавние темы", "x:t:recent")],
             [("📐 Выбрать из списка правил", "x:t:cat")],
             [("✍️ Своя тема", "x:t:own")],
             [("🎲 Случайный микс", "x:t:mix")]])


def short_rule(r: str) -> str:
    """«Творительный падеж (z kim, być kim)» → «Творительный падеж» — для кнопок."""
    return r.split(" (")[0]


def ago(ts: float, now: float) -> str:
    days = int((now - ts) // 86400)
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    word = "дней" if 11 <= days % 100 <= 14 else {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(days % 10, "дней")
    return f"{days} {word} назад"


def ex_pick(title: str, labels: list[str], chosen: list[int],
            extra: list[tuple[str, str]] | None = None) -> tuple[str, list[list[tuple[str, str]]]]:
    """Выбор тем галочками: x:p:<i> — переключить, x:p:go — дальше."""
    on = set(chosen)
    rows = [[((("✅ " if i in on else "☐ ") + lab)[:60], f"x:p:{i}")] for i, lab in enumerate(labels)]
    rows.append((extra or []) + [("▶️ Дальше", "x:p:go")])
    return f"{title}\n<i>Нажми, чтобы отметить или снять.</i>", rows


def ex_card_prompt(topics: list[str]) -> tuple[str, list[list[tuple[str, str]]]]:
    text = ("🧩 <b>Тема:</b> " + e(topics[0]) if len(topics) == 1 else
            "🧩 <b>Темы</b> (вперемешку, на различение):\n" + "\n".join(f"• {e(t)}" for t in topics))
    return text, [[("📖 Сначала кратко правило", "x:c:rule"), ("▶️ Сразу упражнения", "x:c:go")]]


def ex_rule_picker(catalog: list[str], chosen: list[int]) -> tuple[str, list[list[tuple[str, str]]]]:
    on = set(chosen)
    text = ("📐 <b>Выбери правило</b> — одно или несколько (тогда пункты перемешаются, и в каждом нужно понять, "
            "какое правило работает). Можно написать своё — кнопкой ниже.\n\n"
            "Выбрано: " + (", ".join(catalog[i] for i in sorted(on)) or "—"))
    btns = [((("✅ " if i in on else "") + r)[:40], f"x:r:{i}") for i, r in enumerate(catalog)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([("✍️ Своё правило", "x:r:own"), ("▶️ Дальше", "x:r:go")])
    return text, rows


def ex_count_prompt() -> tuple[str, list[list[tuple[str, str]]]]:
    return ("Сколько упражнений? Нажми или напиши число (1–20).",
            [[("1", "x:n:1"), ("2", "x:n:2"), ("3", "x:n:3"), ("5", "x:n:5"), ("10", "x:n:10")]])


def ex_message(ex: dict, idx: int, total: int, voice: bool) -> str:
    lines = [f"🏋️ <b>Упражнение {idx}/{total}</b> · #{ex['id']} — {e(ex['title'])}", ""]
    for i, it in enumerate(ex["items"], 1):
        q = e(it.get("q", "")).replace("___", "<b>___</b>")
        rep = " 🔁" if it.get("_reuse_id") else ""
        lines.append(f"{i}. {q}{rep}")
        if it.get("ru"):  # перевод вместо подсказки: по нему понятно, что вставить, но форму не выдаёт
            lines.append(f"    <i>— {e(it['ru'])}</i>")
        if it.get("options"):
            lines.append("    " + "   ".join(f"{'abcd'[j]}) {e(o)}" for j, o in enumerate(it["options"])))
    lines.append("")
    if voice:
        lines.append("🎙 <i>Пришли голосовое: прочитай все предложения по порядку целиком, с заполненными пропусками. "
                     "Номера говорить не обязательно. Уточнение к пункту — скажи «уточнение» и дальше своими словами, "
                     "до следующего предложения.</i>")
    elif ex["items"] and ex["items"][0].get("options"):
        lines.append("<i>Ответ одним сообщением: 1b 2a 3c … · уточнение или вопрос — в скобках: «2a (винительный)» · "
                     "уверен, не объяснять — !: «3c!»</i>")
    else:
        lines.append("<i>Ответ одним сообщением: 1 piję 2 lubi … · уточнение или вопрос — в скобках: "
                     "«1 piję (ja → -ę)» · уверен, не объяснять — !: «2 lubi!»</i>")
    lines.append("<i>Ответ засчитается этому упражнению. Можно и через «Ответить» на это сообщение.</i>")
    if any(it.get("_reuse_id") for it in ex["items"]):
        lines.append("<i>🔁 — пункт на повтор: в прошлый раз была ошибка или сомнение.</i>")
    return "\n".join(lines)


STATUS_ICON = {"ok": "✅", "wrong": "❌", "unsure": "❓"}


def ex_results(ex: dict, results: list[dict]) -> str:
    ok = sum(1 for r in results if r["final"] in ("ok", "unsure"))
    lines = [f"📊 <b>{ok} из {len(results)}</b> · #{ex['id']} — {e(ex['title'])}", ""]
    for r in results:
        it = ex["items"][r["n"] - 1]
        right = it.get("answer", "")
        user = r.get("heard") or r.get("user") or "—"
        icon = "❓✅" if r["final"] == "unsure" else STATUS_ICON[r["final"]]
        if r.get("logic_wrong"):
            head = f"❌ {r['n']}. <b>{e(user)}</b> ✓ ответ верный, ошибка в рассуждении"
        elif r["final"] == "wrong":
            head = f"{icon} {r['n']}. {e(user)} → <b>{e(right)}</b>"
        else:
            head = f"{icon} {r['n']}. <b>{e(user)}</b>"
        lines.append(f"{head} — <i>{e(it.get('grammar', ''))}</i>")
        if verb_line(it):
            lines.append(f"    {verb_line(it)}")
        if r["final"] != "ok" or r.get("explanation"):  # верный ответ с «!» — без разбора
            lines.append(f"    {e(it.get('full_pl', ''))} [{e(it.get('translit', ''))}] — {e(it.get('ru', ''))}")
            if r.get("explanation"):
                lines.append(f"    {e(r['explanation'])}")
            if r.get("bridge"):
                lines.append(f"    🌉 {e(r['bridge'])}")
            if it.get("rule"):
                lines.append(f"    📐 {e(it['rule'])}")
        if r.get("note"):
            mark = "✅ верно" if r.get("note_ok", True) else "❌ не так"
            line = f"    💭 «{e(r['note'])}» — {mark}"
            if r.get("note_comment"):
                line += f": {e(r['note_comment'])}"
            lines.append(line)
    lines.append("")
    lines.append("<i>Вопрос по пункту — напиши: «5: почему не czasem?»</i>")
    return "\n".join(lines)


def ex_result_buttons(ex_id: int, left: int) -> list[list[tuple[str, str]]]:
    row = [("📖 Правила по ошибкам", f"x:rr:{ex_id}")]
    rows = [row]
    if left > 0:
        rows.append([(f"➡️ Следующее (осталось {left})", "x:next"), ("⏹ Закончить", "x:stop")])
    else:
        rows.append([("🏋️ Ещё упражнения", "x:menu")])
    return rows
