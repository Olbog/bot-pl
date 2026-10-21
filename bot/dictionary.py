"""Словарь ⭐ и «🙅 Не ошибка»: выражения из ответов, оспоренные исправления, исключения."""
from html import escape

from .ai.prompt import (PHRASES_HINT, PHRASES_SCHEMA, TOPICS_HINT, TOPICS_SCHEMA, own_phrases_prompt,
                        phrases_prompt, topics_prompt)
from .core.db import DICT_OTHER
from .ui import fmt
from .exercises import logic as exercises
from .settings import DICT_THINKING, IGNORE_PROMPT

RELAYOUT_CHUNK = 40   # 🤖 «Разложить Разное»: слов в одном запросе к модели


def offer_items(offer: dict) -> list[tuple[str, dict]]:
    """Пункты меню «⭐ В словарь» по порядку: («e0», ошибка) …, («b0», из ответа бота) …"""
    return ([(f"e{i}", it) for i, it in enumerate(offer.get("errs") or [])]
            + [(f"b{i}", it) for i, it in enumerate(offer.get("phrases") or [])])


def _phrase(p: dict) -> dict:
    return {"pl": str(p.get("pl") or "").strip(), "translit": str(p.get("translit") or "").strip(),
            "ru": str(p.get("ru") or "").strip(), "topic": str(p.get("topic") or "").strip()}


class DictionaryMixin:
    # ---------- ⭐ В словарь: меню под ответом бота ----------
    # Сразу (без модели): 🧩 мои ошибки из этого сообщения. По кнопке: 💬 подобрать из ответа бота (модель, «low»),
    # ✍️ свои слова (текстом). 📂 Куда: 🤖 авто — тему каждому слову даёт модель; или своя тема — всё отмеченное туда.
    # «✅ Готово»: слово ложится в набор своей темы «⭐ <тема> N» (db.dict_add).

    def offer_errs(self, msg_id: int) -> list[dict]:
        """Мои ошибки в этом сообщении — правильные фрагменты без повторов: [{pl, translit, ru}]."""
        seen, out = set(), []
        for c in self.db.corrections_for_msg(msg_id):
            pl = str(c.get("correct") or "").strip()
            if pl and pl.lower() not in seen:
                seen.add(pl.lower())
                out.append({"pl": pl, "translit": str(c.get("translit") or "").strip(),
                            "ru": str(c.get("ru") or "").strip()})
        return out[:8]

    def offer_load(self, user_id: int, msg_id: int) -> dict | None:
        offer = self.db.offer_get(msg_id)
        if offer:
            return offer if offer["user_id"] == user_id else None
        row = self.db.model_message(msg_id)
        if not row or row["user_id"] != user_id:
            return None
        offer = {"user_id": user_id, "phrases": [], "saved": [], "errs": self.offer_errs(msg_id), "sel": [],
                 "bot_ready": False, "topic": ""}
        self.offer_store(msg_id, offer)
        return offer

    def offer_store(self, msg_id: int, offer: dict) -> None:
        self.db.offer_save(msg_id, offer["user_id"], offer["phrases"], offer["saved"], offer["errs"], offer["sel"],
                           offer["bot_ready"], offer.get("topic") or "")

    def offer_view(self, user_id: int, msg_id: int, offer: dict) -> tuple[str, list]:
        have = {k for k, it in offer_items(offer) if self.db.dict_has(user_id, it["pl"])}
        return fmt.offer_message(offer, have), fmt.offer_buttons(msg_id, offer, have)

    async def offer_phrases(self, chat_id: int, user_id: int, msg_id: int) -> None:
        """⭐ В словарь — меню приходит сразу, модель не вызывается."""
        offer = self.offer_load(user_id, msg_id)
        if offer:
            await self.tg.send_message(chat_id, *self.offer_view(user_id, msg_id, offer))

    async def offer_bot(self, chat_id: int, user_id: int, message_id: int, msg_id: int) -> None:
        """💬 Подобрать из ответа бота: 7–8 выражений сразу с темой; результат запоминается."""
        offer = self.offer_load(user_id, msg_id)
        if not offer:
            return
        if not offer["bot_ready"]:
            row = self.db.model_message(msg_id)
            corrs = self.db.corrections_for_msg(msg_id)
            corrected = next((c.get("correct") for c in corrs if c.get("correct")), "")
            data = await self.ask(chat_id, phrases_prompt(row["text"], corrected, corrs, self.cfg.level,
                                                          self.db.topic_names(user_id)),
                                  PHRASES_SCHEMA, PHRASES_HINT, wait="⏳ Подбираю выражения из ответа… (обычно 5–15 с)",
                                  thinking=DICT_THINKING)
            if data is None:
                return
            offer["phrases"] = [_phrase(p) for p in data.get("phrases") or []
                                if isinstance(p, dict) and str(p.get("pl") or "").strip()][:8]
            offer["bot_ready"] = True
            self.offer_store(msg_id, offer)
        await self.tg.edit_message(chat_id, message_id, *self.offer_view(user_id, msg_id, offer))

    async def offer_toggle(self, chat_id: int, user_id: int, message_id: int, msg_id: int, key: str) -> None:
        offer = self.offer_load(user_id, msg_id)
        if not offer:
            return
        key = f"b{key}" if key.isdigit() else key          # старые кнопки — номер выражения
        item = dict(offer_items(offer)).get(key)
        if not item or self.db.dict_has(user_id, item["pl"]):
            return
        offer["sel"] = sorted(set(offer["sel"]) ^ {key})
        self.offer_store(msg_id, offer)
        await self.tg.edit_message(chat_id, message_id, *self.offer_view(user_id, msg_id, offer))

    async def offer_topic(self, chat_id: int, user_id: int, message_id: int, msg_id: int, arg: str | None) -> None:
        """📂 Куда положить: arg None — экран выбора; «0» — 🤖 авто; «-» — назад без изменений; id — тема."""
        offer = self.offer_load(user_id, msg_id)
        if not offer:
            return
        if arg is None:
            await self.tg.edit_message(chat_id, message_id,
                                       *fmt.offer_topic_screen(msg_id, self.db.dict_topics(user_id), offer["topic"]))
            return
        if arg == "0":
            offer["topic"] = ""
        elif arg.isdigit():
            t = self.db.topic(int(arg))
            if t and t["user_id"] == user_id:
                offer["topic"] = t["name"]
        self.offer_store(msg_id, offer)
        await self.tg.edit_message(chat_id, message_id, *self.offer_view(user_id, msg_id, offer))

    async def offer_topic_text(self, chat_id: int, user_id: int, msg_id: int, name: str) -> None:
        """✍️ Новая тема для меню «⭐ В словарь» — текстом; меню приходит заново."""
        offer = self.offer_load(user_id, msg_id)
        name = " ".join(name.split())[:40]
        if not offer or not name:
            return
        offer["topic"] = self.db.topic(self.db.topic_get(user_id, name))["name"]
        self.db.conn.commit()
        self.offer_store(msg_id, offer)
        await self.tg.send_message(chat_id, *self.offer_view(user_id, msg_id, offer))

    async def offer_done(self, chat_id: int, user_id: int, message_id: int, msg_id: int) -> None:
        """✅ Готово: отмеченное — в словарь, каждое в набор своей темы (или все — в выбранную тему)."""
        offer = self.offer_load(user_id, msg_id)
        if not offer:
            return
        items = dict(offer_items(offer))
        picked = [(k, items[k]) for k in offer["sel"] if k in items and not self.db.dict_has(user_id, items[k]["pl"])]
        if not picked:
            await self.tg.edit_message(chat_id, message_id,
                                       "⭐ Ничего не отмечено — словарь не изменился. Весь словарь — /dict",
                                       [[("⭐ Ещё раз из этого ответа", f"d:{msg_id}")]])
            return
        fixed = offer.get("topic") or ""
        if not fixed:
            need = [it for _, it in picked if not it.get("topic")]
            if need:
                topics = await self.classify_topics(chat_id, user_id, need)
                for it, t in zip(need, topics):
                    it["topic"] = t
        added = []
        for k, it in picked:
            if self.db.dict_add(user_id, it["pl"], it.get("translit", ""), it.get("ru", ""),
                                fixed or it.get("topic") or DICT_OTHER, src="err" if k.startswith("e") else "bot"):
                added.append(it["pl"])
        offer["sel"] = []
        self.offer_store(msg_id, offer)
        await self.tg.edit_message(chat_id, message_id, fmt.dict_added(self.dict_rows_for(user_id, added)),
                                   [[("⭐ Ещё из этого ответа", f"d:{msg_id}"), ("📚 Словарь", "dl")]])

    async def classify_topics(self, chat_id: int, user_id: int, items: list[dict], wait: str = "⏳ Раскладываю по темам…",
                              topics: list[str] | None = None) -> list[str]:
        """Темы для слов — одним запросом; не вышло — «Разное» (тему можно сменить в /dict)."""
        data = await self.ask(chat_id, topics_prompt([(it["pl"], it.get("ru", "")) for it in items],
                                                     self.db.topic_names(user_id) if topics is None else topics),
                              TOPICS_SCHEMA, TOPICS_HINT, wait=wait, thinking=DICT_THINKING)
        by_n = {}
        for x in (data or {}).get("items") or []:
            if isinstance(x, dict) and str(x.get("topic") or "").strip():
                by_n[x.get("n")] = str(x["topic"]).strip()
        return [by_n.get(n, DICT_OTHER) for n in range(1, len(items) + 1)]

    def dict_rows_for(self, user_id: int, pls: list[str]) -> list:
        want = {p.lower() for p in pls}
        return [r for r in self.db.dict_items(user_id) if r["pl"].lower() in want]

    async def dict_add_text(self, chat_id: int, user_id: int, raw: str, msg_id: int | None = None) -> None:
        """✍️ Свои слова через запятую: модель переводит / исправляет и даёт тему (или своя тема из меню ⭐)."""
        offer = self.offer_load(user_id, msg_id) if msg_id else None
        fixed = (offer or {}).get("topic") or ""
        data = await self.ask(chat_id, own_phrases_prompt(raw, self.cfg.level, self.db.topic_names(user_id)),
                              PHRASES_SCHEMA, PHRASES_HINT,
                              wait="⏳ Перевожу" + (" …" if fixed else " и раскладываю по темам…"),
                              thinking=DICT_THINKING)
        if data is None:
            return
        added = []
        for p in data.get("phrases") or []:
            if not isinstance(p, dict):
                continue
            p = _phrase(p)
            if p["pl"] and self.db.dict_add(user_id, p["pl"], p["translit"], p["ru"],
                                            fixed or p["topic"] or DICT_OTHER, src="own"):
                added.append(p["pl"])
        if added:
            await self.tg.send_message(chat_id, fmt.dict_added(self.dict_rows_for(user_id, added)),
                                       [[("📚 Словарь", "dl")]])
        else:
            await self.tg.send_message(chat_id, "Ничего нового — эти выражения уже в словаре. Весь словарь — /dict")

    # ---------- 📚 /dict: все слова по темам, смена темы ----------

    def dict_has_other(self, user_id: int) -> bool:
        return any(t["name"] == DICT_OTHER for t in self.db.dict_topics(user_id))

    async def dict_show(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        items, topics = self.db.dict_items(user_id), self.db.dict_topics(user_id)
        text = fmt.dict_message(items, topics)
        buttons = fmt.dict_buttons(topics, has_other=self.dict_has_other(user_id))
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    async def dict_relayout(self, chat_id: int, user_id: int) -> None:
        """🤖 Разовая раскладка: слова темы «Разное» модель раскидывает по темам; слово переезжает в набор своей
        темы вместе с прогрессом. Что модель оставила в «Разном» — остаётся там (можно перенести вручную)."""
        other = self.db.topic_get(user_id, DICT_OTHER)
        items = self.db.topic_items(user_id, other)
        if not items:
            await self.tg.send_message(chat_id, "В «Разном» слов нет — раскладывать нечего.", [[("📚 Словарь", "dl")]])
            return
        moved, topics = 0, [t for t in self.db.topic_names(user_id) if t != DICT_OTHER]
        for i in range(0, len(items), RELAYOUT_CHUNK):
            chunk = items[i:i + RELAYOUT_CHUNK]
            names = await self.classify_topics(
                chat_id, user_id, [{"pl": r["pl"], "ru": r["ru"]} for r in chunk], topics=topics,
                wait=f"⏳ Раскладываю «Разное» по темам: {i + 1}–{i + len(chunk)} из {len(items)}…")
            for r, name in zip(chunk, names):
                if name.strip().lower() != DICT_OTHER.lower():
                    if self.db.dict_move(user_id, r["id"], name, commit=False):
                        moved += 1
                    if name not in topics:
                        topics.append(name)
            self.db.conn.commit()
        left = len(self.db.topic_items(user_id, other))
        tops = [t for t in self.db.dict_topics(user_id)]
        await self.tg.send_message(
            chat_id, f"🤖 Разложено по темам: {moved} из {len(items)}." + (f" В «Разном» осталось: {left}." if left else "")
            + "\n\n" + "\n".join(f"🗂 {escape(t['name'])} — {t['n']}" for t in tops)
            + "\n\n<i>Прогресс слов сохранён. Не согласен с темой — /dict → тема → слово → 📂.</i>",
            [[("📚 Словарь", "dl")]])

    async def dict_callback(self, chat_id: int, user_id: int, message_id: int, data: str) -> None:
        """dl — список; dt:<тема> — тема и её наборы; dw:<слово> — слово (сменить тему / удалить);
        dm:<слово>:<тема> — перенести; dz:<слово> — новая тема текстом; dr / dR:<слово> — удалить (с подтверждением);
        dy:<набор> — упражнение по набору; dS — 🤖 разложить «Разное» по темам."""
        cmd, _, arg = data.partition(":")
        if cmd == "dl":
            await self.dict_show(chat_id, user_id, message_id)
        elif cmd == "dS":
            await self.tg.edit_markup(chat_id, message_id)
            await self.dict_relayout(chat_id, user_id)
        elif cmd == "dt":
            t = self.db.topic(int(arg))
            if not t or t["user_id"] != user_id:
                return
            await self.tg.edit_message(chat_id, message_id,
                                       *fmt.dict_topic_screen(t, self.db.topic_items(user_id, t["id"]),
                                                              self.db.dict_sets(user_id, t["id"])))
        elif cmd in ("dw", "dr"):
            it = self.db.dict_item(int(arg))
            if not it or it["user_id"] != user_id:
                return
            screen = (fmt.dict_word_screen(it, self.db.dict_topics(user_id)) if cmd == "dw"
                      else fmt.dict_delete_screen(it))
            await self.tg.edit_message(chat_id, message_id, *screen)
        elif cmd == "dm":
            item_id, topic_id = (int(x) for x in arg.split(":"))
            t = self.db.topic(topic_id)
            if t and t["user_id"] == user_id:
                await self.dict_moved(chat_id, user_id, message_id, item_id, t["name"])
        elif cmd == "dz":
            it = self.db.dict_item(int(arg))
            if it and it["user_id"] == user_id:
                await self.show(chat_id, user_id, {"step": "dict_topic_new", "item": it["id"]},
                                f"✍️ Напиши новую тему для «{escape(it['pl'])}» — по-русски, 1–3 слова. "
                                "Так можно завести свой набор и переносить туда слова из разных тем.", prev=None)
        elif cmd == "dR":
            it = self.db.dict_item(int(arg))
            if it and it["user_id"] == user_id:
                self.db.dict_remove_id(user_id, it["id"])
                await self.tg.edit_message(chat_id, message_id, f"🗑 «{escape(it['pl'])}» убрано из словаря и набора.",
                                           [[("📚 Словарь", "dl")]])
        elif cmd == "dy":
            st = self.db.get_set(int(arg))
            if st and st["user_id"] == user_id:
                await self.ex_from_set(chat_id, user_id, st["id"])

    async def dict_moved(self, chat_id: int, user_id: int, message_id: int | None, item_id: int, topic: str) -> None:
        title = self.db.dict_move(user_id, item_id, topic)
        it = self.db.dict_item(item_id)
        if not title or not it:
            return
        text = f"📂 «{escape(it['pl'])}» → тема «{escape(it['topic'])}», набор {escape(title)}. Прогресс сохранён."
        buttons = [[(f"🗂 {it['topic']}"[:40], f"dt:{it['topic_id']}"), ("📚 Словарь", "dl")]]
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    def ignore_block(self, user_id: int) -> str:
        rows = self.db.ignores(user_id)[-IGNORE_PROMPT:]
        if not rows:
            return ""
        return ("ИСКЛЮЧЕНИЯ: ученик подтвердил, что эти слова говорит и пишет правильно, а раньше их ошибочно "
                "записали или исправили. Не записывай их в искажённом виде и не считай ошибкой:\n"
                + "\n".join(f"- он говорит «{r['correct']}» (не «{r['original']}»)" for r in rows))

    def drop_ignored(self, user_id: int, corrections: list[dict]) -> list[dict]:
        """Убирает исправления, которые ученик раньше отметил «🙅 Не ошибка» (та же пара «было → стало»)."""
        pairs = {(exercises.norm(r["original"]), exercises.norm(r["correct"])) for r in self.db.ignores(user_id)}
        return [c for c in corrections
                if (exercises.norm(c.get("original", "")), exercises.norm(c.get("correct", ""))) not in pairs]

    async def dispute_show(self, chat_id: int, user_id: int, msg_id: int, message_id: int | None = None) -> None:
        corrs = self.db.corrections_for_msg_all(msg_id)
        if not corrs or corrs[0]["_user"] != user_id:
            await self.tg.send_message(chat_id, "В этом сообщении нет исправлений.")
            return
        text, buttons = fmt.dispute_screen(corrs, msg_id)
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    def dispute_toggle(self, user_id: int, c: dict) -> None:
        """Оспорить исправление разговора или вернуть его. Серия слов набора, сброшенная этим сообщением,
        восстанавливается (только для сообщений, где отметки слов привязаны к сообщению)."""
        on = not c["_disputed"]
        pair = (c.get("original", ""), c.get("correct", ""))
        if on:
            restored = []
            if c.get("_msg"):
                others_left = [x for x in self.db.corrections_for_msg_all(c["_msg"])
                               if not x["_disputed"] and x["_id"] != c["_id"]]
                keys = {exercises.norm(pair[0]), exercises.norm(pair[1])}
                for u in self.db.uses_for_msg(c["_msg"]):
                    if not u["correct"] and (exercises.norm(u["form"]) in keys or not others_left):
                        self.db.set_use_correct(u["id"], True)
                        restored.append(u["id"])
            self.db.set_disputed(c["_id"], True, {**c, "_restored": restored})
            self.db.ignore_add(user_id, *pair)
        else:
            for uid in c.get("_restored") or []:
                self.db.set_use_correct(uid, False)
            self.db.set_disputed(c["_id"], False, {**c, "_restored": []})
            self.db.ignore_remove(user_id, pair=pair)

    async def dispute_callback(self, chat_id: int, user_id: int, message_id: int, arg: str) -> None:
        if arg.startswith("t:"):
            c = self.db.correction(int(arg[2:]))
            if not c or c["_user"] != user_id or not c.get("_msg"):
                return
            self.dispute_toggle(user_id, c)
            await self.dispute_show(chat_id, user_id, c["_msg"], message_id)
        elif arg.startswith("ok:"):
            corrs = [c for c in self.db.corrections_for_msg_all(int(arg[3:])) if c["_user"] == user_id]
            n = sum(c["_disputed"] for c in corrs)
            await self.tg.edit_message(chat_id, message_id, fmt.dispute_done(n))
        elif arg.isdigit():
            await self.dispute_show(chat_id, user_id, int(arg))

    async def ignores_show(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        text, buttons = fmt.ignores_screen(self.db.ignores(user_id))
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)
