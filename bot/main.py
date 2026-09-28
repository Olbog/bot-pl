"""Точка входа: обработка сообщений, кнопок и цикл long polling."""
import asyncio
import logging
import time
from datetime import datetime
from html import escape

from . import config as cfg_mod
from . import fmt, training
from .db import DB
from .gemini import Gemini, GeminiError, GeminiExhausted, GeminiOverloaded
from .prompt import WORDS_HINT, WORDS_SCHEMA, own_words_prompt, topic_words_prompt
from .telegram import Telegram
from .tts import synthesize

log = logging.getLogger("bot-pl")

START_TEXT = "Zaczynajmy!"  # реплика ученика, с которой бот открывает тренировку набора


class App:
    def __init__(self, cfg: cfg_mod.Config, tg: Telegram, gemini: Gemini, db: DB, tts=synthesize,
                 clock=time.time):
        self.cfg, self.tg, self.gemini, self.db, self.tts, self.clock = cfg, tg, gemini, db, tts, clock
        self.criteria = training.Criteria(cfg.master_streak, cfg.master_forms, cfg.master_days)

    def today(self) -> str:
        return datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")

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
            await self.command(chat_id, user_id, text.split()[0].split("@")[0].lower())
            return

        pending = self.db.get_state(user_id)["pending"]
        if pending and text and pending.get("step") in ("topic", "own", "add"):
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
        self.db.save_turn(session, user_id, user_text, turn.reply_pl, turn.corrections, turn.new_words)
        uses, newly_mastered = self.apply_target_uses(user_id, targets, turn.target_uses)
        if text == START_TEXT and not voice:  # служебное начало тренировки — разбор не показываем
            turn.user_text = turn.corrected_pl = ""
            turn.corrections, turn.new_words = [], []
        await self.tg.send_message(chat_id, fmt.turn_message(
            turn, from_voice=bool(voice), show_model=self.cfg.show_model, target_line=fmt.target_line(uses)))

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
                chat_id, f"🎉 Освоено: <b>{escape(w['pl'])}</b> — {escape(w['ru'])}\n"
                         f"<i>{self.criteria.streak} раз подряд без ошибок, в разных формах и днях. "
                         f"Теперь оно в повторении.</i>")
        await self.maybe_suggest_next(chat_id, user_id)

    async def report_gemini_error(self, chat_id: int, e: GeminiError) -> None:
        log.error("Gemini: %s", e)
        if isinstance(e, GeminiExhausted):
            reset = datetime.fromtimestamp(e.reset_at).strftime("%H:%M")
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
                block = training.set_block(active["title"], rows, self.criteria)
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
            if ok and training.meets(training.stats(self.db.word_uses(w["id"])), self.criteria):
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
            text = ("📚 Набора пока нет.\n\nСоставь его по теме (я подберу 10 слов) или пришли свои слова — "
                    "и я буду строить разговор так, чтобы ты говорил их как можно чаще и в разных формах.")
        else:
            text = fmt.set_progress(active["title"], self.set_rows(active["id"]), self.criteria, mode)
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
        else:  # own / add
            base = pending if step == "add" else {}
            await self.build_preview(chat_id, user_id, {k: v for k, v in base.items() if k != "step"},
                                     own_words_prompt(text, self.cfg.level))

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
        elif data.startswith("p:"):
            await self.preview_action(chat_id, user_id, message_id, pending, data[2:])

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

    async def command(self, chat_id: int, user_id: int, cmd: str) -> None:
        if cmd == "/new":
            self.db.new_session(user_id)
            await self.tg.send_message(chat_id, "🆕 Новая тема. Zaczynamy! [за-чы-НА-мы] — Начинаем!")
        elif cmd == "/itog":
            session = self.db.current_session(user_id)
            words, corrs = self.db.session_summary(session)
            await self.tg.send_message(chat_id, fmt.summary_message(words, corrs, self.db.message_count(session)))
        elif cmd == "/set":
            await self.show_set(chat_id, user_id)
        elif cmd == "/free":
            self.db.set_mode(user_id, "free")
            self.db.new_session(user_id)
            await self.tg.send_message(chat_id, "🏁 Свободный разговор. О чём поговорим? "
                                                 "Вернуться к набору — /set.")
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
