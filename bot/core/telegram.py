"""Минимальный клиент Telegram Bot API на httpx (long polling)."""
import asyncio
import html
import re

import httpx

LIMIT = 3500  # лимит Telegram — 4096 символов (эмодзи считаются за 2), берём с запасом
TAG_RE = re.compile(r"<[^>]+>")


def plain(text: str) -> str:
    """HTML → обычный текст: без тегов, сущности раскрыты."""
    return html.unescape(TAG_RE.sub("", text))


def split_html(text: str, limit: int = LIMIT) -> list[tuple[str, bool]]:
    """Режет длинный текст на куски ≤ limit, не разрывая HTML-теги: сначала по пустым строкам (абзацы,
    пункты разбора), потом по строкам. Строку длиннее limit режет по пробелам и отправляет без оформления.
    Возвращает [(кусок, это_html)]."""
    if len(text) <= limit:
        return [(text, True)]
    pieces: list[tuple[str, bool, str]] = []  # (текст, html, разделитель перед ним)
    for para in text.split("\n\n"):
        if len(para) <= limit:
            pieces.append((para, True, "\n\n"))
            continue
        for k, line in enumerate(para.split("\n")):
            sep = "\n\n" if k == 0 else "\n"
            if len(line) <= limit:
                pieces.append((line, True, sep))
                continue
            rest = plain(line)
            while rest:
                cut = len(rest) if len(rest) <= limit else (rest.rfind(" ", 0, limit) if rest.rfind(" ", 0, limit) > 0 else limit)
                pieces.append((rest[:cut], False, sep))
                sep = " "
                rest = rest[cut:].lstrip()
    chunks: list[tuple[str, bool]] = []
    for piece, is_html, sep in pieces:
        if chunks and chunks[-1][1] == is_html and len(chunks[-1][0]) + len(sep) + len(piece) <= limit:
            chunks[-1] = (chunks[-1][0] + sep + piece, is_html)
        else:
            chunks.append((piece, is_html))
    return [(c, h) for c, h in chunks if c.strip()]


def fit(text: str, limit: int = LIMIT) -> str:
    """Для редактирования (одно сообщение): обрезать по целой строке."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 2)
    return text[:cut if cut > 0 else 0].rstrip() + "\n…"


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.client = client or httpx.AsyncClient(timeout=70)

    async def call(self, method: str, **params):
        """Запрос к Bot API. Если Telegram просит подождать (429 — много сообщений подряд), ждём и повторяем."""
        for _ in range(4):
            data = await self._call_once(method, dict(params))
            retry = (data.get("parameters") or {}).get("retry_after")
            if data.get("ok") or not retry:
                break
            await asyncio.sleep(min(float(retry), 30) + 0.5)
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    async def _call_once(self, method: str, params: dict) -> dict:
        files = params.pop("_files", None)
        if files:
            resp = await self.client.post(f"{self.base}/{method}", data=params, files=files)
        else:
            resp = await self.client.post(f"{self.base}/{method}", json=params)
        return resp.json()

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", **params)

    async def send_message(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None):
        """buttons — ряды инлайн-кнопок [(текст, callback_data)]; крепятся к последнему куску.
        Длинный текст режется по абзацам и строкам; кусок, который Telegram не принял из-за разметки,
        уходит обычным текстом — ничего не теряется."""
        chunks = split_html(text or "") or [("", True)]
        result = None
        for n, (chunk, is_html) in enumerate(chunks):
            params = {"chat_id": chat_id, "disable_web_page_preview": True}
            if buttons and n == len(chunks) - 1:
                params["reply_markup"] = markup(buttons)
            try:
                if not is_html:
                    raise TelegramError("plain")
                result = await self.call("sendMessage", text=chunk, parse_mode="HTML", **params)
            except TelegramError as e:
                if is_html and "parse" not in str(e).lower() and "entit" not in str(e).lower():
                    raise
                result = await self.call("sendMessage", text=plain(chunk) if is_html else chunk, **params)
        return result

    async def edit_message(self, chat_id: int, message_id: int, text: str,
                           buttons: list[list[tuple[str, str]]] | None = None) -> None:
        params = {"chat_id": chat_id, "message_id": message_id, "text": fit(text), "parse_mode": "HTML",
                  "disable_web_page_preview": True, "reply_markup": markup(buttons or [])}
        try:
            await self.call("editMessageText", **params)
        except TelegramError as e:
            if "not modified" in str(e):
                return
            if "parse" not in str(e).lower() and "entit" not in str(e).lower():
                raise
            params.pop("parse_mode")
            await self.call("editMessageText", **{**params, "text": plain(params["text"])})

    async def edit_markup(self, chat_id: int, message_id: int,
                          buttons: list[list[tuple[str, str]]] | None = None) -> None:
        """Заменить или убрать кнопки под сообщением, не трогая текст."""
        try:
            await self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                            reply_markup=markup(buttons or []))
        except TelegramError:
            pass

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    async def answer_callback(self, callback_id: str, text: str | None = None, alert: bool = False) -> None:
        """Ответ на нажатие кнопки; text — всплывашка (alert=True — окно с «OK», пока не закроешь)."""
        try:
            params = {"callback_query_id": callback_id}
            if text:
                params["text"] = text[:200]
                if alert:
                    params["show_alert"] = True
            await self.call("answerCallbackQuery", **params)
        except Exception:
            pass

    async def send_voice(self, chat_id: int, ogg: bytes) -> None:
        await self.call("sendVoice", chat_id=str(chat_id),
                        _files={"voice": ("reply.ogg", ogg, "audio/ogg")})

    async def send_audio(self, chat_id: int, filename: str, content: bytes, caption: str = "") -> None:
        params = {"chat_id": str(chat_id), "_files": {"audio": (filename, content, "audio/mpeg")}}
        if caption:
            params["caption"] = caption[:1000]
            params["parse_mode"] = "HTML"
        await self.call("sendAudio", **params)

    async def send_photo(self, chat_id: int, filename: str, content: bytes, caption: str = "") -> None:
        params = {"chat_id": str(chat_id), "_files": {"photo": (filename, content, "image/png")}}
        if caption:
            params["caption"] = caption[:1000]
            params["parse_mode"] = "HTML"
        await self.call("sendPhoto", **params)

    async def send_document(self, chat_id: int, filename: str, content: bytes, caption: str = "") -> None:
        params = {"chat_id": str(chat_id), "_files": {"document": (filename, content, "text/plain")}}
        if caption:
            params["caption"] = caption[:1000]
            params["parse_mode"] = "HTML"
        await self.call("sendDocument", **params)

    async def send_action(self, chat_id: int, action: str) -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action=action)
        except Exception:
            pass

    async def download_file(self, file_id: str) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        resp = await self.client.get(f"{self.file_base}/{info['file_path']}")
        resp.raise_for_status()
        return resp.content

    async def set_commands(self) -> None:
        await self.call("setMyCommands", commands=[
            {"command": "menu", "description": "🏠 Главное меню"},
            {"command": "new", "description": "💬 Новый разговор: по набору, новый набор, без набора"},
            {"command": "ex", "description": "🏋️ Упражнения: слова, грамматика, ошибки, голосом, учебник"},
            {"command": "words", "description": "📖 Слова юнита: список с переводом, скрытый перевод, озвучка"},
            {"command": "set", "description": "🎯 Наборы слов: прогресс, новый, 📚 архив"},
            {"command": "itog", "description": "📋 Итог: ошибки и слова за час / сутки / разговор"},
            {"command": "dict", "description": "⭐ Мой словарь выражений"},
            {"command": "rule", "description": "📖 Вопрос о правиле: /rule почему do niej?"},
            {"command": "export", "description": "🗂 Выгрузить всё файлом: ошибки, слова, словарь"},
            {"command": "cancel", "description": "✖️ Отменить текущий выбор (разговор и набор остаются)"},
            {"command": "help", "description": "❓ Что умеет бот"},
        ])


def markup(buttons: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
