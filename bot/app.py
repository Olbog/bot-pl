"""Ядро бота: приём сообщений и кнопок, команды, главное меню, «⬅️ Назад / ✖️ Отмена», цикл."""
import asyncio
import logging
import time
from html import escape
from pathlib import Path

from .core import config as cfg_mod
from .core import training
from .core.db import DB
from .core.telegram import Telegram
from .core.tts import synthesize
from .ai.gemini import Gemini, GeminiError, GeminiExhausted, GeminiOverloaded
from .ai.prompt import own_words_prompt, topic_words_prompt
from .ui import fmt
from .settings import RULE_FACTOR
from .common import AUTO, DICT_MENU, TEXT_STEPS, log

from .conversation import ConversationMixin
from .sets import SetsMixin
from .dictionary import DictionaryMixin
from .reports import ReportsMixin
from .exercises.menu import ExerciseMenuMixin
from .exercises.run import ExerciseRunMixin
from .exercises.book import BookMixin
from .exercises.bookex import BookExMixin


class App(ConversationMixin, SetsMixin, DictionaryMixin, ReportsMixin,
          ExerciseMenuMixin, ExerciseRunMixin, BookMixin, BookExMixin):
    """Бот целиком: ядро здесь, остальное — в модулях-миксинах."""
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
        if pending and pending.get("step") == "bex" and (text or voice):
            await self.bex_text(chat_id, user_id, pending, text, voice)
            return
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

    async def pending_text(self, chat_id: int, user_id: int, pending: dict, text: str) -> None:
        step = pending["step"]
        if step == "new_topic":
            await self.start_free(chat_id, user_id, text.strip()[:100])
        elif step == "topic":
            n = self.cfg.set_size
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
                await self.tg.send_message(chat_id, f"⏹ Упражнения закончены, #{ex_id} — без проверки.")
            else:
                await self.tg.edit_message(chat_id, message_id, "✖️ Отменено.")
            await self.main_menu(chat_id, user_id)  # отмена ничего не сбрасывает: разговор и набор как были
            return None
        prev = pending.get("prev")
        if not prev:
            return "Это первый шаг"
        if prev.get("step") == "set_menu":
            self.db.set_pending(user_id, None)
            await self.show_set(chat_id, user_id, message_id)
        elif prev.get("step") == "new_menu" and not prev.get("screen"):
            await self.new_menu(chat_id, user_id, message_id)
        elif prev.get("step") == "dict_menu":
            self.db.set_pending(user_id, None)
            await self.tg.edit_message(chat_id, message_id, fmt.dict_message(list(self.db.dict_items(user_id))),
                                       fmt.dict_buttons())
        else:
            text, buttons = prev["screen"]
            buttons = [[tuple(b) for b in row] for row in buttons]
            await self.show(chat_id, user_id, prev, text, buttons, message_id, prev=prev.get("prev"))
        return None

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
        if data.startswith("x:h:"):  # 💡 подсказка к пункту упражнения
            await self.tg.answer_callback(cq["id"], self.ex_hint(user_id, data), alert=True)
            return
        if data.startswith("wd:s:"):  # 📖 /words, скрытый перевод: перевод одного слова
            await self.tg.answer_callback(cq["id"], self.words_hint(data), alert=True)
            return
        if data.startswith(("x:a:", "x:go:")):  # интерактивный тест: всплывашка вместо сообщения
            await self.tg.answer_callback(cq["id"], await self.ex_quiz_tap(chat_id, user_id, pending, data))
            return
        if data.startswith(("bx:a:", "bx:go", "bx:h:")):  # 📝 упражнение из книги: всплывашка вместо сообщения
            await self.tg.answer_callback(cq["id"], *await self.bex_tap(chat_id, user_id, pending, data[3:]))
            return
        if data.startswith("nav:"):
            await self.tg.answer_callback(cq["id"], await self.nav(chat_id, user_id, message_id, pending, data[4:]))
            return
        await self.tg.answer_callback(cq["id"])

        if data.startswith("go:"):
            await self.menu_go(chat_id, user_id, data[3:])
        elif data.startswith("nw:"):
            await self.new_callback(chat_id, user_id, message_id, pending, data[3:])
        elif data == "s:show":
            await self.show_set(chat_id, user_id, message_id)
        elif data == "s:topic":
            await self.show(chat_id, user_id, {"step": "topic"}, "Напиши тему — по-русски или по-польски "
                            "(например: кафе, у врача, выходные).", prev=self.set_src_prev(user_id))
        elif data == "s:own":
            await self.show(chat_id, user_id, {"step": "own"}, "Пришли слова через запятую или столбиком — "
                            "по-польски или по-русски, я переведу.", prev=self.set_src_prev(user_id))
        elif data == "s:unit":
            await self.set_units(chat_id, user_id)
        elif data.startswith("su:"):
            if pending and pending.get("step") == "set_unit":
                await self.preview_from_unit(chat_id, user_id, data[3:])
        elif data.startswith("bk:"):
            await self.book_chosen(chat_id, user_id, message_id, pending, data[3:])
        elif data.startswith("bx:"):
            await self.bex_callback(chat_id, user_id, message_id, pending, data[3:])
        elif data.startswith("wd:"):
            await self.words_callback(chat_id, user_id, data[3:])
        elif data.startswith("ar:"):
            await self.archive_action(chat_id, user_id, message_id, pending, data[3:])
        elif data.startswith("mode:"):  # кнопки со старых сообщений
            if data == "mode:set":
                await self.start_set_conversation(chat_id, user_id)
            else:
                await self.start_free(chat_id, user_id, None)
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
            await self.preview_from_dict(chat_id, user_id, prev=self.set_src_prev(user_id))
        elif data.startswith("p:"):
            if data == "p:cancel" and pending and pending.get("step") == "errsel":
                self.db.set_pending(user_id, None)
                await self.tg.edit_message(chat_id, message_id, "✖️ Отменено.")
                await self.main_menu(chat_id, user_id)
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

    async def command(self, chat_id: int, user_id: int, cmd: str, arg: str = "") -> None:
        if cmd in ("/new", "/free"):  # /free — старая команда, теперь «Без набора» внутри /new
            await self.new_menu(chat_id, user_id)
        elif cmd in ("/menu", "/start"):
            await self.main_menu(chat_id, user_id)
        elif cmd == "/itog":
            text, buttons = fmt.itog_choice()
            await self.tg.send_message(chat_id, text, buttons)
        elif cmd == "/set":
            await self.show_set(chat_id, user_id)
        elif cmd == "/dict":
            if arg.lower().startswith("add"):
                raw = arg[3:].strip()
                if raw:
                    await self.dict_add_text(chat_id, user_id, raw)
                else:
                    await self.show(chat_id, user_id, {"step": "dict_own"},
                                    "Пришли выражения через запятую — по-польски или по-русски.", prev=None)
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
        elif cmd == "/words":
            await self.words_menu(chat_id, user_id)
        elif cmd == "/phr":
            await self.phrases_menu(chat_id, user_id)
        elif cmd == "/export":
            await self.send_export(chat_id, user_id, "a")
        elif cmd == "/cancel":
            await self.tg.send_message(chat_id, "✖️ Отменено.")
            await self.main_menu(chat_id, user_id)
        else:  # /help и всё остальное
            await self.tg.send_message(chat_id, fmt.HELP)

    # ---------- 🏠 главное меню ----------

    async def main_menu(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        text, buttons = fmt.main_menu(self.status(user_id))
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    async def menu_go(self, chat_id: int, user_id: int, where: str) -> None:
        """Кнопки главного меню — как соответствующие команды (незаконченный выбор отменяется)."""
        self.db.set_pending(user_id, None)
        cmd = {"new": "/new", "ex": "/ex", "set": "/set", "itog": "/itog", "dict": "/dict", "export": "/export",
               "words": "/words", "phr": "/phr"}
        if where in cmd:
            await self.command(chat_id, user_id, cmd[where])

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

