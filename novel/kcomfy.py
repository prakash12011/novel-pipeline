"""A small ComfyUI client for the FLUX.3 [klein] trial, plus the workflow graph it submits.

Standard library only: the Kaggle notebook (kaggle/comfy_server.ipynb) copies this file next to itself and
imports it, so the probe there runs exactly the graph the local `novel ktrial run` sends.

Sources for every model-specific constant below (read 2026-11-16):
  - https://huggingface.co/black-forest-labs/flux-3-image-klein-comfy (model card, file list via the HF tree API)
  - https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/image_flux3_klein_distilled_image_edit.json
  - https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/image_flux3_klein_distilled_t2i.json
  - https://docs.comfy.org/tutorials/flux/flux-3-klein
  - ComfyUI v0.45.0 source: comfy_extras/nodes_edit_model.py (ReferenceMultiLatent), comfy_extras/nodes_flux.py
    (EmptyFlux2LatentImage), nodes.py (CLIPLoader type "flux3"), comfy_api/latest/_io.py (dotted names of
    DynamicCombo / Autogrow inputs in the API format), server.py (/prompt, /upload/image, /history, /view).

The prompt upsampler (qwen3.5_9b_flux3_prompt_upsampler_*) is deliberately not part of this graph: it is 6.7-18.8 GB
and rewrites the prompt. Our prompts are written as full captions, which is the case the model card allows it off.
"""

from __future__ import annotations

import base64
import http.client
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- versions and files

COMFY_TAG = "v0.45.0"           # ComfyUI release tag (2026-11-10). FLUX.3 support and ReferenceMultiLatent landed in
                                # v0.41.0; docs.comfy.org says "ComfyUI 0.41.0 or later".
HF_REPO = "black-forest-labs/flux-3-image-klein-comfy"   # gated: accept the FLUX Kommunity License first

# (path in the HF repo == path under ComfyUI/models, size in bytes from the HF tree API)
MODEL_FILES = [
    ("diffusion_models/flux3-image-klein-int8-convrot.safetensors", 5827874779),   # distilled, INT8 ConvRot
    ("text_encoders/qwen3vl_4b_int8_convrot.safetensors", 4858559240),            # Qwen3-VL 4B text encoder
    ("vae/flux2-vae.safetensors", 336211292),
]

UNET_NAME = "flux3-image-klein-int8-convrot.safetensors"
CLIP_NAME = "qwen3vl_4b_int8_convrot.safetensors"
CLIP_TYPE = "flux3"
VAE_NAME = "flux2-vae.safetensors"

# ---------------------------------------------------------------- sampling (the distilled templates' KSampler)

STEPS = 16                      # model card: "Klein (distilled): 16 steps, no guidance". 10 was tried for speed
                                # (33 s, not 54 s on a T4) and the chapter came out with too many bad hands.
CFG = 1.0
SAMPLER = "euler"
SCHEDULER = "simple"
DENOISE = 1.0
REF_MEGAPIXELS = 0.25           # template uses 1.0; on a T4 two references then took 135 s a picture. At
                                # 0.25 the face, clothes and place held just as well in 54 s (16 steps).
MAX_REFS = 3                    # our limit; the model takes up to 10
SIZE_STEP = 16                  # EmptyFlux2LatentImage width/height step


def ref_token(i: int) -> str:
    """How the prompt names the i-th reference (0-based): `<ref_image_0>`, `<ref_image_1>`, ... in the order the
    images are connected to ReferenceMultiLatent (model card, template note, docs.comfy.org)."""
    return f"<ref_image_{i}>"


# UNVERIFIED: whether the INT8 ConvRot build runs well on a Turing T4. ComfyUI v0.45.0 lists only bf16/fp32 as
# FLUX.3 inference dtypes (supported_models.Flux3) and the T4 has no native bf16; int8 matmul goes through
# torch._int_mm. The notebook's probe answers it. If the probe is too slow, try ["--fp16-unet"] (fp16 is NOT a listed
# FLUX.3 dtype: watch for black pictures, which the probe also catches). The notebook takes session["comfy_args"]
# over this default, so a retry needs no code change.
COMFY_LAUNCH_ARGS: list[str] = []

STOP_NAME = "KTRIAL_STOP.png"


class ComfyError(RuntimeError):
    """Anything that went wrong talking to ComfyUI, or a picture it could not make."""


# ---------------------------------------------------------------- the graph

def build_workflow(prompt: str, seed: int, width: int, height: int, refs: list[str], prefix: str) -> dict:
    """The API-format graph for one picture. `refs` are image names as returned by ComfyUI's upload (0..3).

    Text-to-image follows image_flux3_klein_distilled_t2i (empty negative); with references it follows
    image_flux3_klein_distilled_image_edit: the negative is the zeroed positive, every reference goes into both
    through ReferenceMultiLatent, and the size comes from EmptyFlux2LatentImage, as that template's note says
    ("Use `Empty Flux 2 Latent` node to customize image size")."""
    refs = list(refs or [])
    if len(refs) > MAX_REFS:
        raise ValueError(f"at most {MAX_REFS} reference images, got {len(refs)}")
    if width % SIZE_STEP or height % SIZE_STEP or width <= 0 or height <= 0:
        raise ValueError(f"width and height must be positive multiples of {SIZE_STEP}, got {width}x{height}")
    if not 0 <= int(seed) <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"seed out of range: {seed}")

    g: dict = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_NAME, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_NAME, "type": CLIP_TYPE, "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
    }
    if refs:
        g["5"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}}
        multi = {"positive": ["4", 0], "negative": ["5", 0], "vae": ["3", 0],
                 "resolution": "resize", "resolution.megapixels": REF_MEGAPIXELS}
        for i, name in enumerate(refs):
            nid = str(20 + i)
            g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
            multi[f"images.image{i}"] = [nid, 0]
        g["10"] = {"class_type": "ReferenceMultiLatent", "inputs": multi}
        positive, negative = ["10", 0], ["10", 1]
    else:
        g["5"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}}
        positive, negative = ["4", 0], ["5", 0]

    g["7"] = {"class_type": "KSampler", "inputs": {
        "seed": int(seed), "steps": STEPS, "cfg": CFG, "sampler_name": SAMPLER, "scheduler": SCHEDULER,
        "denoise": DENOISE, "model": ["1", 0], "positive": positive, "negative": negative,
        "latent_image": ["6", 0]}}
    g["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}}
    g["9"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["8", 0]}}
    return g


SAVE_NODE = "9"


# ---------------------------------------------------------------- the client

RETRY_STATUS = (502, 503, 504)


class Comfy:
    """ComfyUI over HTTP. Every request carries basic auth when a user is given, and is retried with backoff on
    connection errors and 502/503/504 (tunnels drop). A 401 is never retried."""

    def __init__(self, base_url: str, user: str | None = None, password: str | None = None,
                 timeout: float = 60.0, retries: int = 5, backoff: float = 1.0, poll: float = 2.0):
        self.base = base_url.rstrip("/")
        self.poll = poll                    # seconds between /history polls in generate()
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.headers = {
            "User-Agent": "kcomfy/1",
            "ngrok-skip-browser-warning": "1",     # ngrok free plan: skip the browser interstitial
        }
        if user is not None:
            token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
            self.headers["Authorization"] = "Basic " + token

    # -- transport

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                 timeout: float | None = None) -> bytes:
        url = self.base + path
        hdrs = dict(self.headers, **(headers or {}))
        last = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff * 2 ** (attempt - 1))
            req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                text = _read_err(e)
                if e.code in RETRY_STATUS:
                    last = f"HTTP {e.code} from {method} {path}"
                    continue
                if e.code == 401:
                    raise ComfyError(f"401 Unauthorized from {method} {path}: wrong or missing user/password") from None
                raise _HTTPStatus(e.code, text, f"HTTP {e.code} from {method} {path}: {text[:2000]}") from None
            except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError,
                    http.client.HTTPException) as e:
                last = f"{type(e).__name__}: {getattr(e, 'reason', e)} on {method} {path}"
                continue
        raise ComfyError(f"gave up after {self.retries + 1} tries: {last}")

    def _json(self, method: str, path: str, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        hdrs = {"Content-Type": "application/json"} if body is not None else None
        raw = self._request(method, path, body, hdrs)
        try:
            return json.loads(raw or b"null")
        except ValueError:
            raise ComfyError(f"{method} {path} did not return JSON: {raw[:200]!r}") from None

    # -- API

    def ping(self) -> dict:
        """GET /system_stats. Raises ComfyError when the server does not answer."""
        return self._json("GET", "/system_stats")

    def upload_image(self, path, name: str | None = None) -> str:
        """POST /upload/image (overwrite=true) and return the name ComfyUI stored it under."""
        p = Path(path)
        return self.upload_bytes(p.read_bytes(), name or p.name)

    def upload_bytes(self, data: bytes, name: str) -> str:
        boundary = "----kcomfy" + uuid.uuid4().hex
        ctype = "image/png" if name.lower().endswith(".png") else "image/jpeg"
        parts = []
        for field, value in (("overwrite", "true"), ("type", "input")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        raw = self._request("POST", "/upload/image", b"".join(parts),
                            {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            out = json.loads(raw)
        except ValueError:
            raise ComfyError(f"upload of {name} did not return JSON: {raw[:200]!r}") from None
        sub = out.get("subfolder") or ""
        return f"{sub}/{out['name']}" if sub else out["name"]

    def queue(self, workflow: dict) -> str:
        """POST /prompt and return the prompt id. A rejected graph raises ComfyError carrying node_errors.

        The id is chosen here, so a retry after a dropped connection resubmits the same id."""
        pid = str(uuid.uuid4())
        try:
            out = self._json("POST", "/prompt", {"prompt": workflow, "client_id": "kcomfy", "prompt_id": pid})
        except _HTTPStatus as e:
            try:
                info = json.loads(e.body)
            except ValueError:
                raise ComfyError(str(e)) from None
            raise ComfyError("ComfyUI rejected the graph: " + _describe_rejection(info)) from None
        if out.get("node_errors"):
            raise ComfyError("ComfyUI rejected the graph: " + _describe_rejection(out))
        return out.get("prompt_id") or pid

    def wait(self, prompt_id: str, timeout: float, poll: float = 2.0) -> dict:
        """GET /history/<id> until the prompt is done; returns its history entry. Raises on error or timeout."""
        end = time.monotonic() + timeout
        while True:
            hist = self._json("GET", f"/history/{prompt_id}") or {}
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise ComfyError(f"prompt {prompt_id} failed: {_describe_exec_error(status)}")
                if status.get("completed", True) and entry.get("outputs"):
                    return entry
                if status.get("completed"):
                    raise ComfyError(f"prompt {prompt_id} finished without outputs: {json.dumps(status)[:500]}")
            if time.monotonic() >= end:
                raise ComfyError(f"prompt {prompt_id} not done after {timeout:.0f}s")
            time.sleep(poll)

    def fetch(self, image_info: dict) -> bytes:
        """GET /view for one entry of a history entry's outputs[...]["images"]."""
        q = urllib.parse.urlencode({"filename": image_info["filename"],
                                    "subfolder": image_info.get("subfolder", ""),
                                    "type": image_info.get("type", "output")})
        return self._request("GET", "/view?" + q)

    def queue_size(self) -> int:
        """Running plus pending prompts (GET /queue)."""
        q = self._json("GET", "/queue") or {}
        return len(q.get("queue_running") or []) + len(q.get("queue_pending") or [])

    def history_count(self) -> int:
        return len(self._json("GET", "/history") or {})

    def generate(self, prompt: str, seed: int, width: int, height: int, refs: list[str], prefix: str,
                 timeout: float = 600.0) -> tuple[bytes, float]:
        """One picture: queue, wait, fetch. Returns the image bytes (what SaveImage wrote, PNG on a real
        ComfyUI) and the seconds from queueing to done."""
        wf = build_workflow(prompt, seed, width, height, refs, prefix)
        t0 = time.monotonic()
        pid = self.queue(wf)
        entry = self.wait(pid, timeout, self.poll)
        took = time.monotonic() - t0
        images = (entry.get("outputs", {}).get(SAVE_NODE) or {}).get("images") or []
        if not images:
            for out in entry.get("outputs", {}).values():
                images = out.get("images") or []
                if images:
                    break
        if not images:
            raise ComfyError(f"prompt {pid} produced no image")
        return self.fetch(images[0]), took


class _HTTPStatus(ComfyError):
    def __init__(self, code: int, body: str, msg: str):
        super().__init__(msg)
        self.code = code
        self.body = body


def _read_err(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _describe_rejection(info: dict) -> str:
    bits = []
    err = info.get("error")
    if isinstance(err, dict):
        bits.append(f"{err.get('type', '')}: {err.get('message', '')} {err.get('details', '')}".strip())
    elif err:
        bits.append(str(err))
    for nid, ne in (info.get("node_errors") or {}).items():
        for e in ne.get("errors") or []:
            bits.append(f"node {nid} ({ne.get('class_type', '?')}): {e.get('message', '')} {e.get('details', '')}".strip())
    return "; ".join(bits) or json.dumps(info)[:1000]


def _describe_exec_error(status: dict) -> str:
    for msg in status.get("messages") or []:
        if isinstance(msg, (list, tuple)) and len(msg) == 2 and msg[0] == "execution_error":
            d = msg[1] or {}
            return f"{d.get('node_type', '?')} (node {d.get('node_id', '?')}): " \
                   f"{d.get('exception_type', '')}: {d.get('exception_message', '')}".strip()
    return json.dumps(status)[:500]


# ---------------------------------------------------------------- stopping the server from the local side

def tiny_png() -> bytes:
    """A valid 1x1 grey PNG, standard library only."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)          # 1x1, 8-bit greyscale
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b"")


def request_stop(comfy: Comfy) -> str:
    """Upload KTRIAL_STOP.png; the notebook's keep-alive loop sees it in ComfyUI/input/ and shuts down."""
    return comfy.upload_bytes(tiny_png(), STOP_NAME)


# ---------------------------------------------------------------- basic-auth reverse proxy (runs on Kaggle)

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "host", "authorization", "content-length"}


def serve_auth_proxy(port: int, upstream: str, user: str, password: str, host: str = "127.0.0.1"):
    """Start a threaded reverse proxy on host:port that demands HTTP basic auth and forwards to `upstream`
    (e.g. http://127.0.0.1:8188). The tunnel (ngrok or cloudflared) points at this, so auth does not depend on
    the tunnel's plan. Returns the server; `server.last_request` is the time.time() of the last authorised
    request, `server.shutdown()` stops it. Runs in a daemon thread."""
    up = urllib.parse.urlsplit(upstream)
    want = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _proxy(self):
            if self.headers.get("Authorization", "") != want:
                body = b"unauthorized"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="ktrial"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            server.last_request = time.time()
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
            # ComfyUI checks Origin against Host for browser requests; drop Origin, we are not a browser.
            hdrs.pop("Origin", None)
            hdrs.pop("origin", None)
            try:
                conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=600)
                conn.request(self.command, self.path, body=body, headers=hdrs)
                r = conn.getresponse()
                data = r.read()
                status, rh = r.status, r.getheaders()
                conn.close()
            except Exception as e:  # upstream down: tell the client to retry
                data = f"upstream error: {e}".encode()
                status, rh = 502, [("Content-Type", "text/plain")]
            self.send_response(status)
            for k, v in rh:
                if k.lower() not in _HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _proxy

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.last_request = time.time()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
