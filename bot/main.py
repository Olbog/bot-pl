"""Точка входа: обработка сообщений, кнопок и цикл long polling."""
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from . import config as cfg_mod
from . import fmt, rules, training
from .db import DB
from .gemini import Gemini, GeminiError, GeminiExhausted, GeminiOverloaded
from .prompt import (CLASSIFY_HINT, CLASSIFY_SCHEMA, PHRASES_HINT, PHRASES_SCHEMA, RULES_HINT, RULES_SCHEMA,
                     WORDS_HINT, WORDS_SCHEMA, classify_prompt, own_phrases_prompt, own_words_prompt,
                     phrases_prompt, rule_question_prompt, rules_by_name_prompt, rules_for_errors_prompt,
                     topic_words_prompt)
from .telegram import Telegram
from .tts import synthesize

log = logging.getLogger("bot-pl")

START_TEXT = "Zaczynajmy!"  # реплика ученика, с которой бот открывает тренировку набора
RULE_FACTOR = 2               # критерий «освоено» для правил строже, чем для слов, во столько раз
AUTOSAVE_HOUR = 4             # автосохранение: 04:00 по Мск…
AUTOSAVE_WEEKDAY = 0          # …раз в неделю, в понедельник (0 = пн, 6 = вс)
WEEKDAYS = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
TEXT_STEPS = ("topic", "own", "add", "dict_own", "rule")


class App:
    def __init__(self, cfg: cfg_mod.Config, tg: Telegram, gemini: Gemini, db: DB, tts=synthesize,
                 clock=time.time):
        self.cfg, self.tg, self.gemini, self.db, self.tts, self.clock = cfg, tg, gemini, db, tts, clock
        self.db.clock = clock  # одно время для записи и выборок
        self.criteria = training.Criteria(cfg.master_streak, cfg.master_forms, cfg.master_days)
        self.rule_criteria = training.scaled(self.criteria, RULE_FACTOR)
        self.export_dir = Path(cfg.db_path).parent / "exports"

    def criteria_for(self, item) -> training.Criteria:
        return self.rule_criteria if training.kind_of(item) == "rule" else self.criteria

    def today(self) -> str:
        return cfg_mod.local_dt(self.clock()).strftime("%Y-%m-%d")

    # ---------- сообщения ----------

    async def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id")
        if user_id not in self.cfg.allowed_ids:
            await self.tg.send_message(chat_id, f"Нет доступа. Твой Telegram ID: <code>{user_id}</code>")
            return

        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            self.db.set_pending(user_id, None)  # любая команда отменяет незавершённый шаг
            parts = text.split(maxsplit=1)
            await self.command(chat_id, user_id, parts[0].split("@")[0].lower(), parts[1] if len(parts) > 1 else "")
            return

        pending = self.db.get_state(user_id)["pending"]
        if pending and text and pending.get("step") in TEXT_STEPS:
            await self.pending_text(chat_id, user_id, pending, text)
            return

        voice = msg.get("voice") or msg.get("audio")
        if not text and not voice:
            await self.tg.send_message(chat_id, "Пришли текст или голосовое.")
            return
        await self.converse(chat_id, user_id, text=text or None, voice=voice)

    async def converse(self, chat_id: int, user_id: int, text: str | None, voice: dict | None = None) -> None:
        session = self.db.current_session(user_id)
        history = self.db.history(session, self.cfg.history_limit)
        extra, targets = self.training_context(user_id)
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
        for c in turn.corrections:
            c["rule"] = rules.normalize(c.get("rule"))
        msg_id = self.db.save_turn(session, user_id, user_text, turn.reply_pl, turn.corrections, turn.new_words)
        uses, newly_mastered = self.apply_target_uses(user_id, targets, turn.target_uses)
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

    async def report_gemini_error(self, chat_id: int, e: GeminiError) -> None:
        log.error("Gemini: %s", e)
        if isinstance(e, GeminiExhausted):
            reset = cfg_mod.local_dt(e.reset_at).strftime("%H:%M")
            if e.text_still_ok:
                note = f"🎙 Голосовые до {reset} (Мск) недоступны — дневной лимит исчерпан. Текстом пока можно писать."
            else:
                note = f"😴 Дневной лимит всех моделей исчерпан. Сброс около {reset} (Мск)."
        elif isinstance(e, GeminiOverloaded):
            note = "⏳ Gemini сейчас перегружен. Попробуй через минуту — сообщение можно просто переслать ещё раз."
        else:
            note = f"⚠️ Ошибка Gemini, попробуй ещё раз.\n<code>{escape(str(e)[:300])}</code>"
        await self.tg.send_message(chat_id, note)

    # ---------- тренировка ----------

    def set_rows(self, set_id: int) -> list[tuple]:
        return [(w, training.stats(self.db.word_uses(w["id"]))) for w in self.db.set_words(set_id)]

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
        return training.review_block(due), {w["pl"].lower(): w for w in due}

    def apply_target_uses(self, user_id: int, targets: dict, uses: list[dict]) -> tuple[list, list]:
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
            self.db.add_use(w["id"], user_id, form, ok, day)
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
            tail = f" Оставшиеся {left} перейдут в новый набор." if left else ""
            await self.tg.send_message(
                chat_id, f"🏆 Набор «{escape(active['title'])}» почти освоен: {done} из {len(words)}.{tail}\n"
                         "Берём следующий?",
                [[("➕ Набор по теме", "s:topic"), ("✍️ Свои слова", "s:own")],
                 [("🔁 Ещё потренировать этот", "s:show")]])

    async def show_set(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        mode = self.db.get_state(user_id)["mode"]
        active = self.db.active_set(user_id)
        if not active:
            text = ("📚 Набора пока нет.\n\nСоставь его по теме (я подберу 10 слов), из своих слов, из своих ошибок "
                    "или из словаря ⭐ — и я буду строить разговор так, чтобы ты говорил это как можно чаще "
                    "и в разных формах.")
        else:
            text = fmt.set_progress(active["title"], self.set_rows(active["id"]), self.criteria, mode,
                                    self.rule_criteria)
        buttons = fmt.set_buttons(mode, bool(active))
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    def carry_words(self, user_id: int) -> list:
        active = self.db.active_set(user_id)
        return [w for w in self.db.set_words(active["id"]) if w["mastered_at"] is None] if active else []

    async def build_preview(self, chat_id: int, user_id: int, pending: dict, prompt: str) -> None:
        await self.tg.send_action(chat_id, "typing")
        try:
            data = await self.gemini.ask_json(prompt, WORDS_SCHEMA, WORDS_HINT)
        except GeminiError as e:
            await self.report_gemini_error(chat_id, e)
            return
        new = [w for w in data.get("words") or [] if isinstance(w, dict) and str(w.get("pl", "")).strip()]
        known = {w["pl"].lower() for w in pending.get("words", [])} | {w["pl"].lower() for w in self.carry_words(user_id)}
        new = [w for w in new if w["pl"].strip().lower() not in known]
        if not new and not pending.get("words"):
            await self.tg.send_message(chat_id, "Не получилось составить слова, попробуй ещё раз или другую тему.")
            return
        pending = {**pending, "step": "preview", "words": pending.get("words", []) + new}
        pending.setdefault("title", str(data.get("title") or "Свои слова"))
        pending.setdefault("off", [])
        self.db.set_pending(user_id, pending)
        await self.tg.send_message(chat_id, fmt.preview_message(pending, self.carry_words(user_id)),
                                   fmt.preview_buttons(pending))

    async def pending_text(self, chat_id: int, user_id: int, pending: dict, text: str) -> None:
        step = pending["step"]
        carry = self.carry_words(user_id)
        if step == "topic":
            n = max(3, self.cfg.set_size - len(carry))
            exclude = self.db.known_words(user_id)[-80:]
            await self.build_preview(chat_id, user_id, {"topic": text},
                                     topic_words_prompt(text, n, self.cfg.level, exclude))
        elif step == "dict_own":
            self.db.set_pending(user_id, None)
            await self.dict_add_text(chat_id, user_id, text)
        elif step == "rule":
            self.db.set_pending(user_id, None)
            await self.rule_question(chat_id, text)
        else:  # own / add
            base = pending if step == "add" else {}
            await self.build_preview(chat_id, user_id, {k: v for k, v in base.items() if k != "step"},
                                     own_words_prompt(text, self.cfg.level))

    # ---------- правила ----------

    async def ask(self, chat_id: int, prompt: str, schema: dict, hint: str) -> dict | None:
        await self.tg.send_action(chat_id, "typing")
        try:
            return await self.gemini.ask_json(prompt, schema, hint)
        except GeminiError as e:
            await self.report_gemini_error(chat_id, e)
            return None

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

    # ---------- словарь ⭐ ----------

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

    async def preview_from_dict(self, chat_id: int, user_id: int) -> None:
        carry = self.carry_words(user_id)
        n = max(3, self.cfg.set_size - len(carry))
        items = self.db.dict_items(user_id, only_unused=True)[:n]
        if not items:
            await self.tg.send_message(chat_id, "⭐ В словаре нет новых выражений для набора. "
                                                 "Сохраняй их кнопкой «⭐ В словарь» под ответами или /dict add.")
            return
        pending = {"step": "preview", "title": "Из словаря", "off": [], "dict_ids": [i["id"] for i in items],
                   "words": [{"pl": i["pl"], "translit": i["translit"], "ru": i["ru"], "pos": "выраж",
                              "kind": "phrase"} for i in items]}
        self.db.set_pending(user_id, pending)
        await self.tg.send_message(chat_id, fmt.preview_message(pending, carry), fmt.preview_buttons(pending))

    # ---------- итоги и выгрузки ----------

    def period_data(self, user_id: int, period: str) -> tuple[int, list, list, list]:
        """(реплик, ошибки, слова по-русски, словарь) за период h/d/w/a/s."""
        now = self.clock()
        if period == "s":
            session = self.db.current_session(user_id)
            first = self.db.conn.execute("SELECT MIN(created_at) FROM messages WHERE session_id=?",
                                         (session,)).fetchone()[0] or now
            return (self.db.message_count(session), self.db.corrections_session(session),
                    self.db.words_session(session), list(self.db.dict_since(user_id, first)))
        span = fmt.PERIODS[period][1]
        since = now - span if span else 0
        return (self.db.user_turns_since(user_id, since), self.db.corrections_since(user_id, since),
                self.db.words_since(user_id, since), list(self.db.dict_since(user_id, since)))

    async def show_itog(self, chat_id: int, user_id: int, period: str) -> None:
        turns, corrs, words, dct = self.period_data(user_id, period)
        await self.tg.send_message(chat_id, fmt.itog_message(period, turns, corrs, words, len(dct)),
                                   fmt.itog_buttons(period, bool(corrs)))

    def export_bytes(self, user_id: int, period: str) -> tuple[str, bytes]:
        turns, corrs, words, dct = self.period_data(user_id, period)
        if period == "a":
            dct = list(self.db.dict_items(user_id))
        stamp = cfg_mod.local_dt(self.clock()).strftime("%Y-%m-%d_%H%M")
        title = f"Ошибки и слова за {fmt.PERIODS[period][0]} — {stamp.replace('_', ' ')}"
        return f"bot-pl_{period}_{stamp}.txt", fmt.export_text(title, corrs, words, dct).encode("utf-8")

    async def send_export(self, chat_id: int, user_id: int, period: str) -> None:
        if period == "d_":  # только словарь
            items = list(self.db.dict_items(user_id))
            text = "Словарь ⭐\n==========\n\n" + "\n".join(
                f"{i['pl']} [{i['translit']}] — {i['ru']}" for i in items) + "\n"
            await self.tg.send_document(chat_id, "bot-pl_dict.txt", text.encode("utf-8"), "⭐ Словарь")
            return
        name, data = self.export_bytes(user_id, period)
        await self.tg.send_document(chat_id, name, data, f"📄 Итог за {fmt.PERIODS[period][0]}")

    async def autosave(self, user_id: int) -> None:
        """Раз в неделю (пн, 04:00 Мск): полная выгрузка на сервер и в чат."""
        name, data = self.export_bytes(user_id, "a")
        self.export_dir.mkdir(parents=True, exist_ok=True)
        day = cfg_mod.local_dt(self.clock()).strftime("%Y-%m-%d")
        (self.export_dir / f"{user_id}_{day}.txt").write_bytes(data)
        await self.tg.send_document(user_id, name, data, "🗂 Еженедельное автосохранение: все ошибки, слова и словарь")

    @staticmethod
    def autosave_due(now) -> bool:
        return now.weekday() == AUTOSAVE_WEEKDAY and now.hour == AUTOSAVE_HOUR

    async def scheduler(self) -> None:
        last_day = None
        while True:
            try:
                now = cfg_mod.local_dt(self.clock())
                day = now.strftime("%Y-%m-%d")
                if self.autosave_due(now) and last_day != day:
                    last_day = day
                    for uid in self.cfg.allowed_ids:
                        try:
                            await self.autosave(uid)
                        except Exception:
                            log.exception("Автосохранение для %s не удалось", uid)
            except Exception:
                log.exception("Планировщик")
            await asyncio.sleep(60)

    async def classify_old_errors(self) -> None:
        """Раскладывает по каталогу правил ошибки без правила (старые). Пачками по 40."""
        todo = self.db.corrections_unsorted()
        if not todo:
            return
        log.info("Раскладываю по правилам старые ошибки: %d", len(todo))
        for start in range(0, len(todo), 40):
            chunk = todo[start:start + 40]
            items = [(i + 1, c) for i, c in enumerate(chunk)]
            try:
                data = await self.gemini.ask_json(classify_prompt(items), CLASSIFY_SCHEMA, CLASSIFY_HINT)
            except GeminiError as e:
                log.warning("Раскладка ошибок отложена до следующего запуска: %s", e)
                return
            by_num = {int(x.get("id", 0)): x.get("rule") for x in data.get("items") or [] if isinstance(x, dict)}
            for num, c in items:
                self.db.set_correction_rule(c["_id"], rules.normalize(by_num.get(num)))
        log.info("Старые ошибки разложены по правилам")

    # ---------- набор из ошибок ----------

    def errsel_rules(self, user_id: int, period: str) -> list[dict]:
        _, corrs, _, _ = self.period_data(user_id, period)
        out = []
        for rule, n, ex in rules.group(corrs)[:8]:
            if rule in (rules.UNSORTED,):
                continue
            examples = "; ".join(f"{c.get('original', '')} → {c.get('correct', '')}" for c in ex[:4])
            out.append({"rule": rule, "n": n, "examples": examples})
        return out

    async def errsel_show(self, chat_id: int, user_id: int, pending: dict, message_id: int | None = None) -> None:
        self.db.set_pending(user_id, pending)
        text, buttons = fmt.errsel_message(pending), fmt.errsel_buttons(pending)
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    async def errsel_action(self, chat_id: int, user_id: int, message_id: int, pending: dict | None,
                            action: str) -> None:
        if action == "start" or action.startswith("p:"):
            period = action[2:] if action.startswith("p:") else "d"
            rs = self.errsel_rules(user_id, period)
            p = {"step": "errsel", "period": period, "rules": rs, "on": list(range(min(3, len(rs)))),
                 "rule_first": (pending or {}).get("rule_first", True)}
            await self.errsel_show(chat_id, user_id, p, message_id if action.startswith("p:") and
                                   pending and pending.get("step") == "errsel" else None)
            return
        if not pending or pending.get("step") != "errsel":
            await self.tg.edit_message(chat_id, message_id, "Этот выбор уже неактуален. /set → «🧩 Из ошибок»")
            return
        if action.startswith("t:"):
            on = set(pending.get("on", [])) ^ {int(action[2:])}
            await self.errsel_show(chat_id, user_id, {**pending, "on": sorted(on)}, message_id)
        elif action == "rand":
            k = min(3, len(pending["rules"]))
            await self.errsel_show(chat_id, user_id,
                                   {**pending, "on": sorted(random.sample(range(len(pending["rules"])), k))},
                                   message_id)
        elif action == "rule":
            await self.errsel_show(chat_id, user_id, {**pending, "rule_first": not pending.get("rule_first", True)},
                                   message_id)
        elif action == "go":
            chosen = [pending["rules"][i] for i in pending.get("on", []) if i < len(pending["rules"])]
            if not chosen:
                await self.tg.send_message(chat_id, "Отметь хотя бы одно правило.")
                return
            carry = self.carry_words(user_id)
            items = [{"pl": r["rule"], "translit": "", "ru": r["examples"], "pos": "правило", "kind": "rule"}
                     for r in chosen]
            title = "Ошибки: " + ", ".join(r["rule"].split(" (")[0] for r in chosen)[:60]
            self.db.create_set(user_id, title, items, [w["id"] for w in carry])
            self.db.set_pending(user_id, None)
            self.db.set_mode(user_id, "set")
            self.db.new_session(user_id)
            await self.tg.edit_message(chat_id, message_id, fmt.errsel_message(pending).split("\n\n")[0]
                                       + "\n\n✅ <b>Набор сохранён. Начинаем!</b>\n"
                                       + "\n".join(f"📐 {escape(r['rule'])}" for r in chosen))
            if pending.get("rule_first", True):
                data = await self.ask(chat_id, rules_by_name_prompt(
                    [r["rule"] for r in chosen], {r["rule"]: r["examples"].split("; ") for r in chosen},
                    self.cfg.level), RULES_SCHEMA, RULES_HINT)
                if data is not None:
                    await self.tg.send_message(chat_id, fmt.rules_message(data))
            await self.converse(chat_id, user_id, text=START_TEXT)

    # ---------- кнопки ----------

    async def on_callback(self, cq: dict) -> None:
        user_id = cq.get("from", {}).get("id")
        msg = cq.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")
        data = cq.get("data") or ""
        if user_id not in self.cfg.allowed_ids or chat_id is None:
            await self.tg.answer_callback(cq["id"])
            return
        await self.tg.answer_callback(cq["id"])
        state = self.db.get_state(user_id)
        pending = state["pending"]

        if data == "s:show":
            await self.show_set(chat_id, user_id, message_id)
        elif data == "s:topic":
            self.db.set_pending(user_id, {"step": "topic"})
            await self.tg.send_message(chat_id, "Напиши тему — по-русски или по-польски "
                                                 "(например: кафе, у врача, выходные). /cancel — отмена.")
        elif data == "s:own":
            self.db.set_pending(user_id, {"step": "own"})
            await self.tg.send_message(chat_id, "Пришли слова через запятую или столбиком — по-польски или "
                                                 "по-русски, я переведу. /cancel — отмена.")
        elif data.startswith("mode:"):
            mode = data.split(":", 1)[1]
            if mode == "set" and not self.db.active_set(user_id):
                await self.tg.send_message(chat_id, "Сначала составь набор: /set")
                return
            self.db.set_mode(user_id, mode)
            self.db.new_session(user_id)
            await self.show_set(chat_id, user_id, message_id)
            if mode == "set":
                await self.converse(chat_id, user_id, text=START_TEXT)
            else:
                await self.tg.send_message(chat_id, "🏁 Свободный разговор. О чём поговорим? "
                                                     "Можешь просто начать по-польски.")
        elif data == "m:list":
            active = self.db.active_set(user_id)
            if active:
                await self.tg.edit_message(chat_id, message_id, fmt.mastered_message(active["title"]),
                                           fmt.mastered_buttons(self.db.set_words(active["id"])))
        elif data.startswith("m:"):
            w = self.db.word(int(data[2:]))
            active = self.db.active_set(user_id)
            if w and w["user_id"] == user_id and active:
                self.db.set_mastered(w["id"], None if w["mastered_at"] is not None else "user")
                await self.tg.edit_message(chat_id, message_id, fmt.mastered_message(active["title"]),
                                           fmt.mastered_buttons(self.db.set_words(active["id"])))
        elif data == "s:dict":
            await self.preview_from_dict(chat_id, user_id)
        elif data.startswith("p:"):
            if data == "p:cancel" and pending and pending.get("step") == "errsel":
                self.db.set_pending(user_id, None)
                await self.tg.edit_message(chat_id, message_id, "✖️ Отменено.")
                return
            await self.preview_action(chat_id, user_id, message_id, pending, data[2:])
        elif data.startswith("e:"):
            await self.errsel_action(chat_id, user_id, message_id, pending, data[2:])
        elif data.startswith("r:"):
            await self.explain_message_rules(chat_id, user_id, int(data[2:]))
        elif data.startswith("d:"):
            await self.offer_phrases(chat_id, user_id, int(data[2:]))
        elif data.startswith("ds:"):
            _, mid, i = data.split(":")
            await self.offer_toggle(chat_id, user_id, message_id, int(mid), int(i))
        elif data.startswith("dx:"):
            offer = self.db.offer_get(int(data[3:]))
            n = len(offer["saved"]) if offer else 0
            await self.tg.edit_message(chat_id, message_id, f"⭐ Сохранено в словарь: {n}. Весь словарь — /dict")
        elif data == "dn:own":
            self.db.set_pending(user_id, {"step": "dict_own"})
            await self.tg.send_message(chat_id, "Пришли выражения через запятую — по-польски или по-русски. "
                                                 "/cancel — отмена.")
        elif data.startswith("i:"):
            code = data[2:]
            if code == "dict":
                await self.send_export(chat_id, user_id, "d_")
            elif code.startswith("f"):
                await self.send_export(chat_id, user_id, code[1:])
            else:
                await self.show_itog(chat_id, user_id, code)

    async def preview_action(self, chat_id: int, user_id: int, message_id: int, pending: dict | None,
                             action: str) -> None:
        if not pending or pending.get("step") not in ("preview", "add"):
            await self.tg.edit_message(chat_id, message_id, "Этот черновик набора уже неактуален. /set")
            return
        carry = self.carry_words(user_id)
        if action == "cancel":
            self.db.set_pending(user_id, None)
            await self.tg.edit_message(chat_id, message_id, "✖️ Создание набора отменено.")
        elif action == "add":
            self.db.set_pending(user_id, {**pending, "step": "add"})
            await self.tg.send_message(chat_id, "Пришли слова, которые добавить (через запятую или столбиком).")
        elif action == "regen":
            n = max(3, self.cfg.set_size - len(carry) - len(pending["words"]) + len(pending.get("off", [])))
            exclude = self.db.known_words(user_id)[-80:] + [w["pl"] for w in pending["words"]]
            kept = [w for i, w in enumerate(pending["words"]) if i not in set(pending.get("off", []))]
            await self.tg.edit_message(chat_id, message_id, "🔄 Подбираю другие слова…")
            await self.build_preview(chat_id, user_id,
                                     {"topic": pending.get("topic"), "title": pending.get("title"), "words": kept},
                                     topic_words_prompt(pending.get("topic") or "", n, self.cfg.level, exclude))
        elif action == "ok":
            off = set(pending.get("off", []))
            words = [w for i, w in enumerate(pending["words"]) if i not in off]
            if not words and not carry:
                await self.tg.send_message(chat_id, "В наборе не осталось слов.")
                return
            self.db.create_set(user_id, pending.get("title") or "Набор", words, [w["id"] for w in carry])
            if pending.get("dict_ids"):
                kept_pl = {w["pl"].lower() for w in words}
                self.db.dict_mark_used([i for i, w in zip(pending["dict_ids"], pending["words"])
                                        if w["pl"].lower() in kept_pl])
            self.db.set_pending(user_id, None)
            self.db.set_mode(user_id, "set")
            self.db.new_session(user_id)
            await self.tg.edit_message(chat_id, message_id, fmt.preview_message(
                {**pending, "words": words, "off": []}, carry).replace(
                "<i>Нажми на слово, чтобы убрать или вернуть его.</i>", "✅ <b>Набор сохранён. Начинаем!</b>"))
            await self.converse(chat_id, user_id, text=START_TEXT)
        elif action.isdigit():
            i = int(action)
            off = set(pending.get("off", []))
            off ^= {i}
            pending = {**pending, "off": sorted(off)}
            self.db.set_pending(user_id, pending)
            await self.tg.edit_message(chat_id, message_id, fmt.preview_message(pending, carry),
                                       fmt.preview_buttons(pending))

    # ---------- команды ----------

    async def command(self, chat_id: int, user_id: int, cmd: str, arg: str = "") -> None:
        if cmd == "/new":
            self.db.new_session(user_id)
            await self.tg.send_message(chat_id, "🆕 Новая тема. Zaczynamy! [за-чы-НА-мы] — Начинаем!")
        elif cmd == "/itog":
            text, buttons = fmt.itog_choice()
            await self.tg.send_message(chat_id, text, buttons)
        elif cmd == "/set":
            await self.show_set(chat_id, user_id)
        elif cmd == "/free":
            self.db.set_mode(user_id, "free")
            self.db.new_session(user_id)
            await self.tg.send_message(chat_id, "🏁 Свободный разговор. О чём поговорим? "
                                                 "Вернуться к набору — /set.")
        elif cmd == "/dict":
            if arg.lower().startswith("add"):
                raw = arg[3:].strip()
                if raw:
                    await self.dict_add_text(chat_id, user_id, raw)
                else:
                    self.db.set_pending(user_id, {"step": "dict_own"})
                    await self.tg.send_message(chat_id, "Пришли выражения через запятую — по-польски или по-русски.")
            else:
                await self.tg.send_message(chat_id, fmt.dict_message(list(self.db.dict_items(user_id))),
                                           fmt.dict_buttons())
        elif cmd == "/rule":
            if arg:
                await self.rule_question(chat_id, arg)
            else:
                self.db.set_pending(user_id, {"step": "rule"})
                await self.tg.send_message(chat_id, "Напиши вопрос о правиле — например: почему do niej, а не do nie?")
        elif cmd == "/export":
            await self.send_export(chat_id, user_id, "a")
        elif cmd == "/cancel":
            await self.tg.send_message(chat_id, "Отменено.")
        else:  # /start, /help и всё остальное
            await self.tg.send_message(chat_id, fmt.HELP)

    # ---------- цикл ----------

    async def run(self) -> None:
        await self.gemini.check_models()
        try:
            await self.tg.set_commands()
        except Exception:
            log.exception("setMyCommands не удался")
        asyncio.create_task(self.classify_old_errors())
        asyncio.create_task(self.scheduler())
        offset = None
        log.info("Бот запущен")
        while True:
            try:
                updates = await self.tg.get_updates(offset)
            except Exception:
                log.exception("getUpdates")
                await asyncio.sleep(5)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    if "message" in upd:
                        await self.handle(upd["message"])
                    elif "callback_query" in upd:
                        await self.on_callback(upd["callback_query"])
                except Exception:
                    log.exception("Необработанная ошибка")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в логи попадёт токен из URL
    cfg = cfg_mod.load()
    app = App(
        cfg,
        Telegram(cfg.telegram_token),
        Gemini(cfg.worker_url, cfg.proxy_token, list(cfg.models), cfg.thinking_level, cfg.level),
        DB(cfg.db_path),
    )
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
