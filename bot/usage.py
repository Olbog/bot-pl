"""📊 /usage — расход токенов Gemini по пользователям, дням, разделам бота, часам и моделям (только для админа)."""
from collections import defaultdict
from datetime import timedelta
from html import escape

from .core import config as cfg_mod

PERIODS = {"t": ("Сегодня", 1), "w": ("7 дней", 7), "m": ("30 дней", 30)}
VIEWS = {"u": "👥 Люди", "d": "📅 По дням", "s": "🧩 Разделы", "h": "🕘 По часам", "o": "🤖 Модели"}
# модуль кода, откуда пришёл запрос → раздел бота
SECTIONS = {
    "conversation": "💬 Разговор", "lesson": "🎓 Урок", "review": "🔁 Повторение", "memorize": "📜 Тексты наизусть",
    "run": "🏋️ Упражнения", "menu": "🏋️ Упражнения", "book": "📘 Учебник", "bookex": "📘 Учебник",
    "dictionary": "⭐ Словарь", "sets": "🎯 Наборы слов", "reading": "📚 Чтение", "zvuki": "🗣 Скороговорки",
    "reports": "⚙️ Фон (раскладка ошибок)", "": "❔ Другое",
}
BAR = 12
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def tok(n: int) -> str:
    """12345 → «12.3k», 1234567 → «1.23M»."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def bar(n: int, top: int) -> str:
    k = round(BAR * n / top) if top else 0
    return "▇" * k or ("▏" if n else "")


class UsageMixin:
    """Отчёт строится из таблицы ai_usage (пишется в Gemini._count через App.on_usage)."""

    def usage_allowed(self, user_id: int) -> bool:
        return user_id in self.cfg.admin_ids

    def usage_note_user(self, frm: dict) -> None:
        """Имя пользователя для отчёта — один раз за запуск бота."""
        uid = frm.get("id")
        if not uid or uid in self._usage_named:
            return
        self._usage_named.add(uid)
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        if frm.get("username"):
            name = f"{name} @{frm['username']}".strip()
        self.db.tg_user_set(uid, name or str(uid))

    def _usage_since(self, days: int) -> float:
        start = cfg_mod.local_dt(self.clock()).replace(hour=0, minute=0, second=0, microsecond=0)
        return (start - timedelta(days=days - 1)).timestamp()

    def _usage_name(self, uid: int, names: dict) -> str:
        if not uid:
            return "⚙️ фон бота"
        return escape(names.get(uid) or str(uid))

    async def usage_menu(self, chat_id: int, user_id: int, message_id: int | None = None, arg: str = "w:u:a") -> None:
        if not self.usage_allowed(user_id):
            await self.tg.send_message(chat_id, "Нет доступа: 📊 /usage — только для админа (ADMIN_USER_IDS).")
            return
        period, view, who = (arg.split(":") + ["w", "u", "a"])[:3]
        period = period if period in PERIODS else "w"
        view = view if view in VIEWS else "u"
        text, buttons = self.usage_screen(period, view, who)
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    async def usage_callback(self, chat_id: int, user_id: int, message_id: int, data: str) -> str:
        if not self.usage_allowed(user_id):
            return "Нет доступа"
        await self.usage_menu(chat_id, user_id, message_id, data)
        return ""

    def usage_screen(self, period: str, view: str, who: str) -> tuple[str, list]:
        names = self.db.tg_user_names()
        title, days = PERIODS[period]
        rows = self.db.usage_rows(self._usage_since(30 if view == "u" else days))
        uid = int(who) if who.lstrip("-").isdigit() else None
        sel = [r for r in rows if uid is None or r["user_id"] == uid]
        if view != "u":
            since = self._usage_since(days)
            sel = [r for r in sel if r["ts"] >= since]
        whom = "все" if uid is None else self._usage_name(uid, names)
        head = f"📊 <b>Токены Gemini</b> · {VIEWS[view]}"
        if view != "u":
            head += f" · {title} · {whom}"
        lines = [head, ""]
        if not sel:
            lines.append("Пока пусто: расход пишется с момента обновления бота.")
        elif view == "u":
            lines += self._usage_people(rows, names)
        elif view == "d":
            lines += self._usage_days(sel, names, uid is None)
        elif view == "s":
            lines += self._usage_group(sel, lambda r: SECTIONS.get(r["module"], r["module"]))
        elif view == "h":
            lines += self._usage_hours(sel, days)
        else:
            lines += self._usage_group(sel, lambda r: r["model"])
        if sel:
            lines += ["", "<i>Токены: всего = запрос + ответ + 🧠 обдумывание; 🎙 — голос в запросе. "
                          "Время — Мск (TZ в .env).</i>"]
        cur = f"{period}:{view}:{who}"
        buttons = [
            [(("• " if k == period else "") + t, f"us:{k}:{view}:{who}") for k, (t, _) in PERIODS.items()],
            [(("• " if k == view else "") + t, f"us:{period}:{k}:{who}") for k, t in list(VIEWS.items())[:3]],
            [(("• " if k == view else "") + t, f"us:{period}:{k}:{who}") for k, t in list(VIEWS.items())[3:]]
            + [("🔄", f"us:{cur}")],
        ]
        people = sorted({r["user_id"] for r in rows})
        if view != "u" and len(people) > 1:
            row = [(("• " if uid is None else "") + "Все", f"us:{period}:{view}:a")]
            for p in people[:5]:
                label = (names.get(p) or str(p)).split(" @")[0][:14] if p else "⚙️ фон"
                row.append((("• " if p == uid else "") + label, f"us:{period}:{view}:{p}"))
            buttons.append(row)
        return "\n".join(lines)[:4000], buttons

    def _usage_people(self, rows, names) -> list[str]:
        """Каждый пользователь: сегодня / 7 дней / 30 дней — токены и запросы."""
        cuts = {k: self._usage_since(d) for k, (_, d) in PERIODS.items()}
        acc = defaultdict(lambda: {k: [0, 0] for k in PERIODS})
        audio = defaultdict(int)
        thoughts = defaultdict(int)
        for r in rows:
            for k, since in cuts.items():
                if r["ts"] >= since:
                    acc[r["user_id"]][k][0] += r["total"]
                    acc[r["user_id"]][k][1] += 1
            audio[r["user_id"]] += r["audio"]
            thoughts[r["user_id"]] += r["thoughts"]
        out = []
        for u in sorted(acc, key=lambda x: -acc[x]["m"][0]):
            a = acc[u]
            out.append(f"<b>{self._usage_name(u, names)}</b>" + (f" <code>{u}</code>" if u else ""))
            out.append("  " + " · ".join(f"{PERIODS[k][0].lower()}: {tok(a[k][0])} ({a[k][1]} запр.)" for k in PERIODS))
            m = a["m"][0] or 1
            out.append(f"  за 30 дн.: в среднем {tok(a['m'][0] // 30)}/день · 🧠 {round(100 * thoughts[u] / m)}% "
                       f"· 🎙 {round(100 * audio[u] / m)}%")
        return out

    def _usage_days(self, rows, names, split: bool) -> list[str]:
        days = defaultdict(lambda: defaultdict(int))
        for r in rows:
            days[cfg_mod.local_dt(r["ts"]).date()][r["user_id"]] += r["total"]
        top = max(sum(v.values()) for v in days.values())
        out = []
        for d in sorted(days, reverse=True):
            total = sum(days[d].values())
            line = f"<code>{d:%d.%m} {WEEKDAYS[d.weekday()]} {tok(total):>6}</code> {bar(total, top)}"
            if split and len(days[d]) > 1:
                line += "\n   " + ", ".join(f"{self._usage_name(u, names).split(' @')[0]} {tok(n)}"
                                           for u, n in sorted(days[d].items(), key=lambda x: -x[1]))
            out.append(line)
        return out

    def _usage_group(self, rows, key) -> list[str]:
        acc = defaultdict(lambda: [0, 0, 0])   # токены, запросы, обдумывание
        for r in rows:
            a = acc[key(r)]
            a[0] += r["total"]
            a[1] += 1
            a[2] += r["thoughts"]
        all_ = sum(a[0] for a in acc.values()) or 1
        out = [f"Всего: {tok(all_)} за {sum(a[1] for a in acc.values())} запросов", ""]
        for k, (n, c, th) in sorted(acc.items(), key=lambda x: -x[1][0]):
            out.append(f"{escape(k)} — <b>{tok(n)}</b> ({round(100 * n / all_)}%), {c} запр., "
                       f"~{tok(n // c)}/запрос, 🧠 {tok(th)}")
        return out

    def _usage_hours(self, rows, days: int) -> list[str]:
        hours = [0] * 24
        calls = [0] * 24
        for r in rows:
            h = cfg_mod.local_dt(r["ts"]).hour
            hours[h] += r["total"]
            calls[h] += 1
        top = max(hours) or 1
        out = [f"Сумма за {days} дн. по часу суток:"]
        for h in range(24):
            if hours[h] or 7 <= h <= 23:
                out.append(f"<code>{h:02d}:00 {tok(hours[h]):>6} {calls[h]:>3}</code> {bar(hours[h], top)}")
        return out
