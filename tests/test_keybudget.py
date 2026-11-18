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
