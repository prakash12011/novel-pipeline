"""Stand-ins for llama-server and for ntfy.sh, on localhost, for tests of novel.kllm and novel kllm.

FakeLlama: GET /health -> 200; POST /v1/chat/completions, /v1/messages and /completion answer a canned reply.
  With "stream": true the reply is `events` SSE chunks, `delay` seconds apart (chunked), then `data: [DONE]`.
  `first_delay` waits before the first byte (a long prompt read). It records every request in `.requests`.
FakeNtfy: POST /<topic> stores the body, GET /<topic>/json?poll=1&since=all lists them (the shape ntfy.sh returns).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class _Quiet(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""


class _Base:
    def __init__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeLlama(_Base):
    def __init__(self, events: int = 5, delay: float = 0.05, first_delay: float = 0.0):
        self.events, self.delay, self.first_delay = events, delay, first_delay
        self.requests: list[dict] = []
        self.fail_status = 0                 # when set, every inference call answers this status
        super().__init__()

    def handler(self):
        fake = self

        class H(_Quiet):
            def do_GET(self):
                fake.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
                if urlsplit(self.path).path == "/health":
                    return self._send({"status": "ok"})
                self._send({"error": "not found"}, 404)

            def do_POST(self):
                raw = self._body()
                path = urlsplit(self.path).path
                fake.requests.append({"method": "POST", "path": path, "headers": dict(self.headers), "body": raw})
                if fake.fail_status:
                    return self._send({"error": {"message": "model is loading"}}, fake.fail_status)
                try:
                    stream = bool(json.loads(raw or b"{}").get("stream"))
                except ValueError:
                    stream = False
                if not stream:
                    return self._send({"id": "x", "choices": [{"message": {"role": "assistant", "content": "hello"}}]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    time.sleep(fake.first_delay)
                    for i in range(fake.events):
                        data = ("data: " + json.dumps({"i": i, "t": time.time(), "choices": [
                            {"delta": {"content": f"tok{i} "}}]}) + "\n\n").encode()
                        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                        self.wfile.flush()
                        time.sleep(fake.delay)
                    end = b"data: [DONE]\n\n"
                    self.wfile.write(b"%x\r\n" % len(end) + end + b"\r\n0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    fake.requests.append({"method": "ABORT", "path": path})
                self.close_connection = True

        return H


class FakeNtfy(_Base):
    def __init__(self):
        self.topics: dict[str, list[str]] = {}
        super().__init__()

    def handler(self):
        fake = self

        class H(_Quiet):
            def do_POST(self):
                topic = urlsplit(self.path).path.strip("/")
                fake.topics.setdefault(topic, []).append(self._body().decode("utf-8"))
                self._send({"id": "m", "event": "message"})

            def do_GET(self):
                topic = urlsplit(self.path).path.strip("/").split("/")[0]
                lines = [json.dumps({"id": f"m{i}", "time": 0, "event": "message", "topic": topic, "message": m})
                         for i, m in enumerate(fake.topics.get(topic, []))]
                self._send(("\n".join(lines) + "\n").encode() if lines else b"")

        return H
