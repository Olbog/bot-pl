"""Запросы к Gemini через Cloudflare Worker."""
import asyncio
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


RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (2, 5)  # паузы перед 2-й и 3-й попыткой


class GeminiOverloaded(GeminiError):
    """Все модели перегружены или недоступны — временная проблема Google."""


class Gemini:
    def __init__(self, worker_url: str, proxy_token: str, model: str, thinking_level: str, level: str,
                 client: httpx.AsyncClient | None = None, fallback_model: str = "",
                 sleep=asyncio.sleep):
        self.worker_url = worker_url
        self.models = [m for m in (model, fallback_model) if m]
        self.headers = {"x-proxy-token": proxy_token, "content-type": "application/json"}
        self.thinking_level = thinking_level
        self.system = system_prompt(level)
        self.client = client or httpx.AsyncClient(timeout=120)
        self.sleep = sleep

    def _url(self, model: str) -> str:
        return f"{self.worker_url}/v1beta/models/{model}:generateContent"

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

    async def _post(self, model: str, contents: list[dict]) -> httpx.Response:
        url = self._url(model)
        resp = await self.client.post(url, headers=self.headers, json=self._body(contents, True))
        # Если модель не знает thinkingLevel — повторяем без него.
        if resp.status_code == 400 and "thinking" in resp.text.lower():
            log.warning("thinkingConfig отклонён (%s), повтор без него", model)
            resp = await self.client.post(url, headers=self.headers, json=self._body(contents, False))
        return resp

    async def reply(self, history: list[tuple[str, str]], text: str | None = None,
                    audio: bytes | None = None) -> Turn:
        contents = build_contents(history, text, audio)
        last = ""
        for model in self.models:
            for attempt in range(len(RETRY_DELAYS) + 1):
                if attempt:
                    await self.sleep(RETRY_DELAYS[attempt - 1])
                try:
                    resp = await self._post(model, contents)
                except httpx.TransportError as e:  # таймаут, обрыв соединения
                    last = f"{model}: {type(e).__name__}"
                    log.warning("Gemini %s, попытка %d: %s", model, attempt + 1, last)
                    continue
                if resp.status_code == 200:
                    if model != self.models[0]:
                        log.info("Ответила запасная модель %s", model)
                    return parse_turn(resp.json())
                last = f"{model}: HTTP {resp.status_code}"
                if resp.status_code in RETRY_STATUSES:
                    log.warning("Gemini %s, попытка %d: HTTP %d", model, attempt + 1, resp.status_code)
                    continue
                if resp.status_code == 404 and model != self.models[-1]:
                    log.error("Модель %s не найдена, переключаюсь на запасную", model)
                    break
                raise GeminiError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        raise GeminiOverloaded(last)
