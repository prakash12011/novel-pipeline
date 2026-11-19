import json

from novel.core.keybudget import KeyBudget, key_id
from novel.stages import episode_translate as T

DAY = 1795000000.0          # a fixed moment (Pacific afternoon); +86400 is the next Pacific day


class Clock:
    def __init__(self, t=DAY):
        self.t = t

    def __call__(self):
        return self.t


def budget(tmp_path, clock, keys=("k1", "k2"), per_minute=2, per_day=3):
    return KeyBudget(tmp_path / "usage.json", list(keys), "m", per_minute, per_day, now=clock)


def test_minute_then_other_key_then_wait(tmp_path):
    c = Clock()
    b = budget(tmp_path, c)
    assert [b.take(0), b.take(0), b.take(0), b.take(0)] == [0, 0, 1, 1]
    wait = b.take(0)
    assert isinstance(wait, float) and 59 <= wait <= 61
    c.t += 61
    assert b.take(0) == 0


def test_day_limit_then_none_then_next_day(tmp_path):
    c = Clock()
    b = budget(tmp_path, c, keys=("k1",), per_minute=10, per_day=3)
    assert [b.take(), b.take(), b.take()] == [0, 0, 0]
    assert b.take() is None
    c.t += 86400
    assert b.take() == 0


def test_the_file_holds_no_key_and_is_shared(tmp_path):
    c = Clock()
    budget(tmp_path, c).take(0)
    text = (tmp_path / "usage.json").read_text(encoding="utf-8")
    assert "k1" not in json.loads(text) and f"{key_id('k1')}:m" in text
    assert budget(tmp_path, c).status()[0][1] == 1           # a second client sees the same count


def test_a_429_holds_the_key_or_ends_its_day(tmp_path):
    c = Clock()
    b = budget(tmp_path, c, per_minute=10)
    b.limited(0)
    assert b.take(0) == 1
    b.limited(1, whole_day=True)
    assert isinstance(b.take(0), float)                       # key 1 is done today, key 0 waits out its hold
    c.t += 61
    assert b.take(0) == 0


def test_forced_openrouter_never_calls_gemini(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "api_keys", lambda: ["k1"])
    monkeypatch.setenv("NOVEL_FORCE_OPENROUTER", "1")
    g = T.Gemini("m", tmp_path / "log.jsonl")
    monkeypatch.setattr(g, "_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Gemini called")))
    monkeypatch.setattr(g, "_router", lambda *a: T.GenResult("from router", "STOP", "router", 0))
    assert g.generate("s", "t").text == "from router"
    monkeypatch.setenv("NOVEL_FORCE_OPENROUTER", "0")
    assert T.force_openrouter() is False


def test_after_a_daily_429_the_same_request_goes_to_the_next_key(tmp_path, monkeypatch):
    import io
    import urllib.error
    monkeypatch.setattr(T, "api_keys", lambda: ["k1", "k2"])
    g = T.Gemini("m", tmp_path / "log.jsonl", sleep=lambda s: None)
    g._budgets["m"] = budget(tmp_path, Clock(), per_minute=10, per_day=5)
    sent = []

    def post(body, model=None):
        sent.append(body)
        if len(sent) == 1:
            raise urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b'{"quotaId": "GenerateRequestsPerDay"}'))
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
    monkeypatch.setattr(g, "_post", post)
    assert g.generate("s", "t").text == "ok"
    assert sent[0] == sent[1] and isinstance(sent[1], dict)      # the request itself, not Google's error text
    assert g.k == 1


def test_spent_keys_go_to_argon_on_opencode_first_then_openrouter(tmp_path, monkeypatch):
    from novel import kqa
    monkeypatch.setattr(T, "api_keys", lambda: ["k1"])
    monkeypatch.undo()                                      # the conftest stub of _opencode off: the real one, faked below
    monkeypatch.setattr(T, "api_keys", lambda: ["k1"])
    monkeypatch.setattr(T, "default_budget", lambda model, keys: None)
    g = T.Gemini("m", tmp_path / "log.jsonl", sleep=lambda s: None)
    g._budgets["m"] = budget(tmp_path, Clock(), keys=("k1",), per_minute=10, per_day=0)    # every key spent
    asked = []

    class FakeChat:
        def __init__(self, model):
            self.model = model

        def generate(self, system, text, **kw):
            asked.append((self.model, text))
            return T.GenResult("from argon", "STOP", self.model, 0, {"allowance": 0.02})
    monkeypatch.setattr(kqa, "OpenCodeChat", FakeChat)
    monkeypatch.setattr(g, "_router", lambda *a: T.GenResult("from router", "STOP", "router", 0))
    assert g.generate("s", "the chapter", fallback_note="LENGTH: under 800").text == "from argon"
    assert asked == [("gemini-4-argon", "the chapter\n\nLENGTH: under 800")]   # the note goes to Argon only

    class Down(FakeChat):
        def generate(self, system, text, **kw):
            raise kqa.QAError("down")
    monkeypatch.setattr(kqa, "OpenCodeChat", Down)
    assert g.generate("s", "t").text == "from router"

    # scene lines: Claude Sonnet (headless Claude Code) before Argon
    monkeypatch.setattr(kqa, "OpenCodeChat", FakeChat)
    monkeypatch.setattr(g, "_claude", lambda *a: T.GenResult("from sonnet", "STOP", "claude-code", 0))
    assert g.generate("s", "t", scenes=True).text == "from sonnet"
    assert g.generate("s", "t").text == "from argon"                          # a translation skips Claude


def test_claude_writes_the_scene_lines_before_any_gemini_key(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "api_keys", lambda: ["k1"])
    g = T.Gemini("m", tmp_path / "log.jsonl", sleep=lambda s: None)
    posted = []
    monkeypatch.setattr(g, "_post", lambda body, model=None: posted.append(1) or
                        {"candidates": [{"content": {"parts": [{"text": "gemini"}]}, "finishReason": "STOP"}]})
    monkeypatch.setattr(g, "_claude", lambda *a: T.GenResult("sonnet", "STOP", "claude-code", 0))
    assert g.generate("s", "t", scenes=True).text == "sonnet" and posted == []     # scene_writer: claude (default)
    assert g.generate("s", "t").text == "gemini"                                   # translation: Gemini
    monkeypatch.setattr(g, "_claude", lambda *a: None)                             # Claude out: the free keys
    assert g.generate("s", "t", scenes=True).text == "gemini"


def test_gemini_falls_back_to_openrouter_when_every_key_spent_its_day(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "api_keys", lambda: ["k1", "k2"])
    c = Clock()
    g = T.Gemini("m", tmp_path / "log.jsonl", sleep=lambda s: None)
    g._budgets["m"] = budget(tmp_path, c, per_minute=10, per_day=1)
    used = []

    def post(body, model=None):
        used.append(g.k)
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
    monkeypatch.setattr(g, "_post", post)
    monkeypatch.setattr(g, "_router", lambda *a: T.GenResult("from router", "STOP", "router", 0))
    assert [g.generate("s", "t").text for _ in range(3)] == ["ok", "ok", "from router"]
    assert used == [0, 1]
