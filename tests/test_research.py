import json
import urllib.error
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from novel.commands import research_cmd
from novel.publish import research

API_KEY = "SECRET-KEY-VALUE-123"
API_REPLY = {"items": [{"id": {"videoId": "v1"},
                        "snippet": {"title": "Demo Title", "channelTitle": "Demo Channel"}}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    book = data / "demo"
    book.mkdir()
    (book / "book.yaml").write_text(
        yaml.safe_dump({"publish": {"research_seeds": ["demo seed"]}}), encoding="utf-8")
    monkeypatch.setenv("NOVEL_HOME", str(home))
    return home, data, book


def make_opener(table, calls=None, api_reply=None):
    def opener(url):
        if calls is not None:
            calls.append(url)
        if "googleapis.com" in url:
            return json.dumps(api_reply if api_reply is not None else API_REPLY).encode("utf-8")
        q = parse_qs(urlsplit(url).query)["q"][0]
        return json.dumps([q, table.get(q, [])], ensure_ascii=False).encode("utf-8")
    return opener


def cache_files(book):
    return sorted((book / "research").glob("autocomplete-*.json"))


def write_cache(book, name, fetched_at, phrases):
    rdir = book / "research"
    rdir.mkdir(exist_ok=True)
    path = rdir / name
    path.write_text(json.dumps({"book": "demo", "fetched_at": fetched_at, "seeds": ["demo seed"],
                                "queries": 27, "empty_queries": 0, "phrases": phrases},
                               ensure_ascii=False), encoding="utf-8")
    return path


def now_stamp(delta=timedelta(0)):
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_ranking_and_counts(env, capsys):
    _, _, book = env
    table = {"demo seed": ["Alpha", "beta"],
             "demo seed a": ["alpha ", "Gamma"],
             "demo seed b": ["gamma"]}
    rc = research_cmd.main(["--book", "demo"], opener=make_opener(table), pause=0)
    assert rc == 0
    files = cache_files(book)
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    # 3 hits, then 5 empty requests in a row stop the run after 8 queries
    assert data["queries"] == 8
    assert data["empty_queries"] == 5
    assert data["seeds"] == ["demo seed"]
    assert data["phrases"] == [
        {"text": "alpha", "count": 2, "seeds": ["demo seed"]},
        {"text": "gamma", "count": 2, "seeds": ["demo seed"]},
        {"text": "beta", "count": 1, "seeds": ["demo seed"]},
    ]
    assert "top_videos" not in data
    out = capsys.readouterr().out
    assert str(files[0]) in out
    assert "Phrases: 3" in out


def test_latin1_fallback():
    raw = b'["caf", ["caf\xe9 hindi"]]'  # not valid UTF-8, so it is read as latin-1
    assert research.fetch_suggestions("caf", opener=lambda url: raw) == ["café hindi"]


def test_utf8_reply_and_errors():
    raw = '["x", ["के hindi"]]'.encode("utf-8")
    assert research.fetch_suggestions("x", opener=lambda url: raw) == ["के hindi"]

    def broken(url):
        raise OSError("no network")

    assert research.fetch_suggestions("x", opener=broken) == []
    assert research.fetch_suggestions("x", opener=lambda url: b"not json") == []


def test_fresh_cache_is_reused(env, capsys):
    _, _, book = env
    write_cache(book, "autocomplete-20261113.json", now_stamp(),
                [{"text": "cached one", "count": 3, "seeds": ["demo seed"]}])
    calls = []
    rc = research_cmd.main(["--book", "demo"], opener=make_opener({}, calls), pause=0)
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "Using cached research from" in out
    assert "(1 phrases)" in out


def test_refresh_calls_opener(env):
    _, _, book = env
    write_cache(book, "autocomplete-20261113.json", now_stamp(),
                [{"text": "cached one", "count": 3, "seeds": ["demo seed"]}])
    calls = []
    rc = research_cmd.main(["--book", "demo", "--refresh"],
                           opener=make_opener({"demo seed": ["fresh"]}, calls), pause=0)
    assert rc == 0
    assert len(calls) > 0
    texts = [p["text"] for p in json.loads(cache_files(book)[-1].read_text(encoding="utf-8"))["phrases"]]
    assert texts == ["fresh"]


def test_old_cache_kept_when_all_empty(env, capsys):
    _, _, book = env
    old = write_cache(book, "autocomplete-20260101.json", "2026-01-01T00:00:00Z",
                      [{"text": "old one", "count": 2, "seeds": ["demo seed"]}])
    before = old.read_text(encoding="utf-8")
    rc = research_cmd.main(["--book", "demo"], opener=make_opener({}), pause=0)
    assert rc == 0
    assert cache_files(book) == [old]
    assert old.read_text(encoding="utf-8") == before
    assert f"Offline or blocked: using older research from {old}" in capsys.readouterr().out


def test_all_empty_no_cache_returns_1(env, capsys):
    _, _, book = env
    rc = research_cmd.main(["--book", "demo"], opener=make_opener({}), pause=0)
    assert rc == 1
    assert cache_files(book) == []
    assert "No research available (offline?)" in capsys.readouterr().out


def test_no_seeds_returns_1(env, capsys):
    _, _, book = env
    (book / "book.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")

    def must_not_call(url):
        raise AssertionError("network must not be used without seeds")

    rc = research_cmd.main(["--book", "demo"], opener=must_not_call, pause=0)
    assert rc == 1
    assert ('No research seeds. Add publish.research_seeds to book.yaml or pass --seed "..."'
            in capsys.readouterr().out)


def test_api_key_never_saved_or_printed(env, capsys):
    home, _, book = env
    (home / "secrets.env").write_text(f"YOUTUBE_API_KEY={API_KEY}\n", encoding="utf-8")
    calls = []
    rc = research_cmd.main(["--book", "demo", "--seed", "alpha seed", "--seed", "beta seed",
                            "--seed", "gamma seed"],
                           opener=make_opener({"alpha seed": ["x"]}, calls), pause=0)
    assert rc == 0
    api_calls = [u for u in calls if "googleapis.com" in u]
    assert len(api_calls) == 2  # first two seeds only
    data = json.loads(cache_files(book)[-1].read_text(encoding="utf-8"))
    assert data["top_videos"][0]["title"] == "Demo Title"
    assert data["top_videos"][0]["channel"] == "Demo Channel"
    assert "top_videos_error" not in data
    assert API_KEY not in cache_files(book)[-1].read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert API_KEY not in captured.out and API_KEY not in captured.err


def test_api_failure_is_recorded_without_key(env, capsys):
    home, _, book = env
    (home / "secrets.env").write_text(f"YOUTUBE_API_KEY={API_KEY}\n", encoding="utf-8")

    def opener(url):
        if "googleapis.com" in url:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return make_opener({"demo seed": ["hello"]})(url)

    rc = research_cmd.main(["--book", "demo"], opener=opener, pause=0)
    assert rc == 0
    text = cache_files(book)[-1].read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["top_videos_error"] == "HTTP 403"
    assert data["top_videos"] == []
    assert API_KEY not in text
    captured = capsys.readouterr()
    assert API_KEY not in captured.out and API_KEY not in captured.err
