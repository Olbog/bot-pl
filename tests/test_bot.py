import asyncio
import json

import httpx

from bot.core.config import Config
from bot.core.db import DB
from bot.ui.fmt import summary_message, turn_message
from bot.ai.gemini import (DEFAULT_MODELS, Gemini, GeminiError, GeminiExhausted, GeminiOverloaded, Turn,
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
    from bot.ai.prompt import FIELDS, RESPONSE_SCHEMA, json_format_hint, system_prompt
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
        self.toasts, self.deleted, self.markups = [], [], []

    async def send_message(self, chat_id, text, buttons=None):
        self.sent.append(text)
        self.buttons.append(buttons)
        return {"message_id": len(self.sent)}

    async def edit_message(self, chat_id, message_id, text, buttons=None):
        self.edits.append((text, buttons))

    async def send_document(self, chat_id, filename, content, caption=""):
        self.docs.append((filename, content.decode("utf-8"), caption))

    async def answer_callback(self, cid, text=None):
        self.toasts.append(text)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def edit_markup(self, chat_id, message_id, buttons=None):
        self.markups.append((message_id, buttons))

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
        self.extras, self.prompts, self.audios = [], [], []
        self.ex_batch = 0
        self.check_verdict = {}
        self.note_verdict = {}
        self.voice_notes = {}

    def ex_data(self, prompt):
        """10 пунктов; каждый новый вызов — новые предложения (кроме dup — повтор первого)."""
        self.ex_batch += 1
        test = "Формат ТЕСТ" in prompt
        items = []
        for i in range(10):
            ans = f"forma{i}"
            items.append({"q": f"Zdanie {self.ex_batch}-{i} ___ .", "hint": "baza",
                          "options": [ans, f"zle{i}", f"inne{i}"] if test else [], "answer": ans, "accepted": [],
                          "full_pl": f"Zdanie {self.ex_batch}-{i} {ans}.", "translit": "ЗДА-не",
                          "ru": "Предложение ⟪слово⟫" if getattr(self, "spoiler", False) else "Предложение",
                          "grammar": "глагол, 1 л. ед. ч.", "rule": "Спряжение -am / -asz", "lemma": "ciasto" if i == 0 else f"w{i}"})
        if getattr(self, "dup", False):
            items[1] = dict(items[0])
        return {"title": "Тест-упражнение", "items": items}

    def check_data(self, prompt):
        import re as _re
        lines = {int(m.group(1)): m.group(0) for m in _re.finditer(r"^(\d+)\. Задание.*$", prompt, _re.M)}
        out = []
        for n, line in lines.items():
            note = _re.search(r"уточнение ученика: «(.*?)»", line)
            if n in self.voice_notes:
                note = _re.search("(.*)", self.voice_notes[n])
            out.append({"n": n, "heard": "", "correct": self.check_verdict.get(n, False),
                        "explanation": f"объяснение {n}", "bridge": "как в русском",
                        "note": note.group(1) if note else "", "note_ok": self.note_verdict.get(n, True),
                        "note_comment": f"комментарий {n}" if note else ""})
        return {"items": out}

    async def ask_json(self, prompt, schema, hint, audio=None):
        self.prompts.append(prompt)
        self.audios.append(audio)
        props = schema.get("properties", {})
        item_props = props.get("items", {}).get("items", {}).get("properties", {})
        if "q" in item_props:
            self.ex_audio = None
            return self.ex_data(prompt)
        if "heard" in item_props:
            return self.check_data(prompt)
        if "wrong" in item_props:                                # учебник: 2 близких неверных варианта
            import re as _re
            nums = [int(x) for x in _re.findall(r"^(\d+)\. ", prompt, _re.M)]
            return {"items": [{"n": n, "wrong": [f"blisko{n}a", f"blisko{n}b"]} for n in nums]}
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
    assert "Новый разговор" in app.tg.sent[-1]
    run(app.on_callback(cb("i:s")))
    assert "chleb" in app.tg.sent[-1]                          # меню /new ещё ничего не сбросило
    run(app.handle(msg(text="/new")))
    run(app.on_callback(cb("nw:f:none")))
    run(app.on_callback(cb("i:s")))
    assert "разговоров не было" in app.tg.sent[-1]
    run(app.handle(msg(text="/start")))
    assert "Главное меню" in app.tg.sent[-1] and "go:new" in str(app.tg.buttons[-1])


# ---------- тренировка наборов ----------

from bot.core import training  # noqa: E402


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
    assert app.db.get_state(42)["pending"]["step"] == "topic"
    run(app.handle(msg(text="кафе")))
    assert "кафе" in gem.prompts[0] and not gem.calls        # тема ушла в составление, не в разговор
    assert "ciasto" in app.tg.sent[-1] and any(d == "p:0" for row in app.tg.buttons[-1] for _, d in row)
    run(app.on_callback(cb("p:2")))                            # убрать kawa
    assert "<s>" in app.tg.edits[-1][0]
    run(app.on_callback(cb("p:ok")))
    active = app.db.active_set(42)
    assert active["title"] == "Кафе"
    assert [w["pl"] for w in app.db.set_words(active["id"])] == ["ciasto", "piec"]
    assert app.db.get_state(42) == {"mode": "set", "pending": None, "topic": None}
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
    assert new_words == ["herbata"]                                  # хвост старого набора не тянется
    assert [w["pl"] for w in app.db.leftovers(42)] == [old_left["pl"]]   # а ждёт в «🧳 Недоученные»


def test_free_mode_review_block_and_advance():
    t = {**TURN_JSON, "target_uses": [{"lemma": "ciasto", "form": "ciasta", "correct": True}]}
    gem = FakeGemini(turn=t)
    clock = Clock()
    app = make_app(gem=gem, clock=clock)
    start_set(app)
    w = [x for x in app.db.set_words(app.db.active_set(42)["id"]) if x["pl"] == "ciasto"][0]
    app.db.set_mastered(w["id"], "user")
    app.db.conn.execute("UPDATE set_words SET mastered_at=? WHERE id=?", (clock.t, w["id"]))
    run(app.handle(msg(text="/free")))                             # старая команда → меню /new
    run(app.on_callback(cb("nw:f:none")))
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
    assert "Набора пока нет" in app.tg.sent[-1]
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

from bot.core import rules as rules_mod  # noqa: E402
from bot.core.db import DB as _DB  # noqa: E402


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


def test_autosave_weekly_schedule_and_export_command():
    from datetime import datetime
    from bot.main import App as _App
    assert _App.autosave_due(datetime(2026, 10, 5, 4, 30))        # понедельник 04:30
    assert not _App.autosave_due(datetime(2026, 10, 6, 4, 30))    # вторник
    assert not _App.autosave_due(datetime(2026, 10, 5, 5, 0))     # понедельник, но 05:00
    app = make_app()
    run(app.handle(msg(text="x")))
    run(app.handle(msg(text="/export")))
    name, content, caption = app.tg.docs[-1]
    assert "всё время" in caption and "ОШИБКИ — 1" in content and "СЛОВАРЬ" in content


# ---------- упражнения ----------

from bot.exercises import logic as exm  # noqa: E402


def test_parse_answers_and_markers():
    a = exm.parse_answers("1 piję 2 lubi? 3 kupuje НУ 4. nie mam czasu", 10)
    assert a[1] == {"answer": "piję", "unsure": False, "sure": False, "note": ""}
    assert a[2]["answer"] == "lubi?" and not a[2]["unsure"]       # НУ и ? пока не маркеры
    assert a[4]["answer"] == "nie mam czasu"
    c = exm.parse_answers("piję\nlubi\nkupuje", 3)                 # без номеров, по строкам
    assert c[2]["answer"] == "lubi"
    assert exm.parse_answers("piję, lubi", 3) == {}                  # не совпало число — не угадываем
    d = exm.parse_answers("1 piję! 2 lubi (почему не lubią? 3 л.) 3b!", 3)
    assert d[1] == {"answer": "piję", "unsure": False, "sure": True, "note": ""}
    assert d[2] == {"answer": "lubi", "unsure": False, "sure": False, "note": "почему не lubią? 3 л."}
    assert d[3]["answer"] == "b" and d[3]["sure"]
    e = exm.parse_answers("piję (ja), lubi", 2)                     # без номеров — запятые в скобках не мешают
    assert e[1]["note"] == "ja" and e[2]["answer"] == "lubi"
    f = exm.parse_answers("1 a 2 b (потому что", 2)                  # незакрытая скобка — до конца
    assert f[2] == {"answer": "b", "unsure": False, "sure": False, "note": "потому что"}
    assert exm.unclosed_note("1 a (x") and not exm.unclosed_note("1 a (x)")


def test_quick_check_statuses():
    items = [{"answer": "piję", "accepted": []}, {"answer": "lubi", "accepted": ["kocha"]},
             {"answer": "czasu", "accepted": []}, {"answer": "x", "accepted": []}]
    res = exm.quick_check(items, exm.parse_answers("1 pije 2 Kocha 3 czasem", 4), "gap")
    assert [r["status"] for r in res] == ["diacritics", "ok", "check", "missing"]
    t = [{"answer": "czasu", "options": ["czas", "czasu", "czasem"]}]
    assert exm.quick_check(t, exm.parse_answers("1b", 1), "test")[0]["status"] == "ok"
    assert exm.quick_check(t, exm.parse_answers("1a", 1), "test")[0]["status"] == "wrong"
    assert [r["n"] for r in exm.needs_model(res, explain_all=False)] == [1, 3, 4]
    assert [r["n"] for r in exm.needs_model(res)] == [1, 2, 3, 4]          # объяснять и верные
    sure = exm.quick_check(items, exm.parse_answers("1 piję! 2 lubi! 3 czasu! 4 x", 4), "gap")
    assert [r["n"] for r in exm.needs_model(sure)] == [4]                   # «!» — без объяснения


def ex_flow(app, kind_cb, *pre):
    run(app.handle(msg(text="/ex")))
    run(app.on_callback(cb(kind_cb)))
    for c in pre:
        run(app.on_callback(cb(c)))


def answers_all(app=None, wrong=(), unsure=(), test=False, sure=(), notes=None):
    """Ответы по реальному (перемешанному) порядку пунктов текущего упражнения."""
    ex = app.db.ex_get(app.db.get_state(42)["pending"]["ex"]["ex_id"])
    parts = []
    for i, it in enumerate(ex["items"], 1):
        if test:
            right = "abcd"[it["options"].index(it["answer"])]
            a = ("c" if right != "c" else "b") if i in wrong else right
        else:
            a = f"zle{i}" if i in wrong else it["answer"]
        parts.append(f"{i} {a}" + (" НУ" if i in unsure else "") + ("!" if sure == "all" or i in sure else "")
                     + (f" ({(notes or {})[i]})" if i in (notes or {}) else ""))
    return " ".join(parts)


def test_grammar_test_flow_code_check_only():
    gem = FakeGemini()
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:test", "x:t:mix")
    run(app.on_callback(cb("x:n:2")))
    assert "Упражнение 1/2" in app.tg.sent[-1] and "a) " in app.tg.sent[-1]
    assert "Формат ТЕСТ" in gem.prompts[-1]
    n_prompts = len(gem.prompts)
    run(app.handle(msg(text=answers_all(app, test=True, sure="all"))))
    assert len(gem.prompts) == n_prompts                       # всё верно и везде «!» — без запроса
    assert "10 из 10" in app.tg.sent[-1] and "глагол, 1 л. ед. ч." in app.tg.sent[-1]
    assert "объяснение" not in app.tg.sent[-1]
    run(app.on_callback(cb("x:next")))
    assert "Упражнение 2/2" in app.tg.sent[-1]
    assert "Zdanie 1-0" in gem.prompts[-1]                     # прошлые предложения переданы как УЖЕ БЫЛО


def test_gap_flow_with_errors_notes_and_model():
    gem = FakeGemini()
    gem.check_verdict = {3: True}                              # 3: «другой верный вариант»
    gem.note_verdict = {5: False}                              # 5: ответ верный, рассуждение — нет
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix")
    run(app.handle(msg(text="1")))                            # число текстом
    text = answers_all(app, wrong=(2, 3), notes={5: "3 л. ед. ч., -am", 6: "почему не -esz?"})
    run(app.handle(msg(text=text)))
    res = app.tg.sent[-1]
    ex = app.db.ex_get(app.db.get_state(42)["pending"]["ex"]["ex_id"])
    assert "❌ 2." in res and f"→ <b>{ex['items'][1]['answer']}</b>" in res and "объяснение 2" in res
    assert "🌉 как в русском" in res
    assert "✅ 3." in res                                      # модель признала верным
    assert "❌ 5." in res and "ошибка в рассуждении" in res and "💭 «3 л. ед. ч., -am» — ❌ не так: комментарий 5" in res
    assert "✅ 6." in res and "💭 «почему не -esz?» — ✅ верно: комментарий 6" in res
    assert "уточнение ученика: «3 л. ед. ч., -am»" in gem.prompts[-1]   # «3» в скобках — не номер пункта
    assert "объяснение 1" in res                               # верный ответ без «!» — тоже объяснён
    assert "ТОЛЬКО в настоящем времени" in gem.prompts[0]
    assert "8 из 10" in res
    errs = app.db.corrections_since(42, 0)
    assert len(errs) == 2 and errs[0]["rule"] == "Спряжение -am / -asz" and errs[0]["original"] == "zle2"
    assert errs[1]["original"].endswith("(3 л. ед. ч., -am)") and "ошибка в рассуждении" in errs[1]["why"]
    assert app.db.history(app.db.current_session(42), 20) == []   # история разговора не засорена
    run(app.handle(msg(text="2: почему так?")))
    assert "почему так?" in gem.prompts[-1] and "📖" in app.tg.sent[-1]
    assert not gem.calls                                       # вопрос не ушёл в разговор


def test_uniqueness_and_reuse_of_wrong_items():
    gem = FakeGemini()
    gem.dup = True                                             # модель повторила предложение внутри пачки
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix")
    run(app.on_callback(cb("x:n:2")))
    ex1 = app.db.ex_get(app.db.get_state(42)["pending"]["ex"]["ex_id"])
    assert len(ex1["items"]) == 9                              # дубль выкинут
    first_wrong = ex1["items"][0]["answer"]
    ans = " ".join(f"{i} {'BAD' if i == 1 else it['answer']}" for i, it in enumerate(ex1["items"], 1))
    run(app.handle(msg(text=ans)))
    gem.ex_batch = 0                                           # модель «повторила» прошлые предложения
    gem.dup = False
    run(app.on_callback(cb("x:next")))
    ex2 = app.db.ex_get(app.db.get_state(42)["pending"]["ex"]["ex_id"])
    reused = [it for it in ex2["items"] if it.get("_reuse_id")]
    assert len(reused) == 1 and reused[0]["answer"] == first_wrong   # ошибочный пункт вернулся на повтор
    keys = [it["_key"] for it in ex2["items"]]
    assert len(set(keys)) == len(keys)
    assert "🔁" in app.tg.sent[-1]


def test_words_from_set_and_progress_weight():
    gem = FakeGemini()
    app = make_app(gem=gem)
    start_set(app)                                             # ciasto, piec, kawa
    ex_flow(app, "x:k:words", "x:s:set")
    assert "ciasto" in app.tg.sent[-1]
    run(app.on_callback(cb("x:n:1")))
    assert "ciasto" in gem.prompts[-1]
    run(app.handle(msg(text=answers_all(app))))
    w = [x for x in app.db.set_words(app.db.active_set(42)["id"]) if x["pl"] == "ciasto"][0]
    st = training.stats(app.db.word_uses(w["id"]))
    assert st.streak == 0.1 and st.streak_forms == []           # 0.1 и без форм/дней
    assert "x:next" not in str(app.tg.buttons[-1]) and "x:menu" in str(app.tg.buttons[-1])


def test_voice_exercise_sends_audio_to_check():
    gem = FakeGemini()
    gem.check_verdict = {i: True for i in range(1, 11)}
    app = make_app(gem=gem)
    ex_flow(app, "x:k:voice", "x:s:own")
    run(app.handle(msg(text="ciasto, piec")))
    run(app.on_callback(cb("x:n:1")))
    assert "ПРОИЗНОСИТЬ" in gem.prompts[-1] and "🎙" in app.tg.sent[-1]
    run(app.handle(msg(voice={"file_id": "v"})))
    assert gem.audios[-1] == b"OGG-IN" and "голосовое" in gem.prompts[-1]
    assert "10 из 10" in app.tg.sent[-1]


def test_grammar_topics_from_catalog_contrast():
    gem = FakeGemini()
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:gap")
    assert "Что тренируем?" in app.tg.sent[-1]
    run(app.on_callback(cb("x:t:cat")))
    run(app.on_callback(cb("x:r:go")))
    assert "хотя бы одно" in app.tg.sent[-1]
    run(app.on_callback(cb("x:r:2")))
    run(app.on_callback(cb("x:r:4")))
    run(app.on_callback(cb("x:r:go")))
    assert "на различение" in app.tg.sent[-1]
    run(app.on_callback(cb("x:c:go")))
    run(app.on_callback(cb("x:n:1")))
    p = gem.prompts[-1]
    assert "РАЗЛИЧЕНИЕ" in p and rules_mod.CATALOG[2] in p and rules_mod.CATALOG[4] in p
    ex = app.db.ex_get(app.db.get_state(42)["pending"]["ex"]["ex_id"])
    assert ex["topics"] == [rules_mod.CATALOG[2], rules_mod.CATALOG[4]]
    run(app.handle(msg(text=answers_all(app))))
    assert "а НЕ другая" in gem.prompts[-1]                     # проверка: почему эта тема, а не другая
    # недавние темы — это сочетание и предлагается
    ex_flow(app, "x:k:grammar", "x:f:test", "x:t:recent")
    assert "Винительный падеж / Творительный падеж" in str(app.tg.buttons[-1])
    run(app.on_callback(cb("x:p:0")))
    run(app.on_callback(cb("x:p:go")))
    assert app.db.get_state(42)["pending"]["ex"]["rules"] == [rules_mod.CATALOG[2], rules_mod.CATALOG[4]]


def test_grammar_own_topic_and_rule_card():
    gem = FakeGemini()
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:own")
    run(app.handle(msg(text="творительный падеж (z kim, być kim)")))
    assert app.db.get_state(42)["pending"]["ex"]["rules"] == [rules_mod.CATALOG[4]]   # узнал правило каталога
    run(app.on_callback(cb("x:c:rule")))
    assert "📖" in app.tg.sent[-2] and "Сколько упражнений" in app.tg.sent[-1]
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:own")
    run(app.handle(msg(text="разница ile и wiele")))
    assert app.db.get_state(42)["pending"]["ex"]["rules"] == ["разница ile и wiele"]
    run(app.on_callback(cb("x:c:go")))
    run(app.on_callback(cb("x:n:1")))
    assert "СТРОГО на одну тему: «разница ile и wiele»" in gem.prompts[-1]


def test_grammar_topics_from_errors_and_errors_kind():
    t = {**TURN_JSON, "corrections": [{**TURN_JSON["corrections"][0], "rule": "Местный падеж после w / na / o / przy / po"}]}
    app = make_app(gem=FakeGemini(turn=t))
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:err")
    assert "пока нет" in app.tg.sent[-1]
    assert app.db.get_state(42)["pending"]["step"] == "ex_gtopic"   # можно нажать другой вариант
    run(app.handle(msg(text="x")))                             # появилась ошибка с правилом
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:err")
    assert "Местный падеж после w / na / o / przy / po — 1" in str(app.tg.buttons[-1]) and "✅" in str(app.tg.buttons[-1])
    run(app.on_callback(cb("x:p:per")))                        # период → всё время
    assert "всё время" in app.tg.edits[-1][0]
    run(app.on_callback(cb("x:p:go")))
    assert app.db.get_state(42)["pending"]["ex"]["rules"] == ["Местный падеж после w / na / o / przy / po"]
    ex_flow(app, "x:k:errors")
    assert "Местный падеж" in app.tg.sent[-1]
    assert "x:k:rule" not in str(app.tg.buttons)                # пункт «Правило / микс» убран


def test_ex_answer_step_survives_gemini_error_and_chat_after_review():
    gem = FakeGemini()
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix", "x:n:1")
    gem_ask = gem.ask_json

    async def boom(*a, **k):
        raise GeminiOverloaded("x")
    gem.ask_json = boom
    run(app.handle(msg(text=answers_all(app, wrong=(1,)))))
    assert "перегружен" in app.tg.sent[-1] and app.db.get_state(42)["pending"]["step"] == "ex_answer"
    gem.ask_json = gem_ask
    run(app.handle(msg(text=answers_all(app, wrong=(1,)))))
    assert app.db.get_state(42)["pending"]["step"] == "ex_review"
    run(app.handle(msg(text="Cześć, jak się masz?")))          # обычная фраза — в разговор
    assert gem.calls and app.db.get_state(42)["pending"]["step"] == "ex_review"


# ---------- защита от двойных нажатий и ответов «не туда» ----------

def test_wait_message_shown_and_deleted():
    app = make_app()
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix")
    run(app.on_callback(cb("x:n:1")))
    waits = [m for m in app.tg.sent if m.startswith("⏳ Составляю упражнение 1/1")]
    assert len(waits) == 1
    assert app.tg.sent.index(waits[0]) + 1 in app.tg.deleted   # статус удалён после готовности
    assert "#" in app.tg.sent[-1]


def test_repeat_count_tap_does_not_create_second_exercise():
    gem = FakeGemini()
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix")
    run(app.on_callback(cb("x:n:1")))
    n = gem.ex_batch
    run(app.on_callback(cb("x:n:1")))                          # повторное нажатие — шаг уже другой
    run(app.on_callback(cb("x:f:gap")))                        # кнопка с прошлого шага
    assert gem.ex_batch == n
    assert app.db.get_state(42)["pending"]["step"] == "ex_answer"


def test_busy_user_gets_notice_instead_of_queue():
    app = make_app()
    app.busy.add(42)
    run(app.dispatch({"update_id": 1, "message": msg(text="1")}))
    assert "Ещё обрабатываю" in app.tg.sent[-1] and not app.gemini.calls
    run(app.dispatch({"update_id": 2, "callback_query": cb("x:n:1")}))
    assert "Ещё обрабатываю" in (app.tg.toasts[-1] or "")
    app.busy.clear()
    run(app.dispatch({"update_id": 3, "message": msg(text="hej")}))
    assert app.gemini.calls and 42 not in app.busy


def test_answer_sent_before_exercise_is_rejected():
    clock = Clock()
    app = make_app(clock=clock)
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix", "x:n:1")
    ex_id = app.db.get_state(42)["pending"]["ex"]["ex_id"]
    run(app.handle(msg(text="1 a 2 b", date=int(clock.t) - 30)))
    assert f"раньше, чем пришло упражнение #{ex_id}" in app.tg.sent[-1]
    assert app.db.get_state(42)["pending"]["step"] == "ex_answer"


def test_reply_binds_answer_to_specific_exercise():
    gem = FakeGemini()
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix", "x:n:2")
    first = app.db.get_state(42)["pending"]["ex"]["ex_id"]
    first_tg = app.db.ex_get(first)["tg_msg_id"]
    run(app.handle(msg(text=answers_all(app))))
    run(app.on_callback(cb("x:next")))
    second = app.db.get_state(42)["pending"]["ex"]["ex_id"]
    assert second != first
    run(app.handle(msg(text="1 x", reply_to_message={"message_id": first_tg})))
    assert f"#{first} уже проверено" in app.tg.sent[-1]
    second_tg = app.db.ex_get(second)["tg_msg_id"]
    run(app.handle(msg(text=answers_all(app), reply_to_message={"message_id": second_tg})))
    assert app.db.ex_get(second)["results"] and f"#{second}" in app.tg.sent[-1]


def test_exercise_shows_translation_not_polish_hint():
    app = make_app()
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix", "x:n:1")
    msg_text = app.tg.sent[-1]
    assert "(baza)" not in msg_text                           # подсказка-ключ не показывается
    assert "<i>— Предложение</i>" in msg_text                 # перевод под каждым пунктом


# ---------- глагол: инфинитив и спряжение ----------

from bot.core import verbs as verbs_mod  # noqa: E402


def test_verb_line_and_conj_type():
    assert verbs_mod.conj_type("płacić", "-ę / -isz") == "-ę / -isz / -ysz"
    assert verbs_mod.conj_type("mieć", "-am / -asz") == "неправильный"        # известный — решает код
    assert verbs_mod.conj_type("pracować", "-ę / -esz") == "-uję / -ujesz"    # -ować → -uję
    assert verbs_mod.conj_type("umieć", "-em/-esz") == "-em / -esz"
    assert verbs_mod.verb_line({"verb_inf": ""}) == ""
    line = verbs_mod.verb_line({"verb_inf": "płacić", "verb_translit": "ПЛА-чичь", "verb_ru": "платить",
                                "verb_conj": "-ę / -isz / -ysz", "verb_forms": "płacę, płacisz, płacą"})
    assert line == ("🔤 <b>płacić</b> [ПЛА-чичь] — платить · спряжение -ę / -isz / -ysz: "
                    "płacę, płacisz, płacą")


def test_verb_shown_in_turn_and_exercise_results():
    verb = {"verb_inf": "piec", "verb_translit": "ПЕЦ", "verb_ru": "печь", "verb_conj": "-ę / -esz",
            "verb_forms": "piekę, pieczesz, pieką"}
    t = {**TURN_JSON, "corrections": [{**TURN_JSON["corrections"][0], **verb}]}
    app = make_app(gem=FakeGemini(turn=t))
    run(app.handle(msg(text="x")))
    assert "🔤 <b>piec</b> [ПЕЦ] — печь · спряжение -ę / -esz: piekę, pieczesz, pieką" in "\n".join(app.tg.sent)
    from bot.ai import prompt as pr
    assert "verb_conj" in pr.system_prompt("A1") and "verb_conj" in pr.ex_prompt("grammar", "gap", "A1", "", [])
    ex = {"id": 1, "title": "t", "items": [{"answer": "piekę", "grammar": "глагол", **verb}, {"answer": "kot"}]}
    from bot.ui import fmt
    res = fmt.ex_results(ex, [{"n": 1, "user": "piekę", "final": "ok"}, {"n": 2, "user": "kot", "final": "ok"}])
    assert res.count("🔤") == 1 and "спряжение -ę / -esz" in res      # и у пункта с «!» без разбора


# ---------- ⬅️ Назад / ✖️ Отмена ----------

def step_of(app):
    p = app.db.get_state(42)["pending"]
    return p and p["step"]


def test_nav_back_keeps_choices_and_first_step_has_no_back():
    app = make_app()
    run(app.handle(msg(text="/ex")))
    assert str(app.tg.buttons[-1][-1]) == str([("✖️ Отмена", "nav:c:ex_menu")])     # первый шаг — без «Назад»
    for c in ("x:k:grammar", "x:f:test", "x:t:cat", "x:r:2", "x:r:4", "x:r:go"):
        run(app.on_callback(cb(c)))
    assert step_of(app) == "ex_card"
    run(app.on_callback(cb("nav:b:ex_card")))                    # назад к списку правил — галочки на месте
    text, buttons = app.tg.edits[-1]
    assert step_of(app) == "ex_rules" and "✅" in str(buttons) and rules_mod.CATALOG[2] in text
    assert app.db.get_state(42)["pending"]["ex"]["chosen"] == [2, 4]
    run(app.on_callback(cb("nav:b:ex_rules")))
    assert step_of(app) == "ex_gtopic" and "Что тренируем?" in app.tg.edits[-1][0]
    run(app.on_callback(cb("nav:b:ex_gtopic")))
    assert step_of(app) == "ex_fmt" and "формат" in app.tg.edits[-1][0]
    assert app.db.get_state(42)["pending"]["ex"] == {"kind": "grammar"}   # формат ещё не выбран
    run(app.on_callback(cb("nav:b:ex_fmt")))
    assert step_of(app) == "ex_menu" and "nav:b:" not in str(app.tg.edits[-1][1])
    run(app.on_callback(cb("nav:b:ex_rules")))                   # кнопка со старого шага
    assert app.tg.toasts[-1] == "Этот выбор уже неактуален" and step_of(app) == "ex_menu"
    # снова вперёд по тем же кнопкам
    run(app.on_callback(cb("x:k:grammar")))
    run(app.on_callback(cb("x:f:gap")))
    assert step_of(app) == "ex_gtopic" and app.db.get_state(42)["pending"]["ex"]["fmt"] == "gap"


def test_nav_cancel_and_text_step_back():
    gem = FakeGemini()
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:own")
    run(app.on_callback(cb("nav:b:ex_rule_text")))
    assert step_of(app) == "ex_gtopic"
    run(app.handle(msg(text="творительный падеж")))             # текст больше не ждём — это разговор
    assert gem.calls and step_of(app) == "ex_gtopic"
    run(app.on_callback(cb("nav:c:ex_gtopic")))
    assert step_of(app) is None and app.tg.edits[-1][0] == "✖️ Отменено."


def test_nav_in_set_dict_and_rule():
    app = make_app()
    run(app.handle(msg(text="/set")))
    run(app.on_callback(cb("s:topic")))
    assert "nav:b:topic" in str(app.tg.buttons[-1])
    run(app.handle(msg(text="кафе")))
    assert step_of(app) == "preview" and "nav:b:preview" in str(app.tg.buttons[-1])
    run(app.on_callback(cb("p:0")))                               # убрал слово
    run(app.on_callback(cb("nav:b:preview")))
    assert step_of(app) == "topic"
    run(app.on_callback(cb("nav:b:topic")))                       # → меню /set
    assert step_of(app) is None and "Набор" in app.tg.edits[-1][0]
    run(app.on_callback(cb("e:start")))
    assert step_of(app) == "errsel"
    run(app.on_callback(cb("e:p:a")))                             # смена периода — тот же шаг
    run(app.on_callback(cb("nav:b:errsel")))
    assert step_of(app) is None
    run(app.on_callback(cb("dn:own")))
    run(app.on_callback(cb("nav:b:dict_own")))
    assert step_of(app) is None and "Словарь" in app.tg.edits[-1][0]
    run(app.handle(msg(text="/rule")))
    assert str(app.tg.buttons[-1]) == str([[("✖️ Отмена", "nav:c:rule")]])
    run(app.on_callback(cb("nav:c:rule")))
    assert step_of(app) is None


def test_exercise_finish_without_check():
    gem = FakeGemini()
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix", "x:n:3")
    ex_id = app.db.get_state(42)["pending"]["ex"]["ex_id"]
    assert "nav:c:ex_answer" in str(app.tg.buttons[-1])
    run(app.on_callback(cb("nav:c:ex_answer")))
    assert step_of(app) is None and f"#{ex_id} — без проверки" in app.tg.sent[-2]
    assert "Главное меню" in app.tg.sent[-1]                    # после отмены — главное меню
    assert app.tg.markups and app.tg.markups[-1][1] is None and not app.db.ex_get(ex_id)["results"]
    # после проверки кнопка «Закончить без проверки» убирается
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix", "x:n:1")
    tg_id = app.db.ex_get(app.db.get_state(42)["pending"]["ex"]["ex_id"])["tg_msg_id"]
    run(app.handle(msg(text=answers_all(app))))
    assert app.tg.markups[-1] == (tg_id, None)


def test_voice_note_after_word_utochnenie():
    gem = FakeGemini()
    gem.check_verdict = {i: True for i in range(1, 11)}
    gem.voice_notes = {2: "это винительный, потому что widzę"}
    gem.note_verdict = {2: False}
    app = make_app(gem=gem)
    ex_flow(app, "x:k:voice", "x:s:own")
    run(app.handle(msg(text="ciasto, piec")))
    run(app.on_callback(cb("x:n:1")))
    assert "уточнение" in app.tg.sent[-1]                       # подсказка в упражнении
    run(app.handle(msg(voice={"file_id": "v"})))
    assert "«уточнение»" in gem.prompts[-1] and "пункт N" in gem.prompts[-1]
    res = app.tg.sent[-1]
    assert "9 из 10" in res and "💭 «это винительный, потому что widzę» — ❌ не так" in res


# ---------- длинные сообщения ----------

from bot.core import telegram as tg_mod  # noqa: E402


def test_split_html_keeps_tags_and_items_whole():
    items = [f"✅ {i}. <b>słowo{i}</b> — <i>{'объяснение ' * 40}</i>\n    🔤 <b>x</b>" for i in range(1, 11)]
    text = "📊 <b>9 из 10</b>\n\n" + "\n\n".join(items)
    chunks = tg_mod.split_html(text, 1500)
    assert len(chunks) > 1 and all(len(c) <= 1500 and h for c, h in chunks)
    for c, _ in chunks:
        assert c.count("<i>") == c.count("</i>") and c.count("<b>") == c.count("</b>")
    joined = "\n\n".join(c for c, _ in chunks)
    assert all(it in joined for it in items)                      # ни один пункт не потерян и не разрезан
    huge = "<i>" + "слово " * 1000 + "</i>"                       # одна строка длиннее лимита — без разметки
    parts = tg_mod.split_html(huge, 1500)
    assert all(not h and "<i>" not in c and len(c) <= 1500 for c, h in parts)
    assert sum(len(c.split()) for c, _ in parts) == 1000
    assert tg_mod.fit("a\n" * 3000, 100).endswith("…") and len(tg_mod.fit("a\n" * 3000, 100)) <= 100


def test_send_message_falls_back_to_plain_and_keeps_buttons_on_last():
    t = tg_mod.Telegram("x")
    calls = []

    async def fake_call(method, **params):
        calls.append(params)
        if params.get("parse_mode") and "BAD" in params["text"]:
            raise tg_mod.TelegramError("sendMessage: Bad Request: can't parse entities")
        return {"message_id": len(calls)}
    t.call = fake_call
    text = "<b>start</b>\n\n" + "\n\n".join(["<i>" + "a" * 2000 + "</i>", "<i>" + "b" * 2000 + "</i>", "BAD <i>x", "<b>end</b>"])
    run(t.send_message(1, text, [[("ok", "x")]]))
    assert any(c["text"].startswith("<b>start</b>") and c.get("parse_mode") for c in calls)   # первый — с разметкой
    plain_sent = [c["text"] for c in calls if not c.get("parse_mode")]
    assert len(plain_sent) == 1 and plain_sent[0].endswith("BAD x\n\nend") and "<" not in plain_sent[0]
    assert "reply_markup" in calls[-1] and "end" in calls[-1]["text"]
    assert "reply_markup" not in calls[0]                        # кнопки — только под последним куском


def test_test_options_shuffled_in_code():
    app = make_app()
    ex_flow(app, "x:k:grammar", "x:f:test", "x:t:mix", "x:n:1")
    ex = app.db.ex_get(app.db.get_state(42)["pending"]["ex"]["ex_id"])
    pos = [it["options"].index(it["answer"]) for it in ex["items"]]
    assert len(set(pos)) > 1                                    # правильный ответ не всегда под «a»
    run(app.handle(msg(text=answers_all(app, test=True, sure="all"))))
    assert "10 из 10" in app.tg.sent[-1]                        # проверка идёт по тексту варианта


# ---------- 🙅 Не ошибка ----------

def test_dispute_conversation_correction_and_ignore_list():
    gem = FakeGemini()
    app = make_app(gem=gem)
    run(app.handle(msg(text="x")))
    assert "nd:" in str(app.tg.buttons[-1])
    msg_id = app.db.corrections_since(42, 0)[0]["_msg"]
    run(app.on_callback(cb(f"nd:{msg_id}")))
    assert "☐ w sklep → w sklepie" in str(app.tg.buttons[-1])
    cid = app.db.corrections_for_msg_all(msg_id)[0]["_id"]
    run(app.on_callback(cb(f"nd:t:{cid}")))
    assert "✅ w sklep → w sklepie" in str(app.tg.edits[-1][1])
    assert app.db.corrections_since(42, 0) == []                  # ушло из пула (итоги, наборы, упражнения)
    assert [(r["original"], r["correct"]) for r in app.db.ignores(42)] == [("w sklep", "w sklepie")]
    run(app.on_callback(cb(f"nd:ok:{msg_id}")))
    assert "Не ошибка: 1" in app.tg.edits[-1][0]
    run(app.handle(msg(text="y")))                               # модель снова «исправила» так же
    assert "ИСКЛЮЧЕНИЯ" in gem.extras[-1] and "«w sklepie» (не «w sklep»)" in gem.extras[-1]
    assert "w sklep" not in app.tg.sent[-1].split("✔️")[0] and app.db.corrections_since(42, 0) == []
    # список исключений в /dict и удаление пары
    run(app.handle(msg(text="/dict")))
    run(app.on_callback(cb("ig:list")))
    assert "w sklep → w sklepie" in app.tg.sent[-1]
    run(app.on_callback(cb(f"ig:rm:{app.db.ignores(42)[0]['id']}")))
    assert app.db.ignores(42) == [] and "пуст" in app.tg.edits[-1][0]


def test_dispute_undo_and_set_streak_restored():
    t = {**TURN_JSON, "corrections": [{**TURN_JSON["corrections"][0], "original": "czasta", "correct": "ciasta"}],
         "target_uses": [{"lemma": "ciasto", "form": "ciasta", "correct": False}]}
    gem = FakeGemini(turn=t)
    app = make_app(gem=gem)
    start_set(app)
    run(app.handle(msg(text="Lubię czasta")))
    w = [x for x in app.db.set_words(app.db.active_set(42)["id"]) if x["pl"] == "ciasto"][0]
    before = len([u for u in app.db.word_uses(w["id"]) if not u["correct"]])
    assert before >= 1
    msg_id = app.db.corrections_since(42, 0)[-1]["_msg"]
    cid = app.db.corrections_for_msg_all(msg_id)[0]["_id"]
    run(app.on_callback(cb(f"nd:t:{cid}")))
    assert len([u for u in app.db.word_uses(w["id"]) if not u["correct"]]) == before - 1   # серия восстановлена
    run(app.on_callback(cb(f"nd:t:{cid}")))                      # передумал — всё как было
    assert len([u for u in app.db.word_uses(w["id"]) if not u["correct"]]) == before
    assert app.db.ignores(42) == [] and len(app.db.corrections_for_msg(msg_id)) == 1


def test_dispute_exercise_item():
    gem = FakeGemini()
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:gap", "x:t:mix", "x:n:1")
    ex_id = app.db.get_state(42)["pending"]["ex"]["ex_id"]
    run(app.handle(msg(text=answers_all(app, wrong=(2,)))))
    assert "9 из 10" in app.tg.sent[-1] and f"x:dp:{ex_id}" in str(app.tg.buttons[-1])
    assert len(app.db.corrections_since(42, 0)) == 1
    run(app.on_callback(cb(f"x:dp:{ex_id}")))
    assert "☐ 2. zle2" in str(app.tg.buttons[-1]) and "x:dt" in str(app.tg.buttons[-1])
    run(app.on_callback(cb(f"x:dt:{ex_id}:2")))
    assert app.db.corrections_since(42, 0) == []                 # ушло из ошибок
    assert app.db.ex_review_items(42, 10) == []                  # и с повтора
    assert app.db.ignores(42)[0]["original"] == "zle2"
    run(app.on_callback(cb(f"x:dok:{ex_id}")))
    assert "пункты 2" in app.tg.edits[-1][0] and "10 из 10" in app.tg.edits[-1][0]



# ---------- интерактивный тест: кнопки a / b / c ----------

def quiz_state(app):
    return app.db.get_state(42)["pending"]["ex"]


def test_quiz_buttons_pick_notes_and_check():
    gem = FakeGemini()
    app = make_app(gem=gem)
    ex_flow(app, "x:k:grammar", "x:f:test", "x:t:mix", "x:n:1")
    ex = quiz_state(app)
    ex_id, msg_id = ex["ex_id"], ex["quiz"]["msg"]
    assert "Упражнение 1/1" in app.tg.sent[-1] and "a) " in app.tg.sent[-1]       # одно сообщение
    kb = app.tg.buttons[-1]
    assert kb[0] == [("1 a", f"x:a:{ex_id}:1:a"), ("1 b", f"x:a:{ex_id}:1:b"), ("1 c", f"x:a:{ex_id}:1:c"),
                     ("1 !", f"x:a:{ex_id}:1:!")]
    assert len(kb) == 12 and "Проверить (0/10)" in str(kb[10])
    saved = app.db.ex_get(ex_id)
    run(app.on_callback(cb(f"x:go:{ex_id}")))
    assert app.tg.toasts[-1] == "Отмечено 0 из 10 — выбери остальные" and not app.db.ex_get(ex_id)["results"]
    right = ["abcd"[it["options"].index(it["answer"])] for it in saved["items"]]
    wrong2 = "a" if right[1] != "a" else "b"
    run(app.on_callback(cb(f"x:a:{ex_id}:2:{wrong2}")))
    assert app.tg.markups[-1][0] == msg_id and f"✅2{wrong2}" in str(app.tg.markups[-1][1])
    for n, L in enumerate(right, 1):                             # передумал по пункту 2 — выбрал верный
        run(app.on_callback(cb(f"x:a:{ex_id}:{n}:{L}")))
    assert f"✅2{right[1]}" in str(app.tg.markups[-1][1]) and "Проверить (10/10)" in str(app.tg.markups[-1][1])
    run(app.on_callback(cb(f"x:a:{ex_id}:3:!")))                 # уверен — не объяснять
    assert "❗3" in str(app.tg.markups[-1][1])
    run(app.handle(msg(text="4 (почему тут винительный?)")))     # уточнение текстом до проверки
    assert "Уточнение к пункту 4" in app.tg.sent[-1] and "💭 <i>почему тут винительный?</i>" in app.tg.edits[-1][0]
    n_before = len(gem.prompts)
    run(app.on_callback(cb(f"x:go:{ex_id}")))
    res = app.db.ex_get(ex_id)["results"]
    assert res and all(r["final"] == "ok" for r in res)
    p = gem.prompts[-1]
    assert len(gem.prompts) == n_before + 1 and "уточнение ученика: «почему тут винительный?»" in p
    assert "\n3. Задание" not in p                               # «!» — без объяснения
    assert "10 из 10" in app.tg.sent[-1]
    assert "Проверить" not in str(app.tg.markups[-1][1]) and "✅1" in str(app.tg.markups[-1][1])   # выбор виден
    run(app.on_callback(cb(f"x:a:{ex_id}:1:a")))                 # кнопки после проверки не работают
    assert "уже проверено" in app.tg.toasts[-1]


def test_quiz_text_answer_merges_with_buttons():
    app = make_app()
    ex_flow(app, "x:k:grammar", "x:f:test", "x:t:mix", "x:n:1")
    ex_id = quiz_state(app)["ex_id"]
    saved = app.db.ex_get(ex_id)
    right = ["abcd"[it["options"].index(it["answer"])] for it in saved["items"]]
    for n, L in enumerate(right[:9], 1):
        run(app.on_callback(cb(f"x:a:{ex_id}:{n}:{L}")))
    run(app.handle(msg(text="непонятно что")))                   # без номера — не проверяем полтеста
    assert "Не понял" in app.tg.sent[-1] and not app.db.ex_get(ex_id)["results"]
    run(app.handle(msg(text=f"10{right[9]}")))                    # последний пункт текстом
    assert "10 из 10" in app.tg.sent[-1]



# ---------- 📘 учебник ----------

from pathlib import Path  # noqa: E402
from bot.core import textbook as tb  # noqa: E402

UNIT = {"unit": "2", "title": "Rodzina", "summary": "семья, mieć",
        "words": [{"pl": "brat", "translit": "БРАТ", "ru": "брат", "pos": "сущ., м. р."},
                  {"pl": "siostra", "translit": "ЩЁС-тра", "ru": "сестра", "pos": "сущ., ж. р."},
                  {"pl": "rodzeństwo", "translit": "ро-ДЗЕНЬ-ство", "ru": "братья и сёстры",
                   "ru_alt": ["брат и сестра"], "pos": "сущ., ср. р."}]}


def with_unit(tmp_path_factory=None):
    import json as _json
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "unit_02.json").write_text(_json.dumps(UNIT, ensure_ascii=False), encoding="utf-8")
    tb.UNITS_DIR = d
    return d


def test_textbook_pick_and_cards():
    with_unit()
    unit = tb.get_unit("2")
    assert unit and len(unit["words"]) == 3 and tb.load_units()[0]["title"] == "Rodzina"
    stats = {("siostra", "pl"): {"streak": 0, "wrong": 1, "right": 2}}
    picked = tb.pick_words(unit, stats, 3, "pl")
    assert picked[0][0]["pl"] == "siostra"                       # с ошибкой — первым
    c = tb.card_item(unit["words"][2], "pl")
    assert c["q"] == "rodzeństwo [ро-ДЗЕНЬ-ство]" and c["answer"] == "братья и сёстры" and "брат и сестра" in c["accepted"]
    r = tb.card_item(unit["words"][0], "ru")
    assert r["q"] == "брат" and r["answer"] == "brat"


def test_book_cards_flow_stats_not_in_pool():
    with_unit()
    gem = FakeGemini()
    app = make_app(gem=gem)
    run(app.handle(msg(text="/ex")))
    run(app.on_callback(cb("x:k:book")))
    assert "Unit 2 — Rodzina" in app.tg.sent[-1] and "семья, mieć" in app.tg.sent[-1]
    run(app.on_callback(cb("x:b:u:2")))
    assert "выучено 0" in app.tg.sent[-1]
    run(app.on_callback(cb("x:b:m:card")))
    run(app.on_callback(cb("x:b:d:pl")))
    n_prompts = len(gem.prompts)
    run(app.on_callback(cb("x:n:1")))
    assert len(gem.prompts) == n_prompts                         # карточки — без Gemini
    ex = app.db.ex_get(app.db.get_state(42)["pending"]["ex"]["ex_id"])
    text = app.tg.sent[-1]
    assert "rodzeństwo [ро-ДЗЕНЬ-ство]" in text and "братья и сёстры" not in text   # перевод не подсказан
    ans = " ".join(f"{i} {'неверно' if it['lemma'] == 'siostra' else it['answer']}"
                   for i, it in enumerate(ex["items"], 1))
    run(app.handle(msg(text=ans)))
    p = gem.prompts[-1]
    assert "КАРТОЧКИ" in p and p.count(". Задание") == 1        # верные не объясняем, только ошибку
    assert "2 из 3" in app.tg.sent[-1]
    st = app.db.book_stats(42, "2")
    assert st[("brat", "pl")]["streak"] == 1 and st[("siostra", "pl")]["wrong"] == 1
    assert app.db.corrections_since(42, 0) == []                 # в общий пул ошибок не идёт
    run(app.on_callback(cb("x:menu")))
    run(app.on_callback(cb("x:k:book")))
    run(app.on_callback(cb("x:b:u:2")))
    run(app.on_callback(cb("x:b:m:card")))
    run(app.on_callback(cb("x:b:d:pl")))
    run(app.on_callback(cb("x:n:1")))
    ex2 = app.db.ex_get(app.db.get_state(42)["pending"]["ex"]["ex_id"])
    assert ex2["items"][0]["lemma"] == "siostra"                 # слово с ошибкой — первым


def test_book_test_close_options_and_gap_spoiler():
    with_unit()
    gem = FakeGemini()
    gem.spoiler = True
    app = make_app(gem=gem)
    for c in ("x:k:book", "x:b:u:2", "x:b:m:test", "x:b:d:ru"):
        if c == "x:k:book":
            run(app.handle(msg(text="/ex")))
        run(app.on_callback(cb(c)))
    run(app.on_callback(cb("x:n:1")))
    assert "БЛИЗКИХ ПО СМЫСЛУ" in gem.prompts[-1]
    ex = app.db.get_state(42)["pending"]["ex"]
    saved = app.db.ex_get(ex["ex_id"])
    it = saved["items"][0]
    assert len(it["options"]) == 3 and it["answer"] in it["options"] and any("blisko" in o for o in it["options"])
    assert ex.get("quiz") and "1 a" in str(app.tg.buttons[-1])  # кнопки a / b / c
    # пропуски: перевод пропущенного слова — под спойлером
    run(app.handle(msg(text="/ex")))
    for c in ("x:k:book", "x:b:u:2", "x:b:m:gap", "x:n:1"):
        run(app.on_callback(cb(c)))
    assert "⟪ ⟫" in gem.prompts[-1] and ("brat" in gem.prompts[-1] or "siostra" in gem.prompts[-1])
    assert "ТОЛЬКО эти" in gem.prompts[-1] and "творительный" in gem.prompts[-1] and "дательный" not in gem.prompts[-1]
    assert "<tg-spoiler>слово</tg-spoiler>" in app.tg.sent[-1]
    gap = app.db.ex_get(app.db.get_state(42)["pending"]["ex"]["ex_id"])
    assert gap["items"][0]["ru"] == "Предложение слово"          # в разборе — без скобок


def test_book_no_units_message():
    import tempfile
    tb.UNITS_DIR = Path(tempfile.mkdtemp())
    app = make_app()
    run(app.handle(msg(text="/ex")))
    run(app.on_callback(cb("x:k:book")))
    assert "Юнитов пока нет" in app.tg.sent[-1]



def test_real_textbook_units_are_valid():
    real = Path(__file__).resolve().parent.parent / "bot" / "textbook"
    units = tb.load_units(real)
    assert [u["unit"] for u in units][:3] == ["7", "7a", "8"]
    for u in units:
        assert u["title"] and u["summary"]
        for w in u["words"]:
            assert w["pl"] and w["ru"] and w["translit"] and w["pos"], w


# ---------- 🏠 меню, /new, наборы из юнита и 🧳 недоученные ----------

def test_main_menu_buttons_and_cancel_keeps_conversation():
    app = make_app()
    start_set(app)                                              # режим набора, разговор идёт
    session = app.db.current_session(42)
    run(app.handle(msg(text="/menu")))
    assert "Главное меню" in app.tg.sent[-1] and "🎯 набор «" in app.tg.sent[-1]
    run(app.on_callback(cb("go:new")))
    assert "Новый разговор" in app.tg.sent[-1] and "nw:set" in str(app.tg.buttons[-1])
    run(app.on_callback(cb("nw:free")))
    run(app.on_callback(cb("nav:b:new_free")))                  # назад к выбору
    assert step_of(app) == "new_menu"
    run(app.on_callback(cb("nav:c:new_menu")))                  # отмена
    st = app.db.get_state(42)
    assert st["mode"] == "set" and app.db.current_session(42) == session and st["pending"] is None
    assert "Главное меню" in app.tg.sent[-1]
    run(app.on_callback(cb("go:ex")))
    assert "Упражнения" in app.tg.sent[-1]


def test_new_free_topic_own_and_random():
    gem = FakeGemini()
    app = make_app(gem=gem)
    run(app.handle(msg(text="/new")))
    run(app.on_callback(cb("nw:free")))
    run(app.on_callback(cb("nw:f:own")))
    run(app.handle(msg(text="у врача")))
    st = app.db.get_state(42)
    assert st["mode"] == "free" and st["topic"] == "у врача"
    assert "ТЕМА РАЗГОВОРА: «у врача»" in gem.extras[-1] and gem.calls[-1][1] == "Zaczynajmy!"
    run(app.handle(msg(text="/new")))
    run(app.on_callback(cb("nw:free")))
    run(app.on_callback(cb("nw:f:rnd")))
    assert app.db.get_state(42)["topic"] in __import__("bot.settings", fromlist=["x"]).RANDOM_TOPICS
    run(app.handle(msg(text="/new")))
    run(app.on_callback(cb("nw:free")))
    run(app.on_callback(cb("nw:f:none")))
    assert app.db.get_state(42)["topic"] is None
    run(app.handle(msg(text="hej")))
    assert "ТЕМА РАЗГОВОРА" not in gem.extras[-1]


def test_set_from_unit_counts_to_unit_progress():
    with_unit()
    t = {**TURN_JSON, "target_uses": [{"lemma": "brat", "form": "brata", "correct": True}]}
    gem = FakeGemini(turn=t)
    app = make_app(gem=gem)
    run(app.handle(msg(text="/new")))
    run(app.on_callback(cb("nw:newset")))
    assert "s:unit" in str(app.tg.buttons[-1])
    run(app.on_callback(cb("s:unit")))
    run(app.on_callback(cb("nav:b:set_unit")))                  # назад — к источникам набора из /new
    assert step_of(app) == "new_src"
    run(app.on_callback(cb("s:unit")))
    run(app.on_callback(cb("su:2")))
    p = app.db.get_state(42)["pending"]
    assert p["step"] == "preview" and p["unit"] == "2" and len(p["words"]) == 3
    assert all(w["unit"] == "2" for w in p["words"])
    run(app.on_callback(cb("p:ok")))
    active = app.db.active_set(42)
    assert active["title"].startswith("Unit 2") and app.db.get_state(42)["mode"] == "set"
    assert all(w["unit"] == "2" for w in app.db.set_words(active["id"]))
    assert app.db.book_stats(42, "2")[("brat", "ru")]["right"] >= 1   # употребление в разговоре → прогресс юнита


def test_leftovers_collect_and_take_back_with_progress():
    t = {**TURN_JSON, "target_uses": [{"lemma": "ciasto", "form": "ciasta", "correct": True}]}
    gem = FakeGemini(turn=t)
    app = make_app(gem=gem)
    start_set(app)                                              # ciasto, piec, kawa
    old = {w["pl"]: w["id"] for w in app.db.set_words(app.db.active_set(42)["id"])}
    uses_before = len(app.db.word_uses(old["ciasto"]))
    app.gemini.words = {"title": "Чай", "words": [{"pl": "herbata", "translit": "хер-БА-та", "ru": "чай", "pos": "сущ"}]}
    start_set(app, "чай")
    assert [w["pl"] for w in app.db.set_words(app.db.active_set(42)["id"])] == ["herbata"]
    assert {w["pl"] for w in app.db.leftovers(42)} == {"ciasto", "piec", "kawa"}
    run(app.handle(msg(text="/set")))
    assert "Недоученных из прошлых наборов: 3" in app.tg.sent[-1] and "s:left" in str(app.tg.buttons[-1])
    run(app.on_callback(cb("s:left")))
    assert step_of(app) == "leftovers" and len(app.db.get_state(42)["pending"]["chosen"]) == 3
    run(app.on_callback(cb(f"lf:{old['kawa']}")))               # kawa не берём
    run(app.on_callback(cb("lf:go")))
    active = app.db.active_set(42)
    words = {w["pl"]: w["id"] for w in app.db.set_words(active["id"])}
    assert active["title"] == "Недоученные" and set(words) == {"ciasto", "piec"}
    assert words["ciasto"] == old["ciasto"] and len(app.db.word_uses(old["ciasto"])) >= uses_before   # прогресс сохранён
    assert {w["pl"] for w in app.db.leftovers(42)} == {"kawa", "herbata"}
