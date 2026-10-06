"""Итоги и выгрузки: /itog, /export, еженедельное автосохранение, раскладка старых ошибок."""
import asyncio

from .core import config as cfg_mod
from .core import rules
from .ai.gemini import GeminiError
from .ai.prompt import CLASSIFY_HINT, CLASSIFY_SCHEMA, classify_prompt
from .ui import fmt
from .settings import AUTOSAVE_HOUR, AUTOSAVE_WEEKDAY
from .common import log


class ReportsMixin:
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
