"""🗣 /tw — скороговорки и песенки: чтобы правильно и бегло произносить звуки.

Тексты — bot/content/zvuki.json: разделы (скороговорки по звукам; считалки и народные песенки) → группы → пункты
{id, pl, translit, ru, tip}. Пункт: текст, транскрипция и перевод под спойлером, 💡 на что обратить внимание,
аудио в двух темпах (🐢 медленно, 🔊 обычный) — озвучено заранее при старте бота и хранится в data/audio/tw/,
поэтому звучит каждый раз одинаково. Отметка у каждого пункта (zv_status): ⚪ не начато → 🟡 работаю → ✅ готово → ⚪.

🎙 Проверка произношения: после открытия карточки (шаг pending «tw», живёт TW_LISTEN_MIN минут) голосовое идёт
на проверку: модель сравнивает запись с текстом и разбирает каждое слово с неверным звуком. Ошибки ложатся
в общий пул ошибок (правила «Произношение: …»), как в голосовом разговоре; «🙅 Не ошибка» — так же: оспоренное
уходит из ошибок и в исключения, дальше такая пара не считается ошибкой.
"""
import hashlib
import json
import random
from html import escape
from pathlib import Path

from .core import rules
from .ui import fmt
from .ai.prompt import TW_HINT, TW_SCHEMA, tw_check_prompt
from .settings import VOICE_MIN_SEC

CONTENT = Path(__file__).resolve().parent / "content" / "zvuki.json"
RATES = {"s": "-35%", "n": "+0%"}          # 🐢 медленно — расслышать каждый звук; 🔊 обычный темп
STATUS = ["⚪", "🟡", "✅"]
STATUS_NAME = ["не начато", "работаю", "готово"]
NEXT_LABEL = ["🟡 Отметить: работаю", "✅ Отметить: готово", "⚪ Начать заново"]
TW_LISTEN_MIN = 30   # столько минут после открытия карточки голосовое считается чтением скороговорки


def load(path: Path | None = None) -> list[dict]:
    """Пункты по порядку: {id, pl, translit, ru, tip, n, section, kind, group}."""
    try:
        data = json.loads((path or CONTENT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for sec in data.get("sections") or []:
        for g in sec.get("groups") or []:
            for it in g.get("items") or []:
                if it.get("id") and it.get("pl"):
                    out.append({**it, "n": len(out) + 1, "section": sec.get("title", ""), "kind": sec.get("kind", ""),
                                "group": g.get("title", "")})
    return out


def list_screen(items: list[dict], st: dict[str, int]) -> tuple[str, list]:
    cnt = [sum(1 for it in items if st.get(it["id"], 0) == k) for k in range(3)]
    lines = [f"🗣 <b>Скороговорки и песенки</b> · ✅ {cnt[2]} · 🟡 {cnt[1]} · ⚪ {cnt[0]}",
             "<i>Нажми название — придут текст и аудио. Кружок справа — отметка: ⚪ → 🟡 работаю → ✅ готово → ⚪.</i>"]
    sec = grp = None
    for it in items:
        if it["section"] != sec:
            sec = it["section"]
            lines.append(f"\n<b>{escape(sec)}</b>")
        if it["group"] != grp:
            grp = it["group"]
            nums = [x["n"] for x in items if x["group"] == grp]
            lines.append(f"• {escape(grp)} — {nums[0]}{f'–{nums[-1]}' if len(nums) > 1 else ''}")
    rows = []
    for it in items:
        label = f"{it['n']}. " + it["pl"].split("\n")[0]
        label = label.rstrip(" ,") if len(label) <= 40 else label[:39].rstrip(" ,") + "…"
        rows.append([(label, f"tw:o:{it['id']}"),
                     (STATUS[st.get(it["id"], 0)], f"tw:c:{it['id']}")])
    rows.append([("🎲 Случайная из незавершённых", "tw:r")])
    return "\n".join(lines), rows


def item_message(it: dict, status: int, nxt: dict | None) -> tuple[str, list]:
    text = (f"🗣 <b>{escape(it['kind'])} {it['n']}</b> · {escape(it['group'])}\n\n"
            f"<b>{escape(it['pl'])}</b>\n\n"
            f"🔤 <tg-spoiler>{escape(it['translit'])}</tg-spoiler>\n"
            f"🇷🇺 <tg-spoiler>{escape(it['ru'])}</tg-spoiler>\n"
            + (f"\n💡 {escape(it['tip'])}\n" if it.get("tip") else "")
            + f"\nОтметка: {STATUS[status]} {STATUS_NAME[status]}\n"
            f"\n🎙 <i>Прочитай вслух голосовым — проверю произношение по звукам.</i>")
    rows = [[("🐢 Медленно", f"tw:a:{it['id']}:s"), ("🔊 Обычный темп", f"tw:a:{it['id']}:n")],
            [(NEXT_LABEL[status], f"tw:s:{it['id']}")],
            [("📋 Список", "tw:l")] + ([(f"➡️ {nxt['n']}", f"tw:o:{nxt['id']}")] if nxt else [])]
    return text, rows


def result_rows(it: dict, status: int, attempt: str, has_errors: bool) -> list:
    """Кнопки под результатом проверки; attempt — «<id>:<мс>»."""
    rows = [[("🙅 Не ошибка", f"tw:d:{attempt}"), ("⭐ В словарь", f"dX:t:{attempt}")]] if has_errors else []
    rows.append([("🐢 Образец медленно", f"tw:a:{it['id']}:s"), ("🔊 Образец", f"tw:a:{it['id']}:n")])
    rows.append([(NEXT_LABEL[status], f"tw:S:{attempt}"), ("📋 Список", "tw:l")])
    return rows


class ZvukiMixin:
    def tw_items(self) -> list[dict]:
        return load(getattr(self, "tw_content", None))

    def tw_dir(self) -> Path:
        return getattr(self, "tw_audio_dir", None) or Path(self.cfg.db_path).parent / "audio" / "tw"

    def tw_path(self, it: dict, speed: str) -> Path:
        """Имя с хэшем текста, голоса и темпа: поправили текст — озвучка сделается заново, иначе — тот же файл."""
        h = hashlib.md5(f"{self.cfg.tts_voice}|{RATES[speed]}|{it['pl']}".encode()).hexdigest()[:8]
        return self.tw_dir() / f"{it['id']}_{speed}_{h}.ogg"

    def tw_jobs(self) -> list[tuple[Path, str, str]]:
        return [(self.tw_path(it, sp), it["pl"], RATES[sp]) for it in self.tw_items() for sp in ("n", "s")]

    async def tw_pregen(self, sleep=None) -> None:
        await self.pregen_audio(self.tw_jobs, "🗣 Скороговорки", sleep)

    async def tw_menu(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        items = self.tw_items()
        if not items:
            await self.tg.send_message(chat_id, "🗣 Скороговорок пока нет.")
            return
        text, rows = list_screen(items, self.db.zv_status(user_id))
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, rows)
        else:
            await self.tg.send_message(chat_id, text, rows)

    def tw_listening(self, pending: dict | None) -> str | None:
        """Какую скороговорку проверяем голосовым (шаг «tw» не старше TW_LISTEN_MIN минут) или None."""
        if (pending or {}).get("step") == "tw" and self.clock() - pending.get("at", 0) <= TW_LISTEN_MIN * 60:
            return pending.get("item")
        return None

    async def tw_open(self, chat_id: int, user_id: int, it: dict) -> None:
        items = self.tw_items()
        nxt = next((x for x in items if x["n"] == it["n"] + 1), None)
        text, rows = item_message(it, self.db.zv_status(user_id).get(it["id"], 0), nxt)
        await self.tg.send_message(chat_id, text, rows)
        self.db.set_pending(user_id, {"step": "tw", "item": it["id"], "at": self.clock()})
        await self.tw_send_audio(chat_id, it, "n")

    async def tw_send_audio(self, chat_id: int, it: dict, speed: str) -> str | None:
        await self.tg.send_action(chat_id, "record_voice")
        try:
            ogg = await self.cached_tts(self.tw_path(it, speed), it["pl"], RATES[speed])
        except Exception:
            return "🔇 Озвучка не получилась — попробуй позже"
        await self.tg.send_voice(chat_id, ogg)
        return None

    async def tw_callback(self, chat_id: int, user_id: int, message_id: int, arg: str) -> str | None:
        """tw:l — список; tw:o:<id> — открыть; tw:a:<id>:<s|n> — аудио; tw:c:<id> — отметка в списке;
        tw:s:<id> — отметка в карточке; tw:r — случайная незавершённая. Возвращает всплывашку."""
        kind, _, rest = arg.partition(":")
        if kind in ("d", "x", "k"):
            return await self.tw_dispute(chat_id, user_id, message_id, kind, rest)
        items = self.tw_items()
        if kind == "l":
            await self.tw_menu(chat_id, user_id)
            return None
        if kind == "r":
            st = self.db.zv_status(user_id)
            pool = [it for it in items if st.get(it["id"], 0) != 2] or items
            await self.tw_open(chat_id, user_id, random.choice(pool))
            return None
        item_id, _, speed = rest.partition(":")
        it = next((x for x in items if x["id"] == item_id), None)
        if not it:
            return "Не найдено"
        if kind == "o":
            await self.tw_open(chat_id, user_id, it)
        elif kind == "a":
            return await self.tw_send_audio(chat_id, it, speed if speed in RATES else "n")
        elif kind == "S":   # отметка из результата проверки: tw:S:<id>:<мс попытки> — кнопки под результатом
            new = (self.db.zv_status(user_id).get(it["id"], 0) + 1) % 3
            self.db.zv_set(user_id, it["id"], new)
            attempt = f"{it['id']}:{speed}"
            await self.tg.edit_markup(chat_id, message_id,
                                      result_rows(it, new, attempt, bool(self.db.corrections_by(user_id, "tw_attempt", attempt))))
            return f"{STATUS[new]} {it['n']}: {STATUS_NAME[new]}"
        elif kind in ("c", "s"):
            new = (self.db.zv_status(user_id).get(it["id"], 0) + 1) % 3
            self.db.zv_set(user_id, it["id"], new)
            if kind == "c":
                await self.tw_menu(chat_id, user_id, message_id)
            else:
                nxt = next((x for x in items if x["n"] == it["n"] + 1), None)
                text, rows = item_message(it, new, nxt)
                await self.tg.edit_message(chat_id, message_id, text, rows)
            return f"{STATUS[new]} {it['n']}: {STATUS_NAME[new]}"
        return None

    # ---------- 🎙 проверка произношения ----------

    async def tw_voice(self, chat_id: int, user_id: int, item_id: str, voice: dict) -> None:
        it = next((x for x in self.tw_items() if x["id"] == item_id), None)
        if not it:
            return
        sec = voice.get("duration")
        if sec is not None and sec < VOICE_MIN_SEC:
            await self.tg.send_message(chat_id, "🎙 Запись пустая или слишком короткая — пришли ещё раз.")
            return
        self.db.set_pending(user_id, {"step": "tw", "item": it["id"], "at": self.clock()})   # можно ещё раз
        audio = await self.tg.download_file(voice["file_id"])
        prompt = tw_check_prompt(it["pl"], it["ru"], self.cfg.level)
        if self.ignore_block(user_id):
            prompt = self.ignore_block(user_id) + "\n\n" + prompt
        data = await self.ask(chat_id, prompt, TW_SCHEMA, TW_HINT, wait=f"⏳ Слушаю: {it['kind'].lower()} {it['n']}…",
                              audio=audio)
        if data is None:
            return
        heard = str(data.get("heard") or "").strip()
        if not heard:
            await self.tg.send_message(chat_id, "🎙 Не расслышал речь — прочитай ещё раз, поближе к микрофону.")
            return
        errs = []
        for x in data.get("errors") or []:
            if not isinstance(x, dict) or not x.get("word"):
                continue
            rule = rules.normalize(x.get("rule"))
            errs.append({"kind": "pronunciation", "original": str(x.get("said") or "").strip() or "—",
                         "correct": str(x["word"]).strip(), "said_ru": str(x.get("said_ru") or "").strip(),
                         "translit": str(x.get("translit") or "").strip(), "ru": str(x.get("ru") or "").strip(),
                         "why": str(x.get("why") or "").strip(),
                         "rule": rule if rule.startswith("Произношение") else "Произношение: ударение и прочее"})
        errs = self.drop_ignored(user_id, errs)
        attempt = f"{it['id']}:{int(self.clock() * 1000)}"
        if errs:
            self.db.add_corrections(self.db.current_session(user_id), user_id,
                                    [{**c, "tw": it["id"], "tw_attempt": attempt} for c in errs])
        await self.tg.send_message(chat_id, *self.tw_result(user_id, it, heard, errs, str(data.get("fluency") or ""),
                                                           attempt))

    def tw_result(self, user_id: int, it: dict, heard: str, errs: list[dict], fluency: str,
                  attempt: str) -> tuple[str, list]:
        lines = [f"🎙 <b>{escape(it['kind'])} {it['n']}</b> — прозвучало:", f"<i>{escape(heard)}</i>", ""]
        if errs:
            lines.append(f"🗣 <b>Звуки, над которыми поработать ({len(errs)})</b>")
            for c in errs:
                said = escape(c["original"]) + (f" [{escape(c['said_ru'])}]" if c.get("said_ru") else "")
                lines.append(f"❌ <b>{escape(c['correct'])}</b> — сказал: {said} → правильно: "
                             f"[{escape(c['translit'])}] — {escape(c['ru'])}")
                if c.get("why"):
                    lines.append(f"   <i>{escape(c['why'])}</i>")
        else:
            lines.append("✅ <b>Все звуки чисто!</b>")
        if fluency.strip():
            lines += ["", f"💬 {escape(fluency.strip())}"]
        status = self.db.zv_status(user_id).get(it["id"], 0)
        lines += ["", f"<i>Отметка: {STATUS[status]} {STATUS_NAME[status]}. Ещё раз — просто пришли новое голосовое.</i>"]
        return "\n".join(lines), result_rows(it, status, attempt, bool(errs))

    async def tw_dispute(self, chat_id: int, user_id: int, message_id: int, kind: str, arg: str) -> str | None:
        """tw:d:<попытка> — экран «Не ошибка»; tw:x:<id ошибки> — отметить/вернуть; tw:k:<попытка> — готово."""
        if kind == "x":
            c = self.db.correction(int(arg)) if arg.isdigit() else None
            if not c or c["_user"] != user_id or not c.get("tw_attempt"):
                return "Ошибка не найдена"
            self.dispute_toggle(user_id, c)
            arg, kind = c["tw_attempt"], "d"
            edit = message_id
        else:
            edit = None
        corrs = self.db.corrections_by(user_id, "tw_attempt", arg)
        if not corrs:
            return "Ошибок нет"
        if kind == "k":
            await self.tg.edit_message(chat_id, message_id, fmt.dispute_done(sum(c["_disputed"] for c in corrs)))
            return None
        text, rows = fmt.dispute_screen(corrs, 0, toggle="tw:x:", done=f"tw:k:{arg}")
        if edit:
            await self.tg.edit_message(chat_id, edit, text, rows)
        else:
            await self.tg.send_message(chat_id, text, rows)
        return None
