"""📘 Учебник: карточки и тест по словам юнита, статистика юнита; 📖 /words — список слов юнита."""
import random
from html import escape

from ..common import AUTO
from ..core import textbook
from ..ai.prompt import BOOK_OPTS_HINT, BOOK_OPTS_SCHEMA, book_options_prompt
from ..ui import fmt
from ..exercises import logic as exercises
from ..settings import WORDS_VOICE_CHUNK


# purpose → (шаг, префикс кнопок юнита, заголовок)
UNIT_PICK = {"ex": ("ex_book", "x:b:u:", "📘 <b>Учебник</b> — какой юнит тренируем?"),
             "wd": ("words_units", "wd:u:", "📖 <b>Слова юнита</b> — какой юнит?"),
             "su": ("set_unit", "su:", "📘 <b>Набор из юнита</b> — какой юнит?"),
             "ph": ("ex_book", "x:b:u:", "💬 <b>Полезные выражения</b> — какой цикл?")}
STANDALONE_OK = ("su", "ph")   # «отдельные» книги (выражения) — только здесь, не в списке учебников


class BookMixin:
    async def book_items(self, chat_id: int, user_id: int, ex: dict, idx: int) -> tuple[list, str] | None:
        """Карточки и тест по словам юнита. Карточки — без Gemini; для теста Gemini подбирает
        2 неверных варианта, близких по смыслу."""
        unit = textbook.get_unit(ex.get("unit", ""))
        if not unit:
            await self.tg.send_message(chat_id, "Юнит не найден. /ex → 📘 Учебник")
            return None
        stats = self.db.book_stats(user_id, unit["unit"])
        first = [tuple(x) for x in ex.get("redo_words") or []]   # «🔁 Повторить»: ошибки прошлого упражнения
        items = [textbook.card_item(w, d)
                 for w, d in textbook.pick_words(unit, stats, 10, ex.get("dir", "mix"), first=first)]
        for it in items:
            it["unit"] = unit["unit"]
            it["_key"] = f"card:{unit['unit']}:{it['dir']}:{it['lemma']}:{self.clock()}"
        mode = "Карточки" if ex["kind"] == "book_card" else "Тест"
        title = f"{unit['name']} «{unit['title']}» — {mode}, {textbook.DIRS.get(ex.get('dir', 'mix'), '')}"
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
        """Ответы по словам учебника — только в статистику юнита, не в общий пул ошибок.
        Тест (выбор из вариантов) идёт в «🟡 узнаю», написанное самому — и в «✅ знаю»."""
        typed = saved.get("fmt") != "test"
        for r in results:
            it = saved["items"][r["n"] - 1]
            unit = it.get("unit")
            if not unit:
                continue
            if ":" not in str(unit):  # упражнение до перехода на «книга:номер»
                unit = (textbook.get_unit(unit) or {}).get("unit", unit)
            if it.get("card"):
                pl, d = it["lemma"], it["dir"]
            else:  # пропуски: найти слово юнита по словарной форме
                u = textbook.get_unit(unit)
                unit = (u or {}).get("unit", unit)
                lemma = str(it.get("lemma", "")).strip().lower()
                w = next((w for w in (u or {}).get("words", []) if w["pl"].lower() == lemma), None)
                if not w:
                    continue
                pl, d = w["pl"], "ru"
            self.db.book_record(user_id, unit, pl, d, r["final"] != "wrong", typed)

    async def book_unit_screen(self, chat_id: int, user_id: int, ex: dict, unit: dict,
                               message_id: int | None = None) -> None:
        done, recog, total = textbook.unit_progress(unit, self.db.book_stats(user_id, unit["unit"]))
        text, buttons = fmt.book_unit_screen(unit, done, recog, total)
        await self.show(chat_id, user_id, {"step": "ex_bunit", "ex": {**ex, "unit": unit["unit"]}}, text, buttons,
                        message_id)

    # ---------- 📖 /words: слова юнита ----------

    def unit_known(self, user_id: int, unit: dict) -> tuple[set[str], set[str]]:
        """(✅ знаю, 🟡 узнаю) — множества слов юнита."""
        stats = self.db.book_stats(user_id, unit["unit"])
        return ({w["pl"] for w in unit["words"] if textbook.known(stats, w["pl"])},
                {w["pl"] for w in unit["words"] if textbook.recognized(stats, w["pl"])})

    async def phrases_menu(self, chat_id: int, user_id: int) -> None:
        """💬 Полезные выражения: части — как юниты (список, карточки, тест, пропуски, набор для разговора)."""
        await self.pick_unit(chat_id, user_id, "ph", {"ex": {"kind": "book"}}, prev=None)

    async def words_menu(self, chat_id: int, user_id: int) -> None:
        await self.pick_unit(chat_id, user_id, "wd", prev=None)

    # ---------- выбор учебника → юнита (общий для 📘 упражнений, /words и набора из юнита) ----------

    async def pick_unit(self, chat_id: int, user_id: int, purpose: str, extra: dict | None = None, prev=AUTO,
                        book_id: str | None = None, message_id: int | None = None) -> None:
        """Список юнитов; если учебников с юнитами несколько — сначала выбор учебника (кнопки bk:<purpose>:<книга>)."""
        step, prefix, title = UNIT_PICK[purpose]
        books = [b for b in textbook.load_books() if b["units"] and book_id in (None, b["id"])
                 and (not b.get("standalone") or purpose in STANDALONE_OK)
                 and (purpose != "ph" or b.get("phrases"))]
        if not books:
            await self.tg.send_message(chat_id, "📘 Юнитов пока нет. Пришли страницы учебника в чат с Claude — "
                                                 "он добавит слова, и после обновления бота они появятся здесь.")
            return
        extra = extra or {}
        if len(books) == 1:
            b = books[0]
            head = (f"🧱 Блок «{escape(b['title'])}»" if b.get("phrases") else f"📚 {escape(b['title'])}")
            text, buttons = fmt.book_units_screen(b["units"], prefix, f"{title}\n{head}")
            await self.show(chat_id, user_id, {"step": step, **extra}, text, buttons, message_id, prev=prev)
        else:
            what = "какой блок?" if purpose == "ph" else "какой учебник?"
            text, buttons = fmt.books_screen(books, purpose, f"{title.split(' — ')[0]} — {what}")
            await self.show(chat_id, user_id, {"step": f"books_{purpose}", **extra}, text, buttons, message_id,
                            prev=prev)

    async def book_chosen(self, chat_id: int, user_id: int, message_id: int, pending: dict | None, arg: str) -> None:
        """bk:<purpose>:<книга> — учебник выбран, показать его юниты (тем же сообщением)."""
        purpose, _, book_id = arg.partition(":")
        if purpose not in UNIT_PICK or (pending or {}).get("step") != f"books_{purpose}":
            return
        extra = {k: v for k, v in pending.items() if k not in ("step", "prev", "screen")}
        await self.pick_unit(chat_id, user_id, purpose, extra, book_id=book_id, message_id=message_id)

    async def words_callback(self, chat_id: int, user_id: int, arg: str) -> None:
        kind, _, unit_id = arg.partition(":")
        unit = textbook.get_unit(unit_id)
        if not unit:
            return
        known, recog = self.unit_known(user_id, unit)
        if kind == "u":  # список с переводом
            self.db.set_pending(user_id, None)
            text, buttons = fmt.words_list(unit, known, recog)
            await self.tg.send_message(chat_id, text, buttons)
        elif kind == "h":  # скрытый перевод: номера-кнопки
            for text, buttons in fmt.words_hidden(unit, known, recog):
                await self.tg.send_message(chat_id, text, buttons)
        elif kind == "f":
            await self.tg.send_document(chat_id, f"{unit['book']}_{unit['num']}_slova.txt",
                                        fmt.words_file(unit).encode("utf-8"),
                                        f"📄 {unit['name']} — {unit['title']}")
        elif kind == "v":
            await self.words_voice(chat_id, unit)

    async def words_voice(self, chat_id: int, unit: dict) -> None:
        """🔊 Озвучка слов юнита: польское слово, пауза; по темам, части не длиннее WORDS_VOICE_CHUNK."""
        words = unit["words"]
        for grp, k, part in textbook.topic_parts(words, WORDS_VOICE_CHUNK):
            topic = f" · 🗂 {grp}{f' ({k})' if k > 1 else ''}" if grp else ""
            await self.tg.send_message(chat_id, f"🔊 {unit['name']}: слова {part[0][0]}–{part[-1][0]} "
                                                 f"из {len(words)}{escape(topic)}")
            await self.tg.send_action(chat_id, "record_voice")
            try:
                ogg = await self.tts(" ... ".join(w["pl"].rstrip("?!.") for _, w in part) + ".",
                                     self.cfg.tts_voice, self.cfg.tts_rate)
            except Exception:
                await self.tg.send_message(chat_id, "🔇 Озвучка не получилась — попробуй позже.")
                return
            await self.tg.send_voice(chat_id, ogg)

    def words_hint(self, data: str) -> str:
        """wd:s:<unit>|<n> — перевод слова n (всплывашка)."""
        unit_id, _, n = data[5:].rpartition("|")
        unit = textbook.get_unit(unit_id)
        if not unit or not 1 <= int(n) <= len(unit["words"]):
            return "Слово не найдено"
        return f"{n}. " + fmt.word_hint(unit["words"][int(n) - 1])
