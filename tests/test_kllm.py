"""novel.kllm: the streaming proxy (auth, relay, idle clock, stop route, padding) and the address relay, against a
fake llama-server and a fake ntfy on localhost."""

import http.client
import json
import sys
import time
from pathlib import Path

import pytest

from novel import kllm
from novel.kllm import KllmError, ProxyState

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
from fake_llm import FakeLlama, FakeNtfy  # noqa: E402

KEY = "k" * 32
CHAT = "/v1/chat/completions"


@pytest.fixture
def stack():
    up = FakeLlama(events=5, delay=0.2)
    state = ProxyState(idle_minutes=30, info={"ctx": 4096}, pad=0)
    px = kllm.serve_llm_proxy(up.port, KEY, state)
    yield up, px, state
    px.shutdown()
    up.close()


def conn(px):
    return http.client.HTTPConnection("127.0.0.1", px.server_address[1], timeout=20)


def req(px, method, path, body=None, headers=None, key=KEY):
    h = {"Authorization": f"Bearer {key}"} if key else {}
    h.update(headers or {})
    c = conn(px)
    c.request(method, path, body=None if body is None else json.dumps(body), headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def stream(px, body=None, headers=None):
    """POST a streamed chat; returns (first_byte_s, [(t, bytes)], total_s, status) read chunk by chunk."""
    c = conn(px)
    t0 = time.time()
    c.request("POST", CHAT, body=json.dumps(body or {"stream": True}),
              headers={"Authorization": f"Bearer {KEY}", **(headers or {})})
    r = c.getresponse()
    got = []
    while True:
        d = r.read1(65536)
        if not d:
            break
        got.append((time.time() - t0, d))
    c.close()
    return got[0][0] if got else None, got, time.time() - t0, r.status


def data_lines(got):
    text = b"".join(d for _, d in got).decode()
    return [l for l in text.splitlines() if l.startswith("data:")]


# ---------------------------------------------------------------- auth

def test_auth_bearer_and_x_api_key(stack):
    up, px, _ = stack
    assert req(px, "POST", CHAT, {"a": 1}, key=None)[0] == 401
    assert req(px, "POST", CHAT, {"a": 1}, key="wrong-" + "k" * 30)[0] == 401
    assert req(px, "POST", CHAT, {"a": 1})[0] == 200
    assert req(px, "POST", "/v1/messages", {"a": 1}, headers={"x-api-key": KEY}, key=None)[0] == 200
    assert req(px, "GET", "/__kllm/status", key=None)[0] == 401
    assert [r["path"] for r in up.requests] == [CHAT, "/v1/messages"]        # refused calls never reach the server


def test_key_is_not_forwarded(stack):
    up, px, _ = stack
    req(px, "POST", CHAT, {"a": 1}, headers={"x-api-key": KEY})
    sent = {k.lower() for k in up.requests[-1]["headers"]}
    assert not sent & {"authorization", "x-api-key", "x-kllm-pad"}
    assert up.requests[-1]["body"] == b'{"a": 1}'


def test_short_key_refused():
    with pytest.raises(ValueError):
        kllm.serve_llm_proxy(1, "short")


def test_any_path_is_forwarded(stack):
    up, px, _ = stack
    st, data = req(px, "GET", "/health")
    assert st == 200 and json.loads(data) == {"status": "ok"}
    assert req(px, "GET", "/nope")[0] == 404


def test_upstream_down_is_502():
    state = ProxyState(pad=0)
    px = kllm.serve_llm_proxy(1, KEY, state)         # nothing listens on port 1
    try:
        assert req(px, "POST", CHAT, {"a": 1})[0] == 502
        assert state.inflight == 0
    finally:
        px.shutdown()


# ---------------------------------------------------------------- relay

def test_stream_arrives_chunk_by_chunk(stack):
    _, px, _ = stack
    first, got, total, status = stream(px)
    assert status == 200 and total >= 0.8
    assert first < 0.5                                  # the first event is not held until the end
    lines = data_lines(got)
    assert len(lines) == 6 and lines[-1] == "data: [DONE]"
    assert len(got) >= 5


# ---------------------------------------------------------------- idle clock and control routes

def test_idle_clock_only_inference_resets_it(stack):
    _, px, state = stack
    state._last -= 600
    st, data = req(px, "GET", "/__kllm/status")
    snap = json.loads(data)
    assert st == 200 and snap["idle_s"] >= 600 and snap["ctx"] == 4096 and snap["idle_minutes"] == 30
    req(px, "GET", "/health")
    assert state.idle_seconds() >= 600                  # a status ping and a health check do not count
    req(px, "POST", CHAT, {"a": 1})
    assert state.idle_seconds() < 5 and state.requests == 1


def test_no_idle_while_a_request_is_in_flight(stack):
    _, px, state = stack
    state._last -= 600
    c = conn(px)
    c.request("POST", CHAT, body=json.dumps({"stream": True}), headers={"Authorization": f"Bearer {KEY}"})
    r = c.getresponse()
    r.read1(10)
    assert state.inflight == 1 and state.idle_seconds() == 0 and not state.expired()
    while r.read1(65536):
        pass
    c.close()
    time.sleep(0.2)
    assert state.inflight == 0 and state.idle_seconds() < 5


def test_expired_after_idle_minutes():
    s = ProxyState(idle_minutes=2)
    assert not s.expired()
    s._last -= 121
    assert s.expired()


def test_stop_route(stack):
    _, px, state = stack
    assert not state.stop_event.is_set()
    assert req(px, "POST", "/__kllm/stop", key=None)[0] == 401 and not state.stop_event.is_set()
    st, data = req(px, "POST", "/__kllm/stop", {})
    assert st == 200 and json.loads(data) == {"stopping": True} and state.stop_event.is_set()


def test_client_going_away_cancels_upstream():
    up = FakeLlama(events=200, delay=0.05)
    px = kllm.serve_llm_proxy(up.port, KEY, ProxyState(pad=0))
    try:
        c = conn(px)
        c.request("POST", CHAT, body=json.dumps({"stream": True}), headers={"Authorization": f"Bearer {KEY}"})
        c.getresponse().read1(10)
        c.close()
        end = time.time() + 6
        while time.time() < end and not any(r["method"] == "ABORT" for r in up.requests):
            time.sleep(0.1)
        assert any(r["method"] == "ABORT" for r in up.requests)
    finally:
        px.shutdown()
        up.close()


# ---------------------------------------------------------------- padding

def test_padding_comes_first_then_the_live_stream(stack):
    _, px, state = stack
    state.pad = 20000
    first, got, total, status = stream(px)
    assert status == 200 and first < 0.3
    pad = b"".join(d for _, d in got).split(b"data:")[0]
    assert len(pad) >= 20000 and pad.startswith(b": ")
    assert data_lines(got)[-1] == "data: [DONE]" and len(data_lines(got)) == 6
    # the first real event arrives while the upstream is still generating
    t_first_data = next(t for t, d in got if b"data:" in d)
    assert t_first_data < total - 0.4


def test_padding_header_overrides_and_non_streams_are_not_padded(stack):
    _, px, state = stack
    state.pad = 20000
    _, got, _, _ = stream(px, headers={"x-kllm-pad": "0"})
    assert b"".join(d for _, d in got).startswith(b"data:")
    st, data = req(px, "POST", CHAT, {"a": 1})                       # not "stream": true
    assert st == 200 and json.loads(data)["choices"][0]["message"]["content"] == "hello"


def test_padded_upstream_error_becomes_a_data_line(stack):
    up, px, state = stack
    state.pad = 1000
    up.fail_status = 503
    _, got, _, status = stream(px)
    lines = data_lines(got)
    assert status == 200 and len(lines) == 1 and "model is loading" in lines[0]


def test_keepalive_while_upstream_is_silent(monkeypatch):
    monkeypatch.setattr(kllm, "KEEPALIVE_S", 0.2)
    up = FakeLlama(events=2, delay=0.0, first_delay=0.9)
    px = kllm.serve_llm_proxy(up.port, KEY, ProxyState(pad=500))
    try:
        _, got, _, _ = stream(px)
        text = b"".join(d for _, d in got)
        assert text.count(b": keepalive") >= 2 and b"tok0" in text
    finally:
        px.shutdown()
        up.close()


# ---------------------------------------------------------------- the PC's calls and the address relay

def test_client_calls(stack):
    _, px, state = stack
    url = f"http://127.0.0.1:{px.server_address[1]}"
    assert kllm.server_status(url, KEY)["ctx"] == 4096
    with pytest.raises(KllmError, match="401"):
        kllm.server_status(url, "x" * 32)
    assert kllm.request_stop(url, KEY) == {"stopping": True} and state.stop_event.is_set()
    with pytest.raises(KllmError):
        kllm.server_status("http://127.0.0.1:1", KEY, timeout=2)


def test_relay_roundtrip(monkeypatch):
    with FakeNtfy() as nt:
        monkeypatch.setenv("NOVEL_KLLM_NTFY_URL", nt.url)
        assert kllm.relay_fetch("t" * 32) == []
        kllm.relay_post("t" * 32, {"url": "https://x.trycloudflare.com", "ctx": 65536})
        nt.topics["t" * 32].append("not json")
        assert kllm.relay_fetch("t" * 32) == [{"url": "https://x.trycloudflare.com", "ctx": 65536}]
    with pytest.raises(KllmError):
        kllm.relay_fetch("t" * 32, timeout=2)           # the fake is gone
