"""Упражнения: меню /ex и выбор (вид, формат, тема, слова, количество), ввод ответа."""
import re
from html import escape

from ..core import rules, textbook, training
from ..ai.prompt import (RULES_HINT, RULES_SCHEMA, WORDS_HINT, WORDS_SCHEMA, rules_by_name_prompt,
                         rules_for_errors_prompt, topic_words_prompt)
from ..ui import fmt
from ..exercises import logic as exercises
from ..settings import EX_ERR_DAYS, EX_TOP_ERRORS
from ..common import QUESTION_RE


class ExerciseMenuMixin:
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
            await self.tg.send_message(chat_id, "⏹ Упражнения закончены.")
            await self.main_menu(chat_id, user_id)
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
