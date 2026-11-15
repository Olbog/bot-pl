"""📜 /learn — тексты наизусть.

Тексты юнитов — поле "texts" в unit_NN.json ({title, pl, translit, ru}; пишет Claude, 2 на юнит, 40–70 слов, только
лексика и грамматика пройденного). Свои тексты — ученик пишет тему, Gemini составляет текст в рамках пройденных юнитов
(memo_texts). Ключ текста: «kpk:3#1» — юнит и номер, «my12» — свой. Отметка — zv_status с item «mt:<ключ>»:
⚪ не начато → 🟡 учу → ✅ выучено (✅ ставится и сам: наизусть ≥ MT_DONE_SCORE % и без ошибок).

Карточка: текст, транскрипция и перевод под спойлером, аудио 🐢 / 🔊 (тексты юнитов озвучены заранее, свои — при первом
открытии, кэш data/audio/mt/). После открытия карточки (шаг «mt», живёт MT_LISTEN_MIN минут) голосовое или текст —
проверка: 🎯 наизусть (сравнение с текстом: ошибки языка, отличия, пропуски) или 💬 пересказ (ошибки языка, что не
передано, правильная версия). Ошибки языка — в общий пул, как в разговоре: 📖 правило, ⭐ в словарь, 🙅 не ошибка.
"""
import hashlib
from html import escape
from pathlib import Path

from .core import rules, textbook
from .ai.prompt import (MT_CHECK_HINT, MT_CHECK_SCHEMA, MT_TEXT_HINT, MT_TEXT_SCHEMA, RULES_HINT, RULES_SCHEMA,
                        mt_check_prompt, mt_text_prompt, rules_for_errors_prompt)
from .ui import fmt
from .settings import VOICE_MIN_SEC
from .zvuki import RATES, STATUS, STATUS_NAME

MT_LISTEN_MIN = 60     # столько минут после открытия карточки / проверки ответ считается рассказом текста
MT_DONE_SCORE = 90     # наизусть не меньше стольких процентов и без ошибок — отметка ✅ сама
MODES = {"e": "🎯 Наизусть", "r": "💬 Пересказ"}
NEXT_LABEL = ["🟡 Отметить: учу", "✅ Отметить: выучено", "⚪ Начать заново"]
MT_STATUS = ["не начато", "учу", "выучено"]


def unit_texts() -> list[dict]:
    """Юниты, где есть тексты, по порядку."""
    return [u for u in textbook.load_units() if u.get("texts")]


class MemorizeMixin:
    # ---------- данные ----------

    def mt_text(self, user_id: int, key: str) -> dict | None:
        """{key, title, pl, translit, ru, where, own, unit} или None."""
        if key.startswith("my"):
            t = self.db.memo_get(user_id, int(key[2:])) if key[2:].isdigit() else None
            return {**t, "key": key, "where": f"✍️ Мой текст · {t['topic']}", "own": True} if t else None
        unit_id, _, i = key.rpartition("#")
        unit = textbook.get_unit(unit_id) if i.isdigit() else None
        texts = (unit or {}).get("texts") or []
        if not 1 <= int(i or 0) <= len(texts):
            return None
        return {**texts[int(i) - 1], "key": key, "where": f"{unit['book_short']} {unit['name']} · {unit['title']}",
                "own": False, "unit": unit["unit"], "n": int(i)}

    def mt_keys(self, user_id: int, key: str) -> list[str]:
        """Тексты того же списка по порядку (для «➡️ следующий»)."""
        if key.startswith("my"):
            return [f"my{t['id']}" for t in self.db.memo_list(user_id)]
        unit_id = key.rpartition("#")[0]
        unit = textbook.get_unit(unit_id)
        return [f"{unit_id}#{i}" for i in range(1, len((unit or {}).get("texts") or []) + 1)]

    def mt_status(self, user_id: int) -> dict[str, int]:
        return {k[3:]: v for k, v in self.db.zv_status(user_id).items() if k.startswith("mt:")}

    def mt_dir(self) -> Path:
        return getattr(self, "mt_audio_dir", None) or Path(self.cfg.db_path).parent / "audio" / "mt"

    def mt_path(self, t: dict, speed: str) -> Path:
        h = hashlib.md5(f"{self.cfg.tts_voice}|{RATES[speed]}|{t['pl']}".encode()).hexdigest()[:8]
        name = t["key"].replace(":", "_").replace("#", "_")
        return self.mt_dir() / f"{name}_{speed}_{h}.ogg"

    def mt_jobs(self) -> list[tuple[Path, str, str]]:
        jobs = []
        for u in unit_texts():
            for i, t in enumerate(u["texts"], 1):
                t = {**t, "key": f"{u['unit']}#{i}"}
                jobs += [(self.mt_path(t, sp), t["pl"], RATES[sp]) for sp in ("n", "s")]
        return jobs

    async def mt_pregen(self, sleep=None) -> None:
        await self.pregen_audio(self.mt_jobs, "📜 Тексты наизусть", sleep)

    def mt_listening(self, pending: dict | None) -> dict | None:
        if (pending or {}).get("step") == "mt" and self.clock() - pending.get("at", 0) <= MT_LISTEN_MIN * 60:
            return pending
        return None

    # ---------- экраны ----------

    async def mt_menu(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        st = self.mt_status(user_id)
        units = unit_texts()
        own = self.db.memo_list(user_id)
        keys = [f"{u['unit']}#{i}" for u in units for i in range(1, len(u["texts"]) + 1)] + [f"my{t['id']}" for t in own]
        cnt = [sum(1 for k in keys if st.get(k, 0) == s) for s in range(3)]
        text = (f"📜 <b>Тексты наизусть</b> · ✅ {cnt[2]} · 🟡 {cnt[1]} · ⚪ {cnt[0]}\n"
                "<i>Короткие тексты по юнитам и по твоим темам: учишь, потом рассказываешь голосом или текстом — "
                "🎯 наизусть или 💬 пересказом. Ошибки — как в разговоре.</i>")
        rows = []
        for u in units:
            marks = "".join(STATUS[st.get(f"{u['unit']}#{i}", 0)] for i in range(1, len(u["texts"]) + 1))
            rows.append([(f"{u['book_short']} {u['num']} · {u['title']}"[:44] + f" {marks}", f"ml:u:{u['unit']}")])
        rows.append([(f"✍️ Мои тексты ({len(own)})", "ml:my"), ("➕ Текст по своей теме", "ml:new")])
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, rows)
        else:
            await self.tg.send_message(chat_id, text, rows)

    def mt_list_screen(self, user_id: int, which: str) -> tuple[str, list] | None:
        """which: unit_id или «my»."""
        st = self.mt_status(user_id)
        if which == "my":
            items = [(f"my{t['id']}", t["title"]) for t in self.db.memo_list(user_id)]
            head = ("✍️ <b>Мои тексты</b>\n<i>Тексты по твоим темам — Gemini составляет их в рамках пройденных юнитов. "
                    "Кружок справа — отметка: ⚪ → 🟡 учу → ✅ выучено.</i>")
            if not items:
                head += "\n\nПока пусто — «➕ Текст по своей теме»."
        else:
            unit = textbook.get_unit(which)
            if not unit or not unit.get("texts"):
                return None
            items = [(f"{unit['unit']}#{i}", t["title"]) for i, t in enumerate(unit["texts"], 1)]
            head = (f"📜 <b>{escape(unit['book_short'])} {escape(unit['name'])} · {escape(unit['title'])}</b>\n"
                    "<i>Нажми название — придут текст и аудио. Кружок справа — отметка: ⚪ → 🟡 учу → ✅ выучено.</i>")
        rows = [[(f"{n}. {title}"[:40], f"ml:o:{k}"), (STATUS[st.get(k, 0)], f"ml:c:{k}")]
                for n, (k, title) in enumerate(items, 1)]
        if which == "my":
            rows.append([("➕ Текст по своей теме", "ml:new")])
        rows.append([("⬅️ Все тексты", "ml:m")])
        return head, rows

    def mt_card(self, t: dict, status: int, mode: str, nxt: str | None) -> tuple[str, list]:
        text = (f"📜 <b>{escape(t['where'])}</b>\n<b>{escape(t['title'])}</b>\n\n"
                f"<b>{escape(t['pl'])}</b>\n\n"
                f"🔤 <tg-spoiler>{escape(t.get('translit', ''))}</tg-spoiler>\n\n"
                f"🇷🇺 <tg-spoiler>{escape(t.get('ru', ''))}</tg-spoiler>\n\n"
                f"Отметка: {STATUS[status]} {MT_STATUS[status]}\n\n"
                "<i>Выучи и расскажи — голосовым или текстом. 🎯 Наизусть — сравню с текстом; "
                "💬 Пересказ — своими словами, проверю грамматику и смысл. Режим — кнопками ниже. "
                f"Пока карточка открыта (до {MT_LISTEN_MIN} мин после последней проверки), всё, что пришлёшь, — "
                "рассказ текста; чтобы просто поговорить — «⏹ Закончить» или любая команда.</i>")
        return text, self.mt_card_rows(t, status, mode, nxt)

    @staticmethod
    def mt_card_rows(t: dict, status: int, mode: str, nxt: str | None) -> list:
        k = t["key"]
        rows = [[("🐢 Медленно", f"ml:a:{k}:s"), ("🔊 Обычный темп", f"ml:a:{k}:n")],
                [((f"✅ {lbl}" if m == mode else lbl), f"ml:g:{k}:{m}") for m, lbl in MODES.items()],
                [(NEXT_LABEL[status], f"ml:s:{k}")],
                [("📋 Список", f"ml:l:{k}")] + ([("➡️ Следующий", f"ml:o:{nxt}")] if nxt else []),
                [("⏹ Закончить — дальше разговор", "ml:q")]]
        if t.get("own"):
            rows.append([("🗑 Удалить текст", f"ml:del:{k[2:]}")])
        return rows

    async def mt_open(self, chat_id: int, user_id: int, key: str, mode: str = "e") -> None:
        t = self.mt_text(user_id, key)
        if not t:
            await self.tg.send_message(chat_id, "Текст не найден — открой список заново: /learn")
            return
        keys = self.mt_keys(user_id, key)
        nxt = keys[keys.index(key) + 1] if key in keys and keys.index(key) + 1 < len(keys) else None
        await self.tg.send_message(chat_id, *self.mt_card(t, self.mt_status(user_id).get(key, 0), mode, nxt))
        self.db.set_pending(user_id, {"step": "mt", "key": key, "mode": mode, "at": self.clock()})
        await self.mt_send_audio(chat_id, t, "n")

    async def mt_send_audio(self, chat_id: int, t: dict, speed: str) -> str | None:
        await self.tg.send_action(chat_id, "record_voice")
        try:
            ogg = await self.cached_tts(self.mt_path(t, speed), t["pl"], RATES[speed])
        except Exception:
            return "🔇 Озвучка не получилась — попробуй позже"
        await self.tg.send_voice(chat_id, ogg)
        return None

    # ---------- кнопки ----------

    async def mt_callback(self, chat_id: int, user_id: int, message_id: int, pending: dict | None,
                          arg: str) -> str | None:
        """ml:m — все тексты; ml:u:<юнит> / ml:my — список; ml:l:<ключ> — список текста; ml:o:<ключ> — открыть;
        ml:a:<ключ>:<s|n> — аудио; ml:g:<ключ>:<e|r> — режим; ml:s:<ключ> — отметка в карточке, ml:c:<ключ> — в списке;
        ml:S:<попытка> — отметка под результатом; ml:new — свой текст; ml:del / ml:DEL:<id> — удалить;
        ml:d / ml:x / ml:k — «🙅 Не ошибка»; ml:r:<попытка> — правила по ошибкам. Возвращает всплывашку."""
        kind, _, rest = arg.partition(":")
        if kind == "q":   # ⏹ голосовые и текст — снова в разговор
            if (pending or {}).get("step") == "mt":
                self.db.set_pending(user_id, None)
            return "⏹ Готово: сообщения снова идут в разговор"
        if kind == "m":
            await self.mt_menu(chat_id, user_id, message_id)
        elif kind in ("u", "my", "l"):
            which = "my" if kind == "my" or rest.startswith("my") else (rest if kind == "u" else rest.rpartition("#")[0])
            screen = self.mt_list_screen(user_id, which)
            if not screen:
                return "Список не найден"
            if kind == "l":
                await self.tg.send_message(chat_id, *screen)
            else:
                await self.tg.edit_message(chat_id, message_id, *screen)
        elif kind == "o":
            await self.mt_open(chat_id, user_id, rest)
        elif kind == "a":
            key, _, speed = rest.rpartition(":")
            t = self.mt_text(user_id, key)
            return await self.mt_send_audio(chat_id, t, speed if speed in RATES else "n") if t else "Текст не найден"
        elif kind == "g":
            key, _, mode = rest.rpartition(":")
            t = self.mt_text(user_id, key)
            if not t or mode not in MODES:
                return None
            self.db.set_pending(user_id, {"step": "mt", "key": key, "mode": mode, "at": self.clock()})
            keys = self.mt_keys(user_id, key)
            nxt = keys[keys.index(key) + 1] if key in keys and keys.index(key) + 1 < len(keys) else None
            await self.tg.edit_markup(chat_id, message_id,
                                      self.mt_card_rows(t, self.mt_status(user_id).get(key, 0), mode, nxt))
            return f"{MODES[mode]}: жду голосовое или текст"
        elif kind == "R":   # режим под результатом: ml:R:<попытка>|<e|r>
            attempt, _, mode = rest.rpartition("|")
            key = attempt.rpartition("@")[0]
            if mode not in MODES or not self.mt_text(user_id, key):
                return None
            self.db.set_pending(user_id, {"step": "mt", "key": key, "mode": mode, "at": self.clock()})
            await self.tg.edit_markup(chat_id, message_id, self.mt_result_rows(user_id, attempt, mode))
            return f"{MODES[mode]}: жду голосовое или текст"
        elif kind in ("s", "c", "S"):
            key = rest.rpartition("@")[0] if kind == "S" else rest
            t = self.mt_text(user_id, key)
            if not t:
                return "Текст не найден"
            new = (self.mt_status(user_id).get(key, 0) + 1) % 3
            self.db.zv_set(user_id, f"mt:{key}", new)
            if kind == "c":
                await self.tg.edit_message(chat_id, message_id, *self.mt_list_screen(user_id, "my" if key.startswith("my")
                                                                                     else key.rpartition("#")[0]))
            elif kind == "s":
                mode = (pending or {}).get("mode", "e") if (pending or {}).get("key") == key else "e"
                keys = self.mt_keys(user_id, key)
                nxt = keys[keys.index(key) + 1] if key in keys and keys.index(key) + 1 < len(keys) else None
                await self.tg.edit_message(chat_id, message_id, *self.mt_card(t, new, mode, nxt))
            else:
                mode = (pending or {}).get("mode", "e")
                await self.tg.edit_markup(chat_id, message_id, self.mt_result_rows(user_id, rest, mode))
            return f"{STATUS[new]} {MT_STATUS[new]}"
        elif kind == "new":
            self.db.set_pending(user_id, {"step": "mt_topic"})
            await self.tg.send_message(chat_id, "➕ <b>Текст по своей теме</b>\nНапиши тему — например, «мой день», "
                                                 "«в кафе», «моя квартира». Gemini составит текст на 40–70 слов "
                                                 "из того, что ты уже прошёл в юнитах.")
        elif kind in ("del", "DEL") and rest.isdigit():
            t = self.db.memo_get(user_id, int(rest))
            if not t:
                return "Текст уже удалён"
            if kind == "del":
                await self.tg.send_message(chat_id, f"🗑 Удалить текст «{escape(t['title'])}»? Отметка тоже удалится.",
                                           [[("🗑 Да, удалить", f"ml:DEL:{rest}"), ("Нет", "ml:my")]])
                return None
            self.db.memo_delete(user_id, int(rest))
            await self.tg.edit_message(chat_id, message_id, *self.mt_list_screen(user_id, "my"))
            return "Удалено"
        elif kind in ("d", "x", "k"):
            return await self.mt_dispute(chat_id, user_id, message_id, kind, rest)
        elif kind == "r":
            corrs = [c for c in self.db.corrections_by(user_id, "mt_attempt", rest) if not c["_disputed"]]
            if not corrs:
                await self.tg.send_message(chat_id, "Ошибок нет — объяснять нечего 👍")
                return None
            data = await self.ask(chat_id, rules_for_errors_prompt(corrs, self.cfg.level), RULES_SCHEMA, RULES_HINT)
            if data is not None:
                await self.tg.send_message(chat_id, fmt.rules_message(data))
        return None

    # ---------- ➕ свой текст ----------

    def mt_scope(self, user_id: int) -> list[str]:
        """Краткое содержание пройденных юнитов: учебник последнего юнита, с которым работал, — от первого до него.
        Ничего не делал — первые три юнита первого учебника с текстами."""
        recent = [u for u in (textbook.get_unit(x) for x in self.db.recent_units(user_id, 20)) if u and not u.get("rev") and not u.get("les")]
        if recent:
            units = textbook.load_units(book=recent[0]["book"])
            ids = [x["unit"] for x in units]
            units = units[:max(ids.index(u["unit"]) for u in recent if u["unit"] in ids) + 1]
        else:
            units = unit_texts()[:3]
        return [f"{u['book_short']} {u['num']}: {u.get('summary', u['title'])}" for u in units][-12:]

    async def mt_new(self, chat_id: int, user_id: int, topic: str) -> None:
        topic = topic.strip()[:80]
        self.db.set_pending(user_id, None)
        data = await self.ask(chat_id, mt_text_prompt(topic, self.cfg.level, self.mt_scope(user_id)), MT_TEXT_SCHEMA,
                              MT_TEXT_HINT, wait=f"⏳ Составляю текст «{escape(topic)}»… (10–20 с)")
        if data is None:
            return
        t = {k: str(data.get(k) or "").strip() for k in ("title", "pl", "translit", "ru")}
        if not t["pl"]:
            await self.tg.send_message(chat_id, "Не получилось составить текст — попробуй ещё раз или другую тему.")
            return
        t["title"] = t["title"] or topic
        text_id = self.db.memo_add(user_id, topic, t)
        await self.mt_open(chat_id, user_id, f"my{text_id}")

    # ---------- 🎙 проверка ----------

    async def mt_answer(self, chat_id: int, user_id: int, pending: dict, text: str, voice: dict | None) -> None:
        key, mode = pending["key"], pending.get("mode", "e")
        t = self.mt_text(user_id, key)
        if not t:
            self.db.set_pending(user_id, None)
            await self.tg.send_message(chat_id, "Текст не найден — открой его заново: /learn")
            return
        if voice and not text:
            sec = voice.get("duration")
            if sec is not None and sec < VOICE_MIN_SEC:
                await self.tg.send_message(chat_id, "🎙 Запись пустая или слишком короткая — пришли ещё раз.")
                return
        self.db.set_pending(user_id, {"step": "mt", "key": key, "mode": mode, "at": self.clock()})
        audio = await self.tg.download_file(voice["file_id"]) if voice and not text else None
        prompt = mt_check_prompt(t["pl"], t.get("ru", ""), "exact" if mode == "e" else "retell", self.cfg.level,
                                 voice=audio is not None)
        if audio is not None and self.ignore_block(user_id):
            prompt = self.ignore_block(user_id) + "\n\n" + prompt
        if audio is None:
            prompt += f"\n\nОТВЕТ УЧЕНИКА:\n{text}"
        data = await self.ask(chat_id, prompt, MT_CHECK_SCHEMA, MT_CHECK_HINT,
                              wait=f"⏳ {'Слушаю' if audio is not None else 'Читаю'} и проверяю: "
                                   f"{MODES[mode].split(' ', 1)[1].lower()}…", audio=audio)
        if data is None:
            return
        heard = str(data.get("heard") or "").strip() or (text or "")
        if not heard:
            await self.tg.send_message(chat_id, "🎙 Не расслышал речь — расскажи ещё раз, поближе к микрофону.")
            return
        corrs = []
        for c in data.get("corrections") or []:
            if isinstance(c, dict) and c.get("correct"):
                c = {k: (str(v).strip() if isinstance(v, str) else v) for k, v in c.items()}
                c["rule"] = rules.normalize(c.get("rule"))
                c["source"] = "текст наизусть"
                corrs.append(c)
        corrs = self.drop_ignored(user_id, corrs)
        attempt = f"{key}@{int(self.clock() * 1000)}"
        if corrs:
            self.db.add_corrections(self.db.current_session(user_id), user_id,
                                    [{**c, "mt": key, "mt_attempt": attempt} for c in corrs])
        try:
            score = max(0, min(100, int(data.get("score") or 0)))
        except (TypeError, ValueError):
            score = 0
        cur = self.mt_status(user_id).get(key, 0)
        new = 2 if mode == "e" and score >= MT_DONE_SCORE and not corrs else (1 if cur == 0 else cur)
        if new != cur:
            self.db.zv_set(user_id, f"mt:{key}", new)
        diffs = [d for d in data.get("diffs") or [] if isinstance(d, dict) and (d.get("text") or d.get("said"))]
        missed = [str(m).strip() for m in data.get("missed") or [] if str(m).strip()]
        text_out = fmt.mt_result(t, mode, heard, bool(audio), corrs, diffs if mode == "e" else [], missed,
                                 {k: str(data.get(f"corrected_{k}") or "").strip() for k in ("pl", "translit", "ru")}
                                 if mode == "r" else {}, score, str(data.get("comment") or "").strip(),
                                 new, new == 2 and cur != 2)
        await self.tg.send_message(chat_id, text_out, self.mt_result_rows(user_id, attempt, mode))

    def mt_result_rows(self, user_id: int, attempt: str, mode: str) -> list:
        key = attempt.rpartition("@")[0]
        corrs = self.db.corrections_by(user_id, "mt_attempt", attempt)
        rows = []
        if corrs:
            rows.append([("📖 Правило", f"ml:r:{attempt}"), ("⭐ В словарь", f"dX:m:{attempt}"),
                         ("🙅 Не ошибка", f"ml:d:{attempt}")])
        rows.append([((f"✅ {lbl}" if m == mode else lbl), f"ml:R:{attempt}|{m}") for m, lbl in MODES.items()])
        status = self.mt_status(user_id).get(key, 0)
        rows.append([(NEXT_LABEL[status], f"ml:S:{attempt}")])
        rows.append([("📜 Текст", f"ml:o:{key}"), ("📋 Список", f"ml:l:{key}")])
        rows.append([("⏹ Закончить — дальше разговор", "ml:q")])
        return rows

    async def mt_dispute(self, chat_id: int, user_id: int, message_id: int, kind: str, arg: str) -> str | None:
        """ml:d:<попытка> — экран «Не ошибка»; ml:x:<id ошибки> — отметить/вернуть; ml:k:<попытка> — готово."""
        edit = None
        if kind == "x":
            c = self.db.correction(int(arg)) if arg.isdigit() else None
            if not c or c["_user"] != user_id or not c.get("mt_attempt"):
                return "Ошибка не найдена"
            self.dispute_toggle(user_id, c)
            arg, kind, edit = c["mt_attempt"], "d", message_id
        corrs = self.db.corrections_by(user_id, "mt_attempt", arg)
        if not corrs:
            return "Ошибок нет"
        if kind == "k":
            await self.tg.edit_message(chat_id, message_id, fmt.dispute_done(sum(c["_disputed"] for c in corrs)))
            return None
        text, rows = fmt.dispute_screen(corrs, 0, toggle="ml:x:", done=f"ml:k:{arg}")
        if edit:
            await self.tg.edit_message(chat_id, edit, text, rows)
        else:
            await self.tg.send_message(chat_id, text, rows)
        return None

