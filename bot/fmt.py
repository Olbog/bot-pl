"""Оформление сообщений (Telegram HTML)."""
from html import escape as e

from .gemini import Turn
from .training import Criteria, WordStats, progress_bar

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
    "/new — новая тема (начать разговор заново)\n"
    "/itog — новые слова и ошибки за текущий разговор\n"
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
    line = f"{icon} <b>{e(w['pl'])}</b>"
    if w["translit"]:
        line += f" [{e(w['translit'])}]"
    line += f" — {e(w['ru'])}"
    if w["mastered_at"] is None:
        line += (f"\n     {progress_bar(st.streak, c.streak)} {st.streak}/{c.streak}"
                 f" · формы {len(st.streak_forms)}/{c.forms} · дни {st.streak_days}/{c.days}")
        if st.streak_forms:
            line += f"\n     <i>{e(', '.join(st.streak_forms[:8]))}</i>"
    return line


def set_progress(title: str, rows: list[tuple], c: Criteria, mode: str) -> str:
    done = sum(1 for w, _ in rows if w["mastered_at"] is not None)
    head = f"📚 <b>Набор «{e(title)}»</b> — освоено {done} из {len(rows)}"
    mode_line = "Режим: 🎯 тренировка набора" if mode == "set" else "Режим: 🏁 свободный разговор"
    legend = (f"<i>Освоено = {c.streak} раз подряд без ошибок, минимум {c.forms} формы, в {c.days} разных дня. "
              f"Или отметь сам.</i>")
    return "\n".join([head, mode_line, ""] + [word_line(w, st, c) for w, st in rows] + ["", legend])


def set_buttons(mode: str, has_set: bool) -> list[list[tuple[str, str]]]:
    rows = []
    if has_set:
        rows.append([("🏁 Свободный разговор", "mode:free")] if mode == "set"
                    else [("🎯 Тренировать набор", "mode:set")])
    rows.append([("➕ Набор по теме", "s:topic"), ("✍️ Свои слова", "s:own")])
    if has_set:
        rows.append([("✅ Отметить освоенные", "m:list")])
    return rows


def preview_message(p: dict, carry: list) -> str:
    lines = [f"📝 <b>Новый набор «{e(p['title'])}»</b>", ""]
    off = set(p.get("off", []))
    for i, w in enumerate(p["words"]):
        mark = "❌" if i in off else "•"
        txt = f"{mark} <b>{e(w['pl'])}</b> [{e(w.get('translit', ''))}] — {e(w.get('ru', ''))}"
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
    rows.append([("✍️ Добавить свои", "p:add"), ("✖️ Отмена", "p:cancel")])
    return rows


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
