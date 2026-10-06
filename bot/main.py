"""Точка входа: обработка сообщений, кнопок и цикл long polling."""
import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from . import config as cfg_mod
from . import exercises, fmt, rules, textbook, training
from .db import DB
from .gemini import Gemini, GeminiError, GeminiExhausted, GeminiOverloaded
from .prompt import (BOOK_OPTS_HINT, BOOK_OPTS_SCHEMA, book_options_prompt, CHECK_HINT, CHECK_SCHEMA, EX_HINT, EX_SCHEMA, check_prompt, ex_prompt, ex_question_prompt,
                     CLASSIFY_HINT, CLASSIFY_SCHEMA, PHRASES_HINT, PHRASES_SCHEMA, RULES_HINT, RULES_SCHEMA,
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
EX_WEIGHT = 0.1               # вес правильного ответа в упражнении для прогресса «освоено»
EX_REUSE = 3                  # сколько пунктов с прошлыми ошибками/сомнениями добавлять в упражнение
AUTO = object()               # show(): предыдущий шаг определить автоматически
SET_MENU = {"step": "set_menu"}   # «Назад» → меню /set
DICT_MENU = {"step": "dict_menu"}  # «Назад» → словарь /dict
IGNORE_PROMPT = 30            # сколько последних исключений «🙅 Не ошибка» подсказывать модели
EX_QUIZ_BUTTONS = True        # тест: кнопки a / b / c под сообщением упражнения, по строке на пункт
EX_CASES = ["именительный", "винительный", "творительный"]  # падежи, которые уже знаю: слова в упражнениях — только в них
EX_PRESENT_ONLY = True        # упражнения только в настоящем времени (пока ученик знает только его)
EX_EXPLAIN_ALL = True         # объяснять каждый пункт, кроме помеченных «!» (уверен)
EX_TOP_ERRORS = 8             # сколько правил показывать в «Из моих ошибок»
EX_ERR_DAYS = 14              # период «Из моих ошибок» по умолчанию
QUESTION_RE = re.compile(r"^\s*(\d{1,2})\s*[:.)\-]\s*(.+)$", re.S)


class App:
    def __init__(self, cfg: cfg_mod.Config, tg: Telegram, gemini: Gemini, db: DB, tts=synthesize,
                 clock=time.time):
        self.cfg, self.tg, self.gemini, self.db, self.tts, self.clock = cfg, tg, gemini, db, tts, clock
        self.db.clock = clock  # одно время для записи и выборок
        self.criteria = training.Criteria(cfg.master_streak, cfg.master_forms, cfg.master_days)
        self.rule_criteria = training.scaled(self.criteria, RULE_FACTOR)
        self.export_dir = Path(cfg.db_path).parent / "exports"
        self.busy: set[int] = set()  # пользователи, чей запрос сейчас обрабатывается

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
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to and (text or voice):
            replied = self.db.ex_by_tg_msg(user_id, reply_to)
            if replied:
                await self.ex_answer_to(chat_id, user_id, replied, pending, text, voice)
                return
        if pending and str(pending.get("step", "")).startswith("ex_"):
            if await self.ex_input(chat_id, user_id, pending, text, voice, msg.get("date")):
                return
        if not text and not voice:
            await self.tg.send_message(chat_id, "Пришли текст или голосовое.")
            return
        await self.converse(chat_id, user_id, text=text or None, voice=voice)

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
        pending = {k: v for k, v in pending.items() if k not in ("prev", "screen")}
        pending = {**pending, "step": "preview", "words": pending.get("words", []) + new}
        pending.setdefault("title", str(data.get("title") or "Свои слова"))
        pending.setdefault("off", [])
        await self.show(chat_id, user_id, pending, fmt.preview_message(pending, self.carry_words(user_id)),
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

    async def ask(self, chat_id: int, prompt: str, schema: dict, hint: str, wait: str = "⏳ Думаю…",
                  audio: bytes | None = None) -> dict | None:
        """Запрос к Gemini с видимым статусом: сообщение «⏳ …» удаляется, когда ответ готов."""
        note = await self.tg.send_message(chat_id, wait) if wait else None
        await self.tg.send_action(chat_id, "typing")
        try:
            return await self.gemini.ask_json(prompt, schema, hint, audio=audio) if audio else \
                await self.gemini.ask_json(prompt, schema, hint)
        except GeminiError as e:
            await self.report_gemini_error(chat_id, e)
            return None
        finally:
            if note and note.get("message_id"):
                await self.tg.delete_message(chat_id, note["message_id"])

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

    async def preview_from_dict(self, chat_id: int, user_id: int, prev=AUTO) -> None:
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
        await self.show(chat_id, user_id, pending, fmt.preview_message(pending, carry), fmt.preview_buttons(pending),
                        prev=prev)

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

    async def errsel_show(self, chat_id: int, user_id: int, pending: dict, message_id: int | None = None,
                          prev=AUTO) -> None:
        pending = {k: v for k, v in pending.items() if k not in ("prev", "screen")}
        await self.show(chat_id, user_id, pending, fmt.errsel_message(pending), fmt.errsel_buttons(pending),
                        message_id, prev=prev)

    async def errsel_action(self, chat_id: int, user_id: int, message_id: int, pending: dict | None,
                            action: str) -> None:
        if action == "start" or action.startswith("p:"):
            period = action[2:] if action.startswith("p:") else "d"
            rs = self.errsel_rules(user_id, period)
            p = {"step": "errsel", "period": period, "rules": rs, "on": list(range(min(3, len(rs)))),
                 "rule_first": (pending or {}).get("rule_first", True)}
            same = action.startswith("p:") and pending and pending.get("step") == "errsel"
            await self.errsel_show(chat_id, user_id, p, message_id if same else None,
                                   prev=AUTO if same else SET_MENU)
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

    # ---------- 🙅 Не ошибка: оспоренные исправления и исключения ----------

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

    async def ex_dispute(self, chat_id: int, user_id: int, message_id: int, action: str) -> None:
        """Оспорить пункт упражнения: x:dp:<ex> — список, x:dt:<ex>:<n> — переключить, x:dok:<ex> — готово."""
        kind, _, rest = action.partition(":")
        ex_id, _, n = rest.partition(":")
        saved = self.db.ex_get(int(ex_id))
        if not saved or saved["user_id"] != user_id or not saved["results"]:
            return
        results = saved["results"]
        if kind == "dt":
            r = results[int(n) - 1]
            it = saved["items"][r["n"] - 1]
            pair = (r.get("heard") or r.get("user") or "", it.get("answer", ""))
            if exercises.norm(pair[0]) == exercises.norm(pair[1]):  # ответ верный (спорили о рассуждении)
                pair = ("", "")
            on = not r.get("disputed")
            if on and r["final"] != "wrong":
                return
            r["disputed"] = on
            if on:
                r["final_before"], r["final"] = r["final"], "ok"
                self.db.ignore_add(user_id, *pair)
            else:
                r["final"] = r.pop("final_before", "wrong")
                self.db.ignore_remove(user_id, pair=pair)
            for c in self.db.corrections_for_ex(user_id, saved["id"], r["n"]):
                self.db.set_disputed(c["_id"], on)
            self.db.ex_save_results(saved["id"], results)
        if kind == "dok":
            await self.tg.edit_message(chat_id, message_id, fmt.ex_dispute_done(saved))
            return
        text, buttons = fmt.ex_dispute_screen(saved)
        if kind == "dp":
            await self.tg.send_message(chat_id, text, buttons)
        else:
            await self.tg.edit_message(chat_id, message_id, text, buttons)

    async def ignores_show(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        text, buttons = fmt.ignores_screen(self.db.ignores(user_id))
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    # ---------- навигация: ⬅️ Назад / ✖️ Отмена ----------

    async def show(self, chat_id: int, user_id: int, pending: dict, text: str,
                   buttons: list[list[tuple[str, str]]] | None = None, message_id: int | None = None,
                   prev=AUTO) -> None:
        """Показать шаг выбора с рядом «⬅️ Назад / ✖️ Отмена».
        Экран (текст и кнопки) сохраняется в pending — «Назад» показывает его снова с прежним выбором.
        prev: AUTO — предыдущий шаг из базы (тот же шаг — тот же prev), None — первый шаг, dict — явный."""
        if prev is AUTO:
            cur = self.db.get_state(user_id)["pending"]
            if cur and cur.get("step") == pending["step"]:
                prev = cur.get("prev")
            else:
                prev = cur if cur and cur.get("screen") else None
        pending = {**pending, "prev": prev, "screen": [text, buttons or []]}
        self.db.set_pending(user_id, pending)
        rows = (buttons or []) + [fmt.nav_row(pending["step"], bool(prev))]
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, rows)
        else:
            await self.tg.send_message(chat_id, text, rows)

    async def nav(self, chat_id: int, user_id: int, message_id: int, pending: dict | None, action: str) -> str | None:
        """nav:b:<шаг> — назад, nav:c:<шаг> — отмена. Возвращает текст всплывашки, если кнопка устарела."""
        kind, _, step = action.partition(":")
        if not pending or pending.get("step") != step:
            return "Этот выбор уже неактуален"
        if kind == "c":
            self.db.set_pending(user_id, None)
            if step == "ex_answer":
                await self.tg.edit_markup(chat_id, message_id)
                ex_id = (pending.get("ex") or {}).get("ex_id")
                await self.tg.send_message(chat_id, f"⏹ Упражнения закончены, #{ex_id} — без проверки. Ещё — /ex")
            else:
                await self.tg.edit_message(chat_id, message_id, "✖️ Отменено.")
            return None
        prev = pending.get("prev")
        if not prev:
            return "Это первый шаг"
        if prev.get("step") == "set_menu":
            self.db.set_pending(user_id, None)
            await self.show_set(chat_id, user_id, message_id)
        elif prev.get("step") == "dict_menu":
            self.db.set_pending(user_id, None)
            await self.tg.edit_message(chat_id, message_id, fmt.dict_message(list(self.db.dict_items(user_id))),
                                       fmt.dict_buttons())
        else:
            text, buttons = prev["screen"]
            buttons = [[tuple(b) for b in row] for row in buttons]
            await self.show(chat_id, user_id, prev, text, buttons, message_id, prev=prev.get("prev"))
        return None

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
        state = self.db.get_state(user_id)
        pending = state["pending"]
        if data.startswith(("x:a:", "x:go:")):  # интерактивный тест: всплывашка вместо сообщения
            await self.tg.answer_callback(cq["id"], await self.ex_quiz_tap(chat_id, user_id, pending, data))
            return
        if data.startswith("nav:"):
            await self.tg.answer_callback(cq["id"], await self.nav(chat_id, user_id, message_id, pending, data[4:]))
            return
        await self.tg.answer_callback(cq["id"])

        if data == "s:show":
            await self.show_set(chat_id, user_id, message_id)
        elif data == "s:topic":
            await self.show(chat_id, user_id, {"step": "topic"}, "Напиши тему — по-русски или по-польски "
                            "(например: кафе, у врача, выходные).", prev=SET_MENU)
        elif data == "s:own":
            await self.show(chat_id, user_id, {"step": "own"}, "Пришли слова через запятую или столбиком — "
                            "по-польски или по-русски, я переведу.", prev=SET_MENU)
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
            await self.preview_from_dict(chat_id, user_id, prev=SET_MENU)
        elif data.startswith("p:"):
            if data == "p:cancel" and pending and pending.get("step") == "errsel":
                self.db.set_pending(user_id, None)
                await self.tg.edit_message(chat_id, message_id, "✖️ Отменено.")
                return
            await self.preview_action(chat_id, user_id, message_id, pending, data[2:])
        elif data.startswith("x:"):
            await self.ex_callback(chat_id, user_id, message_id, pending, data[2:])
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
            await self.show(chat_id, user_id, {"step": "dict_own"},
                            "Пришли выражения через запятую — по-польски или по-русски.", prev=DICT_MENU)
        elif data.startswith("nd:"):
            await self.dispute_callback(chat_id, user_id, message_id, data[3:])
        elif data == "ig:list":
            await self.ignores_show(chat_id, user_id)
        elif data.startswith("ig:rm:"):
            self.db.ignore_remove(user_id, int(data[6:]))
            await self.ignores_show(chat_id, user_id, message_id)
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
            await self.show(chat_id, user_id, {**pending, "step": "add"},
                            "Пришли слова, которые добавить (через запятую или столбиком).")
        elif action == "regen":
            n = max(3, self.cfg.set_size - len(carry) - len(pending["words"]) + len(pending.get("off", [])))
            exclude = self.db.known_words(user_id)[-80:] + [w["pl"] for w in pending["words"]]
            kept = [w for i, w in enumerate(pending["words"]) if i not in set(pending.get("off", []))]
            await self.tg.edit_message(chat_id, message_id, "🔄 Подбираю другие слова…")
            await self.build_preview(chat_id, user_id,
                                     {"topic": pending.get("topic"), "title": pending.get("title"), "words": kept,
                                      "step": "preview"},
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
            await self.show(chat_id, user_id, pending, fmt.preview_message(pending, carry),
                            fmt.preview_buttons(pending), message_id)

    # ---------- упражнения ----------

    async def ex_menu(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        text, buttons = fmt.ex_menu()
        await self.show(chat_id, user_id, {"step": "ex_menu", "ex": {}}, text, buttons, message_id, prev=None)

    def ex_words_from_set(self, user_id: int) -> list[str]:
        active = self.db.active_set(user_id)
        if not active:
            return []
        words = [w for w in self.db.set_words(active["id"]) if training.kind_of(w) != "rule"]
        words.sort(key=lambda w: w["mastered_at"] is not None)  # сначала неосвоенные
        return [w["pl"] for w in words]

    def ex_top_rules(self, user_id: int, k: int = 3) -> tuple[list[str], dict[str, list[str]]]:
        groups = [g for g in rules.group(self.db.corrections_since(user_id, 0))
                  if g[0] not in (rules.UNSORTED, rules.OTHER)][:k]
        return ([g[0] for g in groups],
                {g[0]: [f"{c.get('original', '')} → {c.get('correct', '')}" for c in g[2]] for g in groups})

    def ex_error_groups(self, user_id: int, days: int | None) -> list[tuple[str, int, list[dict]]]:
        since = self.clock() - days * 86400 if days else 0
        return [g for g in rules.group(self.db.corrections_since(user_id, since))
                if g[0] not in (rules.UNSORTED, rules.OTHER)][:EX_TOP_ERRORS]

    def ex_examples(self, user_id: int, topics: list[str]) -> dict[str, list[str]]:
        """Мои ошибки по выбранным правилам — для короткого правила перед стартом."""
        return {g[0]: [f"{c.get('original', '')} → {c.get('correct', '')}" for c in g[2]]
                for g in rules.group(self.db.corrections_since(user_id, 0)) if g[0] in topics}

    def ex_pick_screen(self, ex: dict) -> tuple[str, list[list[tuple[str, str]]]]:
        if ex.get("src") == "err":
            days = ex.get("days")
            period = f"{days} дней" if days else "всё время"
            return fmt.ex_pick(f"🔁 <b>Твои частые ошибки</b> ({period}), сколько раз ошибался:",
                               ex.get("labels") or [], ex.get("chosen") or [],
                               [(f"📅 {'Всё время' if days else f'{EX_ERR_DAYS} дней'}", "x:p:per")])
        return fmt.ex_pick("🕘 <b>Недавние темы</b> — можно несколько:", ex.get("labels") or [],
                           ex.get("chosen") or [])

    async def ex_show_errors_pick(self, chat_id: int, user_id: int, ex: dict, message_id: int | None = None) -> bool:
        days = ex.get("days", EX_ERR_DAYS)
        groups = self.ex_error_groups(user_id, days)
        if not groups and days:  # за период пусто — берём всё время
            days, groups = None, self.ex_error_groups(user_id, None)
        if not groups:
            await self.tg.send_message(chat_id, "Ошибок с правилами пока нет — выбери другой вариант выше "
                                                 "или сначала поговори с ботом 🙂")
            return False
        ex.update(src="err", days=days, opts=[[g[0]] for g in groups],
                  labels=[f"{fmt.short_rule(g[0])} — {g[1]}" for g in groups], chosen=list(range(min(3, len(groups)))))
        text, buttons = self.ex_pick_screen(ex)
        await self.show(chat_id, user_id, {"step": "ex_pick", "ex": ex}, text, buttons, message_id)
        return True

    async def ex_topics_chosen(self, chat_id: int, user_id: int, ex: dict, topics: list[str]) -> None:
        ex = {k: v for k, v in ex.items() if k not in ("opts", "labels", "chosen", "src", "days")}
        ex["rules"] = list(dict.fromkeys(t for t in topics if t))
        text, buttons = fmt.ex_card_prompt(ex["rules"])
        await self.show(chat_id, user_id, {"step": "ex_card", "ex": ex}, text, buttons)

    async def ex_ask_count(self, chat_id: int, user_id: int, ex: dict, intro: str = "") -> None:
        text, buttons = fmt.ex_count_prompt()
        await self.show(chat_id, user_id, {"step": "ex_count", "ex": ex}, (intro + "\n\n" if intro else "") + text,
                        buttons)

    async def ex_after_words(self, chat_id: int, user_id: int, ex: dict, words: list[str]) -> None:
        words = [w.strip() for w in words if w and w.strip()][:15]
        if not words:
            await self.tg.send_message(chat_id, "Не нашёл слов для упражнения. Выбери другой источник выше.")
            return
        ex = {**ex, "words": words, "fmt": "gap"}
        await self.ex_ask_count(chat_id, user_id, ex, "Слова: " + ", ".join(f"<b>{escape(w)}</b>" for w in words))

    async def ex_callback(self, chat_id: int, user_id: int, message_id: int, pending: dict | None,
                          action: str) -> None:
        ex = dict((pending or {}).get("ex") or {})
        step = (pending or {}).get("step")
        need = {"s:": "ex_src", "f:": "ex_fmt", "n:": "ex_count", "next": "ex_review", "t:": "ex_gtopic",
                "p:": "ex_pick", "c:": "ex_card", "b:u:": "ex_book", "b:m:": "ex_bunit", "b:d:": "ex_bdir"}
        for prefix, want in need.items():
            if action.startswith(prefix) and step != want:
                return  # кнопка из прошлого шага или повторное нажатие — игнорируем
        if action.startswith("r:") and step not in ("ex_rules",):
            return
        if action.startswith(("dp:", "dt:", "dok:")):
            await self.ex_dispute(chat_id, user_id, message_id, action)
        elif action == "menu":
            await self.ex_menu(chat_id, user_id)
        elif action.startswith("k:"):
            kind = action[2:]
            if kind == "rule":  # старая кнопка «Правило / микс правил» — теперь это «Грамматика»
                kind = "grammar"
            ex = {"kind": kind}
            menu_text, menu_buttons = fmt.ex_menu()
            menu = {"step": "ex_menu", "ex": {}, "prev": None, "screen": [menu_text, menu_buttons]}
            if kind in ("words", "voice"):
                await self.show(chat_id, user_id, {"step": "ex_src", "ex": ex},
                                f"{fmt.EX_KINDS[kind]} — откуда взять слова?", fmt.ex_source_buttons(), prev=menu)
            elif kind == "grammar":
                await self.show(chat_id, user_id, {"step": "ex_fmt", "ex": ex}, "🧩 Грамматика — какой формат?",
                                fmt.ex_format_buttons(kind), prev=menu)
            elif kind == "book":
                units = textbook.load_units()
                if not units:
                    await self.tg.send_message(chat_id, "📘 Юнитов пока нет. Пришли скрины юнита в чат с Claude — "
                                                         "он добавит слова, и после обновления бота они появятся здесь.")
                    return
                text, buttons = fmt.book_units_screen(units)
                await self.show(chat_id, user_id, {"step": "ex_book", "ex": ex}, text, buttons, prev=menu)
            elif kind == "errors":
                top, examples = self.ex_top_rules(user_id)
                if not top:
                    await self.tg.send_message(chat_id, "Ошибок с правилами пока нет — сначала поговори с ботом 🙂")
                    return
                ex.update(rules=top, examples=examples)
                await self.show(chat_id, user_id, {"step": "ex_fmt", "ex": ex}, "🔁 Твои самые частые ошибки:\n"
                                + "\n".join(f"• {escape(r)}" for r in top) + "\n\nКакой формат?",
                                fmt.ex_format_buttons(kind), prev=menu)
        elif action.startswith("b:u:"):
            unit = textbook.get_unit(action[4:])
            if unit:
                await self.book_unit_screen(chat_id, user_id, ex, unit)
        elif action.startswith("b:m:"):
            mode = action[4:]
            if mode == "gap":
                await self.ex_ask_count(chat_id, user_id, {**ex, "kind": "book_gap", "fmt": "gap"})
            else:
                await self.show(chat_id, user_id, {"step": "ex_bdir", "ex": {**ex, "mode": mode}},
                                "Направление:", fmt.book_dir_buttons())
        elif action.startswith("b:d:"):
            d = action[4:]
            kind = "book_card" if ex.get("mode") == "card" else "book_test"
            await self.ex_ask_count(chat_id, user_id, {**ex, "kind": kind, "dir": d,
                                                       "fmt": "test" if kind == "book_test" else "gap"})
        elif action.startswith("s:"):
            src = action[2:]
            if src == "set":
                await self.ex_after_words(chat_id, user_id, ex, self.ex_words_from_set(user_id))
            elif src == "dict":
                items = list(self.db.dict_items(user_id))
                items.sort(key=lambda i: i["used_in_set"])
                await self.ex_after_words(chat_id, user_id, ex, [i["pl"] for i in items[:12]])
            elif src == "own":
                await self.show(chat_id, user_id, {"step": "ex_words", "ex": ex},
                                "Пришли слова через запятую — по-польски (лучше) или по-русски.")
            elif src == "topic":
                await self.show(chat_id, user_id, {"step": "ex_topic", "ex": ex},
                                "Напиши тему — подберу 10 слов уровня A1–A2.")
        elif action.startswith("f:"):
            f = action[2:]
            ex.update(fmt="test" if f in ("test", "ctest") else "gap")
            if ex.get("kind") == "grammar":
                text, buttons = fmt.ex_topic_sources()
                await self.show(chat_id, user_id, {"step": "ex_gtopic", "ex": ex}, text, buttons)
            else:
                await self.ex_ask_count(chat_id, user_id, ex)
        elif action.startswith("t:"):
            src = action[2:]
            if src == "err":
                await self.ex_show_errors_pick(chat_id, user_id, ex)
            elif src == "recent":
                recent = self.db.ex_recent_topics(user_id, 6, skip=(rules.OTHER, rules.UNSORTED))
                if not recent:
                    await self.tg.send_message(chat_id, "Недавних тем пока нет — выбери другой вариант выше.")
                    return
                now = self.clock()
                ex.update(src="recent", opts=[t for t, _ in recent], chosen=[],
                          labels=[" / ".join(fmt.short_rule(x) for x in t) + f" — {fmt.ago(ts, now)}"
                                  for t, ts in recent])
                text, buttons = self.ex_pick_screen(ex)
                await self.show(chat_id, user_id, {"step": "ex_pick", "ex": ex}, text, buttons)
            elif src == "cat":
                ex["chosen"] = []
                text, buttons = fmt.ex_rule_picker(rules.CATALOG, [])
                await self.show(chat_id, user_id, {"step": "ex_rules", "ex": ex}, text, buttons)
            elif src == "own":
                await self.show(chat_id, user_id, {"step": "ex_rule_text", "ex": ex},
                                "Напиши тему — например: «творительный падеж» или "
                                "«разница родительного, винительного и творительного».")
            elif src == "mix":
                await self.ex_ask_count(chat_id, user_id, {**ex, "rules": []})
        elif action.startswith("p:"):
            arg = action[2:]
            if arg == "per":
                ex["days"] = None if ex.get("days") else EX_ERR_DAYS
                await self.ex_show_errors_pick(chat_id, user_id, ex, message_id)
            elif arg == "go":
                opts = ex.get("opts") or []
                topics = [t for i in sorted(ex.get("chosen") or []) if i < len(opts) for t in opts[i]]
                if not topics:
                    await self.tg.send_message(chat_id, "Отметь хотя бы одну тему.")
                    return
                await self.ex_topics_chosen(chat_id, user_id, ex, topics)
            elif arg.isdigit():
                ex["chosen"] = sorted(set(ex.get("chosen") or []) ^ {int(arg)})
                text, buttons = self.ex_pick_screen(ex)
                await self.show(chat_id, user_id, {"step": "ex_pick", "ex": ex}, text, buttons, message_id)
        elif action.startswith("c:"):
            if action == "c:rule" and ex.get("rules"):
                data = await self.ask(chat_id, rules_by_name_prompt(ex["rules"], self.ex_examples(user_id, ex["rules"]),
                                                                    self.cfg.level), RULES_SCHEMA, RULES_HINT)
                if data is not None:
                    await self.tg.send_message(chat_id, fmt.rules_message(data))
            await self.ex_ask_count(chat_id, user_id, ex)
        elif action.startswith("r:"):
            arg = action[2:]
            if arg == "own":
                await self.show(chat_id, user_id, {"step": "ex_rule_text", "ex": ex},
                                "Напиши тему — например: «творительный падеж» или "
                                "«разница родительного, винительного и творительного».")
            elif arg == "go":
                chosen = ex.get("chosen") or []
                if not chosen:
                    await self.tg.send_message(chat_id, "Отметь хотя бы одно правило или напиши своё.")
                    return
                await self.ex_topics_chosen(chat_id, user_id, ex, [rules.CATALOG[i] for i in chosen])
            else:
                ex["chosen"] = sorted(set(ex.get("chosen") or []) ^ {int(arg)})
                text, buttons = fmt.ex_rule_picker(rules.CATALOG, ex["chosen"])
                await self.show(chat_id, user_id, {"step": "ex_rules", "ex": ex}, text, buttons, message_id)
        elif action.startswith("n:"):
            await self.ex_start(chat_id, user_id, ex, int(action[2:]))
        elif action == "next":
            if ex.get("left", 0) > 0:
                await self.ex_make(chat_id, user_id, ex)
            else:
                await self.ex_menu(chat_id, user_id)
        elif action == "stop":
            self.db.set_pending(user_id, None)
            await self.tg.send_message(chat_id, "⏹ Упражнения закончены. Ещё — /ex")
        elif action.startswith("rr:"):
            saved = self.db.ex_get(int(action[3:]))
            if not saved or saved["user_id"] != user_id or not saved["results"]:
                return
            wrong = [{"original": r.get("heard") or r.get("user"), "correct": saved["items"][r["n"] - 1].get("answer"),
                      "rule": saved["items"][r["n"] - 1].get("rule")}
                     for r in saved["results"] if r["final"] in ("wrong", "unsure")]
            if not wrong:
                await self.tg.send_message(chat_id, "Ошибок нет — объяснять нечего 👍")
                return
            data = await self.ask(chat_id, rules_for_errors_prompt(wrong, self.cfg.level), RULES_SCHEMA, RULES_HINT)
            if data is not None:
                await self.tg.send_message(chat_id, fmt.rules_message(data))

    async def ex_answer_to(self, chat_id: int, user_id: int, saved: dict, pending: dict | None, text: str,
                           voice: dict | None) -> None:
        """Ответ через «Ответить» на конкретное упражнение."""
        if saved["results"]:
            await self.tg.send_message(chat_id, f"Упражнение #{saved['id']} уже проверено. "
                                                 "Вопрос по пункту — «5: почему…» обычным сообщением.")
            return
        ex = dict((pending or {}).get("ex") or {})
        if ex.get("ex_id") != saved["id"]:
            ex = {k: v for k, v in ex.items() if k != "quiz"}
            ex["ex_id"] = saved["id"]
        elif ex.get("quiz") and text:  # «Ответить» на шапку интерактивного теста — как обычный текст
            if await self.ex_input(chat_id, user_id, {"step": "ex_answer", "ex": ex}, text, None):
                return
        await self.ex_check(chat_id, user_id, ex, text, voice)

    async def ex_input(self, chat_id: int, user_id: int, pending: dict, text: str, voice: dict | None,
                       sent_at: int | None = None) -> bool:
        """Текст/голос во время упражнений. True — обработано здесь."""
        step, ex = pending["step"], dict(pending.get("ex") or {})
        if step == "ex_words" and text:
            await self.ex_after_words(chat_id, user_id, ex, re.split(r"[,;\n]", text))
            return True
        if step == "ex_topic" and text:
            data = await self.ask(chat_id, topic_words_prompt(text, 10, self.cfg.level, []), WORDS_SCHEMA, WORDS_HINT)
            if data is not None:
                await self.ex_after_words(chat_id, user_id, ex, [w.get("pl", "") for w in data.get("words") or []
                                                                 if isinstance(w, dict)])
            return True
        if step == "ex_rule_text" and text:
            own = text.strip()
            known = rules.normalize(own)  # совпало с каталогом — берём его название, иначе тема как написана
            await self.ex_topics_chosen(chat_id, user_id, ex, [known if known != rules.OTHER else own])
            return True
        if step == "ex_count" and text and text.strip().isdigit():
            await self.ex_start(chat_id, user_id, ex, int(text.strip()))
            return True
        if step == "ex_answer" and text and ex.get("quiz"):
            parsed = exercises.parse_answers(text, 10)
            if parsed and all(not a["answer"] and a.get("note") for a in parsed.values()):
                await self.ex_quiz_notes(chat_id, user_id, ex, {n: a["note"] for n, a in parsed.items()})
                return True
            if not parsed:  # ни номеров, ни ответов — не проверяем наполовину нажатый тест
                await self.tg.send_message(chat_id, "Не понял, к какому пункту. Уточнение — с номером: "
                                                     "«2 (почему не …?)», ответы — кнопками a / b / c.")
                return True
        if step == "ex_answer" and (text or voice):
            saved = self.db.ex_get(ex.get("ex_id", 0))
            if saved and sent_at and sent_at < saved["created_at"] - 1:
                await self.tg.send_message(chat_id, f"Это сообщение ушло раньше, чем пришло упражнение "
                                                     f"#{saved['id']}, — пришли ответ ещё раз.")
                return True
            await self.ex_check(chat_id, user_id, ex, text, voice)
            return True
        if step == "ex_review" and text:
            m = QUESTION_RE.match(text)
            if m:
                await self.ex_question(chat_id, user_id, ex, int(m.group(1)), m.group(2).strip())
                return True
        return False  # обычный разговор; шаг упражнений сохраняется

    async def ex_start(self, chat_id: int, user_id: int, ex: dict, n: int) -> None:
        n = max(1, min(20, n))
        await self.ex_make(chat_id, user_id, {**ex, "total": n, "left": n})

    async def ex_make(self, chat_id: int, user_id: int, ex: dict) -> None:
        kind, f = ex.get("kind", "grammar"), ex.get("fmt", "gap")
        idx = ex["total"] - ex["left"] + 1
        if kind in ("book_card", "book_test"):
            built = await self.book_items(chat_id, user_id, ex, idx)
            if built:
                await self.ex_publish(chat_id, user_id, ex, built[0], built[1], idx)
            return
        if kind == "book_gap":
            unit = textbook.get_unit(ex.get("unit", ""))
            if not unit:
                await self.tg.send_message(chat_id, "Юнит не найден. /ex → 📘 Учебник")
                return
            stats = self.db.book_stats(user_id, unit["unit"])
            ex = {**ex, "words": [w["pl"] for w, _ in textbook.pick_words(unit, stats, 10, "gap")]}
        rule_filter = ex.get("rules") or None
        reuse = [it for it in self.db.ex_review_items(user_id, 20, rule_filter)
                 if bool(it.get("options")) == (f == "test") and not it.get("card") and not it.get("unit")
                 and (kind not in ("words", "voice") or it.get("lemma") in (ex.get("words") or []))][:EX_REUSE]
        if kind == "book_gap":
            reuse = []  # слова с ошибками и так идут первыми (статистика юнита)
        prompt = ex_prompt(kind, f, self.cfg.level, rules.catalog_text(), self.db.ex_recent(user_id),
                           words=ex.get("words"), rules_list=ex.get("rules"), examples=ex.get("examples"),
                           voice=kind == "voice", present_only=EX_PRESENT_ONLY, cases=EX_CASES)
        data = await self.ask(chat_id, prompt, EX_SCHEMA, EX_HINT,
                              wait=f"⏳ Составляю упражнение {idx}/{ex['total']}… (10–20 секунд)")
        if data is None:
            return
        seen = self.db.ex_seen_keys(user_id)
        fresh = []
        for it in data.get("items") or []:
            if not isinstance(it, dict) or "___" not in str(it.get("q", "")) or not str(it.get("answer", "")).strip():
                continue
            if f == "test":
                opts = [str(o) for o in it.get("options") or []][:4]
                if exercises.norm(it["answer"]) not in [exercises.norm(o) for o in opts]:
                    continue
                it["options"] = opts
            else:
                it["options"] = []
            it["rule"] = rules.normalize(it.get("rule"))
            ru = str(it.get("ru") or "")
            if "⟪" in ru and "⟫" in ru:  # перевод пропущенного слова — под спойлер
                it["ru_spoiler"], it["ru"] = ru, ru.replace("⟪", "").replace("⟫", "")
            if kind == "book_gap":
                it["unit"] = ex.get("unit")
            key = exercises.norm_sentence(it.get("full_pl") or it["q"])
            if key in seen:
                continue
            seen.add(key)
            it["_key"] = key
            fresh.append(it)
        items = (fresh[:10 - len(reuse)] + reuse)
        random.shuffle(items)
        if f == "test":  # модель почти всегда ставит правильный вариант первым — перемешиваем сами
            for it in items:
                it["options"] = random.sample(it["options"], len(it["options"]))
        for it in items:
            it.setdefault("_key", exercises.norm_sentence(it.get("full_pl") or it.get("q", "")))
        if not items:
            await self.tg.send_message(chat_id, "Не получилось составить упражнение — попробуй ещё раз.")
            return
        title = str(data.get("title") or fmt.EX_KINDS.get(kind, "Упражнение"))
        await self.ex_publish(chat_id, user_id, ex, items, title, idx)

    async def ex_publish(self, chat_id: int, user_id: int, ex: dict, items: list[dict], title: str, idx: int) -> None:
        kind, f = ex.get("kind", "grammar"), ex.get("fmt", "gap")
        ex_id = self.db.ex_create(user_id, kind, f, title, items, ex.get("rules") or [])
        ex = {**ex, "ex_id": ex_id, "left": ex["left"] - 1}
        self.db.set_pending(user_id, {"step": "ex_answer", "ex": ex})
        saved = self.db.ex_get(ex_id)
        if f == "test" and kind != "voice" and EX_QUIZ_BUTTONS:
            await self.ex_send_quiz(chat_id, user_id, ex, saved, idx)
            return
        sent = await self.tg.send_message(chat_id, fmt.ex_message(saved, idx, ex["total"], kind == "voice"),
                                          [[("✖️ Закончить без проверки", "nav:c:ex_answer")]])
        if sent and sent.get("message_id"):
            self.db.ex_set_tg_msg(ex_id, sent["message_id"])

    # ---------- 📘 лексика из учебника ----------

    async def book_items(self, chat_id: int, user_id: int, ex: dict, idx: int) -> tuple[list, str] | None:
        """Карточки и тест по словам юнита. Карточки — без Gemini; для теста Gemini подбирает
        2 неверных варианта, близких по смыслу."""
        unit = textbook.get_unit(ex.get("unit", ""))
        if not unit:
            await self.tg.send_message(chat_id, "Юнит не найден. /ex → 📘 Учебник")
            return None
        stats = self.db.book_stats(user_id, unit["unit"])
        items = [textbook.card_item(w, d) for w, d in textbook.pick_words(unit, stats, 10, ex.get("dir", "mix"))]
        for it in items:
            it["unit"] = unit["unit"]
            it["_key"] = f"card:{unit['unit']}:{it['dir']}:{it['lemma']}:{self.clock()}"
        mode = "Карточки" if ex["kind"] == "book_card" else "Тест"
        title = f"Unit {unit['unit']} «{unit['title']}» — {mode}, {textbook.DIRS.get(ex.get('dir', 'mix'), '')}"
        if ex["kind"] == "book_test":
            prompt = book_options_prompt([(n, it["q"], it["answer"], "ru" if it["dir"] == "pl" else "pl")
                                          for n, it in enumerate(items, 1)])
            data = await self.ask(chat_id, prompt, BOOK_OPTS_SCHEMA, BOOK_OPTS_HINT,
                                  wait=f"⏳ Подбираю близкие варианты {idx}/{ex['total']}…")
            if data is None:
                return None
            wrong = {int(x.get("n", 0)): [str(o) for o in x.get("wrong") or []]
                     for x in data.get("items") or [] if isinstance(x, dict)}
            keep = []
            for n, it in enumerate(items, 1):
                bad = [o for o in wrong.get(n, []) if o.strip() and exercises.norm(o) != exercises.norm(it["answer"])]
                bad = list(dict.fromkeys(bad))[:2]
                if len(bad) < 2:
                    continue  # модель не дала два варианта — пункт пропускаем
                it["options"] = random.sample([it["answer"]] + bad, 3)
                keep.append(it)
            items = keep
            if not items:
                await self.tg.send_message(chat_id, "Не получилось подобрать варианты — попробуй ещё раз.")
                return None
        return items, title

    def book_record_results(self, user_id: int, saved: dict, results: list[dict]) -> None:
        """Ответы по словам учебника — только в статистику юнита, не в общий пул ошибок."""
        for r in results:
            it = saved["items"][r["n"] - 1]
            unit = it.get("unit")
            if not unit:
                continue
            if it.get("card"):
                pl, d = it["lemma"], it["dir"]
            else:  # пропуски: найти слово юнита по словарной форме
                u = textbook.get_unit(unit)
                lemma = str(it.get("lemma", "")).strip().lower()
                w = next((w for w in (u or {}).get("words", []) if w["pl"].lower() == lemma), None)
                if not w:
                    continue
                pl, d = w["pl"], "ru"
            self.db.book_record(user_id, unit, pl, d, r["final"] != "wrong")

    async def book_unit_screen(self, chat_id: int, user_id: int, ex: dict, unit: dict,
                               message_id: int | None = None) -> None:
        done, total = textbook.unit_progress(unit, self.db.book_stats(user_id, unit["unit"]))
        text, buttons = fmt.book_unit_screen(unit, done, total)
        await self.show(chat_id, user_id, {"step": "ex_bunit", "ex": {**ex, "unit": unit["unit"]}}, text, buttons,
                        message_id)

    # ---------- интерактивный тест: кнопки a / b / c под каждым пунктом ----------

    async def ex_send_quiz(self, chat_id: int, user_id: int, ex: dict, saved: dict, idx: int) -> None:
        """Одно сообщение с упражнением, под ним кнопки a / b / c по строке на пункт. Выбор хранится в pending."""
        sent = await self.tg.send_message(chat_id, fmt.ex_message(saved, idx, ex["total"], False, quiz=True),
                                          fmt.ex_quiz_keyboard(saved, {}, []))
        msg = (sent or {}).get("message_id")
        if msg:
            self.db.ex_set_tg_msg(saved["id"], msg)
        ex = {**ex, "quiz": {"msg": msg, "idx": idx, "pick": {}, "sure": [], "notes": {}}}
        self.db.set_pending(user_id, {"step": "ex_answer", "ex": ex})

    async def ex_quiz_tap(self, chat_id: int, user_id: int, pending: dict | None, data: str) -> str | None:
        """x:a:<ex>:<n>:<a|b|c|!> — выбрать вариант / «уверен»; x:go:<ex> — проверить. Возвращает всплывашку."""
        ex = dict((pending or {}).get("ex") or {})
        parts = data.split(":")
        ex_id = int(parts[2])
        quiz = ex.get("quiz")
        if (pending or {}).get("step") != "ex_answer" or ex.get("ex_id") != ex_id or not quiz:
            return "Это упражнение уже проверено или закрыто"
        saved = self.db.ex_get(ex_id)
        total = len(saved["items"])
        if parts[1] == "go":
            picked = len(quiz["pick"])
            if picked < total:
                return f"Отмечено {picked} из {total} — выбери остальные"
            answers = {int(n): {"answer": L, "unsure": False, "sure": int(n) in quiz["sure"],
                                "note": quiz["notes"].get(n, "")} for n, L in quiz["pick"].items()}
            await self.ex_check(chat_id, user_id, ex, None, None, answers=answers)
            return None
        n, choice = int(parts[3]), parts[4]
        if not 1 <= n <= total:
            return None
        if choice == "!":
            quiz["sure"] = sorted(set(quiz["sure"]) ^ {n})
        elif quiz["pick"].get(str(n)) == choice:
            return None  # то же самое — ничего не меняем
        else:
            quiz["pick"][str(n)] = choice
        self.db.set_pending(user_id, {"step": "ex_answer", "ex": {**ex, "quiz": quiz}})
        if quiz.get("msg"):
            await self.tg.edit_markup(chat_id, quiz["msg"], fmt.ex_quiz_keyboard(saved, quiz["pick"], quiz["sure"]))
        return None

    async def ex_quiz_notes(self, chat_id: int, user_id: int, ex: dict, notes: dict[int, str]) -> None:
        """Уточнения текстом «2 (почему …?)» до проверки — прикрепляем к пунктам (💭 в тексте упражнения)."""
        quiz = ex["quiz"]
        saved = self.db.ex_get(ex["ex_id"])
        notes = {n: t for n, t in notes.items() if 1 <= n <= len(saved["items"])}
        for n, note in notes.items():
            quiz["notes"][str(n)] = note
        self.db.set_pending(user_id, {"step": "ex_answer", "ex": {**ex, "quiz": quiz}})
        if quiz.get("msg"):
            await self.tg.edit_message(chat_id, quiz["msg"],
                                       fmt.ex_message(saved, quiz.get("idx", 1), ex.get("total", 1), False,
                                                      quiz=True, notes=quiz["notes"]),
                                       fmt.ex_quiz_keyboard(saved, quiz["pick"], quiz["sure"]))
        await self.tg.send_message(chat_id, "💭 Уточнение к пункту " + ", ".join(str(n) for n in notes)
                                   + " добавлено — уйдёт на проверку вместе с ответами.")

    async def ex_check(self, chat_id: int, user_id: int, ex: dict, text: str | None, voice: dict | None,
                       answers: dict[int, dict] | None = None) -> None:
        saved = self.db.ex_get(ex["ex_id"])
        if not saved:
            return
        items, f = saved["items"], saved["fmt"]
        quiz = ex.get("quiz") if ex.get("ex_id") == saved["id"] else None
        if answers is None and not voice:
            answers = exercises.parse_answers(text or "", len(items))
            if quiz:  # ответ текстом поверх нажатых кнопок: чего нет в тексте — берём из кнопок
                for n, L in quiz["pick"].items():
                    answers.setdefault(int(n), {"answer": L, "unsure": False, "sure": int(n) in quiz["sure"],
                                                "note": quiz["notes"].get(n, "")})
                for n, note in quiz["notes"].items():
                    if int(n) in answers and not answers[int(n)].get("note"):
                        answers[int(n)]["note"] = note
        if voice:
            audio = await self.tg.download_file(voice["file_id"])
            results = [{"n": i, "user": "", "unsure": False, "note": "", "status": "check"}
                       for i in range(1, len(items) + 1)]
        else:
            audio = None
            results = exercises.quick_check(items, answers, f)
        cards = any(it.get("card") for it in items)
        todo = exercises.needs_model(results, EX_EXPLAIN_ALL and not cards)  # карточки: объясняем только ошибки
        verdicts: dict[int, dict] = {}
        if todo:
            prompt = check_prompt([(r["n"], items[r["n"] - 1], r["user"], r["unsure"], r.get("note", "")) for r in todo],
                                  self.cfg.level, voice=bool(voice), topics=saved.get("topics"), cards=cards)
            if voice and self.ignore_block(user_id):
                prompt = self.ignore_block(user_id) + "\n\n" + prompt
            data = await self.ask(chat_id, prompt, CHECK_SCHEMA, CHECK_HINT,
                                  wait=f"⏳ Проверяю упражнение #{saved['id']}…", audio=audio)
            if data is None:
                return  # шаг ex_answer сохраняется — можно прислать ответ ещё раз
            verdicts = {int(v.get("n", 0)): v for v in data.get("items") or [] if isinstance(v, dict)}
        for r in results:
            v = verdicts.get(r["n"], {})
            r["explanation"], r["bridge"] = v.get("explanation", ""), v.get("bridge", "")
            if v.get("heard"):
                r["heard"] = v["heard"]
            if voice and str(v.get("note") or "").strip():  # в голосовом уточнение выделяет модель
                r["note"] = str(v["note"]).strip()
            if r.get("note"):
                r["note_ok"] = bool(v.get("note_ok", True))
                r["note_comment"] = str(v.get("note_comment") or "")
            if r["status"] == "ok":
                right = True
            elif r["status"] in ("diacritics", "wrong", "missing"):
                right = False
                if r["status"] == "diacritics" and not r["explanation"]:
                    r["explanation"] = "Нужны польские буквы — без них это другое слово или ошибка."
            else:  # check
                right = bool(v.get("correct"))
            r["final"] = ("unsure" if r["unsure"] else "ok") if right else "wrong"
            if right and r.get("note") and not r["note_ok"]:  # ответ верный, а рассуждение — нет: это ошибка
                r["final"], r["logic_wrong"] = "wrong", True
        if text and exercises.unclosed_note(text):
            await self.tg.send_message(chat_id, "⚠️ Не закрыта скобка — всё после «(» до конца сообщения "
                                                 "я посчитал уточнением.")
        self.db.ex_save_results(saved["id"], results)
        if quiz and saved.get("tg_msg_id"):  # выбор остаётся виден, «Проверить» и «Закончить» убираем
            await self.tg.edit_markup(chat_id, saved["tg_msg_id"],
                                      fmt.ex_quiz_keyboard(saved, quiz["pick"], quiz["sure"], check=False))
        elif saved.get("tg_msg_id"):
            await self.tg.edit_markup(chat_id, saved["tg_msg_id"])  # кнопка «Закончить без проверки» больше не нужна
        self.ex_record(user_id, saved, results)
        self.db.set_pending(user_id, {"step": "ex_review", "ex": ex})
        await self.tg.send_message(chat_id, fmt.ex_results(saved, results),
                                   fmt.ex_result_buttons(saved["id"], ex.get("left", 0)))

    def ex_record(self, user_id: int, saved: dict, results: list[dict]) -> None:
        """Ошибки — в общий пул; ответы по словам и правилам набора — в прогресс с весом 0.1.
        Учебник — только в статистику юнита."""
        if str(saved.get("kind", "")).startswith("book_"):
            self.book_record_results(user_id, saved, results)
            return
        session = self.db.current_session(user_id)
        wrong = []
        for r in results:
            it = saved["items"][r["n"] - 1]
            if r["final"] == "wrong":
                original = r.get("heard") or r.get("user") or "—"
                why = r.get("explanation") or it.get("grammar", "")
                if r.get("logic_wrong"):  # ответ верный, ошибка в рассуждении
                    original += f" ({r.get('note', '')})"
                    why = "Ответ верный, ошибка в рассуждении: " + (r.get("note_comment") or why)
                wrong.append({"kind": "grammar", "original": original,
                              "correct": it.get("answer", ""), "translit": it.get("translit", ""),
                              "ru": it.get("ru", ""), "why": why,
                              "rule": rules.normalize(it.get("rule")), "source": "упражнение",
                              "ex_id": saved["id"], "n": r["n"]})
        if wrong:
            self.db.add_corrections(session, user_id, wrong)
        active = self.db.active_set(user_id)
        if not active:
            return
        by_name = {w["pl"].lower(): w for w in self.db.set_words(active["id"]) if w["mastered_at"] is None}
        day = self.today()
        for r in results:
            it = saved["items"][r["n"] - 1]
            w = by_name.get(str(it.get("lemma", "")).lower()) or by_name.get(str(it.get("rule", "")).lower())
            if w:
                self.db.add_use(w["id"], user_id, it.get("answer", ""), r["final"] != "wrong", day, EX_WEIGHT)

    async def ex_question(self, chat_id: int, user_id: int, ex: dict, n: int, question: str) -> None:
        saved = self.db.ex_get(ex.get("ex_id", 0))
        if not saved or not 1 <= n <= len(saved["items"]):
            await self.tg.send_message(chat_id, "Нет такого пункта.")
            return
        r = (saved["results"] or [{}] * len(saved["items"]))[n - 1]
        data = await self.ask(chat_id, ex_question_prompt(saved["items"][n - 1], r.get("heard") or r.get("user", ""),
                                                          question, self.cfg.level), RULES_SCHEMA, RULES_HINT)
        if data is not None:
            await self.tg.send_message(chat_id, fmt.rules_message(data))

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
                await self.show(chat_id, user_id, {"step": "rule"},
                                "Напиши вопрос о правиле — например: почему do niej, а не do nie?", prev=None)
        elif cmd == "/ex":
            await self.ex_menu(chat_id, user_id)
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
                asyncio.create_task(self.dispatch(upd))

    async def dispatch(self, upd: dict) -> None:
        """Обработка одного обновления. Пока запрос пользователя в работе, новые не встают в очередь,
        а сразу получают ответ «ещё обрабатываю» — так не бывает двойных упражнений и ответов не туда."""
        src = upd.get("message") or upd.get("callback_query") or {}
        user_id = src.get("from", {}).get("id")
        if user_id in self.busy:
            if "callback_query" in upd:
                await self.tg.answer_callback(upd["callback_query"]["id"], "⏳ Ещё обрабатываю прошлый запрос…")
            else:
                chat_id = src.get("chat", {}).get("id")
                if chat_id is not None:
                    await self.tg.send_message(chat_id, "⏳ Ещё обрабатываю прошлый запрос — подожди пару секунд "
                                                        "и пришли это сообщение снова.")
            return
        self.busy.add(user_id)
        try:
            if "message" in upd:
                await self.handle(upd["message"])
            elif "callback_query" in upd:
                await self.on_callback(upd["callback_query"])
        except Exception:
            log.exception("Необработанная ошибка")
        finally:
            self.busy.discard(user_id)


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
