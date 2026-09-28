"""Минимальный клиент Telegram Bot API на httpx (long polling)."""
import httpx


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.client = client or httpx.AsyncClient(timeout=70)

    async def call(self, method: str, **params):
        files = params.pop("_files", None)
        if files:
            resp = await self.client.post(f"{self.base}/{method}", data=params, files=files)
        else:
            resp = await self.client.post(f"{self.base}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", **params)

    async def send_message(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None):
        """buttons — ряды инлайн-кнопок [(текст, callback_data)]; крепятся к последнему куску."""
        # Лимит Telegram — 4096 символов, режем с запасом.
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
        result = None
        for n, chunk in enumerate(chunks):
            params = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
            if buttons and n == len(chunks) - 1:
                params["reply_markup"] = markup(buttons)
            result = await self.call("sendMessage", **params)
        return result

    async def edit_message(self, chat_id: int, message_id: int, text: str,
                           buttons: list[list[tuple[str, str]]] | None = None) -> None:
        params = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML",
                  "disable_web_page_preview": True, "reply_markup": markup(buttons or [])}
        try:
            await self.call("editMessageText", **params)
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            params = {"callback_query_id": callback_id}
            if text:
                params["text"] = text
            await self.call("answerCallbackQuery", **params)
        except Exception:
            pass

    async def send_voice(self, chat_id: int, ogg: bytes) -> None:
        await self.call("sendVoice", chat_id=str(chat_id),
                        _files={"voice": ("reply.ogg", ogg, "audio/ogg")})

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
            {"command": "set", "description": "🎯 Набор слов: прогресс, новый набор"},
            {"command": "free", "description": "🏁 Свободный разговор без набора"},
            {"command": "new", "description": "🆕 Начать разговор заново"},
            {"command": "itog", "description": "📋 Слова и ошибки этого разговора"},
            {"command": "cancel", "description": "✖️ Отменить создание набора"},
            {"command": "help", "description": "❓ Что умеет бот"},
        ])


def markup(buttons: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
