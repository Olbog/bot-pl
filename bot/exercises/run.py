"""Упражнения: составление, отправка, интерактивный тест, проверка, запись результатов, оспаривание."""
import random

from ..core import rules, textbook
from ..ai.prompt import (CHECK_HINT, CHECK_SCHEMA, EX_HINT, EX_SCHEMA, RULES_HINT, RULES_SCHEMA, check_prompt,
                         ex_prompt, ex_question_prompt)
from ..ui import fmt
from ..exercises import logic as exercises
from ..settings import EX_CASES, EX_EXPLAIN_ALL, EX_PRESENT_ONLY, EX_QUIZ_BUTTONS, EX_REUSE, EX_WEIGHT


class ExerciseRunMixin:
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
                                          fmt.ex_hint_rows(saved) + [[("✖️ Закончить без проверки", "nav:c:ex_answer")]])
        if sent and sent.get("message_id"):
            self.db.ex_set_tg_msg(ex_id, sent["message_id"])

    def ex_hint(self, user_id: int, data: str) -> str:
        """x:h:<ex>:<n> — перевод пропущенного слова пункта n (всплывашка только для этого пункта)."""
        _, _, ex_id, n = data.split(":")
        saved = self.db.ex_get(int(ex_id))
        if not saved or saved["user_id"] != user_id or not 1 <= int(n) <= len(saved["items"]):
            return "Упражнение не найдено"
        hint = fmt.hint_of(saved["items"][int(n) - 1])
        return f"💡{n}: {hint}" if hint else "Для этого пункта подсказки нет"

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
