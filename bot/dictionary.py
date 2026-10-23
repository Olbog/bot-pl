"""Словарь ⭐ и «🙅 Не ошибка»: выражения из ответов, оспоренные исправления, исключения."""
from html import escape

from .ai.prompt import (NORM_HINT, NORM_SCHEMA, PHRASES_HINT, PHRASES_SCHEMA, TOPICS_HINT, TOPICS_SCHEMA,
                        norm_prompt, own_phrases_prompt, phrases_prompt, topics_prompt)
from .core import textbook
from .core.db import DICT_OTHER
from .ui import fmt
from .exercises import logic as exercises
from .settings import DICT_THINKING, IGNORE_PROMPT

RELAYOUT_CHUNK = 40   # 🤖 «Разложить Разное»: слов в одном запросе к модели
OFFER_MAX = 30        # ⭐ из раздела: не больше стольких пунктов в меню (лимит кнопок Telegram)
SKIP_TYPES = ("tf", "free")   # упражнения, где ответ — не слово (правда/неправда, свободный ответ)


def offer_items(offer: dict) -> list[tuple[str, dict]]:
    """Пункты меню «⭐ В словарь» по порядку: («e0», ошибка) …, («b0», из ответа бота / остальное) …"""
    return ([(f"e{i}", it) for i, it in enumerate(offer.get("errs") or [])]
            + [(f"b{i}", it) for i, it in enumerate(offer.get("phrases") or [])])


def _phrase(p: dict) -> dict:
    return {"pl": str(p.get("pl") or "").strip(), "translit": str(p.get("translit") or "").strip(),
            "ru": str(p.get("ru") or "").strip(), "topic": str(p.get("topic") or "").strip()}


def _uniq(items: list[dict], seen: set | None = None) -> list[dict]:
    seen = set() if seen is None else seen
    out = []
    for it in items:
        k = it["pl"].lower()
        if it["pl"] and k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _short(text: str, words: int = 5) -> bool:
    return 0 < len(str(text).split()) <= words


def _fill(q: str, answer: str) -> str:
    return q.replace("___", answer, 1) if "___" in q else ""


def ex_entry(it: dict) -> dict | None:
    """Пункт упражнения → кандидат в словарь {pl, translit, ru, ctx, norm}. Слово-пункт (маска, карточка) — как есть;
    пропуск в предложении — ответ в той же форме, падеж и перевод слова уточнит модель (norm)."""
    if it.get("card"):   # 📘 карточки и тест по словам юнита: слово юнита
        return {"pl": it.get("lemma") or it.get("full_pl") or "", "translit": it.get("translit", ""),
                "ru": it.get("ru", ""), "norm": False}
    answer = str(it.get("answer") or "").strip()
    if not _short(answer):
        return None
    q, ru = str(it.get("q") or ""), str(it.get("ru") or "")
    sentence = it.get("full_pl") or _fill(q, answer)
    if not sentence and it.get("translit") and _short(ru, 4):   # маска / слово с переводом: как есть
        return {"pl": answer, "translit": it["translit"], "ru": ru, "norm": False}
    return {"pl": answer, "translit": "", "ru": "", "ctx": sentence or q, "ctx_ru": ru, "norm": True}


class DictionaryMixin:
    # ---------- ⭐ В словарь: меню под ответом бота или под результатом раздела ----------
    # Под ответом бота: 🧩 мои ошибки из этого сообщения, 💬 подобрать из ответа (модель, «low»), ✍️ свои слова.
    # Под упражнением / итогом / скороговоркой (dX:<откуда>): ❌ ошибки и пропуски — отмечены сразу, ✅ остальное.
    # 📂 Куда: 🤖 авто — тему каждому слову даёт модель; или своя тема — всё отмеченное туда.
    # «✅ Готово»: слово (в той форме, как было, с пометкой падежа) ложится в набор своей темы «⭐ <тема> N».
    # 🛡 Дубли: слово в той же форме уже в словаре — не добавляется, показано, в какой оно теме, можно перенести.

    def offer_errs(self, msg_id: int) -> list[dict]:
        """Мои ошибки в этом сообщении — правильные фрагменты без повторов: [{pl, translit, ru, norm}]."""
        return _uniq([{"pl": str(c.get("correct") or "").strip(), "translit": str(c.get("translit") or "").strip(),
                       "ru": str(c.get("ru") or "").strip(), "norm": True}
                      for c in self.db.corrections_for_msg(msg_id)])[:8]

    def offer_load(self, user_id: int, msg_id: int) -> dict | None:
        offer = self.db.offer_get(msg_id)
        if offer:
            return offer if offer["user_id"] == user_id else None
        if msg_id < 0:
            return None
        row = self.db.model_message(msg_id)
        if not row or row["user_id"] != user_id:
            return None
        offer = {"user_id": user_id, "phrases": [], "saved": [], "errs": self.offer_errs(msg_id), "sel": [],
                 "bot_ready": False, "topic": "", "src": "", "title": "", "key": msg_id}
        self.offer_store(msg_id, offer)
        return offer

    def offer_store(self, msg_id: int, offer: dict) -> None:
        self.db.offer_save(msg_id, offer["user_id"], offer["phrases"], offer["saved"], offer["errs"], offer["sel"],
                           offer["bot_ready"], offer.get("topic") or "", offer.get("src") or "", offer.get("title") or "")

    def offer_have(self, user_id: int, offer: dict) -> dict[str, str]:
        """Пункты, которые уже есть в словаре в той же форме: {ключ: тема}."""
        out = {}
        for k, it in offer_items(offer):
            found = self.db.dict_find(user_id, it["pl"])
            if found:
                out[k] = found["topic"] or DICT_OTHER
        return out

    def offer_view(self, user_id: int, msg_id: int, offer: dict) -> tuple[str, list]:
        have = self.offer_have(user_id, offer)
        return fmt.offer_message(offer, have), fmt.offer_buttons(msg_id, offer, have)

    async def offer_phrases(self, chat_id: int, user_id: int, msg_id: int) -> None:
        """⭐ В словарь — меню приходит сразу, модель не вызывается."""
        offer = self.offer_load(user_id, msg_id)
        if offer:
            await self.tg.send_message(chat_id, *self.offer_view(user_id, msg_id, offer))

    # ---------- ⭐ из разделов: что предложить ----------

    def offer_source_items(self, user_id: int, src: str) -> tuple[str, list[dict], list[dict]] | None:
        """(заголовок, ошибки, остальное) для раздела. src: b:<юнит>|<упр> — упражнение из книги; e:<id> — упражнение
        бота / слова юнита; i:<период> — итог; t:<попытка> — скороговорка."""
        kind, _, arg = src.partition(":")
        if kind == "b":
            unit_id, _, ex_id = arg.partition("|")
            unit = textbook.get_unit(unit_id)
            x = next((e for e in (unit or {}).get("exercises") or [] if e["id"] == ex_id), None)
            if not x or x.get("type") in SKIP_TYPES:
                return None
            done = self.db.bex_results(user_id, unit["unit"])
            errs, rest = [], []
            for n, it in enumerate(x["items"], 1):
                row = done.get((x["id"], n))
                entry = ex_entry(it) if row else None
                if entry:
                    (rest if row["ok"] else errs).append(entry)
            return fmt.bex_name(x), errs, rest
        if kind == "e" and arg.isdigit():
            saved = self.db.ex_get(int(arg))
            if not saved or saved["user_id"] != user_id or not saved.get("results"):
                return None
            errs, rest = [], []
            for r in saved["results"]:
                entry = ex_entry(saved["items"][r["n"] - 1])
                if entry:
                    (rest if r.get("final") == "ok" else errs).append(entry)
            return f"🏋️ Упражнение #{saved['id']}", errs, rest
        if kind == "i" and arg in fmt.PERIODS:
            _, corrs, words, _ = self.period_data(user_id, arg)
            errs = [{"pl": str(c.get("correct") or "").strip(), "translit": str(c.get("translit") or "").strip(),
                     "ru": str(c.get("ru") or "").strip(), "norm": True} for c in corrs]
            rest = [{"pl": str(w.get("pl") or "").strip(), "translit": str(w.get("translit") or "").strip(),
                     "ru": str(w.get("ru") or "").strip(), "norm": False} for w in words]
            return f"📋 Итог за {fmt.PERIODS[arg][0]}", errs, rest
        if kind == "t":
            corrs = self.db.corrections_by(user_id, "tw_attempt", arg)
            errs = [{"pl": str(c.get("correct") or "").strip(), "translit": str(c.get("translit") or "").strip(),
                     "ru": str(c.get("ru") or "").strip(), "norm": True} for c in corrs if not c["_disputed"]]
            return "🗣 Скороговорка", errs, []
        return None

    async def offer_source(self, chat_id: int, user_id: int, src: str) -> None:
        """dX:<откуда> — ⭐ В словарь из раздела. Ошибки отмечены сразу (кроме уже сохранённых), меню запоминается."""
        offer = None   # каждый раз собираем заново: результаты раздела могли измениться
        if not offer:
            got = self.offer_source_items(user_id, src)
            if not got:
                await self.tg.send_message(chat_id, "Здесь нечего добавить в словарь. Своё — /dict add слово, слово")
                return
            title, errs, rest = got
            seen: set = set()
            errs = _uniq(errs, seen)[:OFFER_MAX]
            rest = _uniq(rest, seen)[:max(0, OFFER_MAX - len(errs))]
            offer = {"user_id": user_id, "phrases": rest, "saved": [], "errs": errs, "sel": [], "bot_ready": True,
                     "topic": "", "src": src, "title": title, "key": self.db.offer_new_key()}
            offer["sel"] = [k for k, it in offer_items(offer) if k.startswith("e") and not self.db.dict_has(user_id, it["pl"])]
            self.offer_store(offer["key"], offer)
        await self.tg.send_message(chat_id, *self.offer_view(user_id, offer["key"], offer))

    async def offer_bot(self, chat_id: int, user_id: int, message_id: int, msg_id: int) -> None:
        """💬 Подобрать из ответа бота: 7–8 выражений сразу с темой; результат запоминается."""
        offer = self.offer_load(user_id, msg_id)
        if not offer:
            return
        if not offer["bot_ready"]:
            row = self.db.model_message(msg_id)
            corrs = self.db.corrections_for_msg(msg_id)
            corrected = next((c.get("correct") for c in corrs if c.get("correct")), "")
            data = await self.ask(chat_id, phrases_prompt(row["text"], corrected, corrs, self.cfg.level,
                                                          self.db.topic_names(user_id)),
                                  PHRASES_SCHEMA, PHRASES_HINT, wait="⏳ Подбираю выражения из ответа… (обычно 5–15 с)",
                                  thinking=DICT_THINKING)
            if data is None:
                return
            offer["phrases"] = [_phrase(p) for p in data.get("phrases") or []
                                if isinstance(p, dict) and str(p.get("pl") or "").strip()][:8]
            offer["bot_ready"] = True
            self.offer_store(msg_id, offer)
        await self.tg.edit_message(chat_id, message_id, *self.offer_view(user_id, msg_id, offer))

    async def offer_toggle(self, chat_id: int, user_id: int, message_id: int, msg_id: int, key: str) -> None:
        offer = self.offer_load(user_id, msg_id)
        if not offer:
            return
        key = f"b{key}" if key.isdigit() else key          # старые кнопки — номер выражения
        item = dict(offer_items(offer)).get(key)
        if not item:
            return
        found = self.db.dict_find(user_id, item["pl"])
        if found:   # 🛡 уже в словаре: где лежит и куда перенести
            await self.tg.send_message(chat_id, *fmt.dict_word_screen(found, self.db.dict_topics(user_id),
                                                                      self.offer_fixed_topic(user_id, offer)))
            return
        offer["sel"] = sorted(set(offer["sel"]) ^ {key})
        self.offer_store(msg_id, offer)
        await self.tg.edit_message(chat_id, message_id, *self.offer_view(user_id, msg_id, offer))

    async def offer_topic(self, chat_id: int, user_id: int, message_id: int, msg_id: int, arg: str | None) -> None:
        """📂 Куда положить: arg None — экран выбора; «0» — 🤖 авто; «-» — назад без изменений; id — тема."""
        offer = self.offer_load(user_id, msg_id)
        if not offer:
            return
        if arg is None:
            await self.tg.edit_message(chat_id, message_id,
                                       *fmt.offer_topic_screen(msg_id, self.db.dict_topics(user_id), offer["topic"]))
            return
        if arg == "0":
            offer["topic"] = ""
        elif arg.isdigit():
            t = self.db.topic(int(arg))
            if t and t["user_id"] == user_id:
                offer["topic"] = t["name"]
        self.offer_store(msg_id, offer)
        await self.tg.edit_message(chat_id, message_id, *self.offer_view(user_id, msg_id, offer))

    async def offer_topic_text(self, chat_id: int, user_id: int, msg_id: int, name: str) -> None:
        """✍️ Новая тема для меню «⭐ В словарь» — текстом; меню приходит заново."""
        offer = self.offer_load(user_id, msg_id)
        name = " ".join(name.split())[:40]
        if not offer or not name:
            return
        offer["topic"] = self.db.topic(self.db.topic_get(user_id, name))["name"]
        self.db.conn.commit()
        self.offer_store(msg_id, offer)
        await self.tg.send_message(chat_id, *self.offer_view(user_id, msg_id, offer))

    def offer_fixed_topic(self, user_id: int, offer: dict):
        """Своя тема меню (строка dict_topics) — для кнопки «перенести туда» у дубля; None — 🤖 авто."""
        name = offer.get("topic") or ""
        return self.db.topic(self.db.topic_get(user_id, name)) if name else None

    async def offer_done(self, chat_id: int, user_id: int, message_id: int, msg_id: int) -> None:
        """✅ Готово: отмеченное — в словарь, каждое в набор своей темы (или все — в выбранную тему).
        Слова из предложений / ошибок — в той же форме, модель даёт перевод слова и пометку падежа (один запрос
        «low», вместе с темами). Что уже было в словаре в той же форме — не добавляется, показано где лежит."""
        offer = self.offer_load(user_id, msg_id)
        if not offer:
            return
        items = dict(offer_items(offer))
        picked = [(k, dict(items[k])) for k in offer["sel"] if k in items]
        again = [("⭐ Ещё отсюда", f"d:{msg_id}"), ("📚 Словарь", "dl")]
        if not picked:
            await self.tg.edit_message(chat_id, message_id,
                                       "⭐ Ничего не отмечено — словарь не изменился. Весь словарь — /dict", [again])
            return
        fixed = offer.get("topic") or ""
        fresh = [(k, it) for k, it in picked if not self.db.dict_has(user_id, it["pl"])]
        need = [it for _, it in fresh if it.get("norm") or (not fixed and not it.get("topic"))]
        if need:
            await self.norm_items(chat_id, user_id, need, need_topic=not fixed)
        added, dups = [], []
        for k, it in picked:
            src = ("err" if k.startswith("e") else "bot") if not offer.get("src") else offer["src"].split(":", 1)[0]
            if self.db.dict_add(user_id, it["pl"], it.get("translit", ""), it.get("ru", ""),
                                fixed or it.get("topic") or DICT_OTHER, src=src, form_pl=it.get("form_pl", ""),
                                form_ru=it.get("form_ru", "")):
                added.append(it["pl"])
            else:
                dups.append(self.db.dict_find(user_id, it["pl"]))
        offer["sel"] = []
        self.offer_store(msg_id, offer)
        text, rows = self.added_report(user_id, added, [d for d in dups if d], self.offer_fixed_topic(user_id, offer))
        await self.tg.edit_message(chat_id, message_id, text, rows + [again])

    def added_report(self, user_id: int, added: list[str], dups: list, fixed=None) -> tuple[str, list]:
        """Текст «добавлено / уже было» и кнопки к дублям: 📂 перенести в свою тему (если выбрана) или открыть слово."""
        parts = [fmt.dict_added(self.dict_rows_for(user_id, added))] if added else []
        if dups:
            parts.append(fmt.dict_dups(dups))
        rows = []
        for d in dups[:8]:
            if fixed and d["topic_id"] != fixed["id"]:
                rows.append([(f"📂 {d['pl']} → {fixed['name']}"[:60], f"dm:{d['id']}:{fixed['id']}")])
            else:
                rows.append([(f"📂 {d['pl']}: сменить тему"[:60], f"dw:{d['id']}")])
        return "\n\n".join(parts) or "⭐ Ничего нового.", rows

    async def norm_items(self, chat_id: int, user_id: int, items: list[dict], need_topic: bool) -> None:
        """Словарные записи на месте: перевод слова, транскрипция, падеж (если norm), тема (если need_topic).
        Слово остаётся как было. Модель не ответила — сохраняем как есть, тема «Разное»."""
        data = await self.ask(chat_id, norm_prompt(items, self.db.topic_names(user_id), need_topic),
                              NORM_SCHEMA, NORM_HINT, wait="⏳ Готовлю записи для словаря…", thinking=DICT_THINKING)
        by_n = {x.get("n"): x for x in (data or {}).get("items") or [] if isinstance(x, dict)}
        for n, it in enumerate(items, 1):
            x = by_n.get(n) or {}
            if it.get("norm"):   # падеж — всегда от модели; транскрипция и перевод — только если их не было
                it["form_pl"] = str(x.get("form_pl") or "").strip()
                it["form_ru"] = str(x.get("form_ru") or "").strip()
                for f in ("translit", "ru"):
                    if not it.get(f):
                        it[f] = str(x.get(f) or "").strip() or (it.get("ctx_ru", "") if f == "ru" else "")
            if need_topic and not it.get("topic"):
                it["topic"] = str(x.get("topic") or "").strip() or DICT_OTHER

    async def classify_topics(self, chat_id: int, user_id: int, items: list[dict], wait: str = "⏳ Раскладываю по темам…",
                              topics: list[str] | None = None) -> list[str]:
        """Темы для слов — одним запросом; не вышло — «Разное» (тему можно сменить в /dict)."""
        data = await self.ask(chat_id, topics_prompt([(it["pl"], it.get("ru", "")) for it in items],
                                                     self.db.topic_names(user_id) if topics is None else topics),
                              TOPICS_SCHEMA, TOPICS_HINT, wait=wait, thinking=DICT_THINKING)
        by_n = {}
        for x in (data or {}).get("items") or []:
            if isinstance(x, dict) and str(x.get("topic") or "").strip():
                by_n[x.get("n")] = str(x["topic"]).strip()
        return [by_n.get(n, DICT_OTHER) for n in range(1, len(items) + 1)]

    def dict_rows_for(self, user_id: int, pls: list[str]) -> list:
        want = {p.lower() for p in pls}
        return [r for r in self.db.dict_items(user_id) if r["pl"].lower() in want]

    async def dict_add_text(self, chat_id: int, user_id: int, raw: str, msg_id: int | None = None) -> None:
        """✍️ Свои слова через запятую: модель переводит / исправляет и даёт тему (или своя тема из меню ⭐)."""
        offer = self.offer_load(user_id, msg_id) if msg_id else None
        fixed = (offer or {}).get("topic") or ""
        data = await self.ask(chat_id, own_phrases_prompt(raw, self.cfg.level, self.db.topic_names(user_id)),
                              PHRASES_SCHEMA, PHRASES_HINT,
                              wait="⏳ Перевожу" + (" …" if fixed else " и раскладываю по темам…"),
                              thinking=DICT_THINKING)
        if data is None:
            return
        added, dups = [], []
        for p in data.get("phrases") or []:
            if not isinstance(p, dict):
                continue
            p = _phrase(p)
            if not p["pl"]:
                continue
            if self.db.dict_add(user_id, p["pl"], p["translit"], p["ru"], fixed or p["topic"] or DICT_OTHER, src="own"):
                added.append(p["pl"])
            elif self.db.dict_find(user_id, p["pl"]):
                dups.append(self.db.dict_find(user_id, p["pl"]))
        text, rows = self.added_report(user_id, added, dups, self.offer_fixed_topic(user_id, offer) if offer else None)
        await self.tg.send_message(chat_id, text, rows + [[("📚 Словарь", "dl")]])

    # ---------- 📚 /dict: все слова по темам, смена темы ----------

    def dict_has_other(self, user_id: int) -> bool:
        return any(t["name"] == DICT_OTHER for t in self.db.dict_topics(user_id))

    async def dict_show(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        items, topics = self.db.dict_items(user_id), self.db.dict_topics(user_id)
        text = fmt.dict_message(items, topics)
        buttons = fmt.dict_buttons(topics, has_other=self.dict_has_other(user_id))
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    async def dict_relayout(self, chat_id: int, user_id: int) -> None:
        """🤖 Разовая раскладка: слова темы «Разное» модель раскидывает по темам; слово переезжает в набор своей
        темы вместе с прогрессом. Что модель оставила в «Разном» — остаётся там (можно перенести вручную)."""
        other = self.db.topic_get(user_id, DICT_OTHER)
        items = self.db.topic_items(user_id, other)
        if not items:
            await self.tg.send_message(chat_id, "В «Разном» слов нет — раскладывать нечего.", [[("📚 Словарь", "dl")]])
            return
        moved, topics = 0, [t for t in self.db.topic_names(user_id) if t != DICT_OTHER]
        for i in range(0, len(items), RELAYOUT_CHUNK):
            chunk = items[i:i + RELAYOUT_CHUNK]
            names = await self.classify_topics(
                chat_id, user_id, [{"pl": r["pl"], "ru": r["ru"]} for r in chunk], topics=topics,
                wait=f"⏳ Раскладываю «Разное» по темам: {i + 1}–{i + len(chunk)} из {len(items)}…")
            for r, name in zip(chunk, names):
                if name.strip().lower() != DICT_OTHER.lower():
                    if self.db.dict_move(user_id, r["id"], name, commit=False):
                        moved += 1
                    if name not in topics:
                        topics.append(name)
            self.db.conn.commit()
        left = len(self.db.topic_items(user_id, other))
        tops = [t for t in self.db.dict_topics(user_id)]
        await self.tg.send_message(
            chat_id, f"🤖 Разложено по темам: {moved} из {len(items)}." + (f" В «Разном» осталось: {left}." if left else "")
            + "\n\n" + "\n".join(f"🗂 {escape(t['name'])} — {t['n']}" for t in tops)
            + "\n\n<i>Прогресс слов сохранён. Не согласен с темой — /dict → тема → слово → 📂.</i>",
            [[("📚 Словарь", "dl")]])

    async def dict_callback(self, chat_id: int, user_id: int, message_id: int, data: str) -> None:
        """dl — список; dt:<тема> — тема и её наборы; dw:<слово> — слово (сменить тему / удалить);
        dm:<слово>:<тема> — перенести; dz:<слово> — новая тема текстом; dr / dR:<слово> — удалить (с подтверждением);
        dy:<набор> — упражнение по набору; dS — 🤖 разложить «Разное» по темам."""
        cmd, _, arg = data.partition(":")
        if cmd == "dl":
            await self.dict_show(chat_id, user_id, message_id)
        elif cmd == "dS":
            await self.tg.edit_markup(chat_id, message_id)
            await self.dict_relayout(chat_id, user_id)
        elif cmd == "dt":
            t = self.db.topic(int(arg))
            if not t or t["user_id"] != user_id:
                return
            await self.tg.edit_message(chat_id, message_id,
                                       *fmt.dict_topic_screen(t, self.db.topic_items(user_id, t["id"]),
                                                              self.db.dict_sets(user_id, t["id"])))
        elif cmd in ("dw", "dr"):
            it = self.db.dict_item(int(arg))
            if not it or it["user_id"] != user_id:
                return
            screen = (fmt.dict_word_screen(it, self.db.dict_topics(user_id)) if cmd == "dw"
                      else fmt.dict_delete_screen(it))
            await self.tg.edit_message(chat_id, message_id, *screen)
        elif cmd == "dm":
            item_id, topic_id = (int(x) for x in arg.split(":"))
            t = self.db.topic(topic_id)
            if t and t["user_id"] == user_id:
                await self.dict_moved(chat_id, user_id, message_id, item_id, t["name"])
        elif cmd == "dz":
            it = self.db.dict_item(int(arg))
            if it and it["user_id"] == user_id:
                await self.show(chat_id, user_id, {"step": "dict_topic_new", "item": it["id"]},
                                f"✍️ Напиши новую тему для «{escape(it['pl'])}» — по-русски, 1–3 слова. "
                                "Так можно завести свой набор и переносить туда слова из разных тем.", prev=None)
        elif cmd == "dR":
            it = self.db.dict_item(int(arg))
            if it and it["user_id"] == user_id:
                self.db.dict_remove_id(user_id, it["id"])
                await self.tg.edit_message(chat_id, message_id, f"🗑 «{escape(it['pl'])}» убрано из словаря и набора.",
                                           [[("📚 Словарь", "dl")]])
        elif cmd == "dy":
            st = self.db.get_set(int(arg))
            if st and st["user_id"] == user_id:
                await self.ex_from_set(chat_id, user_id, st["id"])

    async def dict_moved(self, chat_id: int, user_id: int, message_id: int | None, item_id: int, topic: str) -> None:
        title = self.db.dict_move(user_id, item_id, topic)
        it = self.db.dict_item(item_id)
        if not title or not it:
            return
        text = f"📂 «{escape(it['pl'])}» → тема «{escape(it['topic'])}», набор {escape(title)}. Прогресс сохранён."
        buttons = [[(f"🗂 {it['topic']}"[:40], f"dt:{it['topic_id']}"), ("📚 Словарь", "dl")]]
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    def ignore_block(self, user_id: int) -> str:
        rows = self.db.ignores(user_id)[-IGNORE_PROMPT:]
        if not rows:
            return ""
        return ("ИСКЛЮЧЕНИЯ: ученик подтвердил, что эти слова говорит и пишет правильно, а раньше их ошибочно "
                "записали или исправили. Не записывай их в искажённом виде и не считай ошибкой:\n"
                + "\n".join(f"- он говорит «{r['correct']}» (не «{r['original']}»)" for r in rows))

    def drop_ignored(self, user_id: int, corrections: list[dict]) -> list[dict]:
        """Убирает исправления, которые ученик раньше отметил «🙅 Не ошибка» (та же пара «было → стало»)."""
        pairs = {(exercises.norm(r["original"]), exercises.norm(r["correct"])) for r in self.db.ignores(user_id)}
        return [c for c in corrections
                if (exercises.norm(c.get("original", "")), exercises.norm(c.get("correct", ""))) not in pairs]

    async def dispute_show(self, chat_id: int, user_id: int, msg_id: int, message_id: int | None = None) -> None:
        corrs = self.db.corrections_for_msg_all(msg_id)
        if not corrs or corrs[0]["_user"] != user_id:
            await self.tg.send_message(chat_id, "В этом сообщении нет исправлений.")
            return
        text, buttons = fmt.dispute_screen(corrs, msg_id)
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    def dispute_toggle(self, user_id: int, c: dict) -> None:
        """Оспорить исправление разговора или вернуть его. Серия слов набора, сброшенная этим сообщением,
        восстанавливается (только для сообщений, где отметки слов привязаны к сообщению)."""
        on = not c["_disputed"]
        pair = (c.get("original", ""), c.get("correct", ""))
        if on:
            restored = []
            if c.get("_msg"):
                others_left = [x for x in self.db.corrections_for_msg_all(c["_msg"])
                               if not x["_disputed"] and x["_id"] != c["_id"]]
                keys = {exercises.norm(pair[0]), exercises.norm(pair[1])}
                for u in self.db.uses_for_msg(c["_msg"]):
                    if not u["correct"] and (exercises.norm(u["form"]) in keys or not others_left):
                        self.db.set_use_correct(u["id"], True)
                        restored.append(u["id"])
            self.db.set_disputed(c["_id"], True, {**c, "_restored": restored})
            self.db.ignore_add(user_id, *pair)
        else:
            for uid in c.get("_restored") or []:
                self.db.set_use_correct(uid, False)
            self.db.set_disputed(c["_id"], False, {**c, "_restored": []})
            self.db.ignore_remove(user_id, pair=pair)

    async def dispute_callback(self, chat_id: int, user_id: int, message_id: int, arg: str) -> None:
        if arg.startswith("t:"):
            c = self.db.correction(int(arg[2:]))
            if not c or c["_user"] != user_id or not c.get("_msg"):
                return
            self.dispute_toggle(user_id, c)
            await self.dispute_show(chat_id, user_id, c["_msg"], message_id)
        elif arg.startswith("ok:"):
            corrs = [c for c in self.db.corrections_for_msg_all(int(arg[3:])) if c["_user"] == user_id]
            n = sum(c["_disputed"] for c in corrs)
            await self.tg.edit_message(chat_id, message_id, fmt.dispute_done(n))
        elif arg.isdigit():
            await self.dispute_show(chat_id, user_id, int(arg))

    async def ignores_show(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        text, buttons = fmt.ignores_screen(self.db.ignores(user_id))
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)
