"""Оформление сообщений (Telegram HTML)."""
import re
from html import escape as e

from ..ai.gemini import Turn
from ..core.config import local_dt

from ..core.rules import group
from ..core.verbs import verb_line
from ..core.textbook import topic_parts
from ..exercises.logic import CHOICE_TYPES, LETTERS, book_options, book_pages, items_mask
from ..core.training import Criteria, WordStats, is_familiar, kind_of, num, progress_bar

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
    "Пиши или говори голосом по-польски. Не знаешь слово — вставь его по-русски.\n"
    "Я исправлю ошибки, подскажу польские слова и отвечу — текстом и голосом.\n\n"
    "/menu — 🏠 главное меню\n"
    "/new — 💬 новый разговор: по набору, набор из 📚 архива, новый набор (тема, свои слова, юнит, ошибки, словарь) "
    "или без набора (без темы, своя, случайная)\n"
    "/ex — 🏋️ упражнения: слова, грамматика, мои ошибки, голосом, учебник\n"
    "/words — 📖 слова юнита: список с переводом, 🙈 скрытый перевод, 📄 файлом, 🔊 озвучка\n"
    "/phr — 💬 полезные выражения: с транскрипцией, переводом и правилом; карточки, тест, набор для разговора\n"
    "/tw — 🗣 скороговорки и песенки: текст, транскрипция, аудио медленно и в обычном темпе, отметки ⚪🟡✅\n"
    "/read — 📚 чтение: легенды по абзацам с переводом и аудио, слова абзаца в ⭐ словарь, вопросы к главе\n"
    "/set — 🎯 наборы слов: прогресс, новый набор, 📚 архив, отметить освоенные\n"
    "/itog — 📋 итог: ошибки и слова за час / сутки / разговор, всё время — файлом\n"
    "/dict — ⭐ словарь: все слова по темам, наборы по 10 для разговора и упражнений, 📂 сменить тему, 📄 файлом; 🙅 исключения\n"
    "/rule вопрос — 📖 объяснить правило\n"
    "/export — 🗂 выгрузить всё файлом (сам — раз в неделю, пн 04:00)\n\n"
    "Под ответами: 📖 Правило · ⭐ В словарь (мои ошибки / из ответа бота / свои слова) · 🙅 Не ошибка.\n"
    "На любом шаге: ⬅️ Назад и ✖️ Отмена — отмена ничего не сбрасывает, разговор и набор остаются.\n"
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
    if w["mastered_at"] is None and is_familiar(w):
        icon += "👌"
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
    mode_line = ("Сейчас: 🎯 разговор по этому набору" if mode == "set"
                 else "📚 В архиве — прогресс сохранён, можно продолжить с того же места." if mode == "archive"
                 else "Сейчас разговор без набора. Тренировать набор — /new → 🎯")
    legend = (f"<i>Освоено: слово — {c.streak} раз правильно (ошибка не засчитывается, но и не обнуляет), "
              f"{c.forms} формы, {c.days} дня"
              + (f"; 📐 правило — {rule_c.streak} раз, {rule_c.forms} ситуаций, {rule_c.days} дней"
                 if any(kind_of(w) == "rule" for w, _ in rows) else "")
              + ". Или отметь сам.\n👌 — знакомое: вопросы на нём не строятся, возвращается по одному "
                "примерно раз в два круга по остальным словам; из набора уходит только при освоении.</i>")
    lines = [word_line(w, st, rule_c if kind_of(w) == "rule" else c) for w, st in rows]
    return "\n".join([head, mode_line, ""] + lines + ["", legend])


def set_source_rows() -> list[list[tuple[str, str]]]:
    """Откуда взять новый набор — одинаково в /set и в /new → «➕ Новый набор»."""
    return [[("➕ По теме", "s:topic"), ("✍️ Свои слова", "s:own")],
            [("📘 Из юнита", "s:unit"), ("🧩 Из ошибок", "e:start")],
            [("⭐ Из словаря", "s:dict")]]


def set_buttons(has_set: bool, archived: int = 0) -> list[list[tuple[str, str]]]:
    rows = set_source_rows()
    if archived:
        rows.append([(f"📚 Архив наборов ({archived})", "ar:list")])
    if has_set:
        rows.append([("👌 Знакомые слова", "fm:list"), ("✅ Отметить освоенные", "m:list")])
    return rows


# ---------- главное меню и /new ----------

def status_line(mode: str, topic: str | None, active: dict | None, done: int = 0, total: int = 0) -> str:
    if mode == "set" and active:
        return f"🎯 набор «{e(active['title'])}», освоено {done}/{total}"
    if topic:
        return f"🗂 разговор на тему «{e(topic)}»"
    return "🏁 разговор без темы"


def main_menu(status: str) -> tuple[str, list[list[tuple[str, str]]]]:
    return (f"🏠 <b>Главное меню</b>\nСейчас: {status}\n\n<i>Можно просто писать или говорить — "
            "разговор продолжается.</i>",
            [[("💬 Новый разговор", "go:new"), ("🏋️ Упражнения", "go:ex")],
             [("🎯 Наборы слов", "go:set"), ("📋 Итог", "go:itog")],
             [("⭐ Словарь", "go:dict"), ("📖 Слова учебника", "go:words")],
             [("💬 Полезные выражения", "go:phr"), ("📚 Чтение", "go:read")],
             [("🗣 Скороговорки", "go:tw"), ("🗂 Выгрузка", "go:export")]])


def new_menu(status: str, active: dict | None, done: int, total: int) -> tuple[str, list[list[tuple[str, str]]]]:
    rows = []
    if active:
        rows.append([(f"🎯 Тренировать набор «{active['title']}» ({done}/{total})"[:60], "nw:set")])
    rows.append([("📚 Вернуться к набору из архива", "nw:arch")])
    rows.append([("➕ Новый набор…", "nw:newset")])
    rows.append([("🏁 Без набора…", "nw:free")])
    return (f"💬 <b>Новый разговор</b> — о чём?\nСейчас: {status}\n\n"
            "<i>🎯 Набор — бот строит разговор так, чтобы ты употреблял слова набора в разных формах. "
            "Без набора — просто разговор с исправлениями.</i>", rows)


def new_free_buttons() -> list[list[tuple[str, str]]]:
    return [[("💬 Без темы", "nw:f:none")], [("🗂 Своя тема", "nw:f:own")], [("🎲 Случайная тема", "nw:f:rnd")]]


def ago_text(ts: float, now: float) -> str:
    days = int((now - ts) // 86400)
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} дн. назад"
    if days < 30:
        return f"{days // 7} нед. назад"
    return f"{days // 30} мес. назад"


def archive_screen(rows: list[dict], ratio: float, now: float) -> tuple[str, list[list[tuple[str, str]]]]:
    """📚 Архив: незавершённые сверху, завершённые (освоено ≥ ratio) ниже."""
    def finished(r):
        return r["total"] and r["done"] / r["total"] >= ratio
    todo = [r for r in rows if not finished(r)]
    done = [r for r in rows if finished(r)]
    lines = ["📚 <b>Архив наборов</b> — нажми на набор, чтобы посмотреть прогресс и продолжить.", ""]
    btns = []
    if todo:
        lines.append(f"Незавершённые: {len(todo)}")
        btns += [[(f"▶️ {r['title']} — {r['done']}/{r['total']} · {ago_text(r['last_at'], now)}"[:60],
                   f"ar:{r['id']}")] for r in todo]
    if done:
        lines.append(f"Завершённые: {len(done)}")
        btns += [[(f"✅ {r['title']} — {r['done']}/{r['total']}"[:60], f"ar:{r['id']}")] for r in done]
    return "\n".join(lines), btns


def preview_message(p: dict) -> str:
    lines = [f"📝 <b>Новый набор «{e(p['title'])}»</b>", ""]
    off = set(p.get("off", []))
    for i, w in enumerate(p["words"]):
        mark = "❌" if i in off else "•"
        tr = f" [{e(w.get('translit', ''))}]" if w.get("translit") else ""
        txt = f"{mark} <b>{e(w['pl'])}</b>{tr} — {e(w.get('ru', ''))}"
        lines.append(f"<s>{txt}</s>" if i in off else txt)
    lines += ["", "<i>Нажми на слово, чтобы убрать или вернуть его.</i>"]
    return "\n".join(lines)


def preview_buttons(p: dict) -> list[list[tuple[str, str]]]:
    off = set(p.get("off", []))
    word_btns = [((("❌ " if i in off else "") + w["pl"])[:30], f"p:{i}") for i, w in enumerate(p["words"])]
    rows = [word_btns[i:i + 3] for i in range(0, len(word_btns), 3)]
    extra = [("🔄 Другие слова", "p:regen")] if p.get("topic") or p.get("unit") else []
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
        rows = [t for t in r.get("table") or [] if isinstance(t, dict) and t.get("form")]
        if rows:
            lines.append("📋 <b>Таблица</b>")
            for t in rows:
                ex = f": {e(str(t.get('example', '')))}" if t.get("example") else ""
                lines.append(f"▫️ {e(str(t.get('when', '')))} → <b>{e(str(t['form']))}</b>{ex}")
        exc = [str(x) for x in r.get("exceptions") or [] if str(x).strip()]
        if exc:
            lines.append("⚠️ <b>Исключения и особые группы</b>")
            lines += [f"▫️ {e(x)}" for x in exc]
        if rows or exc:
            lines.append("✏️ <b>Примеры</b>")
        for ex in r.get("examples") or []:
            if isinstance(ex, dict):
                lines.append(f"• <b>{e(ex.get('pl', ''))}</b> [{e(ex.get('translit', ''))}] — {e(ex.get('ru', ''))}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "Не получилось объяснить правило, попробуй ещё раз."


def reply_buttons(msg_id: int, has_corrections: bool, has_set_words: bool = False) -> list[list[tuple[str, str]]]:
    row = [("📖 Правило", f"r:{msg_id}")] if has_corrections else []
    row.append(("⭐ В словарь", f"d:{msg_id}"))
    if has_corrections:
        row.append(("🙅 Не ошибка", f"nd:{msg_id}"))
    rows = [row]
    if has_set_words:   # в ответе были слова набора — можно отметить их 👌 знакомыми
        rows.append([("👌 Знакомые", f"fm:{msg_id}")])
    return rows


# ---------- 👌 Знакомые слова набора ----------

FAMILIAR_HINT = ("<i>Отметь слова, которые уже свободно используешь: вопросы на них строиться не будут, "
                 "они будут возвращаться по одному примерно раз в два круга по остальным словам — и копить счётчик. "
                 "Из набора уходят только при освоении. Нажми ещё раз, чтобы снять.</i>")


def familiar_screen(words: list, prefix: str, done: str, title: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """words — неосвоенные слова набора; кнопка: <prefix><id> — переключить 👌."""
    btns = [((("👌 " if is_familiar(w) else "⚪ ") + w["pl"])[:30], f"{prefix}{w['id']}") for w in words]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([("✅ Готово" if done.startswith("fm:ok") else "↩️ К прогрессу", done)])
    return f"👌 <b>{title}</b>\n\n{FAMILIAR_HINT}", rows


# ---------- 🙅 Не ошибка ----------

def dispute_screen(corrs: list[dict], msg_id: int, toggle: str = "nd:t:",
                   done: str | None = None) -> tuple[str, list[list[tuple[str, str]]]]:
    text = ("🙅 <b>Какие исправления неверные?</b>\n"
            "<i>Отмеченное уберу из ошибок, а такую же замену дальше не буду считать ошибкой. "
            "Нажми ещё раз, чтобы вернуть.</i>")
    rows = [[((("✅ " if c["_disputed"] else "☐ ") + f"{c.get('original', '')} → {c.get('correct', '')}")[:60],
              f"{toggle}{c['_id']}")] for c in corrs]
    rows.append([("✅ Готово", done or f"nd:ok:{msg_id}")])
    return text, rows


def dispute_done(n: int) -> str:
    if not n:
        return "🙅 Ничего не отмечено — исправления остались ошибками."
    return (f"🙅 Не ошибка: {n} — убрано из ошибок и добавлено в исключения.\n"
            "<i>Список исключений — /dict → «🙅 Исключения».</i>")


def ex_dispute_screen(ex: dict) -> tuple[str, list[list[tuple[str, str]]]]:
    text = ("🙅 <b>Какие пункты бот засчитал ошибкой зря?</b>\n"
            "<i>Отмеченные станут ✅, уйдут из ошибок и с повтора. Нажми ещё раз, чтобы вернуть.</i>")
    rows = []
    for r in ex["results"]:
        if r["final"] != "wrong" and not r.get("disputed"):
            continue
        it = ex["items"][r["n"] - 1]
        user = r.get("heard") or r.get("user") or "—"
        label = (("✅ " if r.get("disputed") else "☐ ") + f"{r['n']}. {user} → {it.get('answer', '')}")[:60]
        rows.append([(label, f"x:dt:{ex['id']}:{r['n']}")])
    rows.append([("✅ Готово", f"x:dok:{ex['id']}")])
    return text, rows


def ex_dispute_done(ex: dict) -> str:
    res = ex["results"]
    disputed = [str(r["n"]) for r in res if r.get("disputed")]
    ok = sum(1 for r in res if r["final"] in ("ok", "unsure"))
    if not disputed:
        return f"🙅 Ничего не оспорено. #{ex['id']}: {ok} из {len(res)}."
    return f"🙅 Оспорено: пункты {', '.join(disputed)}. #{ex['id']}: теперь {ok} из {len(res)}."


def ignores_screen(rows: list) -> tuple[str, list[list[tuple[str, str]]]]:
    if not rows:
        return ("🙅 <b>Исключения</b>\n\nСписок пуст. Добавляется кнопкой «🙅 Не ошибка» под ответом бота "
                "или «🙅 Оспорить» под проверкой упражнения.", [])
    text = ("🙅 <b>Исключения</b> — эти замены бот не считает ошибкой:\n\n"
            + "\n".join(f"• {e(r['original'])} → {e(r['correct'])}" for r in rows)
            + "\n\n<i>Нажми на пару, чтобы убрать её из списка (уже оспоренные ошибки в пул не вернутся).</i>")
    return text, [[(f"✖️ {r['original']} → {r['correct']}"[:60], f"ig:rm:{r['id']}")] for r in rows]


# ---------- словарь ⭐ ----------

def _offer_items(offer: dict) -> list[tuple[str, dict]]:
    return ([(f"e{i}", it) for i, it in enumerate(offer.get("errs") or [])]
            + [(f"b{i}", it) for i, it in enumerate(offer.get("phrases") or [])]
            + [(f"o{i}", it) for i, it in enumerate(offer.get("own") or [])])


def _get(it, key: str) -> str:
    return (it[key] if key in it.keys() else "") or ""


def _tr(it) -> str:
    t = _get(it, "translit")
    return f" [{e(t)}]" if t else ""


def dict_line(it) -> str:
    """«<b>pl</b> (B.) [транскрипция] — перевод (вин.)» для строки словаря или пункта меню; падеж — если слово
    сохранено не в словарной форме."""
    fp, fr, ru = _get(it, "form_pl"), _get(it, "form_ru"), _get(it, "ru")
    return (f"<b>{e(it['pl'])}</b>" + (f" ({e(fp)})" if fp else "") + _tr(it)
            + (f" — {e(ru)}" + (f" ({e(fr)})" if fr else "") if ru else ""))


def dict_dups(rows: list) -> str:
    """🛡 Уже в словаре в той же форме — не добавлено; где лежит."""
    lines = [f"🛡 <b>Уже в словаре — не добавлял: {len(rows)}</b>"]
    lines += [f"• {dict_line(r)}\n   🗂 тема «{e(r['topic'] or '—')}» · {e(r['set_title'] or '—')}" for r in rows]
    lines.append("<i>Перенести в другую тему — кнопкой ниже (прогресс сохранится).</i>")
    return "\n".join(lines)


def offer_message(offer: dict, have: dict) -> str:
    """Меню «⭐ В словарь»: под ответом бота — 🧩 мои ошибки, 💬 из ответа бота; под разделом (src) — ❌ ошибки и
    пропуски (отмечены сразу), ✅ остальное; ✍️ свои слова. have — {ключ: тема} того, что уже в словаре."""
    sel = set(offer.get("sel") or [])
    src = offer.get("src") or ""

    def line(k, it):
        mark = "⭐" if k in have else "☑️" if k in sel else "▫️"
        where = f" <i>· уже в «{e(have[k])}»</i>" if k in have else ""
        ctx = f" — <i>{e(it['ctx_ru'])}</i>" if not it.get("ru") and it.get("ctx_ru") else ""
        return f"{mark} {dict_line({'translit': '', 'ru': '', **it})}{ctx}{where}"

    head = "⭐ <b>Что добавить в словарь?</b>" + (f" · {e(offer.get('title') or '')}" if src else "")
    note = ("<i>Ошибки и пропуски уже отмечены — сними лишнее, добавь нужное. Слово сохранится в той форме, как "
            "в ответе, с пометкой падежа.</i>" if src else "<i>Отмечай нужное кнопками.</i>")
    lines = [head, note, "", "❌ <b>Ошибки и пропуски</b>" if src else "🧩 <b>Мои ошибки</b>"]
    errs = [(k, it) for k, it in _offer_items(offer) if k.startswith("e")]
    lines += [line(k, it) for k, it in errs] or ["<i>ошибок нет</i>" if src else "<i>в этом сообщении ошибок нет</i>"]
    bots = [(k, it) for k, it in _offer_items(offer) if k.startswith("b")]
    if src:
        if bots:
            lines += ["", "🗣 <b>Сказал по-русски</b>" if src.startswith("i:") else "✅ <b>Остальное</b>"]
            lines += [line(k, it) for k, it in bots]
    else:
        lines += ["", "💬 <b>Из ответа бота</b>"]
        if offer.get("bot_ready"):
            lines += [line(k, it) for k, it in bots] or ["<i>не нашлось, что предложить</i>"]
        else:
            lines.append("<i>нажми «💬 Подобрать из ответа» — бот выберет 7–8 выражений (5–15 с)</i>")
    own = [(k, it) for k, it in _offer_items(offer) if k.startswith("o")]
    lines += ["", "✍️ <b>Свои слова</b>"]
    lines += [line(k, it) for k, it in own] or ["<i>кнопка ниже — напишешь через запятую</i>"]
    lines += ["", "Дальше <b>«✅ Готово»</b> — выберешь, куда положить: 📘 тема юнита, твоя тема или 🤖 ИИ разложит "
              "по темам. Слова попадут в наборы по 10 для тренировки."]
    if have:
        lines.append("\n<i>⭐ — уже в словаре в той же форме, повторно не добавится; нажми — где лежит и перенести.</i>")
    return "\n".join(lines)


def offer_buttons(msg_id: int, offer: dict, have: dict) -> list[list[tuple[str, str]]]:
    sel = set(offer.get("sel") or [])
    rows = []
    for k, it in _offer_items(offer):
        mark = "⭐ " if k in have else "☑️ " if k in sel else ""
        rows.append([((mark + it["pl"])[:40], f"ds:{msg_id}:{k}")])
    tools = [] if offer.get("bot_ready") else [("💬 Подобрать из ответа", f"dg:{msg_id}")]
    rows.append(tools + [("✍️ Свои слова", f"dn:own:{msg_id}")])
    rows.append([(f"✅ Готово — отмечено {len(sel)}", f"dk:{msg_id}")])
    return rows


def offer_topic_screen(msg_id: int, n: int, unit_topic: str, recent: list[dict]) -> tuple[str, list]:
    """📂 Куда положить отмеченные слова: тема юнита, недавние темы, 🤖 ИИ разложит, ✍️ новая тема."""
    text = (f"📂 <b>Куда положить {n} {plural(n, 'слово', 'слова', 'слов')}?</b>\n\n"
            + (f"📘 <b>{e(unit_topic)}</b> — тема юнита: слова будут вместе со словами этого урока.\n" if unit_topic else "")
            + "🗂 Твоя тема — все слова туда.\n🤖 ИИ разложит — каждому слову свою тему (Еда, Работа…).\n"
            "✍️ Новая тема — напишешь название (например «Мой набор»).\n\n"
            "<i>Каждая тема — наборы по 10 слов для тренировки в разговоре и упражнениях.</i>")
    rows = [[(f"📘 {unit_topic}"[:60], f"dq:{msg_id}:u")]] if unit_topic else []
    btns = [(f"🗂 {t['name']} ({t['n']})"[:40], f"dq:{msg_id}:{t['id']}") for t in recent if t["name"] != unit_topic][:6]
    rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([("🤖 Пусть ИИ разложит по темам", f"dq:{msg_id}:0")])
    rows.append([("✍️ Новая тема", f"dQ:{msg_id}"), ("⬅️ Назад к словам", f"dq:{msg_id}:-")])
    return text, rows


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    return many if 11 <= n <= 14 else one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


from ..settings import DICT_SET_SIZE  # noqa: E402


def dict_saved(rows: list, topic: str, sets: list[tuple[str, int]], dups: list) -> str:
    """Итог «✅ Готово»: сколько сохранено и куда (тема, набор для тренировки), дубли — отдельно."""
    parts = []
    if rows:
        n = len(rows)
        if topic:
            head = f"⭐ <b>Сохранил {n} {plural(n, 'слово', 'слова', 'слов')} в тему «{e(topic)}»</b>"
        else:
            counts = {}
            for r in rows:
                counts[r["topic"]] = counts.get(r["topic"], 0) + 1
            head = (f"⭐ <b>Сохранил {n} {plural(n, 'слово', 'слова', 'слов')}</b> — ИИ разложил по темам: "
                    + ", ".join(f"{e(t)} — {k}" for t, k in counts.items()))
        lines = [head]
        lines += [f"• {dict_line(r)}" + ("" if topic else f"  <i>· {e(r['topic'])}</i>") for r in rows]
        lines.append("")
        lines += [f"🎯 Набор для тренировки: «{e(title)}» — {k} из {DICT_SET_SIZE}" for title, k in sets if title]
        parts.append("\n".join(lines))
    if dups:
        parts.append(dict_dups(dups))
    return "\n\n".join(parts) or "⭐ Ничего нового."


def dict_added(rows: list) -> str:
    """Что добавилось и в какой набор попало."""
    if not rows:
        return "⭐ Ничего нового — эти выражения уже в словаре."
    lines = [f"⭐ <b>Добавлено в словарь: {len(rows)}</b>"]
    for r in rows:
        lines.append(f"• {dict_line(r)}\n   🗂 {e(r['topic'])} → {e(r['set_title'] or '—')}")
    lines.append("\n<i>Тренировать: /new → ➕ Новый набор → ⭐ Из словаря, или /ex → слова → 📚 Набор из архива. "
                 "Сменить тему — /dict.</i>")
    return "\n".join(lines)


def dict_message(items: list, topics: list[dict], limit_chars: int = 3300) -> str:
    """📚 /dict: все слова одним списком по темам (✅ — освоено в наборе)."""
    if not items:
        return ("⭐ Словарь пока пуст.\n\nНажимай «⭐ В словарь» под ответами бота или пришли своё: "
                "/dict add najbardziej lubię, szczerze mówiąc")
    done = sum(1 for i in items if i["mastered_at"] is not None)
    head = [f"⭐ <b>Словарь</b> — всего {len(items)}, тем: {len(topics)}, освоено: {done}", ""]
    body, cur, n = [], None, 0
    for it in items:
        if it["topic"] != cur:
            cur = it["topic"]
            body += ["", f"🗂 <b>{e(cur)}</b>"] if body else [f"🗂 <b>{e(cur)}</b>"]
        n += 1
        body.append(f"{'✅' if it['mastered_at'] is not None else '▫️'} {n}. {dict_line(it)}")
    text = "\n".join(body)
    if len(text) > limit_chars:   # длинный словарь — в сообщении только темы, весь список — файлом
        text = "\n".join(f"🗂 <b>{e(t['name'])}</b> — {t['n']} (✅ {t['done']})" for t in topics)
        text += "\n\n<i>Слов много — полный список кнопкой «📄 Файлом».</i>"
    foot = ("\n\n<i>Тема — её слова и наборы, там же «📂 Сменить тему» у слова. "
            "Добавить своё: /dict add слово, слово</i>")
    return "\n".join(head) + "\n" + text + foot


def dict_export(items: list) -> str:
    """📄 Словарь файлом: все слова по темам, с набором и отметкой «освоено»."""
    out = ["Словарь ⭐", "==========", f"Всего: {len(items)}", ""]
    cur = None
    for it in items:
        if it["topic"] != cur:
            cur = it["topic"]
            out += ["", f"■ {cur}"]
        out.append(f"   {'[x]' if it['mastered_at'] is not None else '[ ]'} {it['pl']}"
                   + (f" ({it['form_pl']})" if it["form_pl"] else "")
                   + (f" [{it['translit']}]" if it["translit"] else "") + f" — {it['ru']}"
                   + (f" ({it['form_ru']})" if it["form_ru"] else "")
                   + (f"   ({it['set_title']})" if it["set_title"] else ""))
    return "\n".join(out) + "\n\n[x] — освоено. Сменить тему слова: /dict → тема → слово → 📂.\n"


def dict_buttons(topics: list[dict], has_other: bool = False) -> list[list[tuple[str, str]]]:
    btns = [(f"🗂 {t['name']} ({t['n']})"[:40], f"dt:{t['id']}") for t in topics[:30]]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if has_other:
        rows.append([("🤖 Разложить «Разное» по темам", "dS")])
    rows.append([("✍️ Добавить свои", "dn:own"), ("📄 Файлом", "i:dict")])
    rows.append([("🙅 Исключения", "ig:list")])
    return rows


def dict_topic_screen(topic, items: list, sets: list[dict]) -> tuple[str, list]:
    lines = [f"🗂 <b>{e(topic['name'])}</b> — {len(items)} слов", ""]
    for n, it in enumerate(items[:40], 1):
        lines.append(f"{'✅' if it['mastered_at'] is not None else '▫️'} {n}. {dict_line(it)}"
                     f"  <i>· {e(it['set_title'] or '—')}</i>")
    if len(items) > 40:
        lines.append(f"<i>…и ещё {len(items) - 40} — весь список: /dict → «📄 Файлом»</i>")
    if sets:
        lines += ["", "🎯 <b>Наборы темы</b> — по 10 слов: разговор по набору или упражнение"]
    lines.append("\n<i>Нажми слово — сменить тему или удалить.</i>")
    rows = [[(f"{n}. {it['pl']}"[:40], f"dw:{it['id']}")] for n, it in enumerate(items[:40], 1)]
    for st in sets:
        rows.append([(f"🎯 {st['title']} ({st['done']}/{st['total']})"[:40], f"ar:go:{st['id']}"),
                     ("🏋️ Упражнение", f"dy:{st['id']}")])
    rows.append([("⬅️ Все темы", "dl")])
    return "\n".join(lines), rows


def dict_word_screen(it, topics: list[dict], fixed=None) -> tuple[str, list]:
    """Слово словаря: где лежит, перенести в другую тему (fixed — своя тема меню «⭐ В словарь», первой кнопкой)."""
    state = "✅ освоено" if it["mastered_at"] is not None else "в работе"
    text = (dict_line(it) + f"\n🗂 Тема: {e(it['topic'] or '—')} · набор {e(it['set_title'] or '—')} · {state}"
            "\n\n📂 <b>В какую тему перенести?</b> Слово переедет в набор этой темы вместе с прогрессом. "
            "Своя тема («✍️ Новая тема») — это и есть свой набор: переноси туда слова из разных тем.")
    btns = [(f"🗂 {t['name']}"[:40], f"dm:{it['id']}:{t['id']}") for t in topics if t["id"] != it["topic_id"]][:30]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if fixed and fixed["id"] != it["topic_id"]:
        rows.insert(0, [(f"📂 В тему «{fixed['name']}»"[:60], f"dm:{it['id']}:{fixed['id']}")])
    rows.append([("✍️ Новая тема", f"dz:{it['id']}"), ("🗑 Удалить", f"dr:{it['id']}")])
    rows.append([("⬅️ К теме", f"dt:{it['topic_id']}"), ("📚 Словарь", "dl")])
    return text, rows


def dict_delete_screen(it) -> tuple[str, list]:
    return (f"🗑 Убрать «<b>{e(it['pl'])}</b>» из словаря и из набора {e(it['set_title'] or '')}? "
            "Прогресс этого слова пропадёт.",
            [[("🗑 Да, убрать", f"dR:{it['id']}"), ("⬅️ Нет", f"dw:{it['id']}")]])


def dict_sets_screen(sets: list[dict]) -> tuple[str, list]:
    """⭐ Наборы из словаря — выбрать, какой тренировать в разговоре."""
    if not sets:
        return ("⭐ Наборов из словаря пока нет. Сохраняй слова кнопкой «⭐ В словарь» под ответами бота "
                "или /dict add — они сами разложатся по темам в наборы по 10."), []
    return ("⭐ <b>Наборы из словаря</b> — какой тренируем в разговоре?\n<i>Текущий набор уйдёт в 📚 архив "
            "со всем прогрессом.</i>",
            [[(f"{'🎯' if st['status'] == 'active' else '⭐'} {st['title']} — {st['done']}/{st['total']}"[:60],
               f"ar:go:{st['id']}")] for st in sets[:40]])


def ex_set_pick(sets: list[dict]) -> tuple[str, list]:
    """/ex → слова → 📚 Набор из архива: любой набор (текущий, из архива, из словаря)."""
    if not sets:
        return "Наборов пока нет — создай в /new или сохраняй слова кнопкой «⭐ В словарь».", []
    return ("📚 <b>По какому набору упражнение?</b>",
            [[(f"{'🎯 ' if st.get('active') else ''}{st['title']} — {st['done']}/{st['total']}"[:60],
               f"x:sa:{st['id']}")] for st in sets[:40]])


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


def itog_buttons(period: str, has_errors: bool, has_words: bool = False) -> list[list[tuple[str, str]]]:
    row = [("📄 Файлом", f"i:f{period}")]
    if has_errors:
        row.append(("🎯 Тренировать эти ошибки", f"e:p:{period}"))
    return [row] + ([[("⭐ В словарь", f"dX:i:{period}")]] if has_errors or has_words else [])


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
    "book": "📘 Учебник",
    "book_gap": "📘 Учебник: пропуски",
    "book_card": "📘 Учебник: карточки",
    "book_test": "📘 Учебник: тест",
}


def ex_menu() -> tuple[str, list[list[tuple[str, str]]]]:
    return ("🏋️ <b>Упражнения</b> — что тренируем?\n\n"
            "<i>В каждом упражнении 10 пунктов. Отвечаешь одним сообщением: «1 piję 2 lubi 3 kupuje». "
            "Каждый пункт бот объясняет; уверен — поставь ! («3 kupuje!»), и этот пункт объяснять не будет. "
            "Уточнение или вопрос к пункту — в скобках: «2 lubi (3 л. ед. ч., почему не lubią?)» — "
            "бот проверит и рассуждение.</i>",
            [[(EX_KINDS["words"], "x:k:words"), (EX_KINDS["voice"], "x:k:voice")],
             [(EX_KINDS["grammar"], "x:k:grammar"), (EX_KINDS["errors"], "x:k:errors")],
             [(EX_KINDS["book"], "x:k:book")]])


def ex_source_buttons() -> list[list[tuple[str, str]]]:
    return [[("🎯 Текущий набор", "x:s:set"), ("📚 Набор из архива", "x:s:arch")],
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
    return text, [[("📖 Сначала правило", "x:c:rule"), ("▶️ Сразу упражнения", "x:c:go")]]


def ex_rule_picker(catalog: list[str], chosen: list[int]) -> tuple[str, list[list[tuple[str, str]]]]:
    on = set(chosen)
    text = ("📐 <b>Выбери правило</b> — одно или несколько (тогда пункты перемешаются, и в каждом нужно понять, "
            "какое правило работает). Можно написать своё — кнопкой ниже.\n\n"
            "Выбрано: " + (", ".join(catalog[i] for i in sorted(on)) or "—"))
    btns = [((("✅ " if i in on else "") + r)[:40], f"x:r:{i}") for i, r in enumerate(catalog)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([("✍️ Своё правило", "x:r:own"), ("▶️ Дальше", "x:r:go")])
    return text, rows


def book_units_screen(units: list[dict], prefix: str = "x:b:u:",
                      title: str = "📘 <b>Учебник</b> — какой юнит тренируем?") -> tuple[str, list[list[tuple[str, str]]]]:
    lines = [title, ""]
    for u in units:
        nx = len(u.get("exercises") or [])
        lines.append(f"<b>{e(u['name'])} — {e(u['title'])}</b> · {len(u['words'])} {u.get('item_word', 'слов')}"
                     + (f" · 📝 {nx} упр." if nx else ""))
        if u.get("summary") and not u.get("phrases"):
            lines.append(f"<i>{e(u['summary'])}</i>")
    return "\n".join(lines), [[(f"{u['name']} — {u['title']}"[:60], f"{prefix}{u['unit']}")] for u in units]


def books_screen(books: list[dict], purpose: str, title: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """📚 Выбор учебника (purpose: ex — упражнения, wd — слова, su — набор из юнита)."""
    lines = [title, ""]
    for b in books:
        nx = sum(len(u.get("exercises") or []) for u in b["units"])
        cnt = f"{len(b['units'])} цикл." if b.get("phrases") else f"{len(b['units'])} юн."
        lines.append(f"<b>{e(b['title'])}</b> · {cnt}" + (f" · 📝 {nx} упр." if nx else ""))
    icon = lambda b: "🧱" if b.get("phrases") else "📚"  # noqa: E731
    return "\n".join(lines), [[(f"{icon(b)} {b['title']}"[:60], f"bk:{purpose}:{b['id']}")] for b in books]


def book_unit_screen(unit: dict, done: int, recog: int, total: int) -> tuple[str, list[list[tuple[str, str]]]]:
    ph = unit.get("phrases")
    what = "выражения" if ph else "слова"
    icon = "💬" if ph else "📚" if unit.get("reading") else "📘"
    text = (f"{icon} <b>{e(unit['book_short'])} · {e(unit['name'])} — {e(unit['title'])}</b> · знаю {done} · узнаю {recog} · из {total}\n"
            + (f"<i>{e(unit['summary'])}</i>\n" if unit.get("summary") else "")
            + "\n" + (PHRASE_LEGEND if ph else KNOW_LEGEND)
            + f"\n<i>Сначала идут {what} с ошибками, потом ещё не «знаю», потом редкие.</i>")
    nx = len(unit.get("exercises") or [])
    ex_label = "📝 Вопросы к главе" if unit.get("reading") else "📝 Упражнения из книги"
    ex_row = [[(f"{ex_label} ({nx})", f"bx:l:{unit['unit']}")]] if nx else []
    lst = ("📖 Выражения цикла — список с переводом" if ph else
           "📖 Слова главы — список с переводом" if unit.get("reading") else "📖 Слова юнита — список с переводом")
    return text, ex_row + [[(lst, f"wd:u:{unit['unit']}")],
                  [("✍️ Пропуски в предложениях", "x:b:m:gap")],
                  [("🃏 Карточки — пишу перевод", "x:b:m:card")],
                  [("🔘 Тест — выбираю перевод", "x:b:m:test")]]


def book_dir_buttons() -> list[list[tuple[str, str]]]:
    return [[("🇵🇱→🇷🇺", "x:b:d:pl"), ("🇷🇺→🇵🇱", "x:b:d:ru"), ("🔀 Вперемешку", "x:b:d:mix")]]


def _ru_line(it: dict, n: int | None = None) -> str:
    """Перевод под пунктом; перевод пропущенного слова спрятан — открывается кнопкой 💡n под упражнением.
    (Спойлер Telegram не подходит: нажатие на один открывает все спойлеры сообщения.)"""
    if it.get("ru_spoiler"):
        mark = f"💡{n}" if n else "💡"
        return re.sub(r"⟪(.*?)⟫", f"<b>[{mark}]</b>", e(it["ru_spoiler"]))
    return e(it["ru"])


def hint_of(it: dict) -> str:
    """Русский перевод пропущенного слова — для подсказки 💡."""
    m = re.search(r"⟪(.*?)⟫", it.get("ru_spoiler") or "")
    return m.group(1) if m else ""


def ex_hint_rows(ex: dict) -> list[list[tuple[str, str]]]:
    """Кнопки 💡1…💡10: перевод пропущенного слова только этого пункта — всплывающей подсказкой."""
    btns = [(f"💡{n}", f"x:h:{ex['id']}:{n}") for n, it in enumerate(ex["items"], 1) if hint_of(it)]
    return [btns[i:i + 5] for i in range(0, len(btns), 5)]


# ---------- 📖 слова юнита (/words) ----------

WORDS_CHUNK = 25   # «🙈 Скрыть перевод»: слов в одном сообщении (кнопки-номера 5×5)


def _word_line(n: int, w: dict, mark: str, with_ru: bool = True) -> str:
    """mark: ✅ знаю / 🟡 узнаю / пусто. 🔗 сочетание — с отступом под своим словом."""
    tr = f" [{e(w['translit'])}]" if w.get("translit") else ""
    pre = "      🔗 " if w.get("of") else ""
    wb = " 📒" if w.get("src") == "wb" else ""
    head = f"{pre}{mark + ' ' if mark else ''}{n}. <b>{e(w['pl'])}</b>{tr}{wb}"
    if not with_ru:
        return head
    pos = f" · <i>{e(w['pos'])}</i>" if w.get("pos") and not w.get("of") else ""
    rule = f"\n      📐 <i>{e(w['rule'])}</i>" if w.get("rule") else ""
    return f"{head} — {e(w['ru'])}{pos}{rule}"


def _mark(w: dict, known: set[str], recog: set[str]) -> str:
    return "✅" if w["pl"] in known else "🟡" if w["pl"] in recog else ""


def words_header(unit: dict, known: int, recog: int, total: int) -> str:
    return (f"📖 <b>{e(unit['book_short'])} · {e(unit['name'])} — {e(unit['title'])}</b> · знаю {known} · узнаю {recog} · из {total}\n"
            + (f"<i>{e(unit['summary'])}</i>" if unit.get("summary") else ""))


PHRASE_LEGEND = ("<i>✅ знаю — 3 раза подряд написал сам верно, в обе стороны (карточки, пропуски, разговор). "
                 "🟡 узнаю — 3 раза подряд верно выбрал в тесте. 📐 — правило, которое показывает выражение.</i>")
KNOW_LEGEND = ("<i>✅ знаю — 3 раза подряд написал сам верно, в обе стороны (карточки, пропуски, разговор). "
               "🟡 узнаю — 3 раза подряд верно выбрал в тесте. 🔗 — сочетание с предлогом / устойчивое. "
               "📒 — слово из рабочей тетради.</i>")


def topics_line(words: list[dict]) -> str:
    """Оглавление: «🗂 Темы: Напитки (9) · Овощи (8) · …» — сколько слов (без сочетаний) в каждой."""
    counts: dict[str, int] = {}
    for w in words:
        if w.get("group") and not w.get("of"):
            counts[w["group"]] = counts.get(w["group"], 0) + 1
    return ("🗂 <b>Темы:</b> " + " · ".join(f"{e(t)} ({n})" for t, n in counts.items())) if counts else ""


def _topic_head(t: str) -> str:
    return f"🗂 <b>{e(t)}</b>"


def words_list(unit: dict, known: set[str], recog: set[str]) -> tuple[str, list[list[tuple[str, str]]]]:
    lines = [words_header(unit, len(known), len(recog), len(unit["words"]))]
    if topics_line(unit["words"]):
        lines.append(topics_line(unit["words"]))
    lines.append("")
    grp = None
    for n, w in enumerate(unit["words"], 1):
        if w.get("group") and w["group"] != grp:
            grp = w["group"]
            lines += ([""] if lines[-1] else []) + [_topic_head(grp)]
        lines.append(_word_line(n, w, _mark(w, known, recog)))
    lines += ["", PHRASE_LEGEND if unit.get("phrases") else KNOW_LEGEND]
    u = unit["unit"]
    return "\n".join(lines), [[("🙈 Скрыть перевод", f"wd:h:{u}"), ("📄 Файлом", f"wd:f:{u}"),
                                ("🔊 Озвучить", f"wd:v:{u}")]]


def words_hidden(unit: dict, known: set[str], recog: set[str]) -> list[tuple[str, list[list[tuple[str, str]]]]]:
    """Скрытый перевод: куски по WORDS_CHUNK слов, под каждым — кнопки-номера; нажал — перевод всплывает."""
    out, words, u = [], unit["words"], unit["unit"]
    for grp, k, part in topic_parts(words, WORDS_CHUNK):   # по темам: каждая тема — своё сообщение
        topic = f"🗂 <b>{e(grp)}</b>{f' ({k})' if k > 1 else ''}\n" if grp else ""
        head = (f"🙈 <b>{e(unit['name'])} — {e(unit['title'])}</b> · {part[0][0]}–{part[-1][0]} из {len(words)}\n"
                + topic + "<i>Вспомни перевод, потом нажми номер — подскажу.</i>\n\n")
        text = head + "\n".join(_word_line(n, w, _mark(w, known, recog), with_ru=False) for n, w in part)
        btns = [(str(n), f"wd:s:{u}|{n}") for n, _ in part]
        out.append((text, [btns[i:i + 5] for i in range(0, len(btns), 5)]))
    return out


def word_hint(w: dict) -> str:
    return (f"{w['pl']} — {w['ru']}" + (f" · {w['pos']}" if w.get("pos") else "")
            + (f"\n📐 {w['rule']}" if w.get("rule") else ""))


def words_file(unit: dict) -> str:
    lines = [f"{unit['book_title']} · {unit['name']} — {unit['title']}", unit.get("summary", ""), ""]
    grp = None
    for n, w in enumerate(unit["words"], 1):
        if w.get("group") and w["group"] != grp:
            grp = w["group"]
            lines += ["", f"=== {grp} ==="]
        pre = "    + " if w.get("of") else ""
        wb = " [тетрадь]" if w.get("src") == "wb" else ""
        lines.append(f"{pre}{n}. {w['pl']} [{w.get('translit', '')}] — {w['ru']}"
                     + (f" ({w['pos']})" if w.get("pos") and not w.get("of") else "") + wb
                     + (f"\n      правило: {w['rule']}" if w.get("rule") else ""))
    return "\n".join(lines) + "\n"


def ex_count_prompt() -> tuple[str, list[list[tuple[str, str]]]]:
    return ("Сколько упражнений? Нажми или напиши число (1–20).",
            [[("1", "x:n:1"), ("2", "x:n:2"), ("3", "x:n:3"), ("5", "x:n:5"), ("10", "x:n:10")]])


def ex_message(ex: dict, idx: int, total: int, voice: bool, quiz: bool = False,
               notes: dict | None = None) -> str:
    lines = [f"🏋️ <b>Упражнение {idx}/{total}</b> · #{ex['id']} — {e(ex['title'])}", ""]
    for i, it in enumerate(ex["items"], 1):
        q = e(it.get("q", "")).replace("___", "<b>___</b>")
        rep = " 🔁" if it.get("_reuse_id") else ""
        lines.append(f"{i}. {q}{rep}")
        if it.get("ru") and not it.get("card"):  # перевод вместо подсказки; у карточек перевод — это ответ
            lines.append(f"    <i>— {_ru_line(it, i)}</i>")
        if it.get("options"):
            lines.append("    " + "   ".join(f"{'abcd'[j]}) {e(o)}" for j, o in enumerate(it["options"])))
        if (notes or {}).get(str(i)):
            lines.append(f"    💭 <i>{e(notes[str(i)])}</i>")
    lines.append("")
    if voice:
        lines.append("🎙 <i>Пришли голосовое: прочитай все предложения по порядку целиком, с заполненными пропусками. "
                     "Номера говорить не обязательно. Уточнение к пункту — скажи «уточнение» и дальше своими словами, "
                     "до следующего предложения.</i>")
    elif quiz:
        lines.append("<i>Жми ответ кнопками ниже: строка «1 a · 1 b · 1 c» — пункт 1 (выбор можно менять), "
                     "«1 !» — уверен, не объяснять. Уточнение или вопрос — текстом: «2 (почему не …?)». "
                     "Когда отмечены все — «📨 Проверить». Можно и текстом: 1b 2a 3c …</i>")
    elif ex["items"] and ex["items"][0].get("card") and not ex["items"][0].get("options"):
        lines.append("<i>Ответ одним сообщением: 1 перевод 2 перевод … · уточнение — в скобках · "
                     "уверен — !</i>")
    elif ex["items"] and ex["items"][0].get("options"):
        lines.append("<i>Ответ одним сообщением: 1b 2a 3c … · уточнение или вопрос — в скобках: «2a (винительный)» · "
                     "уверен, не объяснять — !: «3c!»</i>")
    else:
        lines.append("<i>Ответ одним сообщением: 1 piję 2 lubi … · уточнение или вопрос — в скобках: "
                     "«1 piję (ja → -ę)» · уверен, не объяснять — !: «2 lubi!»</i>")
    if not voice:
        lines.append("<i>" + "🎙 Можно и голосом: номер и ответ — «один — jestem, три — mam», «не знаю» — пропуск, «уточнение …» — вопрос к пункту." + "</i>")
    lines.append("<i>Ответ засчитается этому упражнению. Можно и через «Ответить» на это сообщение.</i>")
    if any(it.get("_reuse_id") for it in ex["items"]):
        lines.append("<i>🔁 — пункт на повтор: в прошлый раз была ошибка или сомнение.</i>")
    return "\n".join(lines)


# ---------- интерактивный тест: одно сообщение, под ним кнопки a / b / c по строке на пункт ----------

def ex_quiz_keyboard(ex: dict, pick: dict, sure: list, check: bool = True) -> list[list[tuple[str, str]]]:
    """Строка на пункт: «1 a» «1 b» «1 c» «1 !»; выбранное — ✅, «уверен» — ❗. Внизу «📨 Проверить (N/10)»."""
    rows = []
    for n, it in enumerate(ex["items"], 1):
        chosen = pick.get(str(n))
        row = [((f"✅{n}{L}" if chosen == L else f"{n} {L}"), f"x:a:{ex['id']}:{n}:{L}")
               for L in "abcd"[:len(it.get("options") or [])]]
        row.append((f"❗{n}" if n in sure else f"{n} !", f"x:a:{ex['id']}:{n}:!"))
        rows.append(row)
    if check:
        total = len(ex["items"])
        rows.append([(f"📨 Проверить ({len(pick)}/{total})", f"x:go:{ex['id']}")])
        rows.append([("✖️ Закончить без проверки", "nav:c:ex_answer")])
    return rows


STATUS_ICON = {"ok": "✅", "wrong": "❌", "unsure": "❓"}


TASK_HINT_MAX = 40   # задание длиннее — не повторяем в разборе, чтобы не загромождать


def task_hint(it: dict) -> str:
    """Под ошибкой / пропуском: что было в пункте — русский перевод и короткое задание (маска, вопрос).
    Чтобы не листать вверх к упражнению. Пусто, если ни того, ни другого нет."""
    parts = []
    if it.get("ru"):
        parts.append(f"🇷🇺 {e(it['ru'])}")
    q = " ".join(str(it.get("q") or "").split())
    if q and len(q) <= TASK_HINT_MAX:
        parts.append(f"задание: {e(q)}")
    return " · ".join(parts)


def ex_results(ex: dict, results: list[dict], head: str | None = None, foot: str | None = None) -> str:
    """Разбор проверенных пунктов. head / foot — свои заголовок и подсказка внизу (для упражнений из книги)."""
    ok = sum(1 for r in results if r["final"] in ("ok", "unsure"))
    lines = [head or f"📊 <b>{ok} из {len(results)}</b> · #{ex['id']} — {e(ex['title'])}", ""]
    for r in results:
        if r["n"] > 1:
            lines.append("")  # пустая строка между пунктами: длинный разбор режется по пунктам, а не посреди
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
        lines.append(head + (f" — <i>{e(it['grammar'])}</i>" if it.get("grammar") else ""))
        if r["final"] != "ok" and not it.get("full_pl"):
            hint = task_hint(it)
            if hint:
                lines.append(f"    {hint}")
        if verb_line(it):
            lines.append(f"    {verb_line(it)}")
        if r["final"] != "ok" or r.get("explanation"):  # верный ответ с «!» — без разбора
            if it.get("full_pl"):
                lines.append(f"    {e(it['full_pl'])}" + (f" [{e(it['translit'])}]" if it.get("translit") else "")
                             + (f" — {e(it['ru'])}" if it.get("ru") else ""))
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
    lines.append(foot if foot is not None else "<i>Вопрос по пункту — напиши: «5: почему не czasem?»</i>")
    return "\n".join(lines)


def ex_result_buttons(ex_id: int, left: int) -> list[list[tuple[str, str]]]:
    rows = [[("📖 Правила по ошибкам", f"x:rr:{ex_id}"), ("🙅 Оспорить", f"x:dp:{ex_id}")],
            [("⭐ В словарь", f"dX:e:{ex_id}")]]
    if left > 0:
        rows.append([(f"➡️ Следующее (осталось {left})", "x:next"), ("⏹ Закончить", "x:stop")])
        rows.append([("🔁 Повторить серию", f"x:rp:{ex_id}")])
    else:
        rows.append([("🔁 Повторить", f"x:rp:{ex_id}"), ("🏋️ Другие упражнения", "x:menu")])
    return rows


# ---------- 📝 упражнения из книги (учебник 📗 и рабочая тетрадь 📒) ----------

def bex_name(x: dict) -> str:
    icon = {"wb": "📒", "rd": "📚"}.get(x.get("src"), "📗")   # rd — вопросы к главе книги для чтения
    return f"{icon} {x.get('ref', '')} — {x.get('title_ru') or x.get('title', '')}"


def bex_counts(x: dict, done: dict) -> tuple[int, int]:
    """(верно, отвечено) по пунктам упражнения; done — {(ex_id, n): row}."""
    rows = [done[(x["id"], n)] for n in range(1, len(x["items"]) + 1) if (x["id"], n) in done]
    return sum(1 for r in rows if r["ok"]), len(rows)


def bex_manual(x: dict, done: dict) -> int:
    """Сколько пунктов засчитано пользователем вручную ✋."""
    return sum(1 for n in range(1, len(x["items"]) + 1) if (done.get((x["id"], n)) or {}).get("manual"))


def bex_icon(x: dict, done: dict) -> str:
    """✅ всё верно · ☑️ 100%, но часть засчитана вручную · ◐ начато · ○ не начато."""
    ok, answered = bex_counts(x, done)
    if ok == len(x["items"]):
        return "☑️" if bex_manual(x, done) else "✅"
    return "◐" if answered else "○"


def bex_complete(x: dict, done: dict) -> bool:
    return bex_counts(x, done)[0] == len(x["items"])


def bex_list_screen(unit: dict, done: dict, hist: dict | None = None) -> tuple[str, list[list[tuple[str, str]]]]:
    """hist — {ex_id: [попытки на 100%]}: упражнение, сделанное хоть раз, считается сделанным, даже если идёт новая попытка."""
    hist = hist or {}
    exs = unit.get("exercises") or []
    full = sum(1 for x in exs if bex_complete(x, done) or hist.get(x["id"]))
    src = "" if unit.get("reading") else "📗 — учебник, 📒 — рабочая тетрадь · "
    what = "вопросы к главе" if unit.get("reading") else "упражнения"
    lines = [f"📝 <b>{e(unit['book_short'])} · {e(unit['name'])} — {e(unit['title'])}</b> · {what}: "
             f"сделано {full} из {len(exs)}", "",
             f"{src}✅ всё верно · ☑️ 100%, но часть пунктов засчитана вручную (✋N — "
             "сколько) · ◐ начато · ○ не начато · 🏆 — сколько раз сделано на 100% (🏆✋ — последний раз с ручными)", ""]
    rows = []
    for x in exs:
        ok, _ = bex_counts(x, done)
        attempts = hist.get(x["id"]) or []
        wins = len(attempts)
        cup = (f" · 🏆{wins if wins > 1 else ''}" + ("✋" if attempts[-1].get("manual") else "")) if wins else ""
        hand = bex_manual(x, done)
        lines.append(f"{bex_icon(x, done)} {e(bex_name(x))} · {ok}/{len(x['items'])}"
                     + (f" · ✋{hand}" if hand else "") + cup)
        rows.append([(f"{bex_icon(x, done)}{'🏆' if wins else ''} {bex_name(x)}"[:60], f"bx:o:{unit['unit']}|{x['id']}")])
    return "\n".join(lines), rows


TF_LABELS = {"a": "P", "b": "N"}   # prawda / nieprawda
COMPACT_FROM = 10   # в упражнении с таким числом независимых пунктов верные скрываются


def _same(q: str, ru: str) -> bool:
    """Перевод под пунктом повторяет сам пункт («🍌 банан» — «банан»)."""
    letters = lambda t: re.sub(r"[^\w ]", " ", t.lower()).split()  # noqa: E731
    return bool(ru) and " ".join(letters(ru)) in " ".join(letters(q))


def bex_compact(x: dict) -> bool:
    """Длинный список независимых пунктов (слова, формы): верные можно скрыть. С текстом, диалогом
    или продолжением («…z ___») — нельзя: там нужен контекст."""
    if len(x["items"]) < COMPACT_FROM or x.get("text") or x.get("type") not in ("gap", "choice", "match", "tf"):
        return False
    return not any(it.get("q", "").strip().startswith(("…", "...")) or it.get("q", "").strip().endswith(("…", "..."))
                   for it in x["items"])


def bex_message(unit: dict, x: dict, done: dict, notes: dict | None = None, wins: int = 0) -> str:
    ok, answered = bex_counts(x, done)
    total = len(x["items"])
    complete = ok == total
    lines = [f"📝 <b>{e(unit['book_short'])} · {e(unit['name'])} · {e(bex_name(x))}</b> · {ok}/{total}"]
    if x.get("title"):
        lines.append(f"<i>{e(x['title'])}</i>")
    if x.get("task_ru"):
        lines.append(e(x["task_ru"]))
    if wins:
        lines.append(f"🏆 Уже сделано на 100%: {wins} раз(а) — ответы целиком: «📜 Выполненная попытка».")
    if x.get("text"):
        lines.append(f"<blockquote>{e(x['text'])}</blockquote>")
    if x.get("options") and x.get("type") != "tf":  # «соедини»: общий список вариантов
        lines.append("\n".join(f"<b>{LETTERS[j]})</b> {e(o)}" for j, o in enumerate(x["options"])))
    lines.append("")
    hide = bex_compact(x) and not complete and ok > 0
    if hide:
        lines.append(f"<i>✅ Сделано {ok} из {total} — эти пункты скрыты, ниже только оставшиеся.</i>")
    for n, it in enumerate(x["items"], 1):
        row = done.get((x["id"], n))
        if hide and row and row["ok"]:
            continue
        mark = "" if not row else ("✋ " if row.get("manual") else "✅ ") if row["ok"] else "❌ "
        q = e(it.get("q", "")).replace("___", "<b>___</b>")
        got = f" → <b>{e(row['answer'])}</b>" if row and row["ok"] and row["answer"] else ""
        lines.append(f"{mark}{n}. {q}{got}" + (f" 💡{n}" if it.get("hint") and not (row and row["ok"]) else ""))
        if it.get("ru") and not _same(it.get("q", ""), it["ru"]):
            lines.append(f"    <i>— {e(it['ru'])}</i>")
        if it.get("options"):
            lines.append("    " + "   ".join(f"{LETTERS[j]}) {e(o)}" for j, o in enumerate(it["options"])))
        if (notes or {}).get(str(n)):
            lines.append(f"    💭 <i>{e(notes[str(n)])}</i>")
    lines.append("")
    if complete:
        lines.append("<i>🎉 Всё верно" + (" (✋ — засчитано вручную)" if bex_manual(x, done) else "")
                     + ". Пройти ещё раз — «🔁 Выполнить заново»: эта попытка сохранится.</i>")
    elif x.get("type") in CHOICE_TYPES:
        opts = "P / N" if x.get("type") == "tf" else "a / b / c"
        lines.append(f"<i>Жми ответы кнопками ({opts}) — можно не все: проверю отмеченные, остальное — потом, "
                     "прогресс сохраняется. «n !» — уверен, не объяснять. Можно и текстом: «1b 3a», "
                     "уточнение — в скобках: «2a (почему?)».</i>")
    elif x.get("type") == "free":
        lines.append("<i>Ответь своими словами: «1 … 2 …» — можно на один пункт, остальные потом. "
                     "Вопрос — в скобках.</i>")
    else:
        lines.append("<i>Ответ одним сообщением, номера — в любом порядке: «1 jestem 3 mam». Не знаешь — «4-», "
                     "несколько подряд — «4-8-». Пропущенные пункты до последнего номера — тоже ошибка. "
                     "Пункты после него — потом, прогресс сохраняется. Уточнение — в скобках, уверен — «!».</i>")
    if not complete:
        lines.append("<i>" + "🎙 Можно и голосом: номер и ответ — «один — jestem, три — mam», «не знаю» — пропуск, «уточнение …» — вопрос к пункту." + "</i>")
        lines.append("<i>Засчитать пункт вручную (описка, спорный ответ) — «5+», несколько подряд — «4+8+»; "
                     "всё упражнение — кнопкой «✋ Засчитать всё».</i>")
    left = [n for n in range(1, len(x["items"]) + 1) if not (done.get((x["id"], n)) or {}).get("ok")]
    if len(left) == 1 and len(x["items"]) > 1:
        lines.append(f"<i>✍️ Остался пункт {left[0]} — можно ответить без номера.</i>")
    lines.append("<i>📖 Полное правило — кнопкой ниже (ко всему упражнению или к пункту) или в ответе «3?» "
                 "(голосом — «три — правило»).</i>")
    if answered and not complete:
        lines.append("<i>✅ — уже верно; ✋ — засчитано вручную; ❌ — была ошибка, можно ответить ещё раз.</i>")
    return "\n".join(lines)


def bex_keyboard(unit: dict, x: dict, done: dict, pick: dict, sure: list, wins: int = 0) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    todo = [n for n in range(1, len(x["items"]) + 1) if not (done.get((x["id"], n)) or {}).get("ok")]
    if x.get("type") in CHOICE_TYPES:
        for n in todo:
            opts = book_options(x, x["items"][n - 1])
            row = []
            for L in LETTERS[:len(opts)]:
                label = TF_LABELS.get(L, L) if x.get("type") == "tf" else L
                row.append(((f"✅{n}{label}" if pick.get(str(n)) == L else f"{n} {label}"), f"bx:a:{n}:{L}"))
            if len(row) < 8:
                row.append((f"❗{n}" if n in sure else f"{n} !", f"bx:a:{n}:!"))
            rows.append(row)
    hints = [(f"💡{n}", f"bx:h:{n}") for n in todo if x["items"][n - 1].get("hint")]
    rows += [hints[i:i + 6] for i in range(0, len(hints), 6)]
    if x.get("type") in CHOICE_TYPES and todo:
        rows.append([(f"📨 Проверить отмеченные ({len(pick)})", "bx:go")])
    rows += again_rows(unit, x, done, wins)
    if todo:
        rows.append([("✋ Засчитать всё упражнение", f"bx:m:{unit['unit']}|{x['id']}")])
    rows += page_row(unit, x)
    rows.append([("📖 Правило", f"bx:R:{unit['unit']}|{x['id']}"), ("📋 Все упражнения", f"bx:l:{unit['unit']}")])
    return rows


def again_rows(unit: dict, x: dict, done: dict, wins: int) -> list[list[tuple[str, str]]]:
    """«🔁 Выполнить заново» — только когда текущая попытка на 100%; «📜» — когда есть выполненная попытка."""
    u, row = unit["unit"], []
    if bex_complete(x, done):
        row.append(("🔁 Выполнить заново", f"bx:r:{u}|{x['id']}"))
    if wins:
        row.append(("📜 Выполненная попытка", f"bx:v:{u}|{x['id']}"))
    return [row] if row else []


def bex_attempt_view(unit: dict, x: dict, attempts: list[dict], now: float) -> str:
    """📜 Последняя попытка на 100% — все ответы сразу."""
    last = attempts[-1]
    hand = set(last.get("manual") or [])
    lines = [f"📜 <b>{e(unit['book_short'])} · {e(unit['name'])} · {e(bex_name(x))}</b>",
             f"<i>Выполнено на 100%: {len(attempts)} раз(а), последний — {ago(last['at'], now)}"
             + (f"; ✋ вручную засчитано пунктов: {len(hand)}" if hand else "") + ".</i>", ""]
    if x.get("text"):
        lines += [f"<blockquote>{e(x['text'])}</blockquote>", ""]
    for n, it in enumerate(x["items"], 1):
        ans = last["answers"].get(str(n)) or it.get("answer", "")
        q = e(it.get("q", "")).replace("___", "<b>___</b>")
        lines.append(f"{'✋ ' if n in hand else ''}{n}. {q} → <b>{e(ans)}</b>")
        if it.get("ru") and not _same(it.get("q", ""), it["ru"]):
            lines.append(f"    <i>— {e(it['ru'])}</i>")
    return "\n".join(lines)


def page_row(unit: dict, x: dict) -> list[list[tuple[str, str]]]:
    """«📄 Страница учебника / тетради» — картинка страницы из PDF на сервере (диалоги, таблицы, рисунки)."""
    pages = book_pages(x)
    if not pages or not (unit.get("files") or {}).get(x.get("src", "tb")):
        return []
    what = "тетради" if x.get("src") == "wb" else "учебника"
    nums = ", ".join(map(str, pages))
    return [[(f"📄 Страница {what} (s. {nums})", f"bx:p:{unit['unit']}|{x['id']}")]]


def bex_result_buttons(unit: dict, x: dict, done: dict, wins: int = 0,
                       wrong: list[int] | None = None) -> list[list[tuple[str, str]]]:
    u = unit["unit"]
    exs = unit.get("exercises") or []
    ok, _ = bex_counts(x, done)
    rows = []
    data = f"bx:w:{u}|{x['id']}|{','.join(map(str, wrong or []))}"
    if wrong and len(data.encode()) <= 64:   # описка — засчитать ошибки этой проверки, не переписывая
        rows.append([(f"✋ Засчитать ошибочные ({len(wrong)})", data)])
    if ok < len(x["items"]):
        rows.append([(f"▶️ Продолжить (осталось {len(x['items']) - ok})", f"bx:o:{u}|{x['id']}")])
    i = next((k for k, y in enumerate(exs) if y["id"] == x["id"]), -1)
    nxt = next((y for y in exs[i + 1:] if bex_icon(y, done) != "✅"), None)
    if nxt:
        rows.append([(f"➡️ {bex_name(nxt)}"[:60], f"bx:o:{u}|{nxt['id']}")])
    rows += again_rows(unit, x, done, wins)
    rows.append([("📖 Правило", f"bx:R:{u}|{x['id']}"), ("📋 Все упражнения", f"bx:l:{u}")])
    if x.get("type") not in ("tf", "free"):
        rows.append([("⭐ В словарь", f"dX:b:{u}|{x['id']}")])
    return rows + page_row(unit, x)


def bex_rule_menu(unit: dict, x: dict, sel=()) -> tuple[str, list[list[tuple[str, str]]]]:
    """📖 Правило: ко всему упражнению или к выбранным пунктам. Номера — переключатели (☑️): отмеченные хранятся
    прямо в кнопках (bx:t:<юнит>|<упр>|<маска>|<пункт>), «Объяснить выбранные» — bx:y:<юнит>|<упр>|m<маска>."""
    u, sel = f"{unit['unit']}|{x['id']}", set(sel)
    mask = items_mask(sel)
    nums = [(f"☑️{n}" if n in sel else str(n), f"bx:t:{u}|{mask}|{n}") for n in range(1, len(x["items"]) + 1)]
    rows = [[("📖 Всё упражнение", f"bx:y:{u}|0")]] + [nums[i:i + 8] for i in range(0, len(nums), 8)]
    if sel:
        rows.append([(f"📖 Объяснить выбранные ({len(sel)})", f"bx:y:{u}|m{mask}")])
    return (f"📖 <b>Правило</b> · {e(bex_name(x))}\n<i>Ко всему упражнению или к пунктам? Отметь один или несколько "
            "пунктов и нажми «Объяснить выбранные»: общее правило объясню один раз, разные — по отдельности. "
            "Можно и прямо в ответе: «3? 5?» — проверю ответы и пришлю правило к пунктам 3 и 5.</i>", rows)


# ---------- 📚 чтение ----------

def pct(done: int, total: int) -> int:
    return round(100 * done / total) if total else 0


def read_counts(units: list[dict], done: dict) -> tuple[int, int]:
    """(прочитано абзацев, всего) по списку глав."""
    return (sum(len({i for i in done.get(u["unit"], set()) if i < len(u["paragraphs"])}) for u in units),
            sum(len(u["paragraphs"]) for u in units))


def read_books_screen(books: list[dict], done: dict) -> tuple[str, list[list[tuple[str, str]]]]:
    all_units = [u for b in books for u in b["units"]]
    lines = [f"📚 <b>Чтение</b> · всё прочитанное: {pct(*read_counts(all_units, done))}%", ""]
    for b in books:
        r, t = read_counts(b["units"], done)
        lines.append(f"<b>{e(b['title'])}</b> · {len(b['units'])} гл. · {pct(r, t)}%")
    return "\n".join(lines), [[(f"📚 {b['title']}"[:60], f"rd:b:{b['id']}")] for b in books]


def read_book_screen(book: dict, done: dict, all_units: list[dict], cont: tuple[dict, int] | None,
                     started: bool, many: bool) -> tuple[str, list[list[tuple[str, str]]]]:
    r, t = read_counts(book["units"], done)
    lines = [f"📚 <b>{e(book['title'])}</b> · прочитано {pct(r, t)}% ({r} из {t} абзацев)"]
    if many:
        lines.append(f"Всё чтение в боте: {pct(*read_counts(all_units, done))}%")
    lines.append("")
    for u in book["units"]:
        ur, ut = read_counts([u], done)
        icon = "✅" if ur == ut else "📖" if ur else "▫️"
        ru = f" ({e(u['title_ru'])})" if u.get("title_ru") else ""
        lines.append(f"{icon} {e(u['name'])} — <b>{e(u['title'])}</b>{ru} · {ut} абз. · {pct(ur, ut)}%")
    lines.append("\n<i>Абзац — текст по-польски, перевод под спойлером, 🔊 аудио в обычном темпе, "
                 "📖 слова абзаца — в ⭐ словарь. После главы — вопросы и тренировка её слов.</i>")
    rows = []
    if cont:
        u, i = cont
        rows.append([(f"▶️ {'Продолжить' if started else 'Начать'}: {u['name']}, абзац {i + 1}"[:60],
                      f"rd:g:{u['unit']}|{i}")])
    rows += [[(f"{u['name']} — {u['title']}"[:60], f"rd:o:{u['unit']}")] for u in book["units"]]
    if many:
        rows.append([("📚 Все книги", "rd:l")])
    return "\n".join(lines), rows


def read_paragraph(unit: dict, idx: int, book_pct: int, all_pct: int | None) -> tuple[str, list[list[tuple[str, str]]]]:
    p = unit["paragraphs"][idx]
    n = len(unit["paragraphs"])
    u = unit["unit"]
    total = f"книга {book_pct}%" + (f" · всё чтение {all_pct}%" if all_pct is not None else "")
    text = (f"📚 <b>{e(unit['title'])}</b> · {e(unit['name'])} · абзац {idx + 1}/{n}\n\n"
            f"{e(p['pl'])}\n\n"
            f"🇷🇺 <tg-spoiler>{e(p['ru'])}</tg-spoiler>\n\n"
            f"<i>Прочитано: {total}</i>")
    nav = []
    if idx > 0:
        nav.append(("⬅️", f"rd:g:{u}|{idx - 1}|{idx}"))
    nav.append(("📑 Главы", f"rd:c:{unit['book']}"))
    nav.append(("➡️ Дальше", f"rd:g:{u}|{idx + 1}|{idx}") if idx + 1 < n else ("🏁 Конец главы", f"rd:e:{u}"))
    rows = [[("🔊 Слушать", f"rd:a:{u}|{idx}"), (f"📖 Слова ({len(p.get('words') or [])})", f"rd:w:{u}|{idx}")],
            [("❓ Спросить про абзац", f"rd:q:{u}|{idx}")], nav]
    return text, rows


def read_words(unit: dict, idx: int, in_dict: set[str]) -> tuple[str, list[list[tuple[str, str]]]]:
    words = unit["paragraphs"][idx].get("words") or []
    lines = [f"📖 <b>Слова абзаца {idx + 1}</b> · {e(unit['name'])} — {e(unit['title'])}", ""]
    for k, w in enumerate(words, 1):
        lines.append(f"{k}. <b>{e(w['pl'])}</b> — {e(w['ru'])}")
        if w.get("rule"):
            lines.append(f"     📐 <i>{e(w['rule'])}</i>")
    lines.append("\n<i>⭐ — добавить в словарь, ✅ — уже там (нажми, чтобы убрать).</i>")
    btns = [(f"{'✅' if w['pl'].lower() in in_dict else '⭐'} {w['pl']}"[:40], f"rd:s:{unit['unit']}|{idx}|{k}")
            for k, w in enumerate(words)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if any(w["pl"].lower() not in in_dict for w in words):
        rows.append([("⭐ Все слова абзаца — в словарь", f"rd:S:{unit['unit']}|{idx}")])
    return "\n".join(lines), rows


def read_end(unit: dict, nxt: dict | None) -> tuple[str, list[list[tuple[str, str]]]]:
    u = unit["unit"]
    nx = len(unit.get("exercises") or [])
    text = (f"🏁 <b>{e(unit['name'])} — {e(unit['title'])}</b> прочитана!\n\n"
            f"<i>Закрепить: вопросы по тексту и тренировка слов главы ({len(unit['words'])}) — карточки, тест, пропуски.</i>")
    rows = []
    if nx:
        rows.append([(f"📝 Вопросы к главе ({nx})", f"bx:l:{u}")])
    rows.append([(f"🎯 Слова главы — тренировка ({len(unit['words'])})", f"rd:t:{u}")])
    if nxt:
        rows.append([(f"➡️ {nxt['name']} — {nxt['title']}"[:60], f"rd:o:{nxt['unit']}")])
    rows.append([("📑 Главы", f"rd:c:{unit['book']}")])
    return text, rows
