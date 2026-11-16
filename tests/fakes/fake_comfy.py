"""A tiny in-process stand-in for ComfyUI's HTTP API: just what novel.kcomfy calls.

    with FakeComfy(user="u", password="p", fail_first=2) as fc:
        Comfy(fc.url, "u", "p").generate(...)
        fc.requests   # every request seen: dicts with method, path, headers, body

Routes: GET /system_stats, POST /upload/image, POST /prompt, GET /history[/<id>], GET /view, GET /queue.
Knobs (attributes, may be changed while running):
    user/password      require HTTP basic auth (401 otherwise)
    fail_first         answer the first N requests with 503
    reject             /prompt answers 400 with node_errors
    exec_error         the prompt ends in an execution error in /history
    pending_polls      /history/<id> is empty for this many polls before the result appears
    exec_error_if      optional callable(graph) -> bool: only prompts it accepts end in an execution error
A submitted graph is checked: every LoadImage image must be an uploaded name, every link must point at an
existing node. The picture is a real JPEG (Pillow) of the EmptyFlux2LatentImage size, noisy so it is not tiny.
"""

from __future__ import annotations

import base64
import io
import json
import random
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image


def make_jpeg(width: int, height: int, seed: int = 0) -> bytes:
    rnd = random.Random(seed)
    img = Image.effect_noise((width, height), 60).convert("RGB")
    tint = Image.new("RGB", (width, height), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    img = Image.blend(img, tint, 0.4)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _parse_multipart(body: bytes, ctype: str) -> dict:
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
    out = {}
    for part in body.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        head_s = head.decode("utf-8", "replace")
        name = head_s.split('name="', 1)[1].split('"', 1)[0]
        if 'filename="' in head_s:
            out[name] = (head_s.split('filename="', 1)[1].split('"', 1)[0], data)
        else:
            out[name] = data.decode()
    return out


class FakeComfy:
    def __init__(self, user: str | None = None, password: str | None = None, fail_first: int = 0,
                 reject: bool = False, exec_error: bool = False, pending_polls: int = 1):
        self.user, self.password = user, password
        self.fail_first = fail_first
        self.reject = reject
        self.exec_error = exec_error
        self.pending_polls = pending_polls
        self.exec_error_if = None
        self.requests: list[dict] = []
        self.uploads: dict[str, bytes] = {}
        self.prompts: dict[str, dict] = {}       # id -> {"graph", "polls", "image"}
        self.outputs: dict[str, bytes] = {}      # filename -> bytes
        self._count = 0
        self._lock = threading.Lock()
        self.server = None

    # -- lifecycle
    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                fake._handle(self)

            def do_POST(self):
                fake._handle(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    # -- helpers
    def paths(self, method: str | None = None) -> list[str]:
        return [r["path"] for r in self.requests if method is None or r["method"] == method]

    @staticmethod
    def _send(h, code: int, body, ctype: str = "application/json", extra: dict | None = None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def _handle(self, h):
        n = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(n) if n else b""
        with self._lock:
            self._count += 1
            count = self._count
            self.requests.append({"method": h.command, "path": h.path, "headers": dict(h.headers.items()),
                                  "body": body})
        if count <= self.fail_first:
            return self._send(h, 503, b"tunnel hiccup", "text/plain")
        if self.user is not None:
            want = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            if h.headers.get("Authorization") != want:
                return self._send(h, 401, b"unauthorized", "text/plain", {"WWW-Authenticate": 'Basic realm="x"'})
        url = urllib.parse.urlsplit(h.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        if h.command == "GET" and path == "/system_stats":
            return self._send(h, 200, {"system": {"comfyui_version": "fake"}, "devices": []})
        if h.command == "POST" and path == "/upload/image":
            form = _parse_multipart(body, h.headers.get("Content-Type", ""))
            fname, data = form["image"]
            self.uploads[fname] = data
            return self._send(h, 200, {"name": fname, "subfolder": "", "type": form.get("type", "input")})
        if h.command == "POST" and path == "/prompt":
            return self._prompt(h, json.loads(body))
        if h.command == "GET" and path.startswith("/history/"):
            return self._history(h, path.rsplit("/", 1)[1])
        if h.command == "GET" and path == "/history":
            return self._send(h, 200, {k: {} for k in self.prompts})
        if h.command == "GET" and path == "/view":
            fn = query.get("filename", [""])[0]
            if fn not in self.outputs:
                return self._send(h, 404, b"no such file", "text/plain")
            return self._send(h, 200, self.outputs[fn], "image/jpeg")
        if h.command == "GET" and path == "/queue":
            return self._send(h, 200, {"queue_running": [], "queue_pending": []})
        return self._send(h, 404, b"not found", "text/plain")

    def _prompt(self, h, payload: dict):
        graph = payload["prompt"]
        node_errors = {}
        for nid, node in graph.items():
            errs = []
            for key, val in node.get("inputs", {}).items():
                if isinstance(val, list) and len(val) == 2 and str(val[0]) not in graph:
                    errs.append({"message": "link to missing node", "details": f"{key} -> {val[0]}"})
            if node.get("class_type") == "LoadImage" and node["inputs"].get("image") not in self.uploads:
                errs.append({"message": "Invalid image file", "details": f"image: {node['inputs'].get('image')}"})
            if errs:
                node_errors[nid] = {"errors": errs, "class_type": node.get("class_type")}
        if self.reject and not node_errors:
            node_errors["1"] = {"errors": [{"message": "Value not in list",
                                            "details": "unet_name: 'missing.safetensors' not in []"}],
                                "class_type": "UNETLoader"}
        if node_errors:
            return self._send(h, 400, {"error": {"type": "prompt_outputs_failed_validation",
                                                 "message": "Prompt outputs failed validation", "details": ""},
                                       "node_errors": node_errors})
        pid = payload.get("prompt_id") or f"p{len(self.prompts) + 1}"
        self.prompts[pid] = {"graph": graph, "polls": 0}
        return self._send(h, 200, {"prompt_id": pid, "number": len(self.prompts), "node_errors": {}})

    def _history(self, h, pid: str):
        p = self.prompts.get(pid)
        if p is None:
            return self._send(h, 200, {})
        p["polls"] += 1
        if p["polls"] <= self.pending_polls:
            return self._send(h, 200, {})
        if self.exec_error or (self.exec_error_if and self.exec_error_if(p["graph"])):
            return self._send(h, 200, {pid: {"outputs": {}, "status": {
                "status_str": "error", "completed": False,
                "messages": [["execution_start", {}], ["execution_error", {
                    "node_id": "7", "node_type": "KSampler", "exception_type": "torch.OutOfMemoryError",
                    "exception_message": "CUDA out of memory"}]]}}})
        graph = p["graph"]
        lat = next(n for n in graph.values() if n["class_type"] == "EmptyFlux2LatentImage")["inputs"]
        save_id, save = next((k, n) for k, n in graph.items() if n["class_type"] == "SaveImage")
        fname = f"{save['inputs']['filename_prefix']}_00001_.jpg"
        if fname not in self.outputs:
            seed = next(n for n in graph.values() if n["class_type"] == "KSampler")["inputs"]["seed"]
            self.outputs[fname] = make_jpeg(lat["width"], lat["height"], seed)
        return self._send(h, 200, {pid: {"outputs": {save_id: {"images": [
            {"filename": fname, "subfolder": "", "type": "output"}]}},
            "status": {"status_str": "success", "completed": True, "messages": []}}})
