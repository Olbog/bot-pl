"""📝 Упражнения из книги (учебник 📗 и рабочая тетрадь 📒) внутри юнита.

Упражнение лежит в unit_NN.json → "exercises":
{"id": "tb-8-3", "src": "tb" | "wb", "ref": "Ćw. 3, s. 54", "title": "Uzupełnij…", "title_ru": "Вставь …",
 "task_ru": "условие по-русски", "type": "gap" | "choice" | "match" | "tf" | "open" | "free",
 "audio": "108a2.mp3" (или список), "text": "текст к упражнению", "options": ["…"] (общие варианты для match),
 "items": [{"q": "Ja ___ (być) studentem.", "ru": "перевод", "answer": "jestem", "accepted": ["…"],
            "options": ["…"] (для choice), "full_pl": "…", "translit": "…", "hint": "подсказка 💡",
            "grammar": "…", "rule": "…"}]}
Картинки из книги — текстом: русская подсказка или эмодзи (🍐 → gruszka).

Отвечать можно частями: проверяются только отвеченные пункты, результат по каждому пункту хранится
в таблице book_ex — можно вернуться позже и продолжить. Шаг pending «bex»: {unit, ex, pick, sure, notes, msg}.
"""
import asyncio
import os
from pathlib import Path

from ..core import textbook
from ..ai.prompt import CHECK_HINT, CHECK_SCHEMA, check_prompt
from ..ui import fmt
from ..common import log
from ..exercises import logic
from ..settings import BOOKS_DIR, EX_EXPLAIN_ALL, PAGE_DPI

ROOT = Path(__file__).resolve().parent.parent.parent   # корень проекта: там папка Books (на сервере, не в git)
PAGES_DIR = ROOT / "data" / "pages"   # кэш картинок страниц (data/ — не в git)
TF_TYPED = {"p": "a", "prawda": "a", "n": "b", "nieprawda": "b", "fałsz": "b"}


def find_audio(audio_dir: str, name: str, root: Path | None = None) -> Path | None:
    """Файл аудио в Books/<audio_dir> — прямо или в подпапках, без учёта регистра."""
    base = (root or ROOT / BOOKS_DIR) / audio_dir
    if (base / name).is_file():
        return base / name
    if not base.is_dir():
        return None
    for dirpath, _, files in os.walk(base):
        for f in files:
            if f.lower() == name.lower():
                return Path(dirpath) / f
    return None


def _find(unit: dict, ex_id: str) -> dict | None:
    return next((x for x in unit.get("exercises") or [] if x["id"] == ex_id), None)


class BookExMixin:
    books_root: Path | None = None   # тесты подставляют свою папку Books
    pages_dir: Path = PAGES_DIR

    async def render_page(self, pdf: Path, n: int, out: Path) -> bool:
        """Страница n PDF → PNG (pdftoppm из poppler-utils)."""
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                "pdftoppm", "-f", str(n), "-l", str(n), "-r", str(PAGE_DPI), "-png", "-singlefile",
                str(pdf), str(out.with_suffix("")), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), 60)
        except (OSError, asyncio.TimeoutError) as e:
            log.warning(f"pdftoppm: {e}")
            return False
        if proc.returncode != 0:
            log.warning(f"pdftoppm {pdf.name} s.{n}: {err.decode(errors='ignore')[:200]}")
        return out.is_file()

    async def bex_pages(self, chat_id: int, unit: dict, x: dict) -> None:
        """📄 Страницы книги к упражнению — картинкой из PDF в Books/ (сам текст в репозиторий не попадает)."""
        src = x.get("src", "tb")
        rel = (unit.get("files") or {}).get(src)
        pdf = (self.books_root or ROOT / BOOKS_DIR) / rel if rel else None
        if not pdf or not pdf.is_file():
            await self.tg.send_message(chat_id, f"📄 PDF не нашёлся в папке Books на сервере: {fmt.e(rel or '—')}")
            return
        shift = (unit.get("page_offset") or {}).get(src, 0)
        for page in logic.book_pages(x):
            n = page + shift
            out = self.pages_dir / f"{unit['book']}_{src}_{n:03d}.png"
            await self.tg.send_action(chat_id, "upload_photo")
            if not out.is_file() and not await self.render_page(pdf, n, out):
                await self.tg.send_message(chat_id, f"📄 Не получилось вырезать страницу {page} из PDF.")
                return
            what = "тетрадь" if src == "wb" else "учебник"
            await self.tg.send_photo(chat_id, out.name, out.read_bytes(),
                                     f"📄 {fmt.e(unit['book_short'])} · {what}, s. {page}")

    def bex_get(self, pending: dict | None) -> tuple[dict, dict] | None:
        if (pending or {}).get("step") != "bex":
            return None
        unit = textbook.get_unit(pending.get("unit"))
        x = _find(unit, pending.get("ex")) if unit else None
        return (unit, x) if x else None

    async def bex_callback(self, chat_id: int, user_id: int, message_id: int, pending: dict | None,
                           action: str) -> None:
        """bx:l:<юнит> — список; bx:o:<юнит>|<упр> — открыть; bx:r:<юнит>|<упр> — начать заново;
        bx:p:<юнит>|<упр> — страница книги картинкой."""
        kind, _, arg = action.partition(":")
        unit_id, _, ex_id = arg.partition("|")
        unit = textbook.get_unit(unit_id)
        if not unit:
            return
        if kind == "l":
            text, buttons = fmt.bex_list_screen(unit, self.db.bex_results(user_id, unit["unit"]))
            await self.show(chat_id, user_id, {"step": "bex_list", "unit": unit["unit"]}, text, buttons)
        elif kind == "p":
            x = _find(unit, ex_id)
            if x:
                await self.bex_pages(chat_id, unit, x)
        elif kind in ("o", "r"):
            x = _find(unit, ex_id)
            if not x:
                await self.tg.send_message(chat_id, "Этого упражнения больше нет — открой список заново.")
                return
            if kind == "r":
                self.db.bex_reset(user_id, unit["unit"], x["id"])
            await self.bex_open(chat_id, user_id, unit, x)

    async def bex_open(self, chat_id: int, user_id: int, unit: dict, x: dict) -> None:
        audio = x.get("audio") or []
        for name in [audio] if isinstance(audio, str) else audio:
            path = find_audio(unit.get("audio_dir", ""), name, self.books_root)
            if path:
                await self.tg.send_action(chat_id, "upload_voice")
                await self.tg.send_audio(chat_id, path.name, path.read_bytes(), f"🎧 {fmt.e(x.get('ref', ''))}")
            else:
                await self.tg.send_message(chat_id, f"🎧 Аудио {fmt.e(name)} не нашлось в папке Books на сервере.")
        done = self.db.bex_results(user_id, unit["unit"])
        sent = await self.tg.send_message(chat_id, fmt.bex_message(unit, x, done),
                                          fmt.bex_keyboard(unit, x, done, {}, []))
        self.db.set_pending(user_id, {"step": "bex", "unit": unit["unit"], "ex": x["id"], "pick": {}, "sure": [],
                                      "notes": {}, "msg": (sent or {}).get("message_id")})

    async def bex_tap(self, chat_id: int, user_id: int, pending: dict | None, action: str) -> tuple[str | None, bool]:
        """bx:a:<n>:<буква|!> — выбор; bx:h:<n> — подсказка; bx:go — проверить. Возвращает (всплывашка, окно)."""
        got = self.bex_get(pending)
        if not got:
            return "Это упражнение уже закрыто — открой его заново из списка", False
        unit, x = got
        parts = action.split(":")
        if parts[0] == "h":
            n = int(parts[1])
            hint = x["items"][n - 1].get("hint") if 1 <= n <= len(x["items"]) else ""
            return (f"💡{n}: {hint}" if hint else "Для этого пункта подсказки нет"), True
        pend = dict(pending)
        if parts[0] == "go":
            if not pend["pick"]:
                return "Ничего не отмечено — выбери ответы кнопками", False
            answers = {int(n): {"answer": L, "unsure": False, "sure": int(n) in pend["sure"],
                                "note": pend["notes"].get(n, "")} for n, L in pend["pick"].items()}
            await self.bex_check(chat_id, user_id, pend, unit, x, answers)
            return None, False
        n, choice = int(parts[1]), parts[2]
        if not 1 <= n <= len(x["items"]):
            return None, False
        if choice == "!":
            pend["sure"] = sorted(set(pend["sure"]) ^ {n})
        elif pend["pick"].get(str(n)) == choice:
            pend["pick"].pop(str(n))   # повторное нажатие снимает выбор
        else:
            pend["pick"][str(n)] = choice
        self.db.set_pending(user_id, pend)
        if pend.get("msg"):
            done = self.db.bex_results(user_id, unit["unit"])
            await self.tg.edit_markup(chat_id, pend["msg"], fmt.bex_keyboard(unit, x, done, pend["pick"], pend["sure"]))
        return None, False

    async def bex_text(self, chat_id: int, user_id: int, pending: dict, text: str, voice: dict | None) -> None:
        """Ответ текстом на открытое упражнение: на любые пункты; к кнопкам добавляется то, что написано."""
        got = self.bex_get(pending)
        if not got:
            self.db.set_pending(user_id, None)
            return
        unit, x = got
        if voice and not text:
            await self.tg.send_message(chat_id, "🎙 Голосом здесь пока нельзя — ответь текстом: «1 … 2 …».")
            return
        answers = logic.parse_answers(text, len(x["items"]))
        choice = x.get("type") in logic.CHOICE_TYPES
        for n, a in answers.items():
            if choice and x.get("type") == "tf":
                a["answer"] = TF_TYPED.get(logic.norm(a["answer"]), a["answer"])
            if choice and not a["answer"] and str(n) in pending["pick"]:
                a["answer"] = pending["pick"][str(n)]   # «2 (почему?)» — уточнение к нажатому ответу
        for n, L in pending["pick"].items():
            answers.setdefault(int(n), {"answer": L, "unsure": False, "sure": int(n) in pending["sure"],
                                        "note": pending["notes"].get(n, "")})
        if not any(a["answer"] for a in answers.values()):
            await self.tg.send_message(chat_id, "Не нашёл ответов. Пиши с номерами пунктов: «1 jestem 3 mam».")
            return
        await self.bex_check(chat_id, user_id, pending, unit, x, answers, text=text)

    async def bex_check(self, chat_id: int, user_id: int, pending: dict, unit: dict, x: dict,
                        answers: dict[int, dict], text: str = "") -> None:
        kind = x.get("type", "gap")
        choice = kind in logic.CHOICE_TYPES
        items = [{**it, "options": logic.book_options(x, it)} if choice else it for it in x["items"]]
        results = [r for r in logic.quick_check(items, answers, "test" if choice else "gap")
                   if r["status"] != "missing"]
        if not results:
            await self.tg.send_message(chat_id, "Не нашёл ответов. Пиши с номерами пунктов: «1 jestem 3 mam».")
            return
        if kind == "free":
            for r in results:
                r["status"] = "check"   # эталон — лишь пример: решает модель
        todo = logic.needs_model(results, EX_EXPLAIN_ALL)
        verdicts: dict[int, dict] = {}
        if todo:
            task = " — ".join(t for t in (x.get("title"), x.get("task_ru")) if t)
            if x.get("text"):
                task += f"\nТекст: {x['text']}"
            prompt = check_prompt([(r["n"], items[r["n"] - 1], r["user"], r["unsure"], r.get("note", ""))
                                   for r in todo], self.cfg.level, voice=False, free=kind == "free", task=task)
            data = await self.ask(chat_id, prompt, CHECK_SCHEMA, CHECK_HINT,
                                  wait=f"⏳ Проверяю {fmt.bex_name(x)}…")
            if data is None:
                return   # шаг bex сохраняется — можно прислать ответ ещё раз
            verdicts = {int(v.get("n", 0)): v for v in data.get("items") or [] if isinstance(v, dict)}
        for r in results:
            v = verdicts.get(r["n"], {})
            r["explanation"], r["bridge"] = v.get("explanation", ""), v.get("bridge", "")
            if r.get("note"):
                r["note_ok"] = bool(v.get("note_ok", True))
                r["note_comment"] = str(v.get("note_comment") or "")
            if r["status"] == "ok":
                right = True
            elif r["status"] == "check":
                right = bool(v.get("correct"))
            else:
                right = False
                if r["status"] == "diacritics" and not r["explanation"]:
                    r["explanation"] = "Нужны польские буквы — без них это другое слово или ошибка."
            r["final"] = "ok" if right else "wrong"
            if right and r.get("note") and not r["note_ok"]:
                r["final"], r["logic_wrong"] = "wrong", True
            self.db.bex_save(user_id, unit["unit"], x["id"], r["n"], r["final"] == "ok", r["user"],
                             r["explanation"])
        if text and logic.unclosed_note(text):
            await self.tg.send_message(chat_id, "⚠️ Не закрыта скобка — всё после «(» до конца сообщения "
                                                 "я посчитал уточнением.")
        if pending.get("msg"):
            await self.tg.edit_markup(chat_id, pending["msg"])   # кнопки старого сообщения больше не нужны
        self.db.set_pending(user_id, None)
        done = self.db.bex_results(user_id, unit["unit"])
        ok_now = sum(1 for r in results if r["final"] == "ok")
        ok_all, _ = fmt.bex_counts(x, done)
        head = (f"📊 <b>{ok_now} из {len(results)}</b> · {fmt.e(fmt.bex_name(x))}\n"
                f"<i>Всего в упражнении верно {ok_all} из {len(x['items'])}.</i>")
        await self.tg.send_message(chat_id, fmt.ex_results({"items": items}, results, head=head, foot=""),
                                   fmt.bex_result_buttons(unit, x, done))
