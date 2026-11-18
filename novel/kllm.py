"""The coding-LLM server's front door: a streaming reverse proxy in front of llama-server, plus the small client calls
the PC uses to reach it. Standard library only: this file is copied into the session dataset and imported by
kaggle/llm_server.ipynb, and it is also imported by `novel kllm` on the PC.

What the proxy does (`serve_llm_proxy`):
  * every request needs the key: `Authorization: Bearer <key>` (OpenAI style) or `x-api-key: <key>` (Anthropic style);
  * any path is forwarded to llama-server and the reply is relayed chunk by chunk, so streamed replies stay live
    (novel.kcomfy.serve_auth_proxy buffers whole replies and is not suitable);
  * the idle clock is reset only by inference routes (INFERENCE_ROUTES) and while one is in flight; status pings and
    anything else do not keep the server alive;
  * `/__kllm/status` (GET) reports uptime, idle time and the model/context; `/__kllm/stop` asks the notebook to shut down;
  * optional padding for streamed requests (`pad` bytes, off by default; header `x-kllm-pad: N` overrides per request):
    the proxy sends the headers and that many bytes of SSE comment lines at once, then relays the real stream, and sends
    a comment line every KEEPALIVE_S while llama-server is silent (a long prompt read; a quick tunnel cuts a silent reply
    after ~120 s). A smoke run through a real Cloudflare quick tunnel showed streamed replies arriving live WITHOUT
    padding, so it is off. With padding on, an upstream error arrives as an SSE `data:` line inside a 200 reply,
    because the status was already sent.

The key is never logged, never forwarded to llama-server and never sent to the address relay.
"""

from __future__ import annotations

import hmac
import http.client
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit

INFERENCE_ROUTES = frozenset({"/v1/chat/completions", "/v1/completions", "/v1/messages", "/completion"})
DEFAULT_PAD = 0                 # off: a smoke run measured replies arriving live through the tunnel without padding
KEEPALIVE_S = 20                # a comment line this often while llama-server is silent (padded streams)
NTFY_URL = "https://ntfy.sh"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "host"}
DROP = HOP | {"authorization", "x-api-key", "x-kllm-pad", "accept-encoding"}
UA = "novel-kllm/1"


class KllmError(Exception):
    """A request to the server or to the address relay failed."""


class ProxyState:
    """Clocks and counters shared between the proxy threads and the notebook's keep-alive loop."""

    def __init__(self, idle_minutes: float = 30, info: dict | None = None, pad: int = DEFAULT_PAD):
        self.t0 = self._last = time.time()
        self.idle_minutes, self.pad = float(idle_minutes), int(pad)
        self.info = dict(info or {})
        self.inflight = self.requests = 0
        self.stop_event = threading.Event()
        self._lock = threading.Lock()

    def touch(self) -> None:
        self._last = time.time()

    def begin(self) -> None:
        with self._lock:
            self.inflight += 1
            self.requests += 1
        self.touch()

    def end(self) -> None:
        with self._lock:
            self.inflight -= 1
        self.touch()

    def idle_seconds(self) -> float:
        return 0.0 if self.inflight > 0 else max(time.time() - self._last, 0.0)

    def expired(self) -> bool:
        return self.idle_seconds() >= self.idle_minutes * 60

    def snapshot(self) -> dict:
        return dict(self.info, ok=True, uptime_s=round(time.time() - self.t0), idle_s=round(self.idle_seconds()),
                    idle_minutes=self.idle_minutes, inflight=self.inflight, requests=self.requests,
                    stopping=self.stop_event.is_set(), pad=self.pad)


def _wants_stream(body: bytes) -> bool:
    try:
        return bool(json.loads(body.decode("utf-8")).get("stream"))
    except (ValueError, AttributeError, UnicodeDecodeError):
        return False


def make_handler(state: ProxyState, key: str, up_host: str, up_port: int, upstream_timeout: float):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):          # never log: request lines can carry prompts
            pass

        # ---- small helpers
        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _authorised(self) -> bool:
            got = self.headers.get("x-api-key") or ""
            auth = self.headers.get("Authorization") or ""
            if auth[:7].lower() == "bearer ":
                got = got or auth[7:].strip()
                if hmac.compare_digest(auth[7:].strip().encode(), key.encode()):
                    return True
            return bool(got) and hmac.compare_digest(got.encode(), key.encode())

        def _read_body(self) -> bytes:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                out = b""
                while True:
                    size = int((self.rfile.readline() or b"0").split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        self.rfile.readline()
                        return out
                    out += self.rfile.read(size)
                    self.rfile.readline()
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _chunk(self, data: bytes) -> None:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()

        def _upstream_headers(self, body: bytes) -> dict:
            h = {k: v for k, v in self.headers.items() if k.lower() not in DROP}
            h["Accept-Encoding"] = "identity"
            if body or self.command in ("POST", "PUT", "PATCH"):
                h["Content-Length"] = str(len(body))
            return h

        # ---- the one handler for every method
        def handle_any(self):
            path = urlsplit(self.path).path
            if not self._authorised():
                self.close_connection = True
                return self._json({"error": {"message": "missing or wrong API key", "type": "auth"}}, 401)
            if path == "/__kllm/status":
                return self._json(state.snapshot())
            if path == "/__kllm/stop":
                state.stop_event.set()
                return self._json({"stopping": True})
            try:
                body = self._read_body()
            except (ValueError, OSError):
                self.close_connection = True
                return self._json({"error": {"message": "bad request body"}}, 400)
            infer = self.command == "POST" and path in INFERENCE_ROUTES
            if infer:
                state.begin()
            try:
                pad = state.pad
                if self.headers.get("x-kllm-pad", "").strip().isdigit():
                    pad = int(self.headers["x-kllm-pad"])
                if infer and pad > 0 and _wants_stream(body):
                    self._padded(body, pad)
                else:
                    self._plain(body, infer)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True
            finally:
                if infer:
                    state.end()

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = handle_any

        # ---- relaying
        def _connect(self):
            return http.client.HTTPConnection(up_host, up_port, timeout=upstream_timeout)

        def _plain(self, body: bytes, infer: bool) -> None:
            conn = self._connect()
            try:
                try:
                    conn.request(self.command, self.path, body=body or None, headers=self._upstream_headers(body))
                    resp = conn.getresponse()
                except (OSError, http.client.HTTPException) as e:
                    return self._json({"error": {"message": f"llama-server does not answer: {e}"}}, 502)
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                if resp.getheader("Content-Length") is not None:
                    data = resp.read()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    data = resp.read1(65536)
                    if not data:
                        break
                    self._chunk(data)
                    if infer:
                        state.touch()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            finally:
                conn.close()

        def _padded(self, body: bytes, pad: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            left = pad
            while left > 0:
                n = min(left, 8192)
                self._chunk((b": " + b"." * max(n - 4, 0) + b"\n\n")[:n] if n > 4 else b":\n\n")
                left -= n
            q: queue.Queue = queue.Queue()
            conn = self._connect()

            def pump():
                try:
                    conn.request(self.command, self.path, body=body, headers=self._upstream_headers(body))
                    resp = conn.getresponse()
                    if resp.status != 200:
                        q.put(("err", resp.status, resp.read()[:4000]))
                        return
                    while True:
                        data = resp.read1(65536)
                        if not data:
                            break
                        q.put(("data", data))
                    q.put(("end",))
                except (OSError, http.client.HTTPException) as e:
                    q.put(("err", 502, f"llama-server does not answer: {e}".encode()))

            threading.Thread(target=pump, daemon=True).start()
            try:
                while True:
                    try:
                        item = q.get(timeout=KEEPALIVE_S)
                    except queue.Empty:
                        self._chunk(b": keepalive\n\n")
                        continue
                    if item[0] == "data":
                        self._chunk(item[1])
                        state.touch()
                    elif item[0] == "err":
                        text = " ".join(item[2].decode("utf-8", "replace").split())
                        self._chunk(f"data: {text}\n\n".encode())
                        break
                    else:
                        break
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            finally:
                conn.close()        # a client that went away cancels the generation: llama-server sees the close

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def serve_llm_proxy(upstream_port: int, key: str, state: ProxyState | None = None, host: str = "127.0.0.1",
                    port: int = 0, upstream_host: str = "127.0.0.1", upstream_timeout: float = 3600) -> _Server:
    """Start the proxy in a background thread and return the server (`.server_address[1]` is the port, `.state` the
    ProxyState; `.shutdown()` stops it)."""
    if not key or len(key) < 16:
        raise ValueError("the API key must be at least 16 characters")
    state = state or ProxyState()
    srv = _Server((host, port), make_handler(state, key, upstream_host, upstream_port, upstream_timeout))
    srv.state = state
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- the PC's calls to the server

def call(url: str, key: str, path: str, method: str = "GET", body: dict | None = None, timeout: float = 20) -> dict:
    """One JSON call to the server through the address; raises KllmError."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise KllmError(f"HTTP {e.code} from {url}{path}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise KllmError(f"no answer from {url}: {getattr(e, 'reason', e)}") from None
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        raise KllmError(f"{url}{path} did not return JSON") from None


def server_status(url: str, key: str, timeout: float = 20) -> dict:
    return call(url, key, "/__kllm/status", timeout=timeout)


def request_stop(url: str, key: str, timeout: float = 20) -> dict:
    return call(url, key, "/__kllm/stop", "POST", {}, timeout=timeout)


# ---------------------------------------------------------------- the address relay (ntfy.sh)
# Kaggle shows no log of a running notebook, so the kernel posts its tunnel address to a topic only this run knows
# (128 random bits) and the PC polls it. Only the address and a few numbers travel; never the key.

def ntfy_base() -> str:
    return (os.environ.get("NOVEL_KLLM_NTFY_URL") or NTFY_URL).rstrip("/")


def relay_post(topic: str, payload: dict, timeout: float = 20) -> None:
    req = urllib.request.Request(f"{ntfy_base()}/{quote(topic)}", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "text/plain", "User-Agent": UA})
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
    except (urllib.error.URLError, OSError) as e:
        raise KllmError(f"address relay not reachable: {getattr(e, 'reason', e)}") from None


def relay_fetch(topic: str, timeout: float = 20) -> list[dict]:
    """Every message posted to the topic so far, oldest first, each parsed as a JSON object (others are skipped)."""
    req = urllib.request.Request(f"{ntfy_base()}/{quote(topic)}/json?poll=1&since=all", headers={"User-Agent": UA})
    try:
        text = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise KllmError(f"address relay not reachable: {getattr(e, 'reason', e)}") from None
    out = []
    for line in text.splitlines():
        try:
            ev = json.loads(line)
            msg = json.loads(ev["message"]) if ev.get("event") == "message" else None
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(msg, dict):
            out.append(msg)
    return out
