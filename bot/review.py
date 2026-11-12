"""🔁 Повторение: выбранные юниты одного или нескольких учебников (части — учебник 📗 / рабочая тетрадь 📒).

Экран повторения: 📖 правила (выжимка по юнитам + ❓ диалог с Gemini по конкретному правилу), 🏋️ сложные упражнения
из книги (по 2–3 из каждого юнита и каждой части), 🔤 лексика юнитов (карточки, тест, пропуски), 🤖 упражнения от Gemini
по грамматике юнитов. Прогресс повторения отдельный: упражнения из книги хранятся под ключом «rev:kpk:10», слова —
под «rev:kpk:9,10@tb,wb» (textbook.review_unit) — результаты в самих юнитах не меняются.

Выжимка правил: готовая — в файле юнита ("review": {"rules": [...], "hard": [id упражнений]}), её пишет Claude;
нет — составляет Gemini (review_rules_prompt) и кэширует в rule_cache.
Состояние — review_state: {"units": [...], "parts": [...], "queue": [[юнит, упражнение], ...]}.
"""
import hashlib
import json

from .ai.prompt import QA_HINT, QA_SCHEMA, RULES_HINT, RULES_SCHEMA, review_qa_prompt, review_rules_prompt
from .core import textbook
from .ui import fmt

PARTS = ("tb", "wb")
HARD_TYPES = {"gap": 2, "open": 2, "choice": 1, "match": 1}   # tf и free в «сложные» не берём
QA_HISTORY = 8                                                # реплик диалога по правилу, которые помнит бот


def review_books() -> list[dict]:
    """Учебники для повторения (без 💬 полезных выражений и 📚 книг для чтения)."""
    return [b for b in textbook.load_books() if b["units"] and not b.get("phrases") and not b.get("reading")]


def hard_pick(unit: dict, parts: list[str], errors: dict[str, int]) -> list[str]:
    """Самые сложные упражнения юнита: по 2–3 из каждой части. Сначала те, где у ученика были ошибки, потом
    отмеченные Claude (review.hard), потом по виду (ввод сложнее кнопок) и длине. Две половины одного упражнения
    («a4-1», «a4-2») вместе не берём."""
    marked = set((unit.get("review") or {}).get("hard") or [])
    order = {x["id"]: i for i, x in enumerate(unit.get("exercises") or [])}
    out = []
    for part in parts:
        cand = [x for x in unit.get("exercises") or []
                if x.get("src", "tb") == part and x.get("type", "gap") in HARD_TYPES and x.get("items")]
        cand.sort(key=lambda x: -(errors.get(x["id"], 0) * 10 + (5 if x["id"] in marked else 0)
                                  + HARD_TYPES[x.get("type", "gap")] + min(len(x["items"]), 20) / 10))
        k = 3 if len(cand) >= 6 else min(2, len(cand))
        took, bases = [], set()
        for x in cand:
            base = "-".join(x["id"].split("-")[:3])   # «tb-11-a4-1» и «tb-11-a4-2» — одно упражнение
            if base not in bases:
                bases.add(base)
                took.append(x["id"])
            if len(took) >= k:
                break
        out += sorted(took, key=lambda i: order[i])
    return out


class ReviewMixin:
    # ---------- выбор: учебники, части, юниты ----------

    def review_default(self, user_id: int) -> dict:
        saved = self.db.review_get(user_id)
        if saved and saved.get("units"):
            return {"books": sorted({u.split(":")[0] for u in saved["units"]}), "parts": saved.get("parts") or list(PARTS),
                    "units": saved["units"]}
        books = review_books()
        recent = [u for u in self.db.recent_units(user_id, 30) if not u.startswith(textbook.REVIEW)]
        book = next((u.split(":")[0] for u in recent), books[0]["id"] if books else "kpk")
        sel = {"books": [book], "parts": list(PARTS), "units": []}
        sel["units"] = self.review_last3(user_id, sel)
        return sel

    def review_last3(self, user_id: int, sel: dict) -> list[str]:
        """Последние 3 юнита, с которыми работал (в выбранных учебниках); не работал — первые три."""
        ids = [u["unit"] for b in review_books() if b["id"] in sel["books"] for u in b["units"]]
        recent = [u for u in self.db.recent_units(user_id, 50) if u in ids]
        if not recent:
            return ids[:3]
        top = sorted(recent[:3], key=ids.index)
        return top

    def review_done_units(self, user_id: int, sel: dict) -> list[str]:
        ids = [u["unit"] for b in review_books() if b["id"] in sel["books"] for u in b["units"]]
        seen = set(self.db.recent_units(user_id, 500))
        return [u for u in ids if u in seen]

    async def review_menu(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        sel = self.review_default(user_id)
        await self.show(chat_id, user_id, {"step": "rv_sel", "sel": sel}, *fmt.review_sel_screen(review_books(), sel),
                        message_id, prev=None)

    async def review_sel(self, chat_id: int, user_id: int, message_id: int, pending: dict | None, arg: str) -> None:
        sel = dict((pending or {}).get("sel") or self.review_default(user_id))
        kind, _, val = arg.partition(":")
        if kind == "b":
            books = set(sel["books"]) ^ {val}
            if not books:
                return
            sel["books"] = sorted(books)
            sel["units"] = [u for u in sel["units"] if u.split(":")[0] in books]
        elif kind == "p":
            parts = set(sel["parts"]) ^ {val}
            if not parts:
                return
            sel["parts"] = [p for p in PARTS if p in parts]
        elif kind == "u":
            ids = [u["unit"] for b in review_books() for u in b["units"]]
            sel["units"] = sorted(set(sel["units"]) ^ {val}, key=lambda u: ids.index(u) if u in ids else 999)
        elif kind == "last":
            sel["units"] = self.review_last3(user_id, sel)
        elif kind == "all":
            sel["units"] = self.review_done_units(user_id, sel)
        elif kind == "none":
            sel["units"] = []
        await self.show(chat_id, user_id, {"step": "rv_sel", "sel": sel}, *fmt.review_sel_screen(review_books(), sel),
                        message_id, prev=None)

    async def review_start(self, chat_id: int, user_id: int, message_id: int | None, pending: dict | None) -> None:
        sel = (pending or {}).get("sel") or self.review_default(user_id)
        units = [u for u in sel["units"] if textbook.get_unit(u)]
        if not units:
            await self.tg.send_message(chat_id, "Отметь хотя бы один юнит.")
            return
        queue = []
        for uid in units:
            u = textbook.get_unit(uid)
            queue += [[uid, x] for x in hard_pick(u, sel["parts"], self.db.unit_errors(user_id, uid))]
        self.db.review_set(user_id, {"units": units, "parts": sel["parts"], "queue": queue})
        self.db.set_pending(user_id, None)
        await self.review_home(chat_id, user_id, message_id)

    # ---------- экран повторения ----------

    def review_state(self, user_id: int) -> dict | None:
        st = self.db.review_get(user_id)
        return st if st and st.get("units") else None

    def review_queue(self, user_id: int, st: dict) -> list[tuple[dict, dict, tuple[int, int], bool]]:
        """[(юнит повторения, упражнение, (верно, всего), выполнено)] — по очереди сложных упражнений."""
        out = []
        for uid, ex_id in st.get("queue") or []:
            unit = textbook.get_unit(textbook.REVIEW + uid)
            x = next((e for e in (unit or {}).get("exercises") or [] if e["id"] == ex_id), None)
            if not x:
                continue
            done = self.db.bex_results(user_id, unit["unit"])
            ok, _ = fmt.bex_counts(x, done)
            out.append((unit, x, (ok, len(x["items"])), fmt.bex_complete(x, done)))
        return out

    async def review_home(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        st = self.review_state(user_id)
        if not st:
            await self.review_menu(chat_id, user_id, message_id)
            return
        units = [textbook.get_unit(u) for u in st["units"]]
        lex = textbook.get_unit(textbook.review_id(st["units"], st["parts"]))
        stats = self.db.book_stats(user_id, lex["unit"]) if lex else {}
        known = sum(textbook.known(stats, w["pl"]) for w in (lex or {}).get("words") or [])
        text, buttons = fmt.review_home([u for u in units if u], st["parts"], self.review_queue(user_id, st),
                                        known, len((lex or {}).get("words") or []))
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    # ---------- 📖 правила ----------

    async def review_unit_rules(self, chat_id: int, unit: dict, parts: list[str]) -> list[dict]:
        """Правила юнита для повторения: готовая выжимка из файла юнита, иначе — Gemini (с кэшем)."""
        ready = (unit.get("review") or {}).get("rules")
        if ready:
            return ready
        h = hashlib.md5(json.dumps([unit.get("title"), unit.get("summary"), parts], ensure_ascii=False)
                        .encode()).hexdigest()[:10]
        key = f"review:{unit['unit']}:{','.join(parts)}:{h}"
        data = self.db.rule_cache_get(key)
        if data is None:
            data = await self.ask(chat_id, review_rules_prompt(unit, parts, self.cfg.level), RULES_SCHEMA, RULES_HINT,
                                  wait=f"⏳ Составляю выжимку правил: {unit['book_short']} {unit['num']}… (10–20 с)")
            if data is None:
                return []
            self.db.rule_cache_set(key, data)
        return [r for r in data.get("rules") or [] if isinstance(r, dict)]

    async def review_rules(self, chat_id: int, user_id: int) -> None:
        st = self.review_state(user_id)
        if not st:
            await self.review_menu(chat_id, user_id)
            return
        for uid in st["units"]:
            unit = textbook.get_unit(uid)
            if not unit:
                continue
            rules = await self.review_unit_rules(chat_id, unit, st["parts"])
            if rules:
                await self.tg.send_message(chat_id, *fmt.review_rules_message(unit, rules))
        await self.tg.send_message(chat_id, "📖 <i>Вопрос по правилу — кнопка «❓» под ним: отвечу по этому правилу, "
                                            "можно спрашивать подряд, текстом или голосом.</i>",
                                   [[("🏋️ Сложные упражнения", "rv:e"), ("🔁 К повторению", "rv:h")]])

    async def review_ask_start(self, chat_id: int, user_id: int, uid: str, i: int) -> None:
        st = self.review_state(user_id)
        unit = textbook.get_unit(uid)
        rules = await self.review_unit_rules(chat_id, unit, (st or {}).get("parts") or list(PARTS)) if unit else []
        if not 0 <= i < len(rules):
            return
        self.db.set_pending(user_id, {"step": "rv_q", "unit": uid, "i": i, "hist": []})
        await self.tg.send_message(
            chat_id, f"❓ <b>{fmt.e(rules[i].get('title', ''))}</b>\n\nСпрашивай про это правило — текстом или голосом. "
                     "Можно несколько вопросов подряд: я помню, о чём мы говорили.",
            [[("✖️ Закончить вопросы", "rv:x"), ("🔁 К повторению", "rv:h")]])

    async def review_answer(self, chat_id: int, user_id: int, pending: dict, text: str, voice: dict | None = None) -> None:
        """Ответ Gemini на вопрос по правилу; диалог продолжается (шаг rv_q остаётся)."""
        unit = textbook.get_unit(pending.get("unit", ""))
        st = self.review_state(user_id)
        rules = await self.review_unit_rules(chat_id, unit, (st or {}).get("parts") or list(PARTS)) if unit else []
        i = int(pending.get("i", -1))
        if not 0 <= i < len(rules):
            self.db.set_pending(user_id, None)
            return
        hist = [tuple(h) for h in pending.get("hist") or []]
        audio = None
        if voice:
            sec = voice.get("duration")
            if sec is not None and sec < 1:
                await self.tg.send_message(chat_id, "🎙 Запись пустая — повтори вопрос.")
                return
            audio = await self.tg.download_file(voice["file_id"])
        title = f"{unit['book_short']} {unit['num']} · {unit.get('title', '')}"
        data = await self.ask(chat_id, review_qa_prompt(rules[i], title, hist, text, self.cfg.level, voice=bool(audio)),
                              QA_SCHEMA, QA_HINT, wait="⏳ Думаю над ответом…", audio=audio)
        if data is None:
            return
        answer = str(data.get("answer") or "").strip() or "Не получилось ответить — спроси иначе."
        hist = (hist + [("user", text or "🎙 голосовой вопрос"), ("model", answer)])[-QA_HISTORY:]
        self.db.set_pending(user_id, {**pending, "hist": [list(h) for h in hist]})
        await self.tg.send_message(chat_id, fmt.review_answer(rules[i], answer, data.get("examples") or []),
                                   [[("✖️ Закончить вопросы", "rv:x"), ("📖 К правилам", "rv:r")],
                                    [("🔁 К повторению", "rv:h")]])

    # ---------- 🏋️ сложные упражнения из книги ----------

    async def review_hard(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        st = self.review_state(user_id)
        if not st:
            await self.review_menu(chat_id, user_id)
            return
        text, buttons = fmt.review_hard_screen(self.review_queue(user_id, st))
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    async def review_open(self, chat_id: int, user_id: int, i: int | None) -> None:
        """rv:o:<i> — открыть упражнение очереди; i None — следующее невыполненное."""
        st = self.review_state(user_id)
        q = self.review_queue(user_id, st) if st else []
        if i is None:
            i = next((k for k, (*_, complete) in enumerate(q) if not complete), None)
            if i is None:
                await self.tg.send_message(chat_id, "🎉 <b>Все сложные упражнения повторения сделаны!</b>",
                                           [[("🔁 Пройти заново", "rv:R")], [("🔁 К повторению", "rv:h")]])
                return
        if not 0 <= i < len(q):
            return
        unit, x, *_ = q[i]
        await self.bex_open(chat_id, user_id, unit, x)

    async def review_reset(self, chat_id: int, user_id: int) -> None:
        st = self.review_state(user_id)
        for unit, x, *_ in self.review_queue(user_id, st) if st else []:
            self.db.bex_reset(user_id, unit["unit"], x["id"])
        await self.review_hard(chat_id, user_id)

    # ---------- 🔤 лексика и 🤖 упражнения от Gemini ----------

    async def review_lex(self, chat_id: int, user_id: int, mode: str | None) -> None:
        st = self.review_state(user_id)
        if not st:
            await self.review_menu(chat_id, user_id)
            return
        rid = textbook.review_id(st["units"], st["parts"])
        unit = textbook.get_unit(rid)
        if not unit or not unit["words"]:
            await self.tg.send_message(chat_id, "В выбранных юнитах нет слов.")
            return
        if mode is None:
            stats = self.db.book_stats(user_id, rid)
            known = sum(textbook.known(stats, w["pl"]) for w in unit["words"])
            await self.tg.send_message(chat_id, *fmt.review_lex_screen(unit, known))
            return
        kind = {"pl": "book_card", "ru": "book_card", "mix": "book_card", "test": "book_test", "gap": "book_gap"}.get(mode)
        if not kind:
            return
        ex = {"kind": kind, "unit": rid, "review": True,
              "dir": mode if mode in ("pl", "ru", "mix") else "mix", "fmt": "test" if kind == "book_test" else "gap"}
        await self.ex_ask_count(chat_id, user_id, ex, f"🔤 Лексика: {fmt.e(unit['title'])} — {len(unit['words'])} слов")

    async def review_gemini(self, chat_id: int, user_id: int, f: str | None) -> None:
        st = self.review_state(user_id)
        if not st:
            await self.review_menu(chat_id, user_id)
            return
        titles = []
        for uid in st["units"]:
            unit = textbook.get_unit(uid)
            if unit:
                titles += [str(r.get("title") or "").strip() for r in await self.review_unit_rules(chat_id, unit, st["parts"])]
        titles = [t for t in dict.fromkeys(titles) if t]
        if not titles:
            await self.tg.send_message(chat_id, "Не получилось собрать темы юнитов — попробуй ещё раз.")
            return
        if f is None:
            await self.tg.send_message(chat_id, *fmt.review_gemini_screen(titles))
            return
        ex = {"kind": "grammar", "fmt": "test" if f == "test" else "gap", "rules": titles[:10], "review": True}
        await self.ex_ask_count(chat_id, user_id, ex, "🤖 Упражнения по темам юнитов:\n"
                                + "\n".join(f"• {fmt.e(t)}" for t in titles[:10]))

    # ---------- кнопки rv: ----------

    async def review_callback(self, chat_id: int, user_id: int, message_id: int, pending: dict | None, arg: str) -> None:
        """rv:s — выбор; rv:b/p/u/last/all/none — переключатели выбора; rv:go — начать; rv:h — экран повторения;
        rv:r — правила; rv:q:<юнит>:<i> — вопрос по правилу; rv:x — закончить вопросы; rv:e — сложные упражнения;
        rv:o:<i> / rv:n — открыть / следующее; rv:R — пройти заново; rv:l[:<режим>] — лексика; rv:g[:<формат>] —
        упражнения от Gemini."""
        cmd, _, val = arg.partition(":")
        if cmd == "s":
            await self.review_menu(chat_id, user_id, message_id)
        elif cmd in ("b", "p", "u", "last", "all", "none"):
            if (pending or {}).get("step") == "rv_sel":
                await self.review_sel(chat_id, user_id, message_id, pending, arg)
        elif cmd == "go":
            if (pending or {}).get("step") == "rv_sel":
                await self.review_start(chat_id, user_id, message_id, pending)
        elif cmd == "h":
            if (pending or {}).get("step") in ("rv_q", "rv_sel"):
                self.db.set_pending(user_id, None)
            await self.review_home(chat_id, user_id)
        elif cmd == "r":
            if (pending or {}).get("step") == "rv_q":
                self.db.set_pending(user_id, None)
            await self.review_rules(chat_id, user_id)
        elif cmd == "q":
            uid, _, i = val.rpartition(":")
            if i.isdigit():
                await self.review_ask_start(chat_id, user_id, uid, int(i))
        elif cmd == "x":
            if (pending or {}).get("step") == "rv_q":
                self.db.set_pending(user_id, None)
            await self.tg.edit_markup(chat_id, message_id)
            await self.tg.send_message(chat_id, "Вопросы закончены — дальше обычный разговор.",
                                       [[("🔁 К повторению", "rv:h")]])
        elif cmd == "e":
            await self.review_hard(chat_id, user_id)
        elif cmd == "o" and val.isdigit():
            await self.review_open(chat_id, user_id, int(val))
        elif cmd == "n":
            await self.review_open(chat_id, user_id, None)
        elif cmd == "R":
            await self.review_reset(chat_id, user_id)
        elif cmd == "l":
            await self.review_lex(chat_id, user_id, val or None)
        elif cmd == "g":
            await self.review_gemini(chat_id, user_id, val or None)
