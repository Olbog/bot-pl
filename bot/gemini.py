"""Запросы к Gemini через Cloudflare Worker с цепочкой моделей.

Бот идёт по цепочке от лучшей модели к худшей. Модель, у которой кончился
лимит, временно пропускается:
  - дневной лимит  -> до сброса квоты (полночь по тихоокеанскому времени);
  - минутный лимит -> на время из ответа Google (обычно до минуты);
  - перегрузка/таймаут -> на короткое время.
"""
import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .prompt import RESPONSE_SCHEMA, json_format_hint, system_prompt

log = logging.getLogger(__name__)

DEFAULT_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
]

QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # дневные квоты Google сбрасываются в полночь PT
TRANSIENT_STATUSES = {500, 502, 503, 504}
TRANSIENT_COOLDOWN = 30        # сек: перегрузка, таймаут
MINUTE_COOLDOWN = 60           # сек: минутный лимит, если Google не назвал время
MISSING_COOLDOWN = 24 * 3600   # сек: модели нет (404)
DAY_REASON = "дневной лимит"


class GeminiError(Exception):
    pass


class GeminiOverloaded(GeminiError):
    """Все модели временно недоступны (перегрузка, минутные лимиты)."""


class GeminiExhausted(GeminiError):
    """У всех моделей исчерпан дневной лимит."""

    def __init__(self, reset_at: float, text_still_ok: bool = False):
        super().__init__(f"дневной лимит исчерпан до {datetime.fromtimestamp(reset_at)}")
        self.reset_at = reset_at
        # голосовое не обработать, но модели, не принимающие голос, ещё отвечают на текст
        self.text_still_ok = text_still_ok


@dataclass
class Turn:
    user_text: str
    reply_pl: str
    reply_translit: str
    reply_ru: str
    corrections: list[dict] = field(default_factory=list)
    corrected_pl: str = ""
    corrected_translit: str = ""
    corrected_ru: str = ""
    new_words: list[dict] = field(default_factory=list)
    target_uses: list[dict] = field(default_factory=list)
    model: str = ""


def is_gemma(model: str) -> bool:
    return model.startswith("gemma")


def next_quota_reset(now: float | None = None) -> float:
    now_pt = datetime.fromtimestamp(now if now is not None else time.time(), QUOTA_TZ)
    midnight = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp() + 60  # минута запаса


def classify_429(body: str) -> tuple[str, float | None]:
    """('day' | 'minute', retryDelay в секундах или None) по тексту ошибки 429."""
    kind = "minute"
    delay = None
    try:
        details = json.loads(body).get("error", {}).get("details", []) or []
    except (json.JSONDecodeError, AttributeError):
        details = []
    for d in details:
        if not isinstance(d, dict):
            continue
        for v in d.get("violations", []) or []:
            if "perday" in str(v.get("quotaId", "")).lower():
                kind = "day"
        rd = d.get("retryDelay")
        if rd:
            m = re.match(r"([\d.]+)s", str(rd))
            if m:
                delay = float(m.group(1))
    if kind == "minute" and "perday" in body.lower():
        kind = "day"
    return kind, delay


def build_contents(history: list[tuple[str, str]], text: str | None, audio: bytes | None,
                   preamble: str | None = None) -> list[dict]:
    """history — пары (role, text), role: 'user' | 'model'.
    preamble — инструкция в начале первой реплики (для моделей без systemInstruction)."""
    contents = [{"role": r, "parts": [{"text": t}]} for r, t in history]
    parts: list[dict] = []
    if audio is not None:
        parts.append({"inline_data": {"mime_type": "audio/ogg", "data": base64.b64encode(audio).decode()}})
        parts.append({"text": "(Ученик прислал голосовое сообщение. Расшифруй его в user_text и ответь.)"})
    if text:
        parts.append({"text": text})
    contents.append({"role": "user", "parts": parts})
    if preamble:
        contents[0]["parts"].insert(0, {"text": preamble})
    return contents


def extract_json(raw: str) -> dict:
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    elif not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            raw = raw[start:end + 1]
    return json.loads(raw)


def _raw_text(data: dict) -> str:
    try:
        cand = data["candidates"][0]
        return "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
    except (KeyError, IndexError, TypeError) as e:
        cands = data.get("candidates") or [{}]
        reason = data.get("promptFeedback") or cands[0].get("finishReason")
        raise GeminiError(f"Пустой ответ Gemini: {reason}") from e


def parse_json(data: dict, model: str = "") -> dict:
    raw = _raw_text(data)
    try:
        obj = extract_json(raw)
    except (json.JSONDecodeError, ValueError) as e:
        raise GeminiError(f"Модель вернула не JSON: {raw[:200]}") from e
    if not isinstance(obj, dict):
        raise GeminiError(f"Модель вернула не объект: {raw[:200]}")
    return obj


def parse_turn(data: dict, model: str = "") -> Turn:
    try:
        cand = data["candidates"][0]
        raw = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
    except (KeyError, IndexError, TypeError) as e:
        cands = data.get("candidates") or [{}]
        reason = data.get("promptFeedback") or cands[0].get("finishReason")
        raise GeminiError(f"Пустой ответ Gemini: {reason}") from e
    try:
        obj = extract_json(raw)
    except (json.JSONDecodeError, ValueError) as e:
        raise GeminiError(f"Модель вернула не JSON: {raw[:200]}") from e
    if not isinstance(obj, dict) or not obj.get("reply_pl"):
        raise GeminiError(f"В ответе нет реплики: {raw[:200]}")
    return Turn(
        user_text=str(obj.get("user_text", "")),
        reply_pl=str(obj.get("reply_pl", "")),
        reply_translit=str(obj.get("reply_translit", "")),
        reply_ru=str(obj.get("reply_ru", "")),
        corrected_pl=str(obj.get("corrected_pl", "")),
        corrected_translit=str(obj.get("corrected_translit", "")),
        corrected_ru=str(obj.get("corrected_ru", "")),
        corrections=[c for c in obj.get("corrections") or [] if isinstance(c, dict)],
        target_uses=[u for u in obj.get("target_uses") or [] if isinstance(u, dict) and u.get("lemma")],
        new_words=[w for w in obj.get("new_words") or [] if isinstance(w, dict)],
        model=model,
    )


class Gemini:
    def __init__(self, worker_url: str, proxy_token: str, models: list[str], thinking_level: str, level: str,
                 client: httpx.AsyncClient | None = None, clock=time.time):
        self.worker_url = worker_url
        self.models = list(models) or list(DEFAULT_MODELS)
        self.headers = {"x-proxy-token": proxy_token, "content-type": "application/json"}
        self.thinking_level = thinking_level
        self.system = system_prompt(level)
        self.client = client or httpx.AsyncClient(timeout=120)
        self.clock = clock
        self.blocked: dict[str, tuple[float, str]] = {}  # модель -> (до какого времени, причина)

    # ---------- проверка цепочки при запуске ----------

    async def check_models(self) -> None:
        """Убирает из цепочки модели, которых нет в API. При ошибке списка — оставляет как есть."""
        try:
            resp = await self.client.get(f"{self.worker_url}/v1beta/models?pageSize=1000",
                                         headers=self.headers, timeout=60)
            resp.raise_for_status()
            available = {m["name"].split("/", 1)[-1] for m in resp.json().get("models", [])}
        except Exception as e:
            log.warning("Не удалось получить список моделей, цепочка без проверки: %s", e)
            return
        missing = [m for m in self.models if m not in available]
        if missing:
            log.warning("Модели не найдены в API и пропущены: %s", ", ".join(missing))
        kept = [m for m in self.models if m in available]
        if kept:
            self.models = kept
        log.info("Цепочка моделей: %s", " → ".join(self.models))

    # ---------- запрос ----------

    def _url(self, model: str) -> str:
        return f"{self.worker_url}/v1beta/models/{model}:generateContent"

    def _body(self, model: str, req: dict, with_thinking: bool) -> dict:
        """req: system, schema, hint, history, text, audio."""
        if is_gemma(model):
            # Gemma: без systemInstruction и строгой схемы — инструкция внутри реплики, JSON из текста.
            return {
                "contents": build_contents(req["history"], req["text"], req["audio"],
                                           preamble=req["system"] + "\n\n" + req["hint"]),
                "generationConfig": {"temperature": 0.7},
            }
        gen: dict = {
            "responseMimeType": "application/json",
            "responseSchema": req["schema"],
            "temperature": 0.7,
        }
        if with_thinking:
            if model.startswith("gemini-2.5"):
                gen["thinkingConfig"] = {"thinkingBudget": 1024}
            elif self.thinking_level:
                gen["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        return {
            "systemInstruction": {"parts": [{"text": req["system"]}]},
            "contents": build_contents(req["history"], req["text"], req["audio"]),
            "generationConfig": gen,
        }

    async def _post(self, model: str, req: dict) -> httpx.Response:
        url = self._url(model)
        resp = await self.client.post(url, headers=self.headers, json=self._body(model, req, True))
        if resp.status_code == 400 and "thinking" in resp.text.lower():
            log.warning("thinkingConfig отклонён (%s), повтор без него", model)
            resp = await self.client.post(url, headers=self.headers, json=self._body(model, req, False))
        return resp

    def _block(self, model: str, seconds: float, reason: str) -> None:
        self.blocked[model] = (self.clock() + seconds, reason)
        log.warning("Модель %s пропускается %s: %s", model, _human(seconds), reason)

    def _available(self, model: str) -> bool:
        until = self.blocked.get(model)
        if until and until[0] > self.clock():
            return False
        self.blocked.pop(model, None)
        return True

    async def reply(self, history: list[tuple[str, str]], text: str | None = None,
                    audio: bytes | None = None, extra_system: str = "") -> Turn:
        system = self.system + ("\n\n" + extra_system if extra_system else "")
        req = {"system": system, "schema": RESPONSE_SCHEMA, "hint": json_format_hint(),
               "history": history, "text": text, "audio": audio}
        return await self._run(req, parse_turn)

    async def ask_json(self, prompt: str, schema: dict, hint: str, audio: bytes | None = None) -> dict:
        """Разовый запрос с JSON-ответом (составить набор, упражнение, проверить ответы…)."""
        req = {"system": "Ты помогаешь русскоязычному ученику учить польский. Отвечай строго в заданном JSON-формате.",
               "schema": schema, "hint": hint, "history": [], "text": prompt, "audio": audio}
        return await self._run(req, parse_json)

    async def _run(self, req: dict, parse):
        hard_errors: list[str] = []
        hard_failed: set[str] = set()
        for model in self.models:
            if not self._available(model):
                continue
            try:
                resp = await self._post(model, req)
            except httpx.TransportError as e:
                self._block(model, TRANSIENT_COOLDOWN, type(e).__name__)
                continue

            if resp.status_code == 200:
                try:
                    result = parse(resp.json(), model)
                except GeminiError as e:
                    # модель ответила мусором — пробуем следующую, эту не блокируем
                    log.warning("Модель %s: %s", model, e)
                    hard_errors.append(f"{model}: {e}")
                    hard_failed.add(model)
                    continue
                if model != self.models[0]:
                    log.info("Ответила модель %s", model)
                return result

            code, body = resp.status_code, resp.text
            if code == 429:
                kind, delay = classify_429(body)
                if kind == "day":
                    self._block(model, next_quota_reset(self.clock()) - self.clock(), DAY_REASON)
                else:
                    self._block(model, delay or MINUTE_COOLDOWN, "минутный лимит")
            elif code in TRANSIENT_STATUSES:
                self._block(model, TRANSIENT_COOLDOWN, f"HTTP {code}")
            elif code == 404:
                self._block(model, MISSING_COOLDOWN, "модель не найдена")
            else:
                # 400 и прочее: модель не приняла запрос (например, голос у Gemma) — пробуем следующую
                log.warning("Модель %s: HTTP %d %s", model, code, body[:300])
                hard_errors.append(f"{model}: HTTP {code}: {body[:300]}")
                hard_failed.add(model)

        self._raise_all_failed(hard_errors, hard_failed, req["audio"] is not None)

    def _raise_all_failed(self, hard_errors: list[str], hard_failed: set[str], was_audio: bool):
        now = self.clock()
        active = {m: self.blocked[m] for m in self.models if m in self.blocked and self.blocked[m][0] > now}
        day = {m: u for m, (u, r) in active.items() if r == DAY_REASON}
        if day and set(day) | hard_failed == set(self.models):
            raise GeminiExhausted(min(day.values()), text_still_ok=was_audio and bool(hard_failed))
        if hard_errors and not active:
            raise GeminiError(hard_errors[-1])
        detail = "; ".join(hard_errors[-2:]) or f"временно недоступно моделей: {len(active)}"
        raise GeminiOverloaded(detail)


def _human(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} ч"
    return f"{int(seconds)} с"
