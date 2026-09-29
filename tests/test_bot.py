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
    req = {"system": "S", "schema": {}, "hint": "H", "history": [], "text": "x", "audio": None}
    assert g._body("gemini-2.5-flash-lite", req, True)["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 1024}
    assert g._body("gemini-3.8-flash", req, True)["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}


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
        self.sent, self.voices, self.buttons, self.edits, self.docs = [], [], [], [], []

    async def send_message(self, chat_id, text, buttons=None):
        self.sent.append(text)
        self.buttons.append(buttons)
        return {"message_id": len(self.sent)}

    async def edit_message(self, chat_id, message_id, text, buttons=None):
        self.edits.append((text, buttons))

    async def send_document(self, chat_id, filename, content, caption=""):
        self.docs.append((filename, content.decode("utf-8"), caption))

    async def answer_callback(self, cid, text=None):
        pass

    async def send_voice(self, chat_id, ogg):
        self.voices.append(ogg)

    async def send_action(self, chat_id, action):
        pass

    async def download_file(self, file_id):
        return b"OGG-IN"


class FakeGemini:
    def __init__(self, fail=False, overloaded=False, exhausted=None, turn=None, words=None):
        self.calls, self.fail, self.overloaded, self.exhausted = [], fail, overloaded, exhausted
        self.turn = turn or TURN_JSON
        self.words = words or {"title": "Кафе", "words": [
            {"pl": "ciasto", "translit": "ЧЬЯ-сто", "ru": "пирог", "pos": "сущ"},
            {"pl": "piec", "translit": "пец", "ru": "печь", "pos": "глаг"},
            {"pl": "kawa", "translit": "КА-ва", "ru": "кофе", "pos": "сущ"}]}
        self.extras, self.prompts = [], []

    async def ask_json(self, prompt, schema, hint):
        self.prompts.append(prompt)
        props = schema.get("properties", {})
        if "rules" in props:
            return {"rules": [{"title": "Родительный после отрицания", "explanation": "nie + глагол → kogo? czego?",
                               "examples": [{"pl": "Nie lubię cukru.", "translit": "не ЛЮ-бе ЦУ-кру",
                                             "ru": "Не люблю сахар."}]}]}
        if "phrases" in props:
            return {"phrases": [{"pl": "najbardziej lubię", "translit": "най-бар-ДЗЕЙ ЛЮ-бе", "ru": "больше всего люблю"},
                                {"pl": "tak jak mówię", "translit": "так як МУ-ве", "ru": "как я говорю"}]}
        if "items" in props and "rule" in props["items"]["items"]["properties"]:
            return {"items": [{"id": i, "rule": "Родительный падеж после отрицания"} for i in range(1, 50)]}
        return self.words

    async def check_models(self):
        pass

    async def reply(self, history, text=None, audio=None, extra_system=""):
        self.calls.append((history, text, audio))
        self.extras.append(extra_system)
        if self.exhausted is not None:
            raise GeminiExhausted(1_790_000_000.0, text_still_ok=self.exhausted)
        if self.overloaded:
            raise GeminiOverloaded("lite: HTTP 503")
        if self.fail:
            raise GeminiError("HTTP 400")
        return parse_turn(gemini_response(self.turn), "gemini-3.5-flash-lite")


def make_app(gem=None, tts_fail=False, clock=None):
    cfg = Config(telegram_token="x", worker_url="w", proxy_token="p", allowed_ids={42})

    async def tts(text, voice, rate):
        if tts_fail:
            raise RuntimeError("no ffmpeg")
        return b"OGG:" + text.encode()

    return App(cfg, FakeTG(), gem or FakeGemini(), DB(":memory:"), tts=tts, clock=clock or Clock())


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
    assert "Итог за" in app.tg.sent[-1] and any(d == "i:h" for row in app.tg.buttons[-1] for _, d in row)
    run(app.on_callback(cb("i:s")))
    assert "chleb" in app.tg.sent[-1] and "w sklep→w sklepie" in app.tg.sent[-1]
    run(app.handle(msg(text="/new")))
    run(app.on_callback(cb("i:s")))
    assert "разговоров не было" in app.tg.sent[-1]
    run(app.handle(msg(text="/start")))
    assert "/new" in app.tg.sent[-1]


# ---------- тренировка наборов ----------

from bot import training  # noqa: E402


def use(form, ok, day):
    return {"form": form, "correct": int(ok), "day": day}


def test_stats_streak_resets_on_error():
    uses = [use("piekę", 1, "d1"), use("pieczesz", 1, "d1"), use("piec", 0, "d2"),
            use("piecze", 1, "d2"), use("Piecze", 1, "d3")]
    st = training.stats(uses)
    assert (st.total, st.correct, st.errors, st.streak) == (5, 4, 1, 2)
    assert st.streak_forms == ["piecze"] and st.streak_days == 2
    assert "piekę" in st.all_forms


def test_meets_criteria():
    c = training.Criteria(streak=4, forms=3, days=2)
    good = [use("a", 1, "d1"), use("b", 1, "d1"), use("c", 1, "d2"), use("a", 1, "d2")]
    assert training.meets(training.stats(good), c)
    assert not training.meets(training.stats(good[:3]), c)                      # мало раз
    assert not training.meets(training.stats([use("a", 1, "d1")] * 4), c)        # одна форма, один день
    assert not training.meets(training.stats(good + [use("x", 0, "d3")]), c)     # ошибка сбросила серию


def test_due_for_review():
    w = {"mastered_at": 0.0, "review_stage": 0, "last_review_at": None}
    assert not training.due_for_review(w, 2 * 86400)
    assert training.due_for_review(w, 3 * 86400)
    w2 = {"mastered_at": 0.0, "review_stage": 1, "last_review_at": 3 * 86400.0}
    assert not training.due_for_review(w2, 9 * 86400) and training.due_for_review(w2, 10 * 86400)
    assert not training.due_for_review({"mastered_at": None, "review_stage": 0, "last_review_at": None}, 1e9)


def cb(data, mid=1):
    return {"id": "c", "from": {"id": 42}, "message": {"chat": {"id": 42}, "message_id": mid}, "data": data}


def start_set(app, topic="кафе"):
    run(app.on_callback(cb("s:topic")))
    run(app.handle(msg(text=topic)))
    run(app.on_callback(cb("p:ok")))


def test_topic_flow_preview_toggle_and_start():
    gem = FakeGemini()
    app = make_app(gem=gem)
    run(app.on_callback(cb("s:topic")))
    assert app.db.get_state(42)["pending"] == {"step": "topic"}
    run(app.handle(msg(text="кафе")))
    assert "кафе" in gem.prompts[0] and not gem.calls        # тема ушла в составление, не в разговор
    assert "ciasto" in app.tg.sent[-1] and any(d == "p:0" for row in app.tg.buttons[-1] for _, d in row)
    run(app.on_callback(cb("p:2")))                            # убрать kawa
    assert "<s>" in app.tg.edits[-1][0]
    run(app.on_callback(cb("p:ok")))
    active = app.db.active_set(42)
    assert active["title"] == "Кафе"
    assert [w["pl"] for w in app.db.set_words(active["id"])] == ["ciasto", "piec"]
    assert app.db.get_state(42) == {"mode": "set", "pending": None}
    assert gem.calls[-1][1] == "Zaczynajmy!" and "ТРЕНИРОВКА" in gem.extras[-1] and "ciasto" in gem.extras[-1]
    assert "✅ Без" not in app.tg.sent[-1] and "Исправления" not in app.tg.sent[-1]


def test_own_words_and_add_and_cancel():
    gem = FakeGemini(words={"title": "Мои", "words": [{"pl": "dom", "translit": "дом", "ru": "дом", "pos": "сущ"}]})
    app = make_app(gem=gem)
    run(app.on_callback(cb("s:own")))
    run(app.handle(msg(text="дом")))
    assert "dom" in app.tg.sent[-1]
    run(app.on_callback(cb("p:add")))
    gem.words = {"title": "x", "words": [{"pl": "kot", "translit": "кот", "ru": "кот", "pos": "сущ"},
                                         {"pl": "dom", "translit": "дом", "ru": "дом", "pos": "сущ"}]}
    run(app.handle(msg(text="кот")))
    p = app.db.get_state(42)["pending"]
    assert [w["pl"] for w in p["words"]] == ["dom", "kot"] and p["title"] == "Мои"   # без дублей
    run(app.handle(msg(text="/cancel")))
    assert app.db.get_state(42)["pending"] is None


def test_target_uses_progress_and_auto_mastered():
    t = {**TURN_JSON, "target_uses": [{"lemma": "Piec", "form": "piekę", "correct": True},
                                      {"lemma": "nieznane", "form": "x", "correct": True}]}
    gem = FakeGemini(turn=t)
    clock = Clock()
    app = make_app(gem=gem, clock=clock)
    app.criteria = training.Criteria(streak=3, forms=2, days=2)
    start_set(app)
    word = [w for w in app.db.set_words(app.db.active_set(42)["id"]) if w["pl"] == "piec"][0]
    assert len(app.db.word_uses(word["id"])) == 1                # открывающая реплика тоже учитывается
    assert "🎯 piekę ✓" in app.tg.sent[-1]
    gem.turn = {**t, "target_uses": [{"lemma": "piec", "form": "pieczesz", "correct": True}]}
    clock.t += 86400
    run(app.handle(msg(text="x")))
    assert app.db.word(word["id"])["mastered_at"] is None
    run(app.handle(msg(text="y")))
    assert app.db.word(word["id"])["mastered_by"] == "auto"
    assert any("🎉 Освоено: <b>piec</b>" in m for m in app.tg.sent)


def test_error_resets_streak_in_progress_view():
    t = {**TURN_JSON, "target_uses": [{"lemma": "ciasto", "form": "czasta", "correct": False}]}
    app = make_app(gem=FakeGemini(turn=t))
    start_set(app)
    run(app.handle(msg(text="/set")))
    assert "🔴 <b>ciasto</b>" in app.tg.sent[-1] and "0/20" in app.tg.sent[-1]
    assert any("🎯 czasta ✗" in m for m in app.tg.sent)


def test_manual_mastered_toggle_and_suggest_next_and_carry():
    app = make_app()
    app.cfg = Config(telegram_token="x", worker_url="w", proxy_token="p", allowed_ids={42}, next_set_ratio=0.6)
    start_set(app)
    words = app.db.set_words(app.db.active_set(42)["id"])
    run(app.on_callback(cb("m:list")))
    run(app.on_callback(cb(f"m:{words[0]['id']}")))
    run(app.on_callback(cb(f"m:{words[1]['id']}")))
    assert app.db.word(words[0]["id"])["mastered_by"] == "user"
    run(app.on_callback(cb(f"m:{words[1]['id']}")))                  # повторное нажатие снимает отметку
    assert app.db.word(words[1]["id"])["mastered_at"] is None
    run(app.on_callback(cb(f"m:{words[1]['id']}")))
    run(app.handle(msg(text="x")))                                   # 2 из 3 = 67% ≥ 60% → предложение
    assert any("почти освоен" in m for m in app.tg.sent)
    n = sum("почти освоен" in m for m in app.tg.sent)
    run(app.handle(msg(text="y")))
    assert sum("почти освоен" in m for m in app.tg.sent) == n        # только один раз
    old_left = [w for w in words if w["id"] == words[2]["id"]][0]
    app.gemini.words = {"title": "Новый", "words": [{"pl": "herbata", "translit": "хер-БА-та", "ru": "чай", "pos": "сущ"}]}
    start_set(app, "чай")
    new_words = [w["pl"] for w in app.db.set_words(app.db.active_set(42)["id"])]
    assert "herbata" in new_words and old_left["pl"] in new_words and words[0]["pl"] not in new_words


def test_free_mode_review_block_and_advance():
    t = {**TURN_JSON, "target_uses": [{"lemma": "ciasto", "form": "ciasta", "correct": True}]}
    gem = FakeGemini(turn=t)
    clock = Clock()
    app = make_app(gem=gem, clock=clock)
    start_set(app)
    w = [x for x in app.db.set_words(app.db.active_set(42)["id"]) if x["pl"] == "ciasto"][0]
    app.db.set_mastered(w["id"], "user")
    app.db.conn.execute("UPDATE set_words SET mastered_at=? WHERE id=?", (clock.t, w["id"]))
    run(app.handle(msg(text="/free")))
    assert app.db.get_state(42)["mode"] == "free"
    run(app.handle(msg(text="a")))
    assert "ПОВТОРЕНИЕ" not in gem.extras[-1]                      # ещё рано
    clock.t += 4 * 86400
    run(app.handle(msg(text="b")))
    assert "ПОВТОРЕНИЕ" in gem.extras[-1] and "ciasto" in gem.extras[-1]
    assert app.db.word(w["id"])["review_stage"] == 1
    run(app.handle(msg(text="c")))
    assert "ПОВТОРЕНИЕ" not in gem.extras[-1]                      # следующий раз через 7 дней


def test_mode_switch_buttons_and_set_view():
    app = make_app()
    run(app.handle(msg(text="/set")))
    assert "Набора пока нет" in app.tg.sent[-1]
    run(app.on_callback(cb("mode:set")))
    assert "Сначала составь набор" in app.tg.sent[-1]
    start_set(app)
    run(app.on_callback(cb("mode:free")))
    assert app.db.get_state(42)["mode"] == "free"
    run(app.on_callback(cb("mode:set")))
    assert app.db.get_state(42)["mode"] == "set" and app.gemini.calls[-1][1] == "Zaczynajmy!"


def test_stale_preview_and_foreign_callback():
    app = make_app()
    run(app.on_callback(cb("p:ok")))
    assert "неактуален" in app.tg.edits[-1][0]
    run(app.on_callback({**cb("s:topic"), "from": {"id": 7}}))
    assert app.db.get_state(7)["pending"] is None


# ---------- правила, словарь, итоги, наборы из ошибок ----------

from bot import rules as rules_mod  # noqa: E402
from bot.db import DB as _DB  # noqa: E402


def test_rules_normalize_and_group():
    assert rules_mod.normalize("родительный падеж после отрицания") == "Родительный падеж после отрицания"
    assert rules_mod.normalize("Лексика: неверное слово") == "Лексика: неверное слово или калька с русского"
    assert rules_mod.normalize("что-то странное") == "Другое" and rules_mod.normalize(None) == "Другое"
    assert rules_mod.normalize("падеж") == "Другое"            # слишком коротко для угадывания
    g = rules_mod.group([
        {"rule": "A", "original": "x", "correct": "y"}, {"rule": "A", "original": "X", "correct": "Y"},
        {"rule": "A", "original": "z", "correct": "w"}, {"rule": "B", "original": "q", "correct": "r"}])
    assert g[0][0] == "A" and g[0][1] == 3 and len(g[0][2]) == 2   # дубли примеров схлопнуты
    assert g[1][0] == "B"


def test_corrections_get_catalog_rule_and_buttons():
    t = {**TURN_JSON, "corrections": [{**TURN_JSON["corrections"][0], "rule": "местный падеж после w / na / o / przy / po"}]}
    app = make_app(gem=FakeGemini(turn=t))
    run(app.handle(msg(text="x")))
    c = app.db.corrections_session(app.db.current_session(42))[0]
    assert c["rule"] == "Местный падеж после w / na / o / przy / po"
    btns = [d for row in app.tg.buttons[0] for _, d in row]
    assert btns[0].startswith("r:") and btns[1].startswith("d:")


def test_no_rule_button_without_errors_and_opening_without_buttons():
    t = {**TURN_JSON, "corrections": []}
    app = make_app(gem=FakeGemini(turn=t))
    run(app.handle(msg(text="x")))
    assert [d for row in app.tg.buttons[0] for _, d in row] == [app.tg.buttons[0][0][0][1]]
    assert app.tg.buttons[0][0][0][1].startswith("d:")
    start_set(app)
    assert app.tg.buttons[-1] is None                          # служебное открытие набора — без кнопок


def test_rule_button_explains():
    app = make_app()
    run(app.handle(msg(text="x")))
    rid = app.tg.buttons[0][0][0][1]
    run(app.on_callback(cb(rid)))
    assert "📖 <b>Родительный после отрицания</b>" in app.tg.sent[-1] and "ЦУ-кру" in app.tg.sent[-1]
    assert "w sklep" in app.gemini.prompts[-1]


def test_rule_question_command_and_pending():
    app = make_app()
    run(app.handle(msg(text="/rule почему do niej?")))
    assert "почему do niej?" in app.gemini.prompts[-1] and "📖" in app.tg.sent[-1]
    run(app.handle(msg(text="/rule")))
    run(app.handle(msg(text="а почему cukru?")))
    assert "а почему cukru?" in app.gemini.prompts[-1] and not app.gemini.calls


def test_star_offer_toggle_and_dict():
    app = make_app()
    run(app.handle(msg(text="x")))
    did = app.tg.buttons[0][0][1][1]
    mid = int(did[2:])
    run(app.on_callback(cb(did)))
    assert "najbardziej lubię" in app.tg.sent[-1]
    run(app.on_callback(cb(f"ds:{mid}:0")))
    run(app.on_callback(cb(f"ds:{mid}:1")))
    assert [i["pl"] for i in app.db.dict_items(42)] == ["najbardziej lubię", "tak jak mówię"]
    run(app.on_callback(cb(f"ds:{mid}:1")))                    # повторное нажатие убирает
    assert [i["pl"] for i in app.db.dict_items(42)] == ["najbardziej lubię"]
    n = len(app.gemini.prompts)
    run(app.on_callback(cb(did)))                              # повторное открытие — без нового запроса
    assert len(app.gemini.prompts) == n and "✅" in app.tg.sent[-1]
    run(app.on_callback(cb(f"dx:{mid}")))
    assert "Сохранено в словарь: 1" in app.tg.edits[-1][0]
    run(app.handle(msg(text="/dict")))
    assert "najbardziej lubię" in app.tg.sent[-1]


def test_dict_add_command_dedup():
    app = make_app()
    run(app.handle(msg(text="/dict add najbardziej lubię")))
    assert "Добавлено" in app.tg.sent[-1]
    run(app.handle(msg(text="/dict add najbardziej lubię")))
    assert "уже в словаре" in app.tg.sent[-1]


def test_set_from_dict_marks_used():
    app = make_app()
    app.db.dict_add(42, "tak jak mówię", "так як МУ-ве", "как я говорю")
    app.db.dict_add(42, "po południu", "по по-ЎУ-дню", "после обеда")
    run(app.on_callback(cb("s:dict")))
    assert "tak jak mówię" in app.tg.sent[-1]
    run(app.on_callback(cb("p:1")))                            # убрать po południu
    run(app.on_callback(cb("p:ok")))
    words = app.db.set_words(app.db.active_set(42)["id"])
    assert [w["pl"] for w in words] == ["tak jak mówię"] and words[0]["kind"] == "phrase"
    assert [i["pl"] for i in app.db.dict_items(42, only_unused=True)] == ["po południu"]


def test_itog_periods_and_files():
    clock = Clock()
    app = make_app(clock=clock)
    run(app.handle(msg(text="x")))
    clock.t += 2 * 3600
    run(app.on_callback(cb("i:h")))
    assert "разговоров не было" in app.tg.sent[-1]
    run(app.on_callback(cb("i:d")))
    assert "Ошибки — 1" in app.tg.sent[-1] and "×1" in app.tg.sent[-1]
    btns = [d for row in app.tg.buttons[-1] for _, d in row]
    assert btns == ["i:fd", "e:p:d"]
    run(app.on_callback(cb("i:fd")))
    assert "за сутки" in app.tg.docs[-1][2] and "w sklepie" in app.tg.docs[-1][1]
    app.db.dict_add(42, "po południu", "", "после обеда")
    run(app.on_callback(cb("i:dict")))
    assert app.tg.docs[-1][0] == "bot-pl_dict.txt" and "po południu" in app.tg.docs[-1][1]
    run(app.on_callback(cb("i:fa")))
    name, content, _ = app.tg.docs[-1]
    assert name.endswith(".txt") and "ОШИБКИ — 1" in content and "w sklepie" in content and "<b>" not in content


def test_autosave_writes_file_and_sends(tmp_path=None):
    import tempfile
    from pathlib import Path
    app = make_app()
    app.export_dir = Path(tempfile.mkdtemp()) / "exports"
    run(app.handle(msg(text="x")))
    run(app.autosave(42))
    assert len(list(app.export_dir.glob("42_*.txt"))) == 1 and "автосохранение" in app.tg.docs[-1][2]


def test_classify_old_errors():
    app = make_app()
    s = app.db.current_session(42)
    app.db.conn.execute("INSERT INTO corrections(session_id, user_id, data, created_at) VALUES (?,?,?,?)",
                        (s, 42, json.dumps({"original": "cukier", "correct": "cukru"}), 1.0))
    app.db.conn.commit()
    assert len(app.db.corrections_unsorted()) == 1
    run(app.classify_old_errors())
    assert app.db.corrections_unsorted() == []
    assert app.db.corrections_session(s)[0]["rule"] == "Родительный падеж после отрицания"


def test_error_set_flow_with_rule_first():
    t = {**TURN_JSON, "corrections": [
        {"kind": "grammar", "original": "cukier", "correct": "cukru", "rule": "Родительный падеж после отрицания"},
        {"kind": "grammar", "original": "filmy", "correct": "filmu", "rule": "Родительный падеж после отрицания"},
        {"kind": "grammar", "original": "w sklep", "correct": "w sklepie", "rule": "Местный падеж после w / na / o / przy / po"}]}
    gem = FakeGemini(turn=t)
    app = make_app(gem=gem)
    run(app.handle(msg(text="x")))
    run(app.on_callback(cb("e:start")))
    p = app.db.get_state(42)["pending"]
    assert p["step"] == "errsel" and p["rules"][0]["rule"] == "Родительный падеж после отрицания" and p["rules"][0]["n"] == 2
    assert "cukier → cukru" in p["rules"][0]["examples"]
    run(app.on_callback(cb("e:t:1")))                          # снять второе правило
    assert app.db.get_state(42)["pending"]["on"] == [0]
    run(app.on_callback(cb("e:go")))
    items = app.db.set_words(app.db.active_set(42)["id"])
    assert len(items) == 1 and items[0]["kind"] == "rule" and "cukru" in items[0]["ru"]
    assert any("📖 <b>Родительный после отрицания</b>" in m for m in app.tg.sent)   # правило перед стартом
    assert "Правила, на которых ученик ошибался" in gem.extras[-1]
    assert gem.calls[-1][1] == "Zaczynajmy!"


def test_error_set_rule_toggle_off_and_random_and_period():
    app = make_app()
    run(app.handle(msg(text="x")))
    run(app.on_callback(cb("e:start")))
    run(app.on_callback(cb("e:rule")))
    assert app.db.get_state(42)["pending"]["rule_first"] is False
    run(app.on_callback(cb("e:rand")))
    assert len(app.db.get_state(42)["pending"]["on"]) == 1
    run(app.on_callback(cb("e:p:a")))
    assert app.db.get_state(42)["pending"]["period"] == "a" and app.db.get_state(42)["pending"]["rule_first"] is False
    n = len(app.gemini.prompts)
    run(app.on_callback(cb("e:go")))
    assert len(app.gemini.prompts) == n                        # без правила перед стартом — без запроса


def test_rule_items_need_double_criteria():
    t = {**TURN_JSON, "corrections": [
        {"kind": "grammar", "original": "cukier", "correct": "cukru", "rule": "Родительный падеж после отрицания"}],
         "target_uses": [{"lemma": "Родительный падеж после отрицания", "form": "nie lubię cukru", "correct": True}]}
    clock = Clock()
    app = make_app(gem=FakeGemini(turn=t), clock=clock)
    app.criteria = training.Criteria(streak=2, forms=1, days=1)
    app.rule_criteria = training.scaled(app.criteria, 2)
    run(app.handle(msg(text="x")))
    run(app.on_callback(cb("e:start")))
    run(app.on_callback(cb("e:rule")))
    run(app.on_callback(cb("e:go")))                           # открытие: 1-е употребление
    rule = app.db.set_words(app.db.active_set(42)["id"])[0]
    run(app.handle(msg(text="y")))                             # 2 подряд — для слова хватило бы
    assert app.db.word(rule["id"])["mastered_at"] is None
    app.gemini.turn = {**t, "target_uses": [{"lemma": "Родительный падеж после отрицания",
                                             "form": "nie mam czasu", "correct": True}]}
    clock.t += 86400
    run(app.handle(msg(text="z")))
    run(app.handle(msg(text="w")))
    assert app.db.word(rule["id"])["mastered_by"] == "auto"


def test_db_migration_adds_columns():
    import sqlite3
    import tempfile
    path = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE corrections (id INTEGER PRIMARY KEY, session_id INTEGER, user_id INTEGER,
                         data TEXT, created_at REAL);
                         CREATE TABLE set_words (id INTEGER PRIMARY KEY, set_id INTEGER, user_id INTEGER, pl TEXT,
                         translit TEXT, ru TEXT, pos TEXT, mastered_at REAL, mastered_by TEXT,
                         review_stage INTEGER DEFAULT 0, last_review_at REAL, created_at REAL);""")
    con.commit()
    con.close()
    d = _DB(path)
    assert {"rule", "msg_id"} <= d._columns("corrections") and "kind" in d._columns("set_words")
