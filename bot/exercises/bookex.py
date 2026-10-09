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
import unicodedata
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


def _key(name: str) -> str:
    """Имя файла/папки для сравнения: без регистра, пробелов, «_», знаков и польских букв."""
    name = unicodedata.normalize("NFKD", name.lower().replace("ł", "l"))
    return "".join(ch for ch in name if ch.isalnum() and not unicodedata.combining(ch))


def resolve(root: Path, rel: str) -> Path | None:
    """Путь внутри Books/: сначала как есть, иначе по частям с нестрогим сравнением имён —
    на сервере и на ноутбуке папки могут называться чуть по-разному («Krok po Kroku» / «Krok_po_kroku»)."""
    path = root / rel
    if path.exists():
        return path
    cur = root
    for part in Path(rel).parts:
        if (cur / part).exists():
            cur = cur / part
            continue
        if not cur.is_dir():
            return None
        match = next((c for c in sorted(cur.iterdir()) if _key(c.name) == _key(part)), None)
        if match is None:
            return None
        cur = match
    return cur


def find_audio(audio_dir: str, name: str, root: Path | None = None) -> Path | None:
    """Файл аудио в Books/<audio_dir> — прямо или в подпапках, имя сравнивается нестрого."""
    base = resolve(root or ROOT / BOOKS_DIR, audio_dir) if audio_dir else None
    if base is None or not base.is_dir():
        return None
    if (base / name).is_file():
        return base / name
    for dirpath, _, files in os.walk(base):
        for f in files:
            if _key(f) == _key(name):
                return Path(dirpath) / f
    return None


def audio_dir_of(unit: dict, src: str) -> str:
    d = unit.get("audio_dir") or ""
    return d.get(src) or d.get("tb", "") if isinstance(d, dict) else d


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
        pdf = resolve(self.books_root or ROOT / BOOKS_DIR, rel) if rel else None
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
        bx:p:<юнит>|<упр> — страница книги картинкой; bx:v:<юнит>|<упр> — выполненная попытка целиком.
        «Заново» стирает только текущую попытку: выполненные на 100% хранятся в book_ex_done."""
        kind, _, arg = action.partition(":")
        unit_id, _, ex_id = arg.partition("|")
        unit = textbook.get_unit(unit_id)
        if not unit:
            return
        if kind == "l":
            text, buttons = fmt.bex_list_screen(unit, self.db.bex_results(user_id, unit["unit"]),
                                                self.db.bex_done(user_id, unit["unit"]))
            await self.show(chat_id, user_id, {"step": "bex_list", "unit": unit["unit"]}, text, buttons)
        elif kind == "p":
            x = _find(unit, ex_id)
            if x:
                await self.bex_pages(chat_id, unit, x)
        elif kind in ("m", "M", "w"):   # ✋ засчитать вручную: всё упражнение (m — спросить, M — да) или ошибки проверки
            ex_id, _, ns = ex_id.partition("|")
            x = _find(unit, ex_id)
            if not x:
                return
            done = self.db.bex_results(user_id, unit["unit"])
            todo = [n for n in range(1, len(x["items"]) + 1) if not (done.get((x["id"], n)) or {}).get("ok")]
            if kind == "m":
                await self.tg.send_message(
                    chat_id, f"✋ Засчитать вручную всё упражнение «{fmt.e(fmt.bex_name(x))}»? Невыполненных пунктов: "
                             f"{len(todo)}. В списке оно будет помечено ☑️ — «100%, часть вручную».",
                    [[("✋ Да, засчитать", f"bx:M:{unit['unit']}|{x['id']}"), ("Нет", f"bx:o:{unit['unit']}|{x['id']}")]])
                return
            ok_before = fmt.bex_counts(x, done)[0]
            want = todo if kind == "M" else [int(n) for n in ns.split(",") if n.isdigit()]
            changed = self.db.bex_mark_manual(user_id, unit["unit"], x["id"], want)
            if message_id:
                await self.tg.edit_markup(chat_id, message_id)
            await self.bex_after_manual(chat_id, user_id, pending if (pending or {}).get("step") == "bex" else None,
                                        unit, x, changed, ok_before)
        elif kind == "v":   # 📜 выполненная попытка — все ответы сразу
            x = _find(unit, ex_id)
            attempts = self.db.bex_done(user_id, unit["unit"]).get(ex_id) if x else None
            if attempts:
                await self.tg.send_message(chat_id, fmt.bex_attempt_view(unit, x, attempts, self.clock()),
                                           [[("▶️ Открыть упражнение", f"bx:o:{unit['unit']}|{x['id']}")]])
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
            path = find_audio(audio_dir_of(unit, x.get("src", "tb")), name, self.books_root)
            if path:
                await self.tg.send_action(chat_id, "upload_voice")
                await self.tg.send_audio(chat_id, path.name, path.read_bytes(), f"🎧 {fmt.e(x.get('ref', ''))}")
            else:
                await self.tg.send_message(chat_id, f"🎧 Аудио {fmt.e(name)} не нашлось в папке Books на сервере.")
        done = self.db.bex_results(user_id, unit["unit"])
        wins = self.bex_wins(user_id, unit, x)
        sent = await self.tg.send_message(chat_id, fmt.bex_message(unit, x, done, wins=wins),
                                          fmt.bex_keyboard(unit, x, done, {}, [], wins))
        self.db.set_pending(user_id, {"step": "bex", "unit": unit["unit"], "ex": x["id"], "pick": {}, "sure": [],
                                      "notes": {}, "msg": (sent or {}).get("message_id")})

    def bex_wins(self, user_id: int, unit: dict, x: dict) -> int:
        return len(self.db.bex_done(user_id, unit["unit"]).get(x["id"]) or [])

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
            await self.tg.edit_markup(chat_id, pend["msg"], fmt.bex_keyboard(unit, x, done, pend["pick"], pend["sure"],
                                                                          self.bex_wins(user_id, unit, x)))
        return None, False

    async def bex_text(self, chat_id: int, user_id: int, pending: dict, text: str, voice: dict | None) -> None:
        """Ответ текстом на открытое упражнение: на любые пункты; к кнопкам добавляется то, что написано."""
        got = self.bex_get(pending)
        if not got:
            self.db.set_pending(user_id, None)
            return
        unit, x = got
        if voice and not text:
            await self.bex_voice(chat_id, user_id, pending, unit, x, voice)
            return
        numeric = any(ch.isdigit() for it in x["items"] for ch in str(it.get("answer", "")))
        answers = logic.parse_answers(text, len(x["items"]), numeric=numeric)
        choice = x.get("type") in logic.CHOICE_TYPES
        for n, a in answers.items():
            if choice and x.get("type") == "tf":
                a["answer"] = TF_TYPED.get(logic.norm(a["answer"]), a["answer"])
            if choice and not a["answer"] and str(n) in pending["pick"]:
                a["answer"] = pending["pick"][str(n)]   # «2 (почему?)» — уточнение к нажатому ответу
        for n, L in pending["pick"].items():
            answers.setdefault(int(n), {"answer": L, "unsure": False, "sure": int(n) in pending["sure"],
                                        "note": pending["notes"].get(n, "")})
        if not any(a["answer"] or a.get("skip") or a.get("done") for a in answers.values()):
            await self.tg.send_message(chat_id, "Не нашёл ответов. Пиши с номерами пунктов: «1 jestem 3 mam».")
            return
        await self.bex_check(chat_id, user_id, pending, unit, x, answers, text=text, gaps=True)

    async def bex_voice(self, chat_id: int, user_id: int, pending: dict, unit: dict, x: dict, voice: dict) -> None:
        """🎙 Ответ голосом: «один — jestem, три — mam» (номера в любом порядке, «не знаю» — пропуск).
        Модель в одном запросе распознаёт и проверяет; дальше — как ответ текстом."""
        kind = x.get("type", "gap")
        choice = kind in logic.CHOICE_TYPES
        items = [{**it, "options": logic.book_options(x, it)} if choice else it for it in x["items"]]
        done = self.db.bex_results(user_id, unit["unit"])
        todo = [(n, it) for n, it in enumerate(items, 1) if not (done.get((x["id"], n)) or {}).get("ok")]
        if not todo:
            await self.tg.send_message(chat_id, "Здесь всё уже верно 🎉")
            return
        audio = await self.tg.download_file(voice["file_id"])
        verdicts = await self.ask_voice(chat_id, user_id, todo, audio, wait=f"⏳ Слушаю и проверяю {fmt.bex_name(x)}…",
                                        task=self.bex_task(x), free=kind == "free")
        if verdicts is None:
            return   # шаг bex сохраняется — можно прислать ещё раз
        if not verdicts:
            await self.tg.send_message(chat_id, "🎙 Не расслышал ответов. Называй номер пункта и ответ: "
                                                 "«один — jestem, три — mam».")
            return
        answers: dict[int, dict] = {}
        for n, v in verdicts.items():
            heard = str(v.get("heard") or "").strip()
            skip = not heard or heard in ("-", "—")
            answers[n] = {"answer": "" if skip else heard, "unsure": False, "sure": False,
                          "note": str(v.get("note") or "").strip(), **({"skip": True} if skip else {})}
        await self.bex_check(chat_id, user_id, pending, unit, x, answers, gaps=True, voice_verdicts=verdicts)

    @staticmethod
    def bex_task(x: dict) -> str:
        task = " — ".join(t for t in (x.get("title"), x.get("task_ru")) if t)
        return task + (f"\nТекст: {x['text']}" if x.get("text") else "")

    async def bex_check(self, chat_id: int, user_id: int, pending: dict, unit: dict, x: dict,
                        answers: dict[int, dict], text: str = "", gaps: bool = False,
                        voice_verdicts: dict[int, dict] | None = None) -> None:
        """Проверка отвеченных пунктов. gaps (ответ текстом): «4-» и пропущенные пункты до последнего
        названного номера — ошибка с правильным ответом; пункты после него остаются на потом."""
        kind = x.get("type", "gap")
        choice = kind in logic.CHOICE_TYPES
        items = [{**it, "options": logic.book_options(x, it)} if choice else it for it in x["items"]]
        before = self.db.bex_results(user_id, unit["unit"])
        ok_before = fmt.bex_counts(x, before)[0]
        last = max((n for n, a in answers.items() if a["answer"] or a.get("skip") or a.get("done")), default=0)
        manual = sorted(n for n, a in answers.items() if a.get("done"))   # «5+» — засчитать вручную ✋
        answers = {n: a for n, a in answers.items() if not a.get("done")}

        def take(r: dict) -> bool:
            if r["n"] in manual:
                return False
            if r["status"] != "missing":
                return True
            if not gaps:
                return False
            if (answers.get(r["n"]) or {}).get("skip"):
                return True
            return r["n"] < last and not (before.get((x["id"], r["n"])) or {}).get("ok")
        results = [r for r in logic.quick_check(items, answers, "test" if choice else "gap") if take(r)]
        if manual:
            self.db.bex_mark_manual(user_id, unit["unit"], x["id"], manual)
        if not results:
            if manual:
                await self.bex_after_manual(chat_id, user_id, pending, unit, x, manual, ok_before)
                return
            await self.tg.send_message(chat_id, "Не нашёл ответов. Пиши с номерами пунктов: «1 jestem 3 mam».")
            return
        if kind == "free" or voice_verdicts is not None:
            for r in results:   # свободный ответ или голос — решает модель (голос она уже проверила)
                if r["status"] != "missing":
                    r["status"] = "check"
                    if voice_verdicts is not None:
                        r["heard"] = r["user"]
        todo = [r for r in logic.needs_model(results, EX_EXPLAIN_ALL) if r["status"] != "missing"]
        verdicts: dict[int, dict] = dict(voice_verdicts or {})
        if voice_verdicts is not None:
            todo = []
        if todo:
            task = self.bex_task(x)
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
        total = len(x["items"])
        self.bex_snapshot(user_id, unit, x, done, ok_before)
        head = (f"📊 <b>{ok_now} из {len(results)}</b> · {fmt.e(fmt.bex_name(x))}\n"
                + (f"✋ Засчитано вручную: {', '.join(map(str, manual))}\n" if manual else "")
                + (f"🎉 <b>Упражнение выполнено на 100%!</b>" if ok_all == total
                   else f"<i>Всего в упражнении верно {ok_all} из {total}.</i>"))
        wrong = [r["n"] for r in results if r["final"] != "ok"]
        await self.tg.send_message(chat_id, fmt.ex_results({"items": items}, results, head=head, foot=""),
                                   fmt.bex_result_buttons(unit, x, done, self.bex_wins(user_id, unit, x), wrong))

    def bex_snapshot(self, user_id: int, unit: dict, x: dict, done: dict, ok_before: int) -> bool:
        """Попытка только что добита до 100% — сохраняем её ответы целиком (и какие пункты засчитаны вручную)."""
        total = len(x["items"])
        if not ok_before < total == fmt.bex_counts(x, done)[0]:
            return False
        rows = [done[(x["id"], n)] for n in range(1, total + 1)]
        self.db.bex_done_add(user_id, unit["unit"], x["id"], {str(r["n"]): r["answer"] for r in rows},
                             [r["n"] for r in rows if r.get("manual")])
        return True

    async def bex_after_manual(self, chat_id: int, user_id: int, pending: dict | None, unit: dict, x: dict,
                               ns: list[int], ok_before: int) -> None:
        """Сообщение после «✋ засчитать вручную»: что засчитано и что осталось."""
        if pending and pending.get("msg"):
            await self.tg.edit_markup(chat_id, pending["msg"])
        self.db.set_pending(user_id, None)
        done = self.db.bex_results(user_id, unit["unit"])
        ok_all = fmt.bex_counts(x, done)[0]
        total = len(x["items"])
        self.bex_snapshot(user_id, unit, x, done, ok_before)
        text = (f"✋ <b>Засчитано вручную</b> · {fmt.e(fmt.bex_name(x))}: "
                + (", ".join(map(str, ns)) if ns else "ничего нового — эти пункты уже верны") + "\n"
                + ("🎉 <b>Упражнение выполнено на 100%</b> (часть пунктов — вручную ✋)." if ok_all == total
                   else f"<i>Всего в упражнении верно {ok_all} из {total}.</i>"))
        await self.tg.send_message(chat_id, text, fmt.bex_result_buttons(unit, x, done, self.bex_wins(user_id, unit, x)))
