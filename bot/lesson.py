"""🎓 /lesson — урок по юниту: комплексное прохождение юнита «как с учителем».

План уроков пишет Claude: <книга>/lessons_<номер>.json — 4–5 уроков на юнит, у каждого проба, диалог-образец, фразы,
правило (сначала вывести самому), упражнения из книги, ролевая игра. Бот ведёт по шагам и помнит, где остановились
(lesson_state); Gemini работает только внутри шага и в рамках юнита.

Шаги обычного урока (STEPS):
  warmup   — 🔁 разминка: вспомнить фразы прошлого урока и ошибки из дневника юнита (карточки RU → PL);
  probe    — 🎯 проба: мини-задача до объяснения (не оценивается, в ошибки не идёт);
  dialog   — 👂 образец: короткий диалог с аудио;   compr — вопросы на понимание (тест кнопками);
  chunks   — 🧩 фразы урока с аудио;                 recall — вспомнить их с русского;
  own      — ✍️ свои предложения с фразами (Gemini проверяет);
  disc     — 🔎 вывести правило из примеров;         rule — 📖 правило (выжимка из файла юнита);
  practice — 📝 упражнения из книги (отдельный прогресс: юнит «les:kpk:12»);
  drill    — 🎯 добивка от Gemini на свежие ошибки (если они были);
  task     — 🎭 ролевая игра: бот в роли, не перебивает; разбор ошибок и «как сказать лучше» — после;
  wrap     — 🏁 итог: «теперь я умею», заметки учителя.
Итоговый урок (final: true, FINAL): разминка по всему юниту → текст наизусть → большая игра → тест → результат
(юнит пройден: тест ≥ PASS_TEST % и открытых ошибок в дневнике ≤ PASS_OPEN).

Дневник ошибок юнита (lesson_errors): каждая ошибка урока — фразой целиком; возвращается в разминках, пока не будет
отвечена верно DIARY_NEED раз подряд. Ошибки языка идут и в общий пул, как в разговоре.
🗒 Заметки учителя — что ученик путает в юните: обновляются в конце урока и подставляются в запросы к Gemini.
"""
import hashlib
import random
from html import escape
from pathlib import Path

from .core import rules, textbook
from .ai.prompt import (DISC_HINT, DISC_SCHEMA, NOTES_HINT, NOTES_SCHEMA, QA_HINT, QA_SCHEMA, RULES_HINT, RULES_SCHEMA,
                        SAY_HINT, SAY_SCHEMA, TALK_HINT, TALK_SCHEMA, lesson_discover_prompt, lesson_notes_prompt,
                        lesson_say_prompt, lesson_talk_prompt, review_qa_prompt, rules_for_errors_prompt)
from .exercises import speech
from .exercises import logic as exercises
from .ui import fmt
from .core.readability import tr
from .common import log
from .settings import VOICE_MIN_SEC

STEPS = ["warmup", "probe", "dialog", "compr", "disc", "rule", "build", "practice", "chunks", "recall", "own", "drill",
         "task", "wrap"]   # сначала понять правило и применить его, потом — устойчивые фразы, которые правилом не вывести
FINAL = ["warmup", "text", "task", "test", "result"]
STEP_NAME = {"warmup": "🔁 Разминка", "probe": "🎯 Проба", "dialog": "👂 Образец", "compr": "❓ Понял ли я",
             "chunks": "🧩 Фразы", "recall": "🧠 Вспомнить фразы", "own": "✍️ Свои предложения",
             "disc": "🔎 Найди правило", "rule": "📖 Правило", "build": "🛠 По правилу",
             "practice": "📝 Упражнения из книги",
             "drill": "🎯 Добивка", "task": "🎭 Задача", "wrap": "🏁 Итог урока", "text": "📜 Текст наизусть",
             "test": "📝 Тест по юниту", "result": "🏆 Итог юнита"}
DIARY_NEED = 2        # столько раз подряд верно — ошибка в дневнике закрыта
WARM_DIARY = 6        # в разминке: ошибок из дневника
WARM_CHUNKS = 5       # … и фраз прошлого урока
TALK_TURNS = 8        # реплик ученика в ролевой игре, не больше
PASS_TEST = 85        # юнит пройден: тест не ниже, %
PASS_OPEN = 3         # … и открытых ошибок в дневнике не больше
QA_HISTORY = 8
CHUNK_RATE = "-20%"   # фразы — помедленнее
NAV = [[("➡️ Дальше", "ls:n")], [("📖 Правило", "ls:rl"), ("❓ Вопрос", "ls:ask"), ("⏸ Пауза", "ls:h")]]
HELP_ROW = [("📖 Правило", "ls:rl"), ("❓ Вопрос учителю", "ls:ask")]   # под упражнениями урока


def _variants(pl: str) -> list[str]:
    """Что ещё засчитать: «Byłem w kinie. / Byłam w kinie.» — любую половину; «Nie wiedziałem, że…» — без «…»."""
    parts = [x.strip() for x in pl.split(" / ")] if " / " in pl else []
    out = []
    for x in parts + [pl]:
        y = x.rstrip("…").rstrip(" ,").strip()
        for v in (x, y):
            if v and v != pl and v not in out:
                out.append(v)
    return out


def _card(pl: str, ru: str, translit: str = "", **extra) -> dict:
    """Карточка RU → PL для движка упражнений."""
    return {"q": ru, "answer": pl, "accepted": _variants(pl), "options": [], "full_pl": pl, "translit": translit,
            "ru": ru, "grammar": "", "rule": "", "lemma": pl, "dir": "ru", "card": True, **extra}


def _spoiler(pl: str, translit: str, ru: str) -> str:
    """Реплика: текст, перевод под спойлером; транскрипция — только у коротких фраз с трудными словами."""
    t = tr(pl, translit)
    return (f"<b>{escape(pl)}</b>\n\n" + (f"🔤 <tg-spoiler>{escape(t)}</tg-spoiler>\n\n" if t else "")
            + f"🇷🇺 <tg-spoiler>{escape(ru)}</tg-spoiler>")


def _corr_lines(corrs: list[dict]) -> list[str]:
    out = []
    for c in corrs:
        icon = fmt.KIND_ICON.get(str(c.get("kind", "")).lower(), "•")
        line = f"{icon} {escape(str(c.get('original', '')))} → <b>{escape(str(c.get('correct', '')))}</b>"
        if fmt.ctr(c):
            line += f" [{escape(fmt.ctr(c))}]"
        if c.get("ru"):
            line += f" — {escape(c['ru'])}"
        if c.get("why"):
            line += f"\n   <i>{escape(c['why'])}</i>"
        out.append(line)
    return out


class LessonMixin:
    # ---------- состояние ----------

    def ls_active(self, user_id: int) -> str:
        return self.db.lesson_get(user_id, "").get("unit", "")

    def ls_state(self, user_id: int, unit_id: str) -> dict:
        st = self.db.lesson_get(user_id, unit_id)
        st.setdefault("lesson", "")
        st.setdefault("step", 0)
        st.setdefault("sub", 0)
        st.setdefault("done", {})
        st.setdefault("notes", "")
        st.setdefault("cur", {"errs": [], "redo": []})
        return st

    def ls_ctx(self, user_id: int, unit_id: str | None = None):
        """(юнит, уроки, состояние, урок, шаги) активного юнита или None."""
        unit_id = unit_id or self.ls_active(user_id)
        unit = textbook.get_unit(unit_id) if unit_id else None
        plan = textbook.lessons(unit_id) if unit else []
        if not plan:
            return None
        st = self.ls_state(user_id, unit["unit"])
        lesson = next((x for x in plan if x["id"] == st["lesson"]), None)
        steps = (FINAL if lesson.get("final") else STEPS) if lesson else []
        return unit, plan, st, lesson, steps

    def ls_dir(self) -> Path:
        return getattr(self, "ls_audio_dir", None) or Path(self.cfg.db_path).parent / "audio" / "ls"

    def ls_path(self, text: str, rate: str) -> Path:
        h = hashlib.md5(f"{self.cfg.tts_voice}|{rate}|{text}".encode()).hexdigest()[:12]
        return self.ls_dir() / f"{h}.ogg"

    @staticmethod
    def ls_speech(lesson: dict) -> list[tuple[str, str]]:
        """Что озвучить в уроке: (текст, темп)."""
        out = []
        if (lesson.get("dialog") or {}).get("pl"):
            out.append((lesson["dialog"]["pl"].replace("—", " "), "+0%"))
        if lesson.get("chunks"):
            out.append((" ... ".join(c["pl"].rstrip(".") for c in lesson["chunks"]) + ".", CHUNK_RATE))
        if (lesson.get("task") or {}).get("opening"):
            out.append((lesson["task"]["opening"]["pl"], "+0%"))
        return out

    def ls_jobs(self) -> list[tuple[Path, str, str]]:
        return [(self.ls_path(t, r), t, r) for u in textbook.lesson_units() for x in textbook.lessons(u["unit"])
                for t, r in self.ls_speech(x)]

    async def ls_pregen(self, sleep=None) -> None:
        await self.pregen_audio(self.ls_jobs, "🎓 Уроки", sleep)

    async def ls_voice(self, chat_id: int, text: str, rate: str = "+0%") -> None:
        await self.tg.send_action(chat_id, "record_voice")
        try:
            await self.tg.send_voice(chat_id, await self.cached_tts(self.ls_path(text, rate), text, rate))
        except Exception as err:
            log.warning("Урок: озвучка не удалась: %s", err)
            await self.tg.send_message(chat_id, "🔇 Озвучка не получилась — текст выше.")

    # ---------- экраны ----------

    async def lesson_menu(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        units = textbook.lesson_units()
        if not units:
            await self.tg.send_message(chat_id, "🎓 Планов уроков пока нет.")
            return
        active = self.ls_active(user_id)
        if active and not message_id and any(u["unit"] == active for u in units):
            await self.lesson_home(chat_id, user_id, active)
            return
        rows = []
        for u in units:
            st = self.ls_state(user_id, u["unit"])
            plan = textbook.lessons(u["unit"])
            mark = "🏆" if st.get("passed") else f"{len(st['done'])}/{len(plan)}"
            rows.append([(f"{u['book_short']} {u['num']} · {u['title']}"[:46] + f" · {mark}", f"ls:u:{u['unit']}")])
        text = ("🎓 <b>Уроки по юнитам</b>\n<i>Юнит целиком, как с учителем: разминка, образец, фразы, правило, "
                "упражнения из книги, ролевая игра — и дневник ошибок, которые возвращаются, пока не уйдут.</i>")
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, rows)
        else:
            await self.tg.send_message(chat_id, text, rows)

    async def lesson_home(self, chat_id: int, user_id: int, unit_id: str, message_id: int | None = None) -> None:
        ctx = self.ls_ctx(user_id, unit_id)
        if not ctx:
            await self.tg.send_message(chat_id, "План уроков для этого юнита не найден. /lesson")
            return
        unit, plan, st, lesson, steps = ctx
        self.db.lesson_set(user_id, "", {"unit": unit["unit"]})
        open_errs = self.db.lesson_errs(user_id, unit["unit"], need=DIARY_NEED)
        all_errs = self.db.lesson_errs(user_id, unit["unit"], open_only=False)
        lines = [f"🎓 <b>{escape(unit['book_short'])} {escape(unit['name'])} · {escape(unit['title'])}</b>"
                 + (" — 🏆 юнит пройден" if st.get("passed") else ""), ""]
        rows = []
        for x in plan:
            d = st["done"].get(x["id"])
            cur = lesson and x["id"] == lesson["id"]
            icon = "✅" if d else ("▶️" if cur else "⚪")
            lines.append(f"{icon} <b>Урок {x['id']}.</b> {escape(x['title'])}"
                         + (f" — шаг {st['step'] + 1} из {len(steps)}" if cur and not d else ""))
            rows.append([(f"{icon} Урок {x['id']}. {x['title']}"[:60], f"ls:s:{unit['unit']}|{x['id']}")])
        lines += ["", f"📓 Дневник ошибок: открыто {len(open_errs)} из {len(all_errs)}"
                      f" <i>(закрывается после {DIARY_NEED} верных ответов подряд)</i>"]
        if st.get("test"):
            lines.append(f"📝 Тест по юниту: {st['test']['ok']} из {st['test']['total']}")
        if st["notes"]:
            lines += ["", f"🗒 <i>Заметки учителя: {escape(st['notes'])}</i>"]
        top = []
        if lesson and not st["done"].get(lesson["id"]):
            top.append([(f"▶️ Продолжить: урок {lesson['id']}, {STEP_NAME[steps[st['step']]]}"[:60], "ls:c")])
        elif not lesson or st["done"].get(lesson["id"]):
            nxt = next((x for x in plan if x["id"] not in st["done"]), None)
            if nxt:
                top.append([(f"▶️ Начать урок {nxt['id']}"[:60], f"ls:s:{unit['unit']}|{nxt['id']}")])
        rows = top + rows
        if open_errs:
            rows.append([(f"🔁 Работа над ошибками ({len(open_errs)})", "ls:err"), ("📓 Дневник", "ls:d")])
        elif all_errs:
            rows.append([("📓 Дневник ошибок", "ls:d")])
        rows.append([("🎓 Все юниты", "ls:m")])
        if message_id:
            await self.tg.edit_message(chat_id, message_id, "\n".join(lines), rows)
        else:
            await self.tg.send_message(chat_id, "\n".join(lines), rows)

    def ls_head(self, unit: dict, plan: list, st: dict, lesson: dict, steps: list) -> str:
        n = [x["id"] for x in plan].index(lesson["id"]) + 1
        return (f"🎓 <b>{escape(unit['book_short'])} {escape(unit['num'])} · урок {n}/{len(plan)}</b> · "
                f"шаг {st['step'] + 1}/{len(steps)} — {STEP_NAME[steps[st['step']]]}")

    async def lesson_start(self, chat_id: int, user_id: int, unit_id: str, lesson_id: str) -> None:
        ctx = self.ls_ctx(user_id, unit_id)
        if not ctx or not any(x["id"] == lesson_id for x in ctx[1]):
            return
        unit, plan, st, lesson, _ = ctx
        self.db.lesson_set(user_id, "", {"unit": unit["unit"]})
        st.update(lesson=lesson_id, step=0, sub=0, cur={"errs": [], "redo": []})
        st["done"].pop(lesson_id, None)
        self.db.lesson_set(user_id, unit["unit"], st)
        self.db.set_pending(user_id, None)
        x = next(x for x in plan if x["id"] == lesson_id)
        goals = "\n".join(f"• {escape(g)}" for g in x.get("can_do") or [])
        await self.tg.send_message(chat_id, f"🎓 <b>Урок {escape(lesson_id)}. {escape(x['title'])}</b>\n\n"
                                            + (f"После урока ты сможешь:\n{goals}\n\n" if goals else "")
                                            + "<i>Примерно 25–35 минут. Можно прерваться в любой момент — /lesson вернёт "
                                              "на тот же шаг. «➡️ Дальше» пропускает шаг.</i>")
        await self.lesson_step(chat_id, user_id)

    async def lesson_next(self, chat_id: int, user_id: int) -> None:
        ctx = self.ls_ctx(user_id)
        if not ctx or not ctx[3]:
            await self.lesson_menu(chat_id, user_id)
            return
        unit, plan, st, lesson, steps = ctx
        self.db.set_pending(user_id, None)
        if steps[st["step"]] == "practice" and st["sub"] + 1 < len(lesson.get("practice") or []):
            st["sub"] += 1
        elif st["step"] + 1 < len(steps):
            st["step"] += 1
            st["sub"] = 0
        else:   # последний шаг уже показан — урок закрыт, идём к юниту
            await self.lesson_home(chat_id, user_id, unit["unit"])
            return
        self.db.lesson_set(user_id, unit["unit"], st)
        await self.lesson_step(chat_id, user_id)

    async def lesson_step(self, chat_id: int, user_id: int) -> None:
        """Показать текущий шаг; шаги, для которых в плане ничего нет, пропускаются."""
        for _ in range(len(STEPS) + 1):
            ctx = self.ls_ctx(user_id)
            if not ctx or not ctx[3]:
                await self.lesson_menu(chat_id, user_id)
                return
            unit, plan, st, lesson, steps = ctx
            if await getattr(self, f"ls_{steps[st['step']]}")(chat_id, user_id, unit, plan, st, lesson, steps):
                return
            if st["step"] + 1 >= len(steps):
                await self.lesson_home(chat_id, user_id, unit["unit"])
                return
            st["step"] += 1
            st["sub"] = 0
            self.db.lesson_set(user_id, unit["unit"], st)

    async def ls_ex(self, chat_id: int, user_id: int, unit: dict, title: str, items: list[dict], f: str = "gap",
                    nxt: str = "n", **extra) -> None:
        for i, it in enumerate(items):
            it["_key"] = f"les:{unit['unit']}:{self.clock()}:{i}"
        ex = {"kind": "lesson", "fmt": f, "total": 1, "left": 1, "lesson": nxt, "unit": unit["unit"], **extra}
        await self.ex_publish(chat_id, user_id, ex, items, title, 1)

    # ---------- шаги: каждый возвращает True, если что-то показал ----------

    def ls_warm_items(self, user_id: int, unit: dict, plan: list, lesson: dict) -> list[dict]:
        final = bool(lesson.get("final"))
        errs = self.db.lesson_errs(user_id, unit["unit"], need=DIARY_NEED)
        errs = [x for x in errs if x.get("ru")][:10 if final else WARM_DIARY]
        items = [_card(x["pl"], x["ru"], x.get("translit", ""), _diary=x["id"]) for x in errs]
        i = [x["id"] for x in plan].index(lesson["id"])
        prev = plan[:i] if final else plan[max(0, i - 1):i]
        chunks = [c for x in prev for c in x.get("chunks") or []]
        have = {exercises.norm(it["answer"]) for it in items}
        chunks = [c for c in chunks if exercises.norm(c["pl"]) not in have]
        k = max(0, (12 if final else WARM_DIARY + WARM_CHUNKS) - len(items))
        for c in random.sample(chunks, min(len(chunks), min(k, 6 if final else WARM_CHUNKS))):
            items.append(_card(c["pl"], c["ru"], c.get("translit", "")))
        return items

    async def ls_warmup(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        items = self.ls_warm_items(user_id, unit, plan, lesson)
        if not items:
            return False
        await self.tg.send_message(chat_id, self.ls_head(unit, plan, st, lesson, steps) + "\n\n"
                                   "Вспомни без подсказок: фразы прошлого урока и то, в чём ошибался. "
                                   "Не помнишь — «3-». Ошибка из дневника закрывается после "
                                   f"{DIARY_NEED} верных ответов подряд.")
        await self.ls_ex(chat_id, user_id, unit, "Разминка: скажи по-польски", items)
        return True

    async def ls_probe(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        p = lesson.get("probe")
        if not p:
            return False
        self.db.set_pending(user_id, {"step": "ls_in", "mode": "probe"})
        await self.tg.send_message(
            chat_id, self.ls_head(unit, plan, st, lesson, steps) + f"\n\n{escape(p['task'])}\n\n"
            "<i>Скажи голосом или напиши — как сможешь, хоть половину по-русски. Это не оценка: так видно, чему учиться "
            "на уроке. Совсем не знаешь — «Дальше».</i>", NAV)
        return True

    async def ls_dialog(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        d = lesson.get("dialog")
        if not d:
            return False
        await self.tg.send_message(
            chat_id, self.ls_head(unit, plan, st, lesson, steps) + f"\n<b>{escape(d.get('title', ''))}</b>\n\n"
            "<i>Сначала послушай аудио, не глядя в текст. Потом прочитай. Транскрипция и перевод — под спойлерами.</i>\n\n"
            + _spoiler(d["pl"], d.get("translit", ""), d.get("ru", "")), NAV)
        await self.ls_voice(chat_id, d["pl"].replace("—", " "))
        return True

    async def ls_compr(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        qs = (lesson.get("dialog") or {}).get("questions")
        if not qs:
            return False
        items = [{"q": q["q"], "answer": q["answer"], "options": random.sample(q["options"], len(q["options"])),
                  "accepted": [], "ru": q.get("ru", ""), "full_pl": "", "translit": "", "grammar": "", "rule": "",
                  "lemma": "", "_nopool": True} for q in qs]
        await self.tg.send_message(chat_id, self.ls_head(unit, plan, st, lesson, steps)
                                   + "\n\nВопросы по диалогу — выбери ответ. Диалог выше можно переслушать.")
        await self.ls_ex(chat_id, user_id, unit, "Понял ли я диалог", items, f="test")
        return True

    async def ls_chunks(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        chunks = lesson.get("chunks")
        if not chunks:
            return False
        lines = [self.ls_head(unit, plan, st, lesson, steps), "",
                 "Это готовые куски речи — учи их целиком, как одно слово. Послушай аудио и повтори каждую фразу вслух "
                 "дважды. Потом «Дальше» — проверю, что запомнилось.", ""]
        for i, c in enumerate(chunks, 1):
            lines.append(f"{i}. <b>{escape(c['pl'])}</b>{fmt._br(c)} — {escape(c['ru'])}"
                         + (f"\n    <i>{escape(c['note'])}</i>" if c.get("note") else ""))
        await self.tg.send_message(chat_id, "\n".join(lines), NAV)
        await self.ls_voice(chat_id, self.ls_speech(lesson)[1 if (lesson.get("dialog") or {}).get("pl") else 0][0],
                            CHUNK_RATE)
        return True

    async def ls_recall(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        chunks = lesson.get("chunks")
        if not chunks:
            return False
        items = [_card(c["pl"], c["ru"], c.get("translit", "")) for c in random.sample(chunks, len(chunks))]
        await self.tg.send_message(chat_id, self.ls_head(unit, plan, st, lesson, steps)
                                   + "\n\nТеперь без подглядывания: как это по-польски? Можно голосом. Не помнишь — «3-».")
        await self.ls_ex(chat_id, user_id, unit, "Фразы урока: скажи по-польски", items)
        return True

    async def ls_own(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        if not lesson.get("chunks"):
            return False
        task = lesson.get("own") or "Составь 3 своих предложения с любыми фразами урока — про себя и свою жизнь."
        self.db.set_pending(user_id, {"step": "ls_in", "mode": "own", "task": task})
        await self.tg.send_message(chat_id, self.ls_head(unit, plan, st, lesson, steps) + f"\n\n{escape(task)}\n\n"
                                   "<i>Голосом или текстом, одним сообщением. Фразы — в списке выше.</i>", NAV)
        return True

    async def ls_disc(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        d = lesson.get("discover")
        if not d:
            return False
        ex = "\n".join(f"• <b>{escape(x['pl'])}</b>{fmt._br(x)} — {escape(x['ru'])}"
                       for x in d["examples"])
        self.db.set_pending(user_id, {"step": "ls_in", "mode": "disc"})
        await self.tg.send_message(
            chat_id, self.ls_head(unit, plan, st, lesson, steps) + f"\n\nПосмотри на примеры:\n{ex}\n\n"
            f"❓ <b>{escape(d['question'])}</b>\n\n<i>Ответь по-русски своими словами (текстом или голосом) — или сразу "
            "«Показать правило».</i>",
            [[("💡 Показать правило", "ls:n")], NAV[1]])
        return True

    @staticmethod
    def ls_rules(unit: dict, lesson: dict) -> list[dict]:
        all_rules = (unit.get("review") or {}).get("rules") or []
        return ([all_rules[i] for i in lesson.get("rules") or [] if 0 <= i < len(all_rules)]
                + list(lesson.get("extra_rules") or []))   # extra_rules — правило только этого урока

    async def ls_rule(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        sel = self.ls_rules(unit, lesson)
        if not sel:
            return False
        await self.tg.send_message(chat_id, self.ls_head(unit, plan, st, lesson, steps) + "\n\n"
                                   + fmt.rules_message({"rules": sel}), NAV)
        return True

    async def ls_build(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        """🛠 Тренировка по правилу: сам строишь формы (пишет Claude в плане — build {intro, items})."""
        b = lesson.get("build")
        if not b or not b.get("items"):
            return False
        items = [{"q": t["q"], "answer": t["answer"], "accepted": list(t.get("accepted") or []), "options": [],
                  "full_pl": t.get("full_pl") or speech.fill(t["q"], t["answer"]), "translit": t.get("translit", ""),
                  "ru": t["ru"], "grammar": t.get("grammar", ""), "rule": rules.normalize(t.get("rule")), "lemma": ""}
                 for t in b["items"]]
        await self.tg.send_message(chat_id, self.ls_head(unit, plan, st, lesson, steps) + "\n\n"
                                   + escape(b.get("intro") or "Теперь построй формы сам — по правилу выше. "
                                            "Правило под рукой — кнопка «📖 Правило»."))
        await self.ls_ex(chat_id, user_id, unit, b.get("title") or "По правилу: построй форму", items)
        return True

    async def ls_practice(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        ids = lesson.get("practice") or []
        if st["sub"] >= len(ids):
            return False
        les = textbook.get_unit(textbook.LESSON + unit["unit"])
        x = next((e for e in les["exercises"] if e["id"] == ids[st["sub"]]), None)
        if not x:
            return False
        await self.tg.send_message(chat_id, self.ls_head(unit, plan, st, lesson, steps)
                                   + f" · {st['sub'] + 1} из {len(ids)}\n\n<i>Упражнение из книги на тему урока. Прогресс "
                                     "здесь свой — в самом юните ничего не меняется. После проверки — «🎓 Дальше по уроку».</i>")
        await self.bex_open(chat_id, user_id, les, x)
        return True

    async def ls_drill(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        redo = (st["cur"].get("redo") or [])[-8:]
        titles = [r.get("title", "") for r in self.ls_rules(unit, lesson)] or [lesson["title"]]
        if not redo:
            return False
        await self.tg.send_message(chat_id, self.ls_head(unit, plan, st, lesson, steps)
                                   + f"\n\nВ отработке было ошибок: {len(st['cur']['redo'])}. Сейчас — новые предложения "
                                     "на те же места.")
        await self.ex_make(chat_id, user_id, {"kind": "grammar", "fmt": "gap", "rules": titles, "redo": redo,
                                              "review": True, "lesson": "n", "unit": unit["unit"], "total": 1, "left": 1})
        return True

    async def ls_task(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        t = lesson.get("task")
        if not t:
            return False
        targets = "\n".join(f"▫️ {escape(x)}" for x in t.get("targets") or [])
        need = int(t.get("min_targets") or 3)
        o = t["opening"]
        self.db.set_pending(user_id, {"step": "ls_talk", "hist": [["model", o["pl"]]], "corr": [], "used": [],
                                      "better": [], "turn": 0, "done": False,
                                      "attempt": f"{unit['unit']}#{lesson['id']}@{int(self.clock() * 1000)}"})
        await self.tg.send_message(
            chat_id, self.ls_head(unit, plan, st, lesson, steps) + f"\n<b>{escape(t['title'])}</b>\n\n{escape(t['brief'])}\n\n"
            f"🎯 Цель: {escape(t['goal'])}\n\n🧩 Используй не меньше {need} фраз урока:\n{targets}\n\n"
            "<i>Говори голосом или пиши. Я в роли и не перебиваю: ошибки и «как сказать лучше» разберём после игры. "
            f"До {TALK_TURNS} реплик.</i>")
        await self.tg.send_message(chat_id, "💬 " + _spoiler(o["pl"], o.get("translit", ""), o.get("ru", "")),
                                   [[("🏁 Закончить и разобрать", "ls:tf")], NAV[1]])
        await self.ls_voice(chat_id, o["pl"])
        return True

    async def ls_wrap(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        errs = st["cur"].get("errs") or []
        if errs or st["notes"]:
            data = await self.ask(chat_id, lesson_notes_prompt(unit, st["notes"], errs), NOTES_SCHEMA, NOTES_HINT,
                                  wait="⏳ Подвожу итог урока…")
            if data is not None:
                st["notes"] = str(data.get("notes") or "").strip()[:400]
        st["done"][lesson["id"]] = {"at": self.clock(), "errs": len(errs)}
        nxt = next((x for x in plan if x["id"] not in st["done"]), None)
        self.db.lesson_set(user_id, unit["unit"], st)
        self.db.set_pending(user_id, None)
        open_errs = self.db.lesson_errs(user_id, unit["unit"], need=DIARY_NEED)
        can = "\n".join(f"✅ {escape(g)}" for g in lesson.get("can_do") or [])
        task = st["cur"].get("task")
        lines = [f"🏁 <b>Урок {escape(lesson['id'])} пройден: {escape(lesson['title'])}</b>", ""]
        if can:
            lines += ["Теперь ты умеешь:", can, ""]
        lines.append(f"Ошибок за урок: {len(errs)} · в дневнике юнита открыто: {len(open_errs)}")
        if task:
            lines.append(f"🎭 Задача: {'цель достигнута' if task.get('ok') else 'цель не достигнута — стоит сыграть ещё раз'}"
                         f", фраз урока использовано: {task.get('used', 0)}")
        if st["notes"]:
            lines += ["", f"🗒 <i>Заметки учителя: {escape(st['notes'])}</i>"]
        lines += ["", "<i>До следующего урока: карточки слов юнита (/ex → 📘) и «🔁 Работа над ошибками». Следующий урок "
                      "начнётся с разминки по этому.</i>"]
        rows = [[(f"➡️ Урок {nxt['id']}. {nxt['title']}"[:60], f"ls:s:{unit['unit']}|{nxt['id']}")]] if nxt else []
        rows.append([("🎓 К юниту", "ls:h")])
        await self.tg.send_message(chat_id, "\n".join(lines), rows)
        return True

    # ---------- итоговый урок ----------

    async def ls_text(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        texts = unit.get("texts") or []
        n = int(lesson.get("text") or 0)
        if not 1 <= n <= len(texts):
            return False
        await self.tg.send_message(
            chat_id, self.ls_head(unit, plan, st, lesson, steps) + f"\n\nТекст «{escape(texts[n - 1]['title'])}»: открой, "
            "выучи и расскажи — наизусть или пересказом. Это отдельный раздел: когда закончишь, вернись сюда и нажми «Дальше» "
            "(или /lesson → «Продолжить»).",
            [[("📜 Открыть текст", f"ml:o:{unit['unit']}#{n}")]] + NAV)
        return True

    async def ls_test(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        test = lesson.get("test")
        if not test:
            return False
        items = [{"q": t["q"], "answer": t["answer"], "accepted": list(t.get("accepted") or []), "options": [],
                  "full_pl": t.get("full_pl") or speech.fill(t["q"], t["answer"]), "translit": t.get("translit", ""),
                  "ru": t["ru"], "grammar": t.get("grammar", ""), "rule": rules.normalize(t.get("rule")), "lemma": ""}
                 for t in test]
        await self.tg.send_message(chat_id, self.ls_head(unit, plan, st, lesson, steps)
                                   + f"\n\nИтоговый тест: {len(items)} пунктов на весь юнит, без подсказок. "
                                     f"Засчитывается первая попытка; юнит пройден при {PASS_TEST} % и выше.")
        await self.ls_ex(chat_id, user_id, unit, f"Тест по юниту {unit['num']}", items, ltest=True)
        return True

    async def ls_result(self, chat_id, user_id, unit, plan, st, lesson, steps) -> bool:
        test = st.get("test") or {}
        pct = round(100 * test.get("ok", 0) / test["total"]) if test.get("total") else 0
        open_errs = self.db.lesson_errs(user_id, unit["unit"], need=DIARY_NEED)
        passed = bool(test.get("total")) and pct >= PASS_TEST and len(open_errs) <= PASS_OPEN
        st["done"][lesson["id"]] = {"at": self.clock(), "errs": len(st["cur"].get("errs") or [])}
        if passed:
            st["passed"] = self.clock()
        self.db.lesson_set(user_id, unit["unit"], st)
        self.db.set_pending(user_id, None)
        lines = [f"🏆 <b>Итог юнита {escape(unit['num'])}: {escape(unit['title'])}</b>", "",
                 (f"📝 Тест: {test['ok']} из {test['total']} — {pct} % (нужно {PASS_TEST} %)" if test.get("total")
                  else "📝 Тест не пройден — без него юнит не засчитывается"),
                 f"📓 Открытых ошибок в дневнике: {len(open_errs)} (нужно не больше {PASS_OPEN})", ""]
        rows = []
        if passed:
            lines.append("🎉 <b>Юнит пройден!</b> Можно идти дальше. Дневник ошибок останется — загляни в него через "
                         "несколько дней.")
        else:
            by = {}
            for x in open_errs:
                by[x["lesson"]] = by.get(x["lesson"], 0) + 1
            weak = sorted(by, key=by.get, reverse=True)[:2]
            lines.append("Пока рано идти дальше. Что сделать:")
            if len(open_errs) > PASS_OPEN:
                lines.append(f"• «🔁 Работа над ошибками» — пока открытых не станет {PASS_OPEN} или меньше (каждую нужно "
                             f"ответить верно {DIARY_NEED} раза, лучше в разные дни);")
            for w in weak:
                x = next((x for x in plan if x["id"] == w), None)
                if x and not x.get("final"):
                    lines.append(f"• повторить урок {w} «{escape(x['title'])}» — оттуда ошибок: {by[w]};")
                    rows.append([(f"🔁 Урок {w}. {x['title']}"[:60], f"ls:s:{unit['unit']}|{w}")])
            if not test.get("total") or pct < PASS_TEST:
                lines.append("• пройти итоговый урок ещё раз — тест засчитается заново.")
                rows.append([("🔁 Итоговый урок заново", f"ls:s:{unit['unit']}|{lesson['id']}")])
        if open_errs:
            rows.append([(f"🔁 Работа над ошибками ({len(open_errs)})", "ls:err")])
        rows.append([("🎓 К юниту", "ls:h")])
        await self.tg.send_message(chat_id, "\n".join(lines), rows)
        return True

    # ---------- дневник ошибок ----------

    def ls_err(self, user_id: int, unit_id: str, st: dict, pl: str, ru: str, translit: str = "", said: str = "",
               why: str = "", redo: dict | None = None) -> None:
        """Ошибка урока: в дневник юнита (если есть фраза и перевод) и в сводку текущего урока."""
        pl = pl.strip()
        if pl and ru and len(pl) <= 160:
            self.db.lesson_err_add(user_id, unit_id, st.get("lesson") or "", pl,
                                   {"ru": ru, "translit": translit, "said": said, "why": why})
        st["cur"].setdefault("errs", []).append({"pl": pl, "said": said, "why": why})
        if redo:
            st["cur"].setdefault("redo", []).append(redo)

    def lesson_after_ex(self, user_id: int, ex: dict, saved: dict, results: list[dict]) -> None:
        """Проверено упражнение урока: дневник (ответ на старую ошибку / новая ошибка), результат теста."""
        unit_id = str(ex.get("unit") or "").removeprefix(textbook.LESSON)
        if not unit_id:
            return
        st = self.ls_state(user_id, unit_id)
        for r in results:
            it = saved["items"][r["n"] - 1]
            ok = r["final"] != "wrong"
            if it.get("_diary"):
                self.db.lesson_err_result(user_id, it["_diary"], ok)
                if not ok:
                    st["cur"].setdefault("errs", []).append({"pl": it["answer"], "said": r.get("user", ""), "why": ""})
            elif not ok and not it.get("_nopool"):
                self.ls_err(user_id, unit_id, st, speech.phrase(it), it.get("ru", ""), it.get("translit", ""),
                            r.get("heard") or r.get("user") or "", r.get("explanation", ""),
                            redo=None if it.get("card") else {"rule": it.get("rule", ""), "q": it.get("q", ""),
                                                              "answer": it.get("answer", ""), "lemma": ""})
        if ex.get("ltest"):
            st["test"] = {"ok": sum(r["final"] != "wrong" for r in results), "total": len(results), "at": self.clock()}
        self.db.lesson_set(user_id, unit_id, st)

    def lesson_after_bex(self, user_id: int, unit: dict, x: dict, items: list[dict], results: list[dict]) -> None:
        """Проверено упражнение из книги внутри урока: ошибки — в дневник и на добивку."""
        st = self.ls_state(user_id, unit["base"])
        for r in results:
            if r["final"] == "ok":
                continue
            it = items[r["n"] - 1]
            pl = speech.phrase(it) if x.get("type") in ("gap", "open", "choice") else ""
            self.ls_err(user_id, unit["base"], st, pl, it.get("ru", ""), it.get("translit", ""), r.get("user", ""),
                        r.get("explanation", ""),
                        redo={"rule": it.get("rule", "") or x.get("title_ru", ""), "q": it.get("q", ""),
                              "answer": it.get("answer", ""), "lemma": ""})
        self.db.lesson_set(user_id, unit["base"], st)

    async def lesson_errors_ex(self, chat_id: int, user_id: int) -> None:
        """🔁 Работа над ошибками юнита — отдельно от шагов урока."""
        unit = textbook.get_unit(self.ls_active(user_id))
        if not unit:
            await self.lesson_menu(chat_id, user_id)
            return
        errs = [x for x in self.db.lesson_errs(user_id, unit["unit"], need=DIARY_NEED) if x.get("ru")][:10]
        if not errs:
            await self.tg.send_message(chat_id, "📓 Открытых ошибок в дневнике юнита нет 👍", [[("🎓 К юниту", "ls:h")]])
            return
        items = [_card(x["pl"], x["ru"], x.get("translit", ""), _diary=x["id"]) for x in errs]
        await self.ls_ex(chat_id, user_id, unit, f"Работа над ошибками: {unit['book_short']} {unit['num']}", items,
                         nxt="h")

    async def lesson_diary(self, chat_id: int, user_id: int) -> None:
        unit = textbook.get_unit(self.ls_active(user_id))
        if not unit:
            return
        errs = self.db.lesson_errs(user_id, unit["unit"], open_only=False)
        if not errs:
            await self.tg.send_message(chat_id, "📓 Дневник ошибок пуст.", [[("🎓 К юниту", "ls:h")]])
            return
        lines = [f"📓 <b>Дневник ошибок · {escape(unit['book_short'])} {escape(unit['num'])}</b>",
                 f"<i>✅ закрыта ({DIARY_NEED} верных подряд) · ◐ один верный · ❌ открыта; ×N — сколько раз ошибся</i>", ""]
        for x in errs:
            icon = "✅" if x["streak"] >= DIARY_NEED else "◐" if x["streak"] else "❌"
            lines.append(f"{icon} <b>{escape(x['pl'])}</b>" + fmt._br(x)
                         + (f" — {escape(x['ru'])}" if x.get("ru") else "")
                         + (f" ×{x['wrong']}" if x["wrong"] > 1 else "") + f" <i>(урок {escape(x['lesson'] or '—')})</i>"
                         + (f"\n    <i>было: {escape(x['said'])}</i>" if x.get("said") else ""))
        n_open = sum(x["streak"] < DIARY_NEED for x in errs)
        rows = ([[(f"🔁 Работа над ошибками ({n_open})", "ls:err")]] if n_open else []) + [[("🎓 К юниту", "ls:h")]]
        await self.tg.send_message(chat_id, "\n".join(lines), rows)

    # ---------- ввод ученика: проба, свои предложения, вывод правила, ролевая игра, вопрос ----------

    async def ls_audio(self, chat_id: int, text: str, voice: dict | None):
        """(аудио | None, ок). Пустая запись — сообщение и ок=False."""
        if not voice or text:
            return None, True
        sec = voice.get("duration")
        if sec is not None and sec < VOICE_MIN_SEC:
            await self.tg.send_message(chat_id, "🎙 Запись пустая или слишком короткая — пришли ещё раз.")
            return None, False
        return await self.tg.download_file(voice["file_id"]), True

    def ls_corrs(self, user_id: int, raw) -> list[dict]:
        out = []
        for c in raw or []:
            if isinstance(c, dict) and c.get("correct"):
                c = {k: (str(v).strip() if isinstance(v, str) else v) for k, v in c.items()}
                c["rule"] = rules.normalize(c.get("rule"))
                c["source"] = "урок"
                out.append(c)
        return self.drop_ignored(user_id, out)

    def ls_pool(self, user_id: int, unit_id: str, st: dict, corrs: list[dict], attempt: str) -> None:
        """Ошибки языка — в общий пул (как в разговоре) и в дневник юнита."""
        if not corrs:
            return
        self.db.add_corrections(self.db.current_session(user_id), user_id,
                                [{**c, "ls": unit_id, "ls_attempt": attempt} for c in corrs])
        for c in corrs:
            self.ls_err(user_id, unit_id, st, c["correct"], c.get("ru", ""), c.get("translit", ""),
                        c.get("original", ""), c.get("why", ""))

    @staticmethod
    def ls_corr_rows(attempt: str, has: bool) -> list:
        return [[("📖 Правило", f"ls:r:{attempt}"), ("⭐ В словарь", f"dX:l:{attempt}"),
                 ("🙅 Не ошибка", f"ls:D:{attempt}")]] if has else []

    async def lesson_input(self, chat_id: int, user_id: int, pending: dict, text: str, voice: dict | None) -> None:
        step = pending["step"]
        if step == "ls_q":
            await self.ls_answer(chat_id, user_id, pending, text, voice)
            return
        ctx = self.ls_ctx(user_id)
        if not ctx or not ctx[3]:
            self.db.set_pending(user_id, None)
            await self.lesson_menu(chat_id, user_id)
            return
        unit, plan, st, lesson, steps = ctx
        audio, ok = await self.ls_audio(chat_id, text, voice)
        if not ok:
            return
        if step == "ls_talk":
            await self.ls_talk(chat_id, user_id, pending, unit, st, lesson, text, audio)
            return
        mode = pending.get("mode")
        if mode == "disc":
            d = lesson["discover"]
            data = await self.ask(chat_id, lesson_discover_prompt(d["examples"], d["question"], d["answer"], text,
                                                                  self.cfg.level, audio is not None),
                                  DISC_SCHEMA, DISC_HINT, wait="⏳ Читаю твой вывод…", audio=audio)
            if data is None:
                return
            await self.tg.send_message(chat_id, ("✅ " if data.get("ok") else "🤔 ") + escape(str(data.get("comment") or "")))
            await self.lesson_next(chat_id, user_id)
            return
        probe = mode == "probe"
        p = lesson.get("probe") or {}
        task = p.get("task", "") if probe else pending.get("task", "")
        prompt = lesson_say_prompt(unit, lesson, st["notes"], "probe" if probe else "own", task, p.get("sample", ""),
                                   self.cfg.level, audio is not None, text)
        if audio is not None and self.ignore_block(user_id):
            prompt = self.ignore_block(user_id) + "\n\n" + prompt
        data = await self.ask(chat_id, prompt, SAY_SCHEMA, SAY_HINT,
                              wait="⏳ Слушаю…" if audio is not None else "⏳ Читаю…", audio=audio)
        if data is None:
            return
        heard = str(data.get("heard") or "").strip() or text
        if not heard:
            await self.tg.send_message(chat_id, "🎙 Не расслышал речь — скажи ещё раз, поближе к микрофону.")
            return
        corrs = self.ls_corrs(user_id, data.get("corrections"))
        attempt = f"{unit['unit']}#{lesson['id']}@{int(self.clock() * 1000)}"
        out = []
        if audio is not None:
            out.append(f"🎙 <i>{escape(heard)}</i>")
        if probe:
            out.append("👍 Тебя бы поняли." if data.get("ok") else "Пока не совсем — этому и научимся.")
            sample = str(data.get("better_pl") or "").strip() or p.get("sample", "")
            if sample:
                t = tr(sample, str(data.get("better_translit") or "").strip() or p.get("sample_translit", ""))
                ru = str(data.get("better_ru") or "").strip() or p.get("sample_ru", "")
                out.append(f"✔️ <b>Можно так:</b> {escape(sample)}" + (f"\n[{escape(t)}]" if t else "")
                           + (f"\n— {escape(ru)}" if ru else ""))
            if corrs:
                out.append("👀 <b>На что обратить внимание</b> <i>(в ошибки не записываю)</i>\n" + "\n".join(_corr_lines(corrs[:3])))
            rows = []
        else:
            self.ls_pool(user_id, unit["unit"], st, corrs, attempt)
            self.db.lesson_set(user_id, unit["unit"], st)
            if corrs:
                out.append(f"✏️ <b>Исправления ({len(corrs)})</b>\n" + "\n".join(_corr_lines(corrs)))
                better = str(data.get("better_pl") or "").strip()
                if better:
                    out.append(f"✔️ <b>Правильно:</b> {escape(better)}"
                               + (f"\n[{escape(tr(better, data.get('better_translit')))}]"
                                  if tr(better, data.get("better_translit")) else "")
                               + (f"\n— {escape(str(data.get('better_ru') or ''))}" if data.get("better_ru") else ""))
            else:
                out.append("✅ Без ошибок")
            rows = self.ls_corr_rows(attempt, bool(corrs))
        if str(data.get("comment") or "").strip():
            out.append(f"💬 {escape(str(data['comment']).strip())}")
        out.append("<i>Хочешь ещё раз — просто пришли новый вариант.</i>")
        await self.tg.send_message(chat_id, "\n\n".join(out), rows + NAV)

    async def ls_talk(self, chat_id, user_id, pending, unit, st, lesson, text, audio) -> None:
        t = lesson["task"]
        turn = int(pending.get("turn", 0)) + 1
        last = turn >= TALK_TURNS
        prompt = lesson_talk_prompt(unit, lesson, st["notes"], t, pending["hist"], self.cfg.level, audio is not None,
                                    text, last)
        if audio is not None and self.ignore_block(user_id):
            prompt = self.ignore_block(user_id) + "\n\n" + prompt
        data = await self.ask(chat_id, prompt, TALK_SCHEMA, TALK_HINT, wait="", audio=audio)
        if data is None:
            return
        said = str(data.get("user_text") or "").strip() or text
        if not said:
            await self.tg.send_message(chat_id, "🎙 Не расслышал — скажи ещё раз.")
            return
        reply = str(data.get("reply_pl") or "").strip()
        targets = t.get("targets") or []
        used = set(pending.get("used") or [])
        for u in data.get("used") or []:
            hit = next((x for x in targets if exercises.norm(str(u)) and (
                exercises.norm(str(u)) in exercises.norm(x) or exercises.norm(x) in exercises.norm(str(u)))), None)
            if hit:
                used.add(hit)
        better = list(pending.get("better") or [])
        if str(data.get("better_pl") or "").strip() and exercises.norm(data["better_pl"]) != exercises.norm(said):
            better.append([said, str(data["better_pl"]).strip(), str(data.get("better_ru") or "").strip()])
        pending = {**pending, "turn": turn, "used": sorted(used), "better": better,
                   "done": bool(pending.get("done")) or bool(data.get("done")),
                   "corr": list(pending.get("corr") or []) + [c for c in data.get("corrections") or [] if isinstance(c, dict)],
                   "hist": pending["hist"] + [["user", said], ["model", reply]]}
        self.db.set_pending(user_id, pending)
        head = f"🎙 <i>{escape(said)}</i>\n\n" if audio is not None else ""
        finish = pending["done"] or last
        if reply:
            await self.tg.send_message(
                chat_id, head + "💬 " + _spoiler(reply, str(data.get("reply_translit") or ""), str(data.get("reply_ru") or "")),
                None if finish else [[("🏁 Закончить и разобрать", "ls:tf")]])
            await self.ls_voice(chat_id, reply)
        if finish:
            await self.ls_talk_finish(chat_id, user_id, pending)

    async def ls_talk_finish(self, chat_id: int, user_id: int, pending: dict) -> None:
        ctx = self.ls_ctx(user_id)
        if not ctx or not ctx[3] or pending.get("step") != "ls_talk":
            return
        unit, plan, st, lesson, steps = ctx
        t = lesson["task"]
        self.db.set_pending(user_id, None)
        if not pending.get("turn"):
            await self.tg.send_message(chat_id, "🎭 Игра не началась — разбирать нечего.",
                                       [[("🔁 Сыграть", "ls:c")]] + NAV)
            return
        corrs, seen = [], set()
        for c in self.ls_corrs(user_id, pending.get("corr")):
            key = exercises.norm(c["correct"])
            if key not in seen:
                seen.add(key)
                corrs.append(c)
        self.ls_pool(user_id, unit["unit"], st, corrs, pending["attempt"])
        targets, used = t.get("targets") or [], set(pending.get("used") or [])
        need = int(t.get("min_targets") or 3)
        ok = bool(pending.get("done")) and len(used) >= min(need, len(targets))
        st["cur"]["task"] = {"ok": ok, "used": len(used)}
        self.db.lesson_set(user_id, unit["unit"], st)
        out = [f"🎭 <b>Разбор: {escape(t['title'])}</b>",
               ("✅ Цель достигнута" if pending.get("done") else "❌ Цель не достигнута") + f": {escape(t['goal'])}",
               f"🧩 <b>Фразы урока: {len(used)} из {len(targets)}</b> (нужно {need})\n"
               + "\n".join(("✅ " if x in used else "▫️ ") + escape(x) for x in targets)]
        if corrs:
            out.append(f"✏️ <b>Исправления ({len(corrs)})</b>\n" + "\n".join(_corr_lines(corrs)))
        else:
            out.append("✅ Ошибок языка нет")
        if pending.get("better"):
            out.append("⬆️ <b>Как сказать лучше</b>\n" + "\n".join(
                f"• {escape(a)} → <b>{escape(b)}</b>" + (f" — {escape(r)}" if r else "") for a, b, r in pending["better"][:6]))
        out.append("🎉 Задача выполнена!" if ok else "<i>Задача пока не засчитана — можно сыграть ещё раз: получится лучше.</i>")
        await self.tg.send_message(chat_id, "\n\n".join(out),
                                   self.ls_corr_rows(pending["attempt"], bool(corrs)) + [[("🔁 Сыграть ещё раз", "ls:c")]] + NAV)

    # ---------- ❓ вопрос учителю ----------

    # ---------- 📖 правила: в любой момент урока ----------

    def ls_all_rules(self, unit: dict, plan: list) -> list[tuple[str, dict]]:
        """Все правила юнита: выжимка (u0, u1…) + правила отдельных уроков (x<урок>.<n>)."""
        out = [(f"u{i}", r) for i, r in enumerate((unit.get("review") or {}).get("rules") or [])]
        for x in plan:
            out += [(f"x{x['id']}.{j}", r) for j, r in enumerate(x.get("extra_rules") or [])]
        return out

    def ls_rule_by(self, user_id: int, key: str) -> dict | None:
        ctx = self.ls_ctx(user_id)
        return dict(self.ls_all_rules(ctx[0], ctx[1])).get(key) if ctx else None

    async def ls_rules_show(self, chat_id: int, user_id: int, key: str | None = None) -> None:
        """ls:rl — правила текущего урока (нет урока — список всех); ls:ri:<ключ> — одно правило. Шаг не меняется."""
        ctx = self.ls_ctx(user_id)
        if not ctx:
            await self.tg.send_message(chat_id, "Правил не нашлось — открой урок: /lesson")
            return
        unit, plan, st, lesson, steps = ctx
        if key:
            r = dict(self.ls_all_rules(unit, plan)).get(key)
            sel = [(key, r)] if r else []
        else:
            keys = {id(r): k for k, r in self.ls_all_rules(unit, plan)}
            sel = [(keys.get(id(r), ""), r) for r in self.ls_rules(unit, lesson)] if lesson else []
        if not sel:
            await self.ls_rules_list(chat_id, user_id)
            return
        rows = [[(f"❓ Спросить: {r.get('title', '')}"[:60], f"ls:ask:{k}")] for k, r in sel if k]
        rows.append([("📚 Все правила юнита", "ls:ra"), ("↩️ К уроку", "ls:qx")])
        head = "📖 <b>Правило урока</b>\n\n" if not key else ""
        await self.tg.send_message(chat_id, head + fmt.rules_message({"rules": [r for _, r in sel]}), rows)

    async def ls_rules_list(self, chat_id: int, user_id: int) -> None:
        ctx = self.ls_ctx(user_id)
        if not ctx:
            return
        unit, plan = ctx[0], ctx[1]
        rows = [[(f"📖 {r.get('title', '')}"[:60], f"ls:ri:{k}")] for k, r in self.ls_all_rules(unit, plan)]
        rows.append([("❓ Вопрос учителю", "ls:ask"), ("↩️ К уроку", "ls:qx")])
        await self.tg.send_message(chat_id, f"📚 <b>Правила юнита {escape(unit['num'])}</b> — какое открыть?", rows)

    async def ls_ask_start(self, chat_id: int, user_id: int, pending: dict | None, rule_key: str = "") -> None:
        prev = pending if pending and pending.get("step") != "ls_q" else (pending or {}).get("prev")
        self.db.set_pending(user_id, {"step": "ls_q", "hist": [], "prev": prev, "rule": rule_key})
        r = self.ls_rule_by(user_id, rule_key) if rule_key else None
        if r:
            await self.tg.send_message(chat_id, f"❓ <b>{escape(r.get('title', ''))}</b>\n\nСпрашивай про это правило — "
                                                "текстом или голосом. Можно несколько вопросов подряд.",
                                       [[("↩️ Вернуться к уроку", "ls:qx")]])
            return
        await self.tg.send_message(chat_id, "❓ Спрашивай — текстом или голосом, по-русски: про правило, фразу, разницу "
                                            "между словами. Можно несколько вопросов подряд.",
                                   [[("↩️ Вернуться к уроку", "ls:qx")]])

    async def ls_answer(self, chat_id: int, user_id: int, pending: dict, text: str, voice: dict | None) -> None:
        ctx = self.ls_ctx(user_id)
        if not ctx:
            self.db.set_pending(user_id, None)
            return
        unit, plan, st, lesson, steps = ctx
        audio, ok = await self.ls_audio(chat_id, text, voice)
        if not ok:
            return
        sel = self.ls_rules(unit, lesson) if lesson else []
        chunks = "; ".join(f"{c['pl']} — {c['ru']}" for c in (lesson or {}).get("chunks") or [])
        picked = self.ls_rule_by(user_id, pending.get("rule") or "") if pending.get("rule") else None
        rule = picked or sel[0] if picked or len(sel) == 1 else {
            "title": (lesson or {}).get("title", unit["title"]),
            "explanation": " ".join(f"{r.get('title', '')}: {r.get('explanation', '')}" for r in sel)
                           + (f" Фразы урока: {chunks}" if chunks else "")}
        hist = [tuple(h) for h in pending.get("hist") or []]
        title = f"{unit['book_short']} {unit['num']} · {unit.get('title', '')}"
        data = await self.ask(chat_id, review_qa_prompt(rule, title, hist, text, self.cfg.level, voice=audio is not None),
                              QA_SCHEMA, QA_HINT, wait="⏳ Думаю над ответом…", audio=audio)
        if data is None:
            return
        answer = str(data.get("answer") or "").strip() or "Не получилось ответить — спроси иначе."
        hist = (hist + [("user", text or "🎙 голосовой вопрос"), ("model", answer)])[-QA_HISTORY:]
        self.db.set_pending(user_id, {**pending, "hist": [list(h) for h in hist]})
        await self.tg.send_message(chat_id, fmt.review_answer(rule, answer, data.get("examples") or []),
                                   [[("↩️ Вернуться к уроку", "ls:qx")]])

    # ---------- кнопки ----------

    async def lesson_callback(self, chat_id: int, user_id: int, message_id: int, pending: dict | None,
                              arg: str) -> str | None:
        """ls:m — юниты; ls:u:<юнит> — юнит; ls:h — активный юнит; ls:s:<юнит>|<урок> — начать урок; ls:c — текущий шаг;
        ls:n — дальше; ls:tf — закончить игру; ls:ask / ls:qx — вопрос учителю; ls:err — работа над ошибками;
        ls:d — дневник; ls:r / ls:D / ls:x / ls:k — правило по ошибкам и «🙅 Не ошибка» для попытки."""
        kind, _, rest = arg.partition(":")
        if kind == "m":
            self.db.lesson_set(user_id, "", {})
            await self.lesson_menu(chat_id, user_id, message_id)
        elif kind == "u":
            await self.lesson_home(chat_id, user_id, rest, message_id)
        elif kind == "h":
            if (pending or {}).get("step", "").startswith(("ls_", "ex_", "bex")):
                self.db.set_pending(user_id, None)
            await self.lesson_home(chat_id, user_id, self.ls_active(user_id))
        elif kind == "s":
            unit_id, _, lesson_id = rest.partition("|")
            await self.lesson_start(chat_id, user_id, unit_id, lesson_id)
        elif kind == "c":
            self.db.set_pending(user_id, None)
            await self.lesson_step(chat_id, user_id)
        elif kind == "n":
            await self.lesson_next(chat_id, user_id)
        elif kind == "tf":
            if (pending or {}).get("step") != "ls_talk":
                return "Игра уже закончена"
            await self.ls_talk_finish(chat_id, user_id, pending)
        elif kind == "ask":
            await self.ls_ask_start(chat_id, user_id, pending, rest)
        elif kind == "rl":
            await self.ls_rules_show(chat_id, user_id)
        elif kind == "ra":
            await self.ls_rules_list(chat_id, user_id)
        elif kind == "ri":
            await self.ls_rules_show(chat_id, user_id, rest)
        elif kind == "qx":
            prev = (pending or {}).get("prev") if (pending or {}).get("step") == "ls_q" else None
            self.db.set_pending(user_id, prev)
            await self.tg.send_message(chat_id, "↩️ Вернулись к уроку — продолжай с того же места.",
                                       [[("🎓 Показать шаг ещё раз", "ls:c"), ("➡️ Дальше", "ls:n")]])
        elif kind == "err":
            await self.lesson_errors_ex(chat_id, user_id)
        elif kind == "d":
            await self.lesson_diary(chat_id, user_id)
        elif kind == "r":
            corrs = [c for c in self.db.corrections_by(user_id, "ls_attempt", rest) if not c["_disputed"]]
            if not corrs:
                return "Ошибок нет"
            data = await self.ask(chat_id, rules_for_errors_prompt(corrs, self.cfg.level), RULES_SCHEMA, RULES_HINT)
            if data is not None:
                await self.tg.send_message(chat_id, fmt.rules_message(data))
        elif kind in ("D", "x", "k"):
            return await self.ls_dispute(chat_id, user_id, message_id, kind, rest)
        return None

    async def ls_dispute(self, chat_id: int, user_id: int, message_id: int, kind: str, arg: str) -> str | None:
        """ls:D:<попытка> — экран «Не ошибка»; ls:x:<id ошибки> — отметить / вернуть; ls:k:<попытка> — готово."""
        edit = None
        if kind == "x":
            c = self.db.correction(int(arg)) if arg.isdigit() else None
            if not c or c["_user"] != user_id or not c.get("ls_attempt"):
                return "Ошибка не найдена"
            self.dispute_toggle(user_id, c)
            arg, kind, edit = c["ls_attempt"], "D", message_id
        corrs = self.db.corrections_by(user_id, "ls_attempt", arg)
        if not corrs:
            return "Ошибок нет"
        if kind == "k":
            await self.tg.edit_message(chat_id, message_id, fmt.dispute_done(sum(c["_disputed"] for c in corrs)))
            return None
        text, rows = fmt.dispute_screen(corrs, 0, toggle="ls:x:", done=f"ls:k:{arg}")
        if edit:
            await self.tg.edit_message(chat_id, edit, text, rows)
        else:
            await self.tg.send_message(chat_id, text, rows)
        return None
