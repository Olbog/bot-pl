"""Разговор: /new (по набору, новый набор, без набора: без темы / своя / случайная тема),
реплика → Gemini → исправления, слова набора, озвучка; вопросы о правилах."""
import random
from html import escape

from .core import rules, training
from .ai.gemini import GeminiError
from .ai.prompt import RULES_HINT, RULES_SCHEMA, rule_question_prompt, rules_for_errors_prompt
from .ui import fmt
from .settings import RANDOM_TOPICS, START_TEXT
from .common import AUTO, log


class ConversationMixin:
    async def converse(self, chat_id: int, user_id: int, text: str | None, voice: dict | None = None) -> None:
        session = self.db.current_session(user_id)
        history = self.db.history(session, self.cfg.history_limit)
        extra, targets = self.training_context(user_id)
        extra = (extra + "\n\n" + self.ignore_block(user_id)).strip()
        await self.tg.send_action(chat_id, "typing")
        try:
            audio = await self.tg.download_file(voice["file_id"]) if voice else None
            turn = await self.gemini.reply(history, text=text, audio=audio, extra_system=extra)
        except GeminiError as e:
            await self.report_gemini_error(chat_id, e)
            return
        except Exception as e:  # сеть, таймаут, Telegram
            log.exception("Ошибка обработки")
            await self.tg.send_message(chat_id, f"⚠️ Не получилось обработать: <code>{escape(str(e)[:300])}</code>")
            return

        user_text = turn.user_text or text or ""
        opening = text == START_TEXT and not voice  # служебное начало тренировки — разбор не показываем
        if opening:
            turn.user_text = turn.corrected_pl = ""
            turn.corrections, turn.new_words = [], []
        turn.corrections = self.drop_ignored(user_id, turn.corrections)
        for c in turn.corrections:
            c["rule"] = rules.normalize(c.get("rule"))
        msg_id = self.db.save_turn(session, user_id, user_text, turn.reply_pl, turn.corrections, turn.new_words)
        uses, newly_mastered = self.apply_target_uses(user_id, targets, turn.target_uses, msg_id)
        await self.tg.send_message(
            chat_id,
            fmt.turn_message(turn, from_voice=bool(voice), show_model=self.cfg.show_model,
                             target_line=fmt.target_line(uses)),
            None if opening else fmt.reply_buttons(msg_id, bool(turn.corrections)))

        if turn.reply_pl:
            await self.tg.send_action(chat_id, "record_voice")
            try:
                ogg = await self.tts(turn.reply_pl, self.cfg.tts_voice, self.cfg.tts_rate)
                await self.tg.send_voice(chat_id, ogg)
            except Exception:
                log.exception("Озвучка не удалась")
                await self.tg.send_message(chat_id, "🔇 Озвучка не получилась, текст выше.")

        for w in newly_mastered:
            await self.tg.send_message(
                chat_id, f"🎉 Освоено: <b>{escape(w['pl'])}</b>"
                         + ("" if training.kind_of(w) == "rule" else f" — {escape(w['ru'])}") + "\n"
                         f"<i>{self.criteria_for(w).streak} раз подряд без ошибок, в разных формах и днях. "
                         f"Теперь в повторении.</i>")
        await self.maybe_suggest_next(chat_id, user_id)

    def training_context(self, user_id: int) -> tuple[str, dict]:
        """Блок инструкции и слова, употребление которых отслеживаем: {lemma_lower: word_row}."""
        mode = self.db.get_state(user_id)["mode"]
        active = self.db.active_set(user_id)
        if mode == "set" and active:
            rows = [(w, st) for w, st in self.set_rows(active["id"]) if w["mastered_at"] is None]
            if rows:
                block = training.set_block(active["title"], rows, self.criteria, self.rule_criteria)
                return block, {w["pl"].lower(): w for w, _ in rows}
        now = self.clock()
        due = [w for w in self.db.mastered_words(user_id) if training.due_for_review(w, now)][:5]
        block = training.review_block(due)
        topic = self.db.get_state(user_id).get("topic") if mode != "set" else None
        if topic:
            block = (f"ТЕМА РАЗГОВОРА: «{topic}». Веди разговор вокруг этой темы: задавай вопросы о ней, "
                     "подкидывай жизненные ситуации; если ученик ушёл в сторону — мягко возвращай.\n\n" + block).strip()
        return block, {w["pl"].lower(): w for w in due}

    def apply_target_uses(self, user_id: int, targets: dict, uses: list[dict],
                          msg_id: int | None = None) -> tuple[list, list]:
        shown, newly = [], []
        day, now = self.today(), self.clock()
        seen: set[tuple] = set()
        for u in uses:
            w = targets.get(str(u.get("lemma", "")).strip().lower())
            form = str(u.get("form", "")).strip()
            ok = bool(u.get("correct"))
            key = (w["id"] if w else None, form.lower())
            if not w or key in seen:
                continue
            seen.add(key)
            self.db.add_use(w["id"], user_id, form, ok, day, msg_id=msg_id)
            if w["unit"]:  # слово из юнита учебника — употребление идёт и в прогресс юнита (как RU→PL)
                self.db.book_record(user_id, w["unit"], w["pl"], "ru", ok)
            shown.append((w["pl"], form, ok))
            if w["mastered_at"] is not None:  # повторение освоенного слова
                if ok:
                    self.db.advance_review(w["id"], now)
                continue
            if ok and training.meets(training.stats(self.db.word_uses(w["id"])), self.criteria_for(w)):
                self.db.set_mastered(w["id"], "auto")
                newly.append(w)
        return shown, newly

    async def maybe_suggest_next(self, chat_id: int, user_id: int) -> None:
        active = self.db.active_set(user_id)
        if not active or active["suggested_next"]:
            return
        words = self.db.set_words(active["id"])
        if not words:
            return
        done = sum(1 for w in words if w["mastered_at"] is not None)
        if done / len(words) >= self.cfg.next_set_ratio:
            self.db.mark_suggested(active["id"])
            left = len(words) - done
            tail = (f" Оставшиеся {left} останутся в наборе — он будет в 📚 архиве, вернёшься к нему, когда захочешь."
                    if left else "")
            await self.tg.send_message(
                chat_id, f"🏆 Набор «{escape(active['title'])}» почти освоен: {done} из {len(words)}.{tail}\n"
                         "Берём следующий?",
                [[("➕ Новый набор", "nw:newset")], [("🔁 Ещё потренировать этот", "s:show")]])

    async def explain_message_rules(self, chat_id: int, user_id: int, msg_id: int) -> None:
        corrs = self.db.corrections_for_msg(msg_id)
        row = self.db.model_message(msg_id)
        if not corrs or not row or row["user_id"] != user_id:
            await self.tg.send_message(chat_id, "В этом сообщении нет исправлений.")
            return
        data = await self.ask(chat_id, rules_for_errors_prompt(corrs, self.cfg.level), RULES_SCHEMA, RULES_HINT)
        if data is not None:
            await self.tg.send_message(chat_id, fmt.rules_message(data))

    async def rule_question(self, chat_id: int, question: str) -> None:
        data = await self.ask(chat_id, rule_question_prompt(question, self.cfg.level), RULES_SCHEMA, RULES_HINT)
        if data is not None:
            await self.tg.send_message(chat_id, fmt.rules_message(data))

    # ---------- /new: о чём говорим ----------

    def status(self, user_id: int) -> str:
        st = self.db.get_state(user_id)
        active = self.db.active_set(user_id)
        words = self.db.set_words(active["id"]) if active else []
        done = sum(1 for w in words if w["mastered_at"] is not None)
        return fmt.status_line(st["mode"], st.get("topic"), active, done, len(words))

    async def new_menu(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        """Выбор нового разговора. Текущий разговор не трогаем, пока не выбрано, — отмена ничего не сбрасывает."""
        active = self.db.active_set(user_id)
        words = self.db.set_words(active["id"]) if active else []
        done = sum(1 for w in words if w["mastered_at"] is not None)
        text, buttons = fmt.new_menu(self.status(user_id), active, done, len(words))
        await self.show(chat_id, user_id, {"step": "new_menu"}, text, buttons, message_id, prev=None)

    async def new_callback(self, chat_id: int, user_id: int, message_id: int, pending: dict | None,
                           arg: str) -> None:
        step = (pending or {}).get("step")
        if arg == "set":
            await self.start_set_conversation(chat_id, user_id, message_id)
        elif arg == "newset":
            await self.show(chat_id, user_id, {"step": "new_src"}, "➕ <b>Новый набор</b> — откуда взять слова?\n"
                            "<i>Текущий набор уйдёт в 📚 архив с прогрессом — вернёшься к нему, когда захочешь.</i>",
                            fmt.set_source_rows(), prev=AUTO if step == "new_menu" else None)
        elif arg == "arch":
            await self.show_archive(chat_id, user_id)
        elif arg == "free":
            await self.show(chat_id, user_id, {"step": "new_free"}, "🏁 <b>Без набора</b> — тема разговора?",
                            fmt.new_free_buttons())
        elif arg == "f:none":
            await self.start_free(chat_id, user_id, None, message_id)
        elif arg == "f:rnd":
            await self.start_free(chat_id, user_id, random.choice(RANDOM_TOPICS), message_id)
        elif arg == "f:own":
            await self.show(chat_id, user_id, {"step": "new_topic"},
                            "Напиши тему — например: «у врача», «мои выходные», «покупка квартиры».")

    async def start_set_conversation(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        active = self.db.active_set(user_id)
        if not active:
            await self.tg.send_message(chat_id, "Набора пока нет — составь его: /new → «➕ Новый набор».")
            return
        self.db.set_pending(user_id, None)
        self.db.set_mode(user_id, "set")
        self.db.set_topic(user_id, None)
        self.db.new_session(user_id)
        if message_id:
            await self.tg.edit_message(chat_id, message_id, f"🎯 Тренируем набор «{escape(active['title'])}».")
        await self.converse(chat_id, user_id, text=START_TEXT)

    async def start_free(self, chat_id: int, user_id: int, topic: str | None, message_id: int | None = None) -> None:
        self.db.set_pending(user_id, None)
        self.db.set_mode(user_id, "free")
        self.db.set_topic(user_id, topic)
        self.db.new_session(user_id)
        note = (f"🗂 Разговор на тему «{escape(topic)}»." if topic
                else "🏁 Разговор без темы. О чём поговорим? Можешь просто начать по-польски.")
        if message_id:
            await self.tg.edit_message(chat_id, message_id, note)
        else:
            await self.tg.send_message(chat_id, note)
        if topic:
            await self.converse(chat_id, user_id, text=START_TEXT)
