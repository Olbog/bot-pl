"""Оформление сообщений (Telegram HTML)."""
from html import escape as e

from .gemini import Turn

KIND_ICON = {"word": "🔤", "grammar": "📝", "pronunciation": "🗣"}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def turn_message(t: Turn, from_voice: bool, show_model: bool = False) -> str:
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
    "Я исправлю явные ошибки, подскажу польские слова и отвечу — текстом и голосом.\n\n"
    "/new — новая тема (начать разговор заново)\n"
    "/itog — новые слова и ошибки за текущий разговор\n"
    "/help — эта подсказка"
)
