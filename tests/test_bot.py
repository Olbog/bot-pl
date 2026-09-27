import asyncio
import json

import httpx

from bot.config import Config
from bot.db import DB
from bot.fmt import summary_message, turn_message
from bot.gemini import (DEFAULT_MODELS, Gemini, GeminiError, GeminiExhausted, GeminiOverloaded, Turn,
                        build_contents, classify_429, next_quota_reset, parse_turn)
from bot.main import App

TURN_JSON = {
    "user_text": "Wczoraj byłem w sklep i kupiłem хлеб",
    "corrected_pl": "Wczoraj byłem w sklepie i kupiłem chleb.",
    "corrected_translit": "ВЧО-рай БЫ-уэм ф СКЛЕ-пе и ку-ПИ-уэм хлеп",
    "corrected_ru": "Вчера я был в магазине и купил хлеб.",
    "corrections": [{"kind": "grammar", "original": "w sklep", "correct": "w sklepie", "translit": "в СКЛЕ-пе",
                     "ru": "в магазине", "why": "после w — предложный падеж"}],
    "new_words": [{"ru": "хлеб", "pl": "chleb", "translit": "хлеб"}],
    "reply_pl": "Co jeszcze kupiłeś?",
    "reply_translit": "цо ЕЩ-че ку-ПИ-уэщ",
    "reply_ru": "Что ещё купил?",
}


def gemini_response(obj: dict) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj, ensure_ascii=False)}]}}]}


def run(coro):
    return asyncio.run(coro)


# ---------- gemini ----------

def test_parse_turn():
    t = parse_turn(gemini_response(TURN_JSON))
    assert t.reply_pl == "Co jeszcze kupiłeś?"
    assert t.corrections[0]["correct"] == "w sklepie"
    assert t.new_words[0]["pl"] == "chleb"


def test_parse_turn_skips_thought_parts():
    data = {"candidates": [{"content": {"parts": [
        {"text": "думаю...", "thought": True},
        {"text": json.dumps(TURN_JSON)},
    ]}}]}
    assert parse_turn(data).reply_pl == "Co jeszcze kupiłeś?"


def test_parse_turn_errors():
    for bad in ({"candidates": []}, {"promptFeedback": {"blockReason": "X"}},
                {"candidates": [{"content": {"parts": [{"text": "не json"}]}}]}):
        try:
            parse_turn(bad)
        except GeminiError:
            pass
        else:
            raise AssertionError(bad)


def test_build_contents_with_audio():
    c = build_contents([("user", "a"), ("model", "b")], None, b"OGG")
    assert [x["role"] for x in c] == ["user", "model", "user"]
    assert c[-1]["parts"][0]["inline_data"]["mime_type"] == "audio/ogg"


def test_gemini_request_and_thinking_fallback():
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append(body)
        assert req.headers["x-proxy-token"] == "tok"
        assert req.url.path == "/v1beta/models/m:generateContent"
        if "thinkingConfig" in body["generationConfig"]:
            return httpx.Response(400, text='{"error":"Unknown name thinkingLevel"}')
        return httpx.Response(200, json=gemini_response(TURN_JSON))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    g = Gemini("https://w.example", "tok", ["m"], "low", "A1", client=client)
    t = run(g.reply([], text="hej"))
    assert t.reply_pl == "Co jeszcze kupiłeś?" and t.model == "m"
    assert len(calls) == 2
    assert calls[0]["generationConfig"]["responseMimeType"] == "application/json"
    assert "systemInstruction" in calls[0]


def test_hard_error_only_model():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, text='{"error":"User location is not supported"}')))
    g = Gemini("https://w", "t", ["m"], "", "A1", client=client)
    try:
        run(g.reply([], text="x"))
    except GeminiOverloaded:
        raise AssertionError("должна быть обычная ошибка")
    except GeminiError as e:
        assert "location" in str(e)
    else:
        raise AssertionError


class Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def by_model(table, seen):
    """table: модель -> список ответов по очереди (последний повторяется)."""
    queues = {k: list(v) for k, v in table.items()}

    def handler(req: httpx.Request):
        if req.method == "GET":
            return httpx.Response(200, json={"models": [{"name": f"models/{m}"} for m in table]})
        model = req.url.path.split("/")[-1].split(":")[0]
        seen.append(model)
        q = queues[model]
        r = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(r, Exception):
            raise r
        return r
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


OK = httpx.Response(200, json=gemini_response(TURN_JSON))
BUSY = httpx.Response(503, text="high demand")
DAY_429 = httpx.Response(429, text=json.dumps({"error": {"code": 429, "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "50s"}]}}))
MIN_429 = httpx.Response(429, text=json.dumps({"error": {"code": 429, "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}}))


def test_classify_429():
    assert classify_429(DAY_429.text) == ("day", 50.0)
    assert classify_429(MIN_429.text) == ("minute", 17.0)
    assert classify_429("garbage")[0] == "minute"


def test_next_quota_reset_is_pt_midnight():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    r = datetime.fromtimestamp(next_quota_reset(1_790_000_000.0), ZoneInfo("America/Los_Angeles"))
    assert (r.hour, r.minute) == (0, 1)
    assert 0 < next_quota_reset(1_790_000_000.0) - 1_790_000_000.0 <= 86400 + 60


def test_chain_day_limit_skips_until_reset():
    seen, clock = [], Clock()
    g = Gemini("https://w", "t", ["a", "b"], "", "A1", client=by_model({"a": [DAY_429], "b": [OK]}, seen),
               clock=clock)
    assert run(g.reply([], text="x")).model == "b"
    assert run(g.reply([], text="y")).model == "b"
    assert seen == ["a", "b", "b"]          # во второй раз "a" уже не трогаем
    clock.t = next_quota_reset(clock.t) + 1  # после сброса снова пробуем "a"
    run(g.reply([], text="z"))
    assert seen[-2:] == ["a", "b"]


def test_chain_minute_limit_and_overload_short_block():
    seen, clock = [], Clock()
    g = Gemini("https://w", "t", ["a", "b", "c"], "", "A1",
               client=by_model({"a": [MIN_429, OK], "b": [BUSY, OK], "c": [OK]}, seen), clock=clock)
    assert run(g.reply([], text="x")).model == "c"
    clock.t += 18                             # минутный лимит "a" (17 с) прошёл, "b" ещё на паузе (30 с)
    assert run(g.reply([], text="y")).model == "a"
    assert seen == ["a", "b", "c", "a"]


def test_chain_timeout_and_garbage_go_next():
    seen = []
    garbage = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "не json"}]}}]})
    g = Gemini("https://w", "t", ["a", "b", "c"], "", "A1",
               client=by_model({"a": [httpx.ReadTimeout("t")], "b": [garbage], "c": [OK]}, seen))
    assert run(g.reply([], text="x")).model == "c"


def test_all_day_exhausted():
    seen = []
    g = Gemini("https://w", "t", ["a", "b"], "", "A1", client=by_model({"a": [DAY_429], "b": [DAY_429]}, seen))
    try:
        run(g.reply([], text="x"))
    except GeminiExhausted as e:
        assert e.reset_at > 0 and not e.text_still_ok
    else:
        raise AssertionError


def test_voice_exhausted_but_text_ok():
    seen = []
    bad_audio = httpx.Response(400, text='{"error":"audio input not supported"}')
    g = Gemini("https://w", "t", ["gemini-x", "gemma-4-31b-it"], "", "A1",
               client=by_model({"gemini-x": [DAY_429], "gemma-4-31b-it": [bad_audio, OK]}, seen))
    try:
        run(g.reply([], audio=b"OGG"))
    except GeminiExhausted as e:
        assert e.text_still_ok
    else:
        raise AssertionError
    assert run(g.reply([], text="x")).model == "gemma-4-31b-it"


def test_overloaded_when_all_busy():
    g = Gemini("https://w", "t", ["a", "b"], "", "A1", client=by_model({"a": [BUSY], "b": [MIN_429]}, []))
    try:
        run(g.reply([], text="x"))
    except GeminiExhausted:
        raise AssertionError
    except GeminiOverloaded:
        pass


def test_gemma_body_and_fenced_json():
    bodies = []
    fenced = "Oto odpowiedź:\n```json\n" + json.dumps(TURN_JSON, ensure_ascii=False) + "\n```"

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": fenced}]}}]})

    g = Gemini("https://w", "t", ["gemma-4-31b-it"], "low", "A1",
               client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    t = run(g.reply([("user", "u"), ("model", "m")], text="x"))
    assert t.reply_pl == "Co jeszcze kupiłeś?"
    b = bodies[0]
    assert "systemInstruction" not in b and "responseSchema" not in b["generationConfig"]
    assert "JSON" in b["contents"][0]["parts"][0]["text"]       # инструкция в первой реплике
    assert b["contents"][0]["parts"][1]["text"] == "u"


def test_thinking_config_per_family():
    g = Gemini("https://w", "t", ["x"], "low", "A1")
    assert g._body("gemini-2.5-flash-lite", [], "x", None, True)["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 1024}
    assert g._body("gemini-3.8-flash", [], "x", None, True)["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}


def test_check_models_drops_missing():
    seen = []
    g = Gemini("https://w", "t", ["a", "nope", "b"], "", "A1", client=by_model({"a": [OK], "b": [OK]}, seen))
    run(g.check_models())
    assert g.models == ["a", "b"]


def test_default_chain_when_empty():
    assert Gemini("https://w", "t", [], "", "A1").models == DEFAULT_MODELS


# ---------- db ----------

def test_db_sessions_history_summary():
    db = DB(":memory:")
    s1 = db.current_session(1)
    assert db.current_session(1) == s1
    db.save_turn(s1, 1, "u1", "m1", TURN_JSON["corrections"], TURN_JSON["new_words"])
    db.save_turn(s1, 1, "u2", "m2", [], [])
    assert db.history(s1, 20) == [("user", "u1"), ("model", "m1"), ("user", "u2"), ("model", "m2")]
    # обрезка истории не должна начинаться с реплики модели
    assert db.history(s1, 3) == [("user", "u2"), ("model", "m2")]
    words, corrs = db.session_summary(s1)
    assert words[0]["pl"] == "chleb" and corrs[0]["correct"] == "w sklepie"
    assert db.message_count(s1) == 2
    s2 = db.new_session(1)
    assert s2 != s1 and db.history(s2, 20) == [] and db.current_session(1) == s2
    assert db.current_session(2) not in (s1, s2)


# ---------- fmt ----------

def test_turn_message_voice_and_escape():
    t = parse_turn(gemini_response({**TURN_JSON, "user_text": "a <b> & c"}))
    msg = turn_message(t, from_voice=True)
    assert "🎙" in msg and "a &lt;b&gt; &amp; c" in msg
    assert "📝 w sklep" in msg and "✔️" in msg and "w sklepie i kupiłem chleb" in msg
    assert "w sklepie" in msg and "chleb" in msg and "Co jeszcze kupiłeś?" in msg


def test_turn_message_no_errors():
    t = Turn(user_text="Dzień dobry", reply_pl="Cześć!", reply_translit="чещчь", reply_ru="Привет!")
    msg = turn_message(t, from_voice=False)
    assert "✅" in msg and "🎙" not in msg and "✔️" not in msg


def test_corrected_same_as_user_means_no_errors():
    t = Turn(user_text="Dzień dobry!", reply_pl="Cześć!", reply_translit="", reply_ru="",
             corrected_pl="Dzień dobry.")
    assert "✅" in turn_message(t, from_voice=False)


def test_pronunciation_icon_and_unknown_kind():
    t = Turn(user_text="tulke", reply_pl="Tak?", reply_translit="", reply_ru="", corrected_pl="tylko",
             corrections=[{"kind": "pronunciation", "original": "tulke", "correct": "tylko"},
                          {"kind": "???", "original": "a", "correct": "b"}])
    msg = turn_message(t, from_voice=True)
    assert "🗣 tulke" in msg and "• a" in msg


def test_prompt_and_schema_consistent():
    from bot.prompt import FIELDS, RESPONSE_SCHEMA, json_format_hint, system_prompt
    assert set(FIELDS) == set(RESPONSE_SCHEMA["properties"])
    assert all(f in json_format_hint() for f in FIELDS)
    assert "ДОСЛОВНО" in system_prompt("A1")


def test_summary_dedup_and_empty():
    w = [{"ru": "хлеб", "pl": "chleb", "translit": "хлеб"}, {"ru": "хлеб", "pl": "Chleb", "translit": "хлеб"}]
    msg = summary_message(w, [], 3)
    assert msg.count("chleb") + msg.count("Chleb") == 1
    assert "пока ничего" in summary_message([], [], 0)


# ---------- app ----------

class FakeTG:
    def __init__(self):
        self.sent, self.voices = [], []

    async def send_message(self, chat_id, text):
        self.sent.append(text)

    async def send_voice(self, chat_id, ogg):
        self.voices.append(ogg)

    async def send_action(self, chat_id, action):
        pass

    async def download_file(self, file_id):
        return b"OGG-IN"


class FakeGemini:
    def __init__(self, fail=False, overloaded=False, exhausted=None):
        self.calls, self.fail, self.overloaded, self.exhausted = [], fail, overloaded, exhausted

    async def check_models(self):
        pass

    async def reply(self, history, text=None, audio=None):
        self.calls.append((history, text, audio))
        if self.exhausted is not None:
            raise GeminiExhausted(1_790_000_000.0, text_still_ok=self.exhausted)
        if self.overloaded:
            raise GeminiOverloaded("lite: HTTP 503")
        if self.fail:
            raise GeminiError("HTTP 400")
        return parse_turn(gemini_response(TURN_JSON), "gemini-3.5-flash-lite")


def make_app(gem=None, tts_fail=False):
    cfg = Config(telegram_token="x", worker_url="w", proxy_token="p", allowed_ids={42})

    async def tts(text, voice, rate):
        if tts_fail:
            raise RuntimeError("no ffmpeg")
        return b"OGG:" + text.encode()

    return App(cfg, FakeTG(), gem or FakeGemini(), DB(":memory:"), tts=tts)


def msg(**kw):
    return {"chat": {"id": 42}, "from": {"id": kw.pop("uid", 42)}, **kw}


def test_foreign_user_gets_id():
    app = make_app()
    run(app.handle(msg(uid=7, text="hej")))
    assert "7" in app.tg.sent[0] and not app.gemini.calls


def test_text_turn_sends_text_and_voice_and_saves():
    app = make_app()
    run(app.handle(msg(text="Wczoraj byłem w sklep")))
    assert "w sklepie" in app.tg.sent[0]
    assert app.tg.voices == [b"OGG:Co jeszcze kupi\xc5\x82e\xc5\x9b?"]
    run(app.handle(msg(text="dalej")))
    history = app.gemini.calls[1][0]
    assert history[0][0] == "user" and history[1] == ("model", "Co jeszcze kupiłeś?")


def test_voice_turn_passes_audio():
    app = make_app()
    run(app.handle(msg(voice={"file_id": "f1"})))
    assert app.gemini.calls[0][2] == b"OGG-IN" and app.gemini.calls[0][1] is None
    assert "🎙" in app.tg.sent[0]


def test_gemini_error_is_reported_and_not_saved():
    app = make_app(gem=FakeGemini(fail=True))
    run(app.handle(msg(text="x")))
    assert "Ошибка Gemini" in app.tg.sent[0]
    assert app.db.message_count(app.db.current_session(42)) == 0


def test_overloaded_friendly_message():
    app = make_app(gem=FakeGemini(overloaded=True))
    run(app.handle(msg(text="x")))
    assert "перегружен" in app.tg.sent[0] and "HTTP" not in app.tg.sent[0]


def test_exhausted_messages():
    app = make_app(gem=FakeGemini(exhausted=False))
    run(app.handle(msg(text="x")))
    assert "Дневной лимит" in app.tg.sent[0]
    app = make_app(gem=FakeGemini(exhausted=True))
    run(app.handle(msg(voice={"file_id": "f"})))
    assert "Голосовые" in app.tg.sent[0] and "Текстом" in app.tg.sent[0]


def test_model_footer():
    app = make_app()
    run(app.handle(msg(text="x")))
    assert "3.5-flash-lite" in app.tg.sent[0]
    t = parse_turn(gemini_response(TURN_JSON), "gemini-3.8-flash")
    assert "3.8-flash" not in turn_message(t, from_voice=False, show_model=False)


def test_tts_failure_keeps_text():
    app = make_app(tts_fail=True)
    run(app.handle(msg(text="x")))
    assert "w sklepie" in app.tg.sent[0] and "Озвучка" in app.tg.sent[1]


def test_commands_new_and_itog():
    app = make_app()
    run(app.handle(msg(text="hej")))
    run(app.handle(msg(text="/itog")))
    assert "chleb" in app.tg.sent[-1] and "w sklepie" in app.tg.sent[-1]
    run(app.handle(msg(text="/new")))
    run(app.handle(msg(text="/itog")))
    assert "пока ничего" in app.tg.sent[-1]
    run(app.handle(msg(text="/start")))
    assert "/new" in app.tg.sent[-1]
