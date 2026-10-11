"""📚 /read — чтение: книга (book.json: "reading": true) по главам и абзацам.

Абзац — текст по-польски, перевод под спойлером, 🔊 аудио в обычном темпе (озвучено заранее, при старте бота,
кэш data/audio/read/), 📖 слова абзаца с кнопками ⭐ в словарь, ❓ вопрос по абзацу.
Абзац прочитан (read_done), когда ученик сам нажал «➡️ Дальше» (на последнем — «🏁 Конец главы»);
«⬅️» и повторный просмотр ничего не засчитывают. Последний открытый абзац — место в книге (read_pos).
Конец главы: 📝 вопросы к главе (упражнения юнита, как у учебника) и 🎯 тренировка слов главы.
"""
import asyncio
import hashlib
from pathlib import Path

from .common import log
from .core import textbook
from .ai.prompt import RULES_HINT, RULES_SCHEMA, read_question_prompt
from .ui import fmt

READ_RATE = "+0%"   # обычный темп речи (для слов юнита — медленнее, cfg.tts_rate)
PREGEN_PAUSE = 3                  # секунд между абзацами при озвучке заранее
PREGEN_RETRY = (60, 300, 1800)    # повтор после ошибки: 1, 5, 30 мин, дальше раз в 30 мин


class ReadingMixin:
    # ---------- данные ----------

    def read_books(self) -> list[dict]:
        return [b for b in textbook.load_books() if b.get("reading") and b["units"]]

    def read_dir(self) -> Path:
        return getattr(self, "read_audio_dir", None) or Path(self.cfg.db_path).parent / "audio" / "read"

    def read_audio_path(self, unit: dict, idx: int) -> Path:
        """Имя с хэшем текста: поправили абзац — озвучка сделается заново."""
        text = unit["paragraphs"][idx]["pl"]
        h = hashlib.md5(f"{self.cfg.tts_voice}|{READ_RATE}|{text}".encode()).hexdigest()[:8]
        return self.read_dir() / f"{unit['book']}_{unit['num']}_{idx + 1:02d}_{h}.ogg"

    async def read_audio(self, unit: dict, idx: int) -> bytes:
        path = self.read_audio_path(unit, idx)
        if path.exists():
            return path.read_bytes()
        ogg = await self.tts(unit["paragraphs"][idx]["pl"], self.cfg.tts_voice, READ_RATE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ogg)
        return ogg

    async def read_pregen(self, sleep=None) -> None:
        """Озвучить заранее все абзацы всех книг для чтения (уже готовые — пропускаются).
        Между абзацами — пауза PREGEN_PAUSE (сервис не любит частые запросы подряд). Ошибка — не конец:
        повтор через PREGEN_RETRY (1, 5, 30 мин), дальше раз в 30 мин, пока все абзацы не будут готовы;
        после удачной озвучки отсчёт задержек начинается заново."""
        sleep = sleep or asyncio.sleep
        made = fails = 0
        while True:
            todo = [(u, i) for b in self.read_books() for u in b["units"] for i in range(len(u["paragraphs"]))
                    if not self.read_audio_path(u, i).exists()]
            if not todo:
                if made:
                    log.info("📚 Чтение: озвучено абзацев — %s, готово всё", made)
                return
            try:
                for u, i in todo:
                    if self.read_audio_path(u, i).exists():   # уже озвучен по кнопке «Слушать»
                        continue
                    await self.read_audio(u, i)
                    made += 1
                    fails = 0
                    await sleep(PREGEN_PAUSE)
            except Exception as e:
                delay = PREGEN_RETRY[min(fails, len(PREGEN_RETRY) - 1)]
                fails += 1
                log.warning("📚 Озвучка абзаца не удалась (%s: %s); осталось %s, повтор через %s мин",
                            type(e).__name__, str(e)[:200], len(todo), delay // 60)
                await sleep(delay)

    def read_unit(self, unit_id: str) -> dict | None:
        u = textbook.get_unit(unit_id)
        return u if u and u.get("reading") and u.get("paragraphs") else None

    def read_continue(self, user_id: int, book: dict, done: dict) -> tuple[tuple[dict, int] | None, bool]:
        """Куда «▶️ Продолжить»: первый непрочитанный, начиная с места, где остановился (сам этот абзац,
        если дальше не перешёл), по кругу; всё прочитано — следующий за местом. Второе — начинал ли читать."""
        seq = [(u, i) for u in book["units"] for i in range(len(u["paragraphs"]))]
        if not seq:
            return None, False
        pos = self.db.read_pos(user_id, book["id"])
        start = next((k for k, (u, i) in enumerate(seq) if pos and (u["unit"], i) == pos), 0)
        order = seq[start:] + seq[:start]
        nxt = next(((u, i) for u, i in order if i not in done.get(u["unit"], set())), None)
        return nxt or (order[1] if len(order) > 1 and pos else order[0]), pos is not None

    # ---------- экраны ----------

    async def read_menu(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        books = self.read_books()
        if not books:
            await self.tg.send_message(chat_id, "📚 Книг для чтения пока нет.")
            return
        if len(books) == 1:
            await self.read_book(chat_id, user_id, books[0]["id"], message_id)
            return
        text, buttons = fmt.read_books_screen(books, self.db.read_done(user_id))
        await self.send_or_edit(chat_id, message_id, text, buttons)

    async def read_book(self, chat_id: int, user_id: int, book_id: str, message_id: int | None = None) -> None:
        books = self.read_books()
        book = next((b for b in books if b["id"] == book_id), None)
        if not book:
            return
        done = self.db.read_done(user_id)
        cont, started = self.read_continue(user_id, book, done)
        text, buttons = fmt.read_book_screen(book, done, [u for b in books for u in b["units"]], cont, started,
                                             len(books) > 1)
        await self.send_or_edit(chat_id, message_id, text, buttons)

    async def send_or_edit(self, chat_id: int, message_id: int | None, text: str, buttons) -> None:
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    async def read_show(self, chat_id: int, user_id: int, unit: dict, idx: int, from_mid: int | None = None,
                        from_idx: int | None = None) -> None:
        """Абзац новым сообщением (прочитанное остаётся в чате); у прошлого абзаца кнопки перехода убираются."""
        idx = max(0, min(idx, len(unit["paragraphs"]) - 1))
        if from_idx is not None and from_idx == idx - 1:   # «➡️ Дальше» — прошлый абзац прочитан
            self.db.read_mark(user_id, unit["unit"], from_idx)
        self.db.read_seen(user_id, unit["unit"], idx)
        done = self.db.read_done(user_id)
        books = self.read_books()
        book = next(b for b in books if b["id"] == unit["book"])
        book_pct = fmt.pct(*fmt.read_counts(book["units"], done))
        all_pct = fmt.pct(*fmt.read_counts([u for b in books for u in b["units"]], done)) if len(books) > 1 else None
        if from_mid:   # у прошлого абзаца остаются 🔊 📖 ❓, переход — только из нового сообщения
            keep = fmt.read_paragraph(unit, from_idx, 0, None)[1][:2] if from_idx is not None \
                and 0 <= from_idx < len(unit["paragraphs"]) else []
            await self.tg.edit_markup(chat_id, from_mid, keep)
        text, buttons = fmt.read_paragraph(unit, idx, book_pct, all_pct)
        await self.tg.send_message(chat_id, text, buttons)

    # ---------- кнопки rd: ----------

    async def read_callback(self, chat_id: int, user_id: int, message_id: int, arg: str) -> str | None:
        """Возвращает текст всплывашки или None."""
        kind, _, rest = arg.partition(":")
        if kind == "l":
            await self.read_menu(chat_id, user_id, message_id)
            return None
        if kind == "b":
            await self.read_book(chat_id, user_id, rest, message_id)
            return None
        if kind == "c":
            await self.read_book(chat_id, user_id, rest)
            return None
        parts = rest.split("|")
        unit = self.read_unit(parts[0])
        if not unit:
            return "Глава не найдена"
        n = len(unit["paragraphs"])
        idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if kind != "o" and kind not in ("e", "t") and not 0 <= idx < n:
            return "Абзац не найден"
        if kind == "o":   # открыть главу: первый непрочитанный абзац (всё прочитано — с начала)
            done = self.db.read_done(user_id).get(unit["unit"], set())
            await self.read_show(chat_id, user_id, unit, next((i for i in range(n) if i not in done), 0))
        elif kind == "g":
            frm = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            await self.read_show(chat_id, user_id, unit, idx, from_mid=message_id, from_idx=frm)
        elif kind == "a":
            await self.tg.send_action(chat_id, "record_voice")
            try:
                ogg = await self.read_audio(unit, idx)
            except Exception:
                log.exception("Озвучка абзаца")
                return "🔇 Озвучка не получилась — попробуй позже"
            await self.tg.send_voice(chat_id, ogg)
        elif kind == "w":
            text, buttons = fmt.read_words(unit, idx, self.read_in_dict(user_id, unit, idx))
            await self.tg.send_message(chat_id, text, buttons)
        elif kind in ("s", "S"):
            return await self.read_star(chat_id, user_id, message_id, unit, idx, parts, kind == "S")
        elif kind == "q":
            await self.show(chat_id, user_id, {"step": "read_q", "unit": unit["unit"], "idx": idx},
                            f"❓ Напиши вопрос по абзацу {idx + 1} — по-русски, можно про слово, форму или смысл. "
                            "Например: <i>почему nad Wisłą, а не nad Wisła?</i>", prev=None)
        elif kind == "e":   # 🏁 Конец главы — последний абзац прочитан
            self.db.read_mark(user_id, unit["unit"], n - 1)
            await self.tg.edit_markup(chat_id, message_id)
            units = next(b for b in self.read_books() if b["id"] == unit["book"])["units"]
            k = next(i for i, u in enumerate(units) if u["unit"] == unit["unit"])
            text, buttons = fmt.read_end(unit, units[k + 1] if k + 1 < len(units) else None)
            await self.tg.send_message(chat_id, text, buttons)
        elif kind == "t":   # 🎯 слова главы — тот же экран, что у юнита учебника (карточки, тест, пропуски)
            await self.book_unit_screen(chat_id, user_id, {"kind": "book"}, unit)
        return None

    def read_in_dict(self, user_id: int, unit: dict, idx: int) -> set[str]:
        return {w["pl"].lower() for w in unit["paragraphs"][idx].get("words") or []
                if self.db.dict_has(user_id, w["pl"])}

    async def read_star(self, chat_id: int, user_id: int, message_id: int, unit: dict, idx: int, parts: list,
                        all_words: bool) -> str:
        words = unit["paragraphs"][idx].get("words") or []
        if all_words:
            added = sum(self.db.dict_add(user_id, w["pl"], "", w["ru"]) for w in words)
            note = f"⭐ Добавлено в словарь: {added}"
        else:
            k = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
            if not 0 <= k < len(words):
                return "Слово не найдено"
            w = words[k]
            if self.db.dict_has(user_id, w["pl"]):
                self.db.dict_remove(user_id, w["pl"])
                note = f"Убрано из словаря: {w['pl']}"
            else:
                self.db.dict_add(user_id, w["pl"], "", w["ru"])
                note = f"⭐ В словаре: {w['pl']} — {w['ru']}"
        _, buttons = fmt.read_words(unit, idx, self.read_in_dict(user_id, unit, idx))
        await self.tg.edit_markup(chat_id, message_id, buttons)
        return note

    async def read_question(self, chat_id: int, user_id: int, pending: dict, question: str) -> None:
        self.db.set_pending(user_id, None)
        unit = self.read_unit(pending.get("unit", ""))
        idx = int(pending.get("idx", 0))
        if not unit or not 0 <= idx < len(unit["paragraphs"]):
            await self.tg.send_message(chat_id, "Абзац не найден — открой его снова: /read")
            return
        p = unit["paragraphs"][idx]
        data = await self.ask(chat_id, read_question_prompt(p["pl"], p["ru"], question, self.cfg.level),
                              RULES_SCHEMA, RULES_HINT)
        if data is not None:
            await self.tg.send_message(chat_id, fmt.rules_message(data))
