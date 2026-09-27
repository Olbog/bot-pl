"""Запросы к Gemini через Cloudflare Worker."""
import base64
import json
import logging
from dataclasses import dataclass, field

import httpx

from .prompt import RESPONSE_SCHEMA, system_prompt

log = logging.getLogger(__name__)


class GeminiError(Exception):
    pass


@dataclass
class Turn:
    user_text: str
    reply_pl: str
    reply_translit: str
    reply_ru: str
    corrections: list[dict] = field(default_factory=list)
    new_words: list[dict] = field(default_factory=list)


def build_contents(history: list[tuple[str, str]], text: str | None, audio: bytes | None) -> list[dict]:
    """history — пары (role, text), role: 'user' | 'model'."""
    contents = [{"role": r, "parts": [{"text": t}]} for r, t in history]
    parts: list[dict] = []
    if audio is not None:
        parts.append({"inline_data": {"mime_type": "audio/ogg", "data": base64.b64encode(audio).decode()}})
        parts.append({"text": "(Ученик прислал голосовое сообщение. Расшифруй его в user_text и ответь.)"})
    if text:
        parts.append({"text": text})
    contents.append({"role": "user", "parts": parts})
    return contents


def parse_turn(data: dict) -> Turn:
    try:
        cand = data["candidates"][0]
        raw = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
    except (KeyError, IndexError) as e:
        cands = data.get("candidates") or [{}]
        reason = data.get("promptFeedback") or cands[0].get("finishReason")
        raise GeminiError(f"Пустой ответ Gemini: {reason}") from e
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GeminiError(f"Gemini вернул не JSON: {raw[:200]}") from e
    return Turn(
        user_text=str(obj.get("user_text", "")),
        reply_pl=str(obj.get("reply_pl", "")),
        reply_translit=str(obj.get("reply_translit", "")),
        reply_ru=str(obj.get("reply_ru", "")),
        corrections=[c for c in obj.get("corrections") or [] if isinstance(c, dict)],
        new_words=[w for w in obj.get("new_words") or [] if isinstance(w, dict)],
    )


class Gemini:
    def __init__(self, worker_url: str, proxy_token: str, model: str, thinking_level: str, level: str,
                 client: httpx.AsyncClient | None = None):
        self.url = f"{worker_url}/v1beta/models/{model}:generateContent"
        self.headers = {"x-proxy-token": proxy_token, "content-type": "application/json"}
        self.thinking_level = thinking_level
        self.system = system_prompt(level)
        self.client = client or httpx.AsyncClient(timeout=120)

    def _body(self, contents: list[dict], with_thinking: bool) -> dict:
        gen = {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.7,
        }
        if with_thinking and self.thinking_level:
            gen["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        return {
            "systemInstruction": {"parts": [{"text": self.system}]},
            "contents": contents,
            "generationConfig": gen,
        }

    async def reply(self, history: list[tuple[str, str]], text: str | None = None,
                    audio: bytes | None = None) -> Turn:
        contents = build_contents(history, text, audio)
        resp = await self.client.post(self.url, headers=self.headers, json=self._body(contents, True))
        # Если модель не знает thinkingLevel — повторяем без него.
        if resp.status_code == 400 and "thinking" in resp.text.lower():
            log.warning("thinkingConfig отклонён, повтор без него: %s", resp.text[:300])
            resp = await self.client.post(self.url, headers=self.headers, json=self._body(contents, False))
        if resp.status_code != 200:
            raise GeminiError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        return parse_turn(resp.json())
