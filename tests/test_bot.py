import asyncio
import json

import httpx

from bot.config import Config
from bot.db import DB
from bot.fmt import summary_message, turn_message
from bot.gemini import Gemini, GeminiError, GeminiOverloaded, Turn, build_contents, parse_turn
from bot.main import App

TURN_JSON = {
    "user_text": "Wczoraj byłem w sklep i kupiłem хлеб",
    "corrections": [{"original": "w sklep", "correct": "w sklepie", "translit": "в СКЛЕ-пе",
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
    g = Gemini("https://w.example", "tok", "m", "low", "A1", client=client)
    t = run(g.reply([], text="hej"))
    assert t.reply_pl == "Co jeszcze kupiłeś?"
    assert len(calls) == 2
    assert calls[0]["generationConfig"]["responseMimeType"] == "application/json"
    assert "systemInstruction" in calls[0]


def test_gemini_http_error():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, text='{"error":"User location is not supported"}')))
    g = Gemini("https://w", "t", "m", "", "A1", client=client)
    try:
        run(g.reply([], text="x"))
    except GeminiError as e:
        assert "location" in str(e)
    else:
        raise AssertionError


async def _nosleep(_):
    pass


def scripted(responses, seen):
    """Отдаёт ответы по очереди и запоминает, к какой модели был запрос."""
    it = iter(responses)

    def handler(req: httpx.Request):
        seen.append(req.url.path.split("/")[-1].split(":")[0])
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_retry_503_then_ok():
    seen = []
    ok = httpx.Response(200, json=gemini_response(TURN_JSON))
    busy = httpx.Response(503, text="high demand")
    g = Gemini("https://w", "t", "main", "", "A1", client=scripted([busy, busy, ok], seen),
               fallback_model="lite", sleep=_nosleep)
    assert run(g.reply([], text="x")).reply_pl == "Co jeszcze kupiłeś?"
    assert seen == ["main", "main", "main"]


def test_fallback_after_retries_and_timeouts():
    seen = []
    busy = httpx.Response(503, text="high demand")
    ok = httpx.Response(200, json=gemini_response(TURN_JSON))
    g = Gemini("https://w", "t", "main", "", "A1",
               client=scripted([busy, httpx.ReadTimeout("t"), busy, ok], seen),
               fallback_model="lite", sleep=_nosleep)
    assert run(g.reply([], text="x")).reply_pl == "Co jeszcze kupiłeś?"
    assert seen == ["main", "main", "main", "lite"]


def test_all_overloaded():
    seen = []
    busy = httpx.Response(429, text="quota")
    g = Gemini("https://w", "t", "main", "", "A1", client=scripted([busy] * 6, seen),
               fallback_model="lite", sleep=_nosleep)
    try:
        run(g.reply([], text="x"))
    except GeminiOverloaded as e:
        assert "lite" in str(e)
    else:
        raise AssertionError
    assert seen == ["main"] * 3 + ["lite"] * 3


def test_primary_404_switches_to_fallback():
    seen = []
    ok = httpx.Response(200, json=gemini_response(TURN_JSON))
    g = Gemini("https://w", "t", "main", "", "A1",
               client=scripted([httpx.Response(404, text="not found"), ok], seen),
               fallback_model="lite", sleep=_nosleep)
    assert run(g.reply([], text="x")).reply_pl
    assert seen == ["main", "lite"]


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
    assert "🗣" in msg and "a &lt;b&gt; &amp; c" in msg
    assert "w sklepie" in msg and "chleb" in msg and "Co jeszcze kupiłeś?" in msg


def test_turn_message_no_errors():
    t = Turn(user_text="Dzień dobry", reply_pl="Cześć!", reply_translit="чещчь", reply_ru="Привет!")
    msg = turn_message(t, from_voice=False)
    assert "✅" in msg and "🗣" not in msg


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
    def __init__(self, fail=False, overloaded=False):
        self.calls, self.fail, self.overloaded = [], fail, overloaded

    async def reply(self, history, text=None, audio=None):
        self.calls.append((history, text, audio))
        if self.overloaded:
            raise GeminiOverloaded("lite: HTTP 503")
        if self.fail:
            raise GeminiError("HTTP 400")
        return parse_turn(gemini_response(TURN_JSON))


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
    assert "🗣" in app.tg.sent[0]


def test_gemini_error_is_reported_and_not_saved():
    app = make_app(gem=FakeGemini(fail=True))
    run(app.handle(msg(text="x")))
    assert "Ошибка Gemini" in app.tg.sent[0]
    assert app.db.message_count(app.db.current_session(42)) == 0


def test_overloaded_friendly_message():
    app = make_app(gem=FakeGemini(overloaded=True))
    run(app.handle(msg(text="x")))
    assert "перегружен" in app.tg.sent[0] and "HTTP" not in app.tg.sent[0]


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
