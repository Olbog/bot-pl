"""Словарь ⭐ и «🙅 Не ошибка»: выражения из ответов, оспоренные исправления, исключения."""
from html import escape

from .ai.prompt import PHRASES_HINT, PHRASES_SCHEMA, own_phrases_prompt, phrases_prompt
from .ui import fmt
from .exercises import logic as exercises
from .settings import IGNORE_PROMPT


class DictionaryMixin:
    async def offer_phrases(self, chat_id: int, user_id: int, msg_id: int) -> None:
        offer = self.db.offer_get(msg_id)
        if not offer:
            row = self.db.model_message(msg_id)
            if not row or row["user_id"] != user_id:
                return
            corrs = self.db.corrections_for_msg(msg_id)
            corrected = next((c.get("correct") for c in corrs if c.get("correct")), "")
            data = await self.ask(chat_id, phrases_prompt(row["text"], corrected, corrs, self.cfg.level),
                                  PHRASES_SCHEMA, PHRASES_HINT)
            if data is None:
                return
            phrases = [p for p in data.get("phrases") or [] if isinstance(p, dict) and p.get("pl")][:10]
            if not phrases:
                await self.tg.send_message(chat_id, "Не нашёл, что предложить. Добавь своё: /dict add выражение")
                return
            offer = {"user_id": user_id, "phrases": phrases, "saved": []}
            self.db.offer_save(msg_id, user_id, phrases, [])
        await self.tg.send_message(chat_id, fmt.offer_message(offer["phrases"], offer["saved"]),
                                   fmt.offer_buttons(msg_id, offer["phrases"], offer["saved"]))

    async def offer_toggle(self, chat_id: int, user_id: int, message_id: int, msg_id: int, i: int) -> None:
        offer = self.db.offer_get(msg_id)
        if not offer or offer["user_id"] != user_id or i >= len(offer["phrases"]):
            return
        p, saved = offer["phrases"][i], set(offer["saved"])
        if i in saved:
            saved.discard(i)
            self.db.dict_remove(user_id, p["pl"])
        else:
            saved.add(i)
            self.db.dict_add(user_id, p["pl"], p.get("translit", ""), p.get("ru", ""))
        self.db.offer_save(msg_id, user_id, offer["phrases"], sorted(saved))
        await self.tg.edit_message(chat_id, message_id, fmt.offer_message(offer["phrases"], sorted(saved)),
                                   fmt.offer_buttons(msg_id, offer["phrases"], sorted(saved)))

    async def dict_add_text(self, chat_id: int, user_id: int, raw: str) -> None:
        data = await self.ask(chat_id, own_phrases_prompt(raw, self.cfg.level), PHRASES_SCHEMA, PHRASES_HINT)
        if data is None:
            return
        added = [p for p in data.get("phrases") or [] if isinstance(p, dict) and p.get("pl")
                 and self.db.dict_add(user_id, p["pl"], p.get("translit", ""), p.get("ru", ""))]
        if added:
            await self.tg.send_message(chat_id, "⭐ Добавлено в словарь:\n" + "\n".join(
                f"• <b>{escape(p['pl'])}</b> [{escape(p.get('translit', ''))}] — {escape(p.get('ru', ''))}"
                for p in added))
        else:
            await self.tg.send_message(chat_id, "Ничего нового — эти выражения уже в словаре.")

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
