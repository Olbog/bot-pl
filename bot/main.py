"""Точка входа: обработка сообщений и цикл long polling."""
import asyncio
import logging
from html import escape

from . import config as cfg_mod
from . import fmt
from .db import DB
from .gemini import Gemini, GeminiError
from .telegram import Telegram
from .tts import synthesize

log = logging.getLogger("bot-pl")


class App:
    def __init__(self, cfg: cfg_mod.Config, tg: Telegram, gemini: Gemini, db: DB, tts=synthesize):
        self.cfg, self.tg, self.gemini, self.db, self.tts = cfg, tg, gemini, db, tts

    async def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id")
        if user_id not in self.cfg.allowed_ids:
            await self.tg.send_message(chat_id, f"Нет доступа. Твой Telegram ID: <code>{user_id}</code>")
            return

        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            await self.command(chat_id, user_id, text.split()[0].split("@")[0].lower())
            return

        voice = msg.get("voice") or msg.get("audio")
        if not text and not voice:
            await self.tg.send_message(chat_id, "Пришли текст или голосовое.")
            return

        session = self.db.current_session(user_id)
        history = self.db.history(session, self.cfg.history_limit)
        await self.tg.send_action(chat_id, "typing")
        try:
            audio = await self.tg.download_file(voice["file_id"]) if voice else None
            turn = await self.gemini.reply(history, text=text or None, audio=audio)
        except GeminiError as e:
            log.error("Gemini: %s", e)
            await self.tg.send_message(chat_id, f"⚠️ Ошибка Gemini, попробуй ещё раз.\n<code>{escape(str(e)[:300])}</code>")
            return
        except Exception as e:  # сеть, таймаут, Telegram
            log.exception("Ошибка обработки")
            await self.tg.send_message(chat_id, f"⚠️ Не получилось обработать: <code>{escape(str(e)[:300])}</code>")
            return

        user_text = turn.user_text or text
        self.db.save_turn(session, user_id, user_text, turn.reply_pl, turn.corrections, turn.new_words)
        await self.tg.send_message(chat_id, fmt.turn_message(turn, from_voice=bool(voice)))

        if turn.reply_pl:
            await self.tg.send_action(chat_id, "record_voice")
            try:
                ogg = await self.tts(turn.reply_pl, self.cfg.tts_voice, self.cfg.tts_rate)
                await self.tg.send_voice(chat_id, ogg)
            except Exception:
                log.exception("Озвучка не удалась")
                await self.tg.send_message(chat_id, "🔇 Озвучка не получилась, текст выше.")

    async def command(self, chat_id: int, user_id: int, cmd: str) -> None:
        if cmd == "/new":
            self.db.new_session(user_id)
            await self.tg.send_message(chat_id, "🆕 Новая тема. Zaczynamy! [за-чы-НА-мы] — Начинаем!")
        elif cmd == "/itog":
            session = self.db.current_session(user_id)
            words, corrs = self.db.session_summary(session)
            await self.tg.send_message(chat_id, fmt.summary_message(words, corrs, self.db.message_count(session)))
        else:  # /start, /help и всё остальное
            await self.tg.send_message(chat_id, fmt.HELP)

    async def run(self) -> None:
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
                if "message" in upd:
                    try:
                        await self.handle(upd["message"])
                    except Exception:
                        log.exception("Необработанная ошибка")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в логи попадёт токен из URL
    cfg = cfg_mod.load()
    app = App(
        cfg,
        Telegram(cfg.telegram_token),
        Gemini(cfg.worker_url, cfg.proxy_token, cfg.model, cfg.thinking_level, cfg.level),
        DB(cfg.db_path),
    )
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
