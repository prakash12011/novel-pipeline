"""A small client for the OpenRouter image API, used by `novel ktrial run --via openrouter` to make the trial pictures
with the SAME model (FLUX.3 [klein]) as the Kaggle ComfyUI server, but hosted and at the provider's own settings.

Standard library + Pillow only. The API key is never printed, logged or put in a URL; it is also scrubbed from every
error text.

What the documentation confirms (read 2026-11-16) and what is not confirmed:

  * POST {base}/images with `model, prompt, n, aspect_ratio, resolution, seed, output_format, input_references`;
    references are `{"type": "image_url", "image_url": {"url": <https URL or base64 data URL>}}`; the answer is
    `{"data": [{"b64_json", "media_type"}], "usage": {"cost"}}` (USD).
        https://openrouter.ai/docs/guides/overview/multimodal/image-generation
  * For black-forest-labs/flux-3-image-klein the endpoint list says: `seed` supported, `output_format` jpeg|png,
    `n` exactly 1, `input_references` 0..10, resolution 768|1K|1.5K|2K|4K, aspect ratios incl. 16:9, the only
    passthrough parameters are `safety_tolerance` and `steps`, no guidance; price per picture 768 $0.008,
    1K $0.009, 1.5K $0.011, 2K $0.014, 4K $0.036.
        https://openrouter.ai/api/v1/images/models/black-forest-labs/flux-3-image-klein/endpoints
        https://openrouter.ai/black-forest-labs/flux-3-image-klein
  * References in a prompt: neither OpenRouter nor the BFL pages define a syntax, but the BFL prompting guide writes
    plain numbered labels ("subject from image 1, style from image 2"), 1-based. The earlier OpenRouter test used the
    same wording and it worked.
        https://docs.bfl.ai/guides/prompting_guide_flux2

Not sent: `steps` (UNVERIFIED: it is only listed as a passthrough name with no range, and klein is step-distilled
with 4 fixed steps, https://docs.bfl.ai/flux_2/flux2_overview), guidance (not accepted by this model), safety_tolerance.

Test hook: $NOVEL_OPENROUTER_URL replaces the base URL where the CLI builds its client (ktrial_run), so a test can
point it at a local fake. Never set it for real use.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "google/gemini-nano-banana-2.1"       # trial; was "black-forest-labs/flux-3-image-klein"
KEY_VAR = "OPENROUTER_API_KEY"
COST_PER_IMAGE = 0.014          # fallback estimate when the answer carries no cost (the 2K price)
RESOLUTION_PRICE = {"768": 0.008, "1K": 0.009, "1.5K": 0.011, "2K": 0.014, "4K": 0.036}     # USD, from the endpoint list
MAX_REFS = 10                   # `input_references` range of this model
REF_JPEG_QUALITY = 92
RETRY_STATUS = frozenset({403, 408, 429}) | frozenset(range(500, 600))     # 403 was seen once, and succeeded on retry
USER_AGENT = "novel-ktrial/1.0"
# The key file of the earlier OpenRouter test (tools/pretest_images.py reads `<project>/.env`, KEY=VALUE lines).
EARLIER_TEST_ENV = Path(r"D:\Automations\audiobook\.env")

_sleep = time.sleep             # replaced in tests


class RouterError(Exception):
    """A request that failed for good (a 4xx answer, retries used up, or an answer that is not a picture)."""


def estimate(resolution: str = "1K") -> float:
    """The price of one picture at this tier (what the cost guard counts before the real cost is known)."""
    return RESOLUTION_PRICE.get(str(resolution), COST_PER_IMAGE)


def hosted_text(prompt: str) -> str:
    """`<ref_image_0>` (ComfyUI's, 0-based) -> `image 1` (the hosted API's wording, 1-based)."""
    return re.sub(r"<ref_image_(\d+)>", lambda m: f"image {int(m.group(1)) + 1}", prompt)


# ---------------------------------------------------------------- the key

def _env_file_value(path: Path) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k.lower().startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k == KEY_VAR:
            return v.strip()
    return ""


def key_places() -> list[Path]:
    """The files looked in after the environment: this project's secrets.env, then the earlier test's .env."""
    from novel.core import paths
    return [paths.secrets_file(), EARLIER_TEST_ENV]


def find_key() -> str:
    """$OPENROUTER_API_KEY, then OPENROUTER_API_KEY=... in <novel home>/secrets.env, then in the earlier test's .env."""
    key = os.environ.get(KEY_VAR, "").strip()
    if key:
        return key
    places = key_places()
    for f in places:
        key = _env_file_value(f)
        if key:
            return key
    raise RouterError(f"no OpenRouter key: set the environment variable {KEY_VAR}, or add a line {KEY_VAR}=... to "
                      + " or ".join(str(p) for p in places))


# ---------------------------------------------------------------- the client

def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key and len(key) >= 6 else text


def _trim(text: str, n: int = 500) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "..."


def _data_url(ref) -> str:
    """A reference as a JPEG data URL. Decoded and re-encoded with Pillow, as the earlier test did, so a WebP or PNG
    saved under a .jpg name is still sent as what it says it is."""
    try:
        src = io.BytesIO(ref) if isinstance(ref, (bytes, bytearray)) else Path(ref)
        with Image.open(src) as im:
            im.load()
            rgb = im.convert("RGB")
    except (OSError, ValueError) as e:
        raise RouterError(f"a reference picture cannot be read: {e}") from None
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=REF_JPEG_QUALITY)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class OpenRouter:
    def __init__(self, key: str, base_url: str = BASE_URL, timeout: float = 300, retries: int = 4,
                 backoff: float = 2.0):
        if not key:
            raise RouterError("an OpenRouter key is needed")
        self._key = key
        self.base_url = base_url.rstrip("/")
        self.timeout, self.retries, self.backoff = timeout, retries, backoff

    def __repr__(self) -> str:                       # never shows the key
        return f"OpenRouter({self.base_url!r})"

    def _wait(self, attempt: int, retry_after: str | None = None) -> None:
        delay = self.backoff * (2 ** attempt)
        try:
            if retry_after:
                delay = max(delay, min(float(retry_after), 60.0))
        except ValueError:
            pass
        _sleep(delay)

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        last = "no attempt was made"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                f"{self.base_url}/images", data=body, method="POST",
                headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                         "Accept": "application/json", "User-Agent": USER_AGENT})
            retry_after = None
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    raise RouterError(f"OpenRouter answered with something that is not JSON: "
                                      f"{_scrub(_trim(raw[:300].decode('utf-8', 'replace')), self._key)}") from None
            except urllib.error.HTTPError as e:
                text = _scrub(_trim(e.read().decode("utf-8", "replace")), self._key)
                if e.code not in RETRY_STATUS:
                    raise RouterError(f"OpenRouter answered HTTP {e.code}: {text}") from None
                last = f"HTTP {e.code}: {text}"
                retry_after = e.headers.get("Retry-After") if e.headers else None
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                last = f"{type(e).__name__}: {_scrub(_trim(getattr(e, 'reason', e)), self._key)}"
            if attempt < self.retries:
                self._wait(attempt, retry_after)
        raise RouterError(f"OpenRouter did not answer after {self.retries + 1} tries; last: {last}")

    def _download(self, url: str) -> bytes:
        """A picture given as a link. The key is NOT sent to it."""
        last = ""
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code not in RETRY_STATUS:
                    raise RouterError(f"the picture link answered HTTP {e.code}") from None
                last = f"HTTP {e.code}"
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                last = type(e).__name__
            if attempt < self.retries:
                self._wait(attempt)
        raise RouterError(f"the picture could not be downloaded after {self.retries + 1} tries; last: {last}")

    def generate(self, prompt: str, refs: list, *, aspect_ratio: str = "16:9", resolution: str = "1K",
                 seed: int | None = None, output_format: str = "png") -> tuple[bytes, float, dict]:
        """One picture. `refs` are bytes or paths, sent in order as `input_references` (the prompt calls them
        image 1, image 2, ...; see hosted_text). Returns (picture bytes, seconds, info)."""
        if len(refs) > MAX_REFS:
            raise RouterError(f"{len(refs)} references: this model takes at most {MAX_REFS}")
        payload: dict = {"model": MODEL, "prompt": prompt, "n": 1, "aspect_ratio": aspect_ratio,
                         "resolution": resolution, "output_format": output_format}
        if seed is not None:
            payload["seed"] = int(seed)       # UNVERIFIED: the accepted range (the endpoint list only says it is supported)
        if refs:
            payload["input_references"] = [{"type": "image_url", "image_url": {"url": _data_url(r)}} for r in refs]
        t0 = time.monotonic()
        answer = self._post(payload)
        try:
            item = answer["data"][0]
            if item.get("b64_json"):
                data = base64.b64decode(item["b64_json"], validate=True)
            elif str(item.get("url", "")).startswith("data:"):
                data = base64.b64decode(item["url"].split(",", 1)[1], validate=True)
            elif item.get("url"):
                data = self._download(item["url"])
            else:
                raise KeyError("b64_json")
            if not data:
                raise ValueError("empty picture")
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as e:
            raise RouterError(f"OpenRouter's answer holds no picture ({type(e).__name__}): "
                              f"{_scrub(_trim(json.dumps(answer)[:400]), self._key)}") from None
        took = time.monotonic() - t0
        usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        cost = usage.get("cost")
        info = {"cost": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                "usage": usage, "model": answer.get("model") or MODEL, "media_type": item.get("media_type"),
                "bytes": len(data)}
        try:
            with Image.open(io.BytesIO(data)) as im:
                info["width"], info["height"] = im.size
        except (OSError, ValueError):
            pass
        return data, took, info
