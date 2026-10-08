"""Наборы слов: экран /set, создание (тема, свои слова, юнит, ошибки, словарь), 📚 архив, предпросмотр.

Набор — 10 слов или правил, к которым бот подводит в разговоре. Активный набор один. Когда берёшь новый,
старый уходит в 📚 архив целиком, с прогрессом; из архива его можно вернуть и продолжить с того же места."""
import random
from html import escape

from .core import rules, textbook, training
from .ai.gemini import GeminiError
from .ai.prompt import RULES_HINT, RULES_SCHEMA, WORDS_HINT, WORDS_SCHEMA, rules_by_name_prompt, topic_words_prompt
from .ui import fmt
from .common import AUTO, SET_MENU


class SetsMixin:
    def set_rows(self, set_id: int) -> list[tuple]:
        return [(w, training.stats(self.db.word_uses(w["id"]))) for w in self.db.set_words(set_id)]

    async def show_set(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        mode = self.db.get_state(user_id)["mode"]
        active = self.db.active_set(user_id)
        archived = len(self.db.sets_archive(user_id))
        if not active:
            text = ("📚 Набора пока нет.\n\nСоставь его по теме (я подберу 10 слов), из своих слов, из юнита "
                    "учебника, из своих ошибок или из словаря ⭐ — и я буду строить разговор так, чтобы ты "
                    "говорил это как можно чаще и в разных формах.")
        else:
            text = fmt.set_progress(active["title"], self.set_rows(active["id"]), self.criteria, mode,
                                    self.rule_criteria)
        buttons = fmt.set_buttons(bool(active), archived)
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    def set_src_prev(self, user_id: int):
        """«Назад» с первого шага создания набора: в /new, если пришли оттуда, иначе в /set."""
        cur = self.db.get_state(user_id)["pending"]
        return AUTO if cur and cur.get("step") == "new_src" else SET_MENU

    async def build_preview(self, chat_id: int, user_id: int, pending: dict, prompt: str) -> None:
        await self.tg.send_action(chat_id, "typing")
        try:
            data = await self.gemini.ask_json(prompt, WORDS_SCHEMA, WORDS_HINT)
        except GeminiError as e:
            await self.report_gemini_error(chat_id, e)
            return
        new = [w for w in data.get("words") or [] if isinstance(w, dict) and str(w.get("pl", "")).strip()]
        known = {w["pl"].lower() for w in pending.get("words", [])}
        new = [w for w in new if w["pl"].strip().lower() not in known]
        if not new and not pending.get("words"):
            await self.tg.send_message(chat_id, "Не получилось составить слова, попробуй ещё раз или другую тему.")
            return
        pending = {k: v for k, v in pending.items() if k not in ("prev", "screen")}
        pending = {**pending, "step": "preview", "words": pending.get("words", []) + new}
        pending.setdefault("title", str(data.get("title") or "Свои слова"))
        pending.setdefault("off", [])
        await self.show(chat_id, user_id, pending, fmt.preview_message(pending), fmt.preview_buttons(pending))

    async def preview_from_dict(self, chat_id: int, user_id: int, prev=AUTO) -> None:
        n = self.cfg.set_size
        items = self.db.dict_items(user_id, only_unused=True)[:n]
        if not items:
            await self.tg.send_message(chat_id, "⭐ В словаре нет новых выражений для набора. "
                                                 "Сохраняй их кнопкой «⭐ В словарь» под ответами или /dict add.")
            return
        pending = {"step": "preview", "title": "Из словаря", "off": [], "dict_ids": [i["id"] for i in items],
                   "words": [{"pl": i["pl"], "translit": i["translit"], "ru": i["ru"], "pos": "выраж",
                              "kind": "phrase"} for i in items]}
        await self.show(chat_id, user_id, pending, fmt.preview_message(pending), fmt.preview_buttons(pending),
                        prev=prev)

    # ---------- 📘 набор из юнита ----------

    def unit_candidates(self, user_id: int, unit: dict, exclude: set[str]) -> list[dict]:
        """Невыученные слова юнита: сначала с ошибками, потом реже тренированные."""
        stats = self.db.book_stats(user_id, unit["unit"])
        out = []
        for w, _ in textbook.pick_words(unit, stats, len(unit["words"]), "ru"):
            if textbook.known(stats, w["pl"]) or w["pl"].lower() in exclude:
                continue
            out.append({"pl": w["pl"], "translit": w.get("translit", ""), "ru": w["ru"], "pos": w.get("pos", ""),
                        "kind": "word", "unit": unit["unit"]})
        return out

    async def set_units(self, chat_id: int, user_id: int) -> None:
        await self.pick_unit(chat_id, user_id, "su", prev=self.set_src_prev(user_id))

    async def preview_from_unit(self, chat_id: int, user_id: int, unit_id: str) -> None:
        unit = textbook.get_unit(unit_id)
        if not unit:
            return
        words = self.unit_candidates(user_id, unit, set())[:self.cfg.set_size]
        if not words:
            await self.tg.send_message(chat_id, f"🎉 В {escape(unit['name'])} все слова уже выучены.")
            return
        pending = {"step": "preview", "title": f"{unit['book_short']} {unit['name']} — {unit['title']}", "unit": unit["unit"],
                   "off": [], "words": words}
        await self.show(chat_id, user_id, pending, fmt.preview_message(pending), fmt.preview_buttons(pending))

    # ---------- 📚 архив наборов ----------

    async def show_archive(self, chat_id: int, user_id: int, message_id: int | None = None, prev=AUTO) -> None:
        rows = self.db.sets_archive(user_id)
        if not rows:
            await self.tg.send_message(chat_id, "📚 Архив пуст — сюда попадают прошлые наборы, когда берёшь новый.")
            return
        text, buttons = fmt.archive_screen(rows, self.cfg.next_set_ratio, self.clock())
        await self.show(chat_id, user_id, {"step": "archive"}, text, buttons, message_id, prev=prev)

    async def archive_action(self, chat_id: int, user_id: int, message_id: int, pending: dict | None,
                             arg: str) -> None:
        if arg == "list":
            await self.show_archive(chat_id, user_id, prev=SET_MENU)
        elif arg.startswith("go:"):
            st = self.db.get_set(int(arg[3:]))
            if not st or st["user_id"] != user_id:
                return
            self.db.activate_set(user_id, st["id"])
            await self.start_set_conversation(chat_id, user_id, message_id)
        elif arg.isdigit():
            st = self.db.get_set(int(arg))
            if not st or st["user_id"] != user_id or not pending or pending.get("step") != "archive":
                return
            text = fmt.set_progress(st["title"], self.set_rows(st["id"]), self.criteria, "archive",
                                    self.rule_criteria)
            await self.show(chat_id, user_id, {"step": "arch_set"}, text,
                            [[("▶️ Продолжить тренировку", f"ar:go:{st['id']}")]], message_id)

    async def preview_action(self, chat_id: int, user_id: int, message_id: int, pending: dict | None,
                             action: str) -> None:
        if not pending or pending.get("step") not in ("preview", "add"):
            await self.tg.edit_message(chat_id, message_id, "Этот черновик набора уже неактуален. /set")
            return
        if action == "cancel":
            self.db.set_pending(user_id, None)
            await self.tg.edit_message(chat_id, message_id, "✖️ Создание набора отменено.")
            await self.main_menu(chat_id, user_id)
        elif action == "add":
            await self.show(chat_id, user_id, {**pending, "step": "add"},
                            "Пришли слова, которые добавить (через запятую или столбиком).")
        elif action == "regen" and pending.get("unit"):  # юнит: следующие невыученные слова
            unit = textbook.get_unit(pending["unit"])
            off = set(pending.get("off", []))
            kept = [w for i, w in enumerate(pending["words"]) if i not in off]
            shown = {w["pl"].lower() for w in pending["words"]}
            more = self.unit_candidates(user_id, unit, shown)[:self.cfg.set_size - len(kept)] if unit else []
            if not more:
                await self.tg.send_message(chat_id, "Других невыученных слов в юните нет.")
                return
            new = {**pending, "words": kept + more, "off": []}
            await self.show(chat_id, user_id, new, fmt.preview_message(new), fmt.preview_buttons(new), message_id)
        elif action == "regen":
            n = max(3, self.cfg.set_size - len(pending["words"]) + len(pending.get("off", [])))
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
            if not words:
                await self.tg.send_message(chat_id, "В наборе не осталось слов.")
                return
            self.db.create_set(user_id, pending.get("title") or "Набор", words)
            if pending.get("dict_ids"):
                kept_pl = {w["pl"].lower() for w in words}
                self.db.dict_mark_used([i for i, w in zip(pending["dict_ids"], pending["words"])
                                        if w["pl"].lower() in kept_pl])
            self.db.set_pending(user_id, None)
            await self.tg.edit_message(chat_id, message_id, fmt.preview_message(
                {**pending, "words": words, "off": []}).replace(
                "<i>Нажми на слово, чтобы убрать или вернуть его.</i>", "✅ <b>Набор сохранён. Начинаем!</b>"))
            await self.start_set_conversation(chat_id, user_id)
        elif action.isdigit():
            i = int(action)
            off = set(pending.get("off", []))
            off ^= {i}
            pending = {**pending, "off": sorted(off)}
            await self.show(chat_id, user_id, pending, fmt.preview_message(pending),
                            fmt.preview_buttons(pending), message_id)

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
                                   prev=AUTO if same else self.set_src_prev(user_id))
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
            items = [{"pl": r["rule"], "translit": "", "ru": r["examples"], "pos": "правило", "kind": "rule"}
                     for r in chosen]
            title = "Ошибки: " + ", ".join(r["rule"].split(" (")[0] for r in chosen)[:60]
            self.db.create_set(user_id, title, items)
            self.db.set_pending(user_id, None)
            await self.tg.edit_message(chat_id, message_id, fmt.errsel_message(pending).split("\n\n")[0]
                                       + "\n\n✅ <b>Набор сохранён. Начинаем!</b>\n"
                                       + "\n".join(f"📐 {escape(r['rule'])}" for r in chosen))
            if pending.get("rule_first", True):
                data = await self.ask(chat_id, rules_by_name_prompt(
                    [r["rule"] for r in chosen], {r["rule"]: r["examples"].split("; ") for r in chosen},
                    self.cfg.level), RULES_SCHEMA, RULES_HINT)
                if data is not None:
                    await self.tg.send_message(chat_id, fmt.rules_message(data))
            await self.start_set_conversation(chat_id, user_id)
