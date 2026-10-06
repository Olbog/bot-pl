"""📘 Учебник: карточки и тест по словам юнита, статистика юнита."""
import random

from ..core import textbook
from ..ai.prompt import BOOK_OPTS_HINT, BOOK_OPTS_SCHEMA, book_options_prompt
from ..ui import fmt
from ..exercises import logic as exercises


class BookMixin:
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
