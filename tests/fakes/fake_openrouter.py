"""A tiny in-process stand-in for OpenRouter's image API: just what novel.korouter calls.

    with FakeOpenRouter(cost=0.009) as fo:
        OpenRouter("key", fo.url).generate("a cat", [])
        fo.requests    # every POST /images seen: dicts with headers, body (parsed JSON)

Knobs (attributes, may be changed while running):
    script        list of (status, body-dict-or-str) answered first, one per request, before normal answers
    mode          "b64" (default) or "url": how the picture comes back (url: a link to GET /pic/<n>.png on this server)
    cost          usage.cost in the answer (None: no usage block)
    size          (w, h) of the picture made
    fmt           "PNG" or "JPEG": the format of the picture bytes
    key           when set, requests without `Authorization: Bearer <key>` get 401
    retry_after   value of a Retry-After header on scripted 429 answers
    fail_if       optional callable(body) -> status int or None: answer that status with an error body
The picture is a real image (Pillow), noisy so it is not tiny.
"""

from __future__ import annotations

import base64
import io
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image


def make_image(width: int, height: int, fmt: str = "PNG", seed: int = 0) -> bytes:
    rnd = random.Random(seed)
    img = Image.effect_noise((width, height), 60).convert("RGB")
    tint = Image.new("RGB", (width, height), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    img = Image.blend(img, tint, 0.4)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


class FakeOpenRouter:
    def __init__(self, mode: str = "b64", cost: float | None = 0.009, size=(320, 192), fmt: str = "PNG",
                 key: str | None = None, script=None):
        self.mode, self.cost, self.size, self.fmt, self.key = mode, cost, size, fmt, key
        self.script = list(script or [])
        self.retry_after = None
        self.fail_if = None
        self.requests: list[dict] = []
        self.downloads: list[dict] = []
        self.pics: dict[str, bytes] = {}
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, ctype="application/json", extra=None):
                data = body if isinstance(body, bytes) else (
                    body.encode() if isinstance(body, str) else json.dumps(body).encode())
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                owner.downloads.append({"path": self.path, "headers": dict(self.headers)})
                pic = owner.pics.get(self.path)
                if pic is None:
                    return self._send(404, "no such picture")
                self._send(200, pic, "image/png")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = None
                with owner._lock:
                    owner.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                    scripted = owner.script.pop(0) if owner.script else None
                if self.path != "/images":
                    return self._send(404, "not found")
                if owner.key is not None and self.headers.get("Authorization") != f"Bearer {owner.key}":
                    return self._send(401, {"error": {"message": "no auth"}})
                if scripted is not None:
                    status, payload = scripted
                    extra = {"Retry-After": str(owner.retry_after)} if owner.retry_after is not None else None
                    return self._send(status, payload, extra=extra)
                if owner.fail_if is not None:
                    status = owner.fail_if(body)
                    if status:
                        return self._send(status, {"error": {"message": f"fake failure {status}"}})
                w, h = owner.size
                data = make_image(w, h, owner.fmt, seed=len(owner.requests))
                item: dict = {"media_type": "image/png" if owner.fmt == "PNG" else "image/jpeg"}
                if owner.mode == "url":
                    path = f"/pic/{len(owner.requests)}.png"
                    owner.pics[path] = data
                    item["url"] = f"{owner.url}{path}"
                else:
                    item["b64_json"] = base64.b64encode(data).decode()
                answer: dict = {"created": 1, "data": [item]}
                if owner.cost is not None:
                    answer["usage"] = {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1,
                                       "cost": owner.cost}
                self._send(200, answer)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *a):
        self.stop()

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()
