"""Picture QA through OpenCode Go: a panel of vision models checks each new picture, a referee settles a fail.

The flow (one picture):
  1. The panel (images.qa.panel, up to 3 models, asked at once) checks it against CHECKLIST. All pass -> "pass".
  2. Any panel fail -> the referee (images.qa.referee) checks it, told what the panel saw. Referee pass -> "kept".
  3. Referee fail -> one redraw with every issue the referee listed as a fix line, a new seed.
  4. The referee checks the redraw: pass -> "fixed", fail -> "failed" (recorded; the redraw stays).
No answer from any panel model -> "unchecked". At most 2 pictures per shot. In `novel images` this runs on each
picture as soon as it is drawn (Live), and the record file is the chapter's status list (states: see Live).

Every request is a fresh, single-turn call: the checklist, the picture's own prompt and the picture (scaled to
QA_WIDTH), nothing else; a new x-opencode-session id each time, so no context is carried from one call to the next.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

KEY_VAR = "OPENCODE_API_KEY"
BASE_URL = "https://opencode.ai/inference/go/v1"
USER_AGENT = "novel-pipeline-qa/1.0"     # Go asks direct clients for their own user agent, never a library's
QA_WIDTH = 1024                          # the long side sent: enough to see faces and hands, a third of the tokens
MAX_TOKENS = 3000                        # room for a reasoning model's thinking before the JSON
LIMIT_STATUS = (402, 403, 429)           # quota or allowance used up: the model sits out the rest of the run
OPENROUTER = "openrouter:"               # a model id with this prefix is asked through OpenRouter (its free models)
CLAUDE = "claude:"                       # ... through headless Claude Code on the subscription (`claude:claude-haiku-5-5`)
CLAUDE_MAX_TOKENS = 8000                 # room for Claude's thinking before the JSON
CLAUDE_TIMEOUT = 300                     # seconds for one picture check
OPENROUTER_URL = "https://openrouter.ai/api/v1"
KINDS = ("duplicate_person", "people_count", "anatomy", "face", "text", "prompt", "style")

CHECKLIST = """You check one illustration made by an image model for an anime-style YouTube story recap. You get the \
prompt it was drawn from (tokens like <image1> name the reference pictures it was given; ignore them) and the picture.

Fail it only for a clear, visible defect of these kinds:
- duplicate_person: the same named person drawn twice (same face, hair and clothes in two places), also when the \
second one is small, half hidden or in the background. Compare every figure with the named people.
- people_count: a named person of the prompt is missing, or clearly more people are in the foreground than it asks for.
- anatomy: extra, missing or fused limbs or fingers, broken hands, a twisted or merged body.
- face: a distorted face, white specks or blank white eyes, crossed eyes.
- text: letters, words, captions, speech bubbles, a watermark, borders, or several panels or views instead of one scene.
- prompt: the main action or setting is plainly not what the prompt says.
- style: photo-realistic or 3D instead of 2D anime.
Do not fail it for small style choices, background details or anything you are not sure of.

Answer with JSON only, no other text:
{"pass": true or false, "issues": [{"kind": "<one of the kinds>", "where": "<where in the picture>", \
"fix": "<one short instruction for the redraw>"}]}"""

REFEREE_NOTE = """Other checkers flagged the problems below. Look for yourself: keep the ones you can see, drop the \
ones you cannot, and add any other clear defect of the listed kinds. Pass it if none is really there.
Flagged: {flagged}"""


class QAError(RuntimeError):
    """A model gave no usable verdict (HTTP error, not JSON, ...)."""


class LimitReached(QAError):
    """The model's allowance is used up (or the key may not use it)."""


def find_key() -> str:
    """$OPENCODE_API_KEY, else OPENCODE_API_KEY=... in this project's secrets.env. Empty when neither."""
    from novel.core import secrets
    return os.environ.get(KEY_VAR, "").strip() or secrets.load().get(KEY_VAR, "").strip()


class Jpeg(bytes):
    """The scaled picture sent to the Go models; it remembers its file, which Claude (headless) reads in place."""
    path: Path | None = None


def small_jpeg(path: Path, width: int = QA_WIDTH) -> Jpeg:
    """The picture with its long side at most `width`, as JPEG."""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((width, width))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    out = Jpeg(buf.getvalue())
    out.path = Path(path)
    return out


def parse_verdict(text: str) -> dict:
    """{"pass": bool, "issues": [...]} from a model's answer: the first JSON object with a true/false "pass", after
    any <think>...</think> (some models think aloud, and some write the object twice)."""
    text = re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S)
    dec, d = json.JSONDecoder(), None
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("pass"), bool):
            d = obj
            break
    if d is None:
        raise QAError(f"no verdict JSON in the answer: {text[:120]!r}")
    issues = [i for i in d.get("issues") or [] if isinstance(i, dict)]
    ok = d["pass"] or not issues                                    # a fail must name what is wrong
    return {"pass": ok, "issues": [] if ok else issues}


def _post(base: str, key: str, body: dict, timeout: float = 180) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": USER_AGENT,
         "x-opencode-session": uuid.uuid4().hex})                   # a new session per call: no shared context
    return json.load(urllib.request.urlopen(req, timeout=timeout))


class QA:
    """The panel, the spares and the referee of images.qa, on one key. `post` is replaced in tests."""

    def __init__(self, cfg: dict, key: str, post=_post, say=print, claude=None):
        self.panel = list(cfg.get("panel") or [])[:3]
        self.spare = list(cfg.get("spare") or [])
        self.referee = cfg.get("referee")
        self.base = cfg.get("base_url") or BASE_URL
        self.key, self.post, self.say = key, post, say
        self.claude = claude                  # model -> client with .generate(); None: kclaude.ClaudeHeadless (tests replace it)
        self.out_of_quota: set[str] = set()
        self.calls = 0

    def ask(self, model: str, jpeg: bytes, brief: str, note: str = "") -> dict:
        """One stateless check by one model: its verdict, or QAError / LimitReached."""
        text = f"THE PROMPT:\n{brief}" + (f"\n\n{note}" if note else "")
        if model.startswith(CLAUDE):
            return self._ask_claude(model, jpeg, text)
        body = {"model": model, "temperature": 0, "max_tokens": MAX_TOKENS, "messages": [
            {"role": "system", "content": CHECKLIST},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}]}]}
        base, key = self.base, self.key
        if model.startswith(OPENROUTER):          # a free model of OpenRouter (`openrouter:nvidia/...:free`)
            from novel import korouter
            base, key, body = OPENROUTER_URL, korouter.find_key(), dict(body, model=model[len(OPENROUTER):])
        for attempt in range(3):
            self.calls += 1
            try:
                r = self.post(base, key, body)
            except urllib.error.HTTPError as e:
                if e.code in LIMIT_STATUS:
                    self.out_of_quota.add(model)
                    raise LimitReached(f"{model}: HTTP {e.code}") from None
                if e.code < 500 or attempt == 2:
                    raise QAError(f"{model}: HTTP {e.code} {e.read()[:200]!r}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 2:
                    raise QAError(f"{model}: {e}") from None
            else:
                try:
                    content = r["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    raise QAError(f"{model}: unexpected answer {str(r)[:200]}") from None
                if isinstance(content, list):                      # some models answer in parts
                    content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                return dict(parse_verdict(content), model=model)
            time.sleep(3 * (attempt + 1))
        raise QAError(f"{model}: no answer")

    def _ask_claude(self, model: str, jpeg: bytes, text: str) -> dict:
        """The same check through headless Claude Code (the subscription, not Go's allowance): Claude reads the picture
        file with its Read tool. A usage limit counts as out of allowance, so the next spare stands in."""
        from novel import kclaude
        path = getattr(jpeg, "path", None)
        if path is None:
            raise QAError(f"{model}: Claude reads the picture file, and this picture has none (use small_jpeg(path))")
        self.calls += 1
        try:
            client = (self.claude or (lambda m: kclaude.ClaudeHeadless(m, timeout=CLAUDE_TIMEOUT)))(model[len(CLAUDE):])
            r = client.generate(CHECKLIST, text, image=path, max_output_tokens=CLAUDE_MAX_TOKENS)
        except kclaude.ClaudeError as e:
            if re.search(r"limit|quota|usage", str(e), re.I):
                self.out_of_quota.add(model)
                raise LimitReached(f"{model}: {str(e)[:160]}") from None
            raise QAError(f"{model}: {str(e)[:200]}") from None
        return dict(parse_verdict(r.text), model=model)

    def _try(self, model: str, jpeg: bytes, brief: str, note: str = "") -> dict:
        try:
            return self.ask(model, jpeg, brief, note)
        except QAError as e:
            return {"model": model, "error": str(e)[:300]}

    def vote(self, jpeg: bytes, brief: str) -> list[dict]:
        """The panel's verdicts, asked at once; a model out of allowance is replaced by the next spare."""
        models = [m for m in self.panel + self.spare if m not in self.out_of_quota][:len(self.panel)]
        with ThreadPoolExecutor(max(len(models), 1)) as pool:
            votes = list(pool.map(lambda m: self._try(m, jpeg, brief), models))
        for i, v in enumerate(votes):                               # one spare try for a model that just ran out
            if "error" in v and v["model"] in self.out_of_quota:
                rest = [m for m in self.spare if m not in self.out_of_quota and m not in {x["model"] for x in votes}]
                if rest:
                    votes[i] = self._try(rest[0], jpeg, brief)
        return votes

    def settle(self, jpeg: bytes, brief: str, flagged: list[dict]) -> dict | None:
        """The referee's verdict (None when it cannot answer), told what was flagged."""
        if not self.referee or self.referee in self.out_of_quota:
            return None
        note = REFEREE_NOTE.format(flagged=json.dumps(flagged, ensure_ascii=False)) if flagged else ""
        v = self._try(self.referee, jpeg, brief, note)
        return None if "error" in v else v

    def judge(self, jpeg: bytes, brief: str) -> dict:
        """First check: {"pass": bool|None, "votes": [...], "referee": {...}|None, "issues": [...]}. pass None =
        nobody answered."""
        votes = self.vote(jpeg, brief)
        answered = [v for v in votes if "error" not in v]
        if not answered:
            return {"pass": None, "votes": votes, "referee": None, "issues": []}
        if all(v["pass"] for v in answered):
            return {"pass": True, "votes": votes, "referee": None, "issues": []}
        flagged = [dict(i, by=v["model"]) for v in answered if not v["pass"] for i in v["issues"]]
        ref = self.settle(jpeg, brief, flagged)
        if ref is None:                                             # no referee: the panel's fails stand
            return {"pass": False, "votes": votes, "referee": None, "issues": flagged}
        return {"pass": ref["pass"], "votes": votes, "referee": ref, "issues": ref["issues"]}

    def recheck(self, jpeg: bytes, brief: str) -> dict:
        """Second check of a redraw: the referee alone, else the panel."""
        ref = self.settle(jpeg, brief, [])
        if ref is not None:
            return {"pass": ref["pass"], "referee": ref, "issues": ref["issues"]}
        j = self.judge(jpeg, brief)
        return {"pass": j["pass"], "votes": j["votes"], "issues": j["issues"]}


class OpenCodeChat:
    """Text through OpenCode Go (chat completions), shaped like episode_translate.Gemini.generate so the writers can use
    it (`novel ktrial scenes --writer haiku`). One single-turn request per call, a new session id each time."""

    def __init__(self, model: str, key: str | None = None, base: str = BASE_URL, post=None, timeout: float = 900):
        self.model, self.key, self.base = model, key or find_key(), base
        self.post = post or (lambda b, k, body: _post(b, k, body, timeout=timeout))
        if not self.key:
            raise QAError(f"no {KEY_VAR}: add it to secrets.env")
        self.allowance = 0.0                    # list-price dollars of the Go allowance used by this client

    def generate(self, system: str, text: str, *, temperature: float = 0.3, max_output_tokens: int = 32000,
                 json_out: bool = False):
        from novel.stages.episode_translate import GenResult
        t0 = time.monotonic()
        r = self.post(self.base, self.key, {"model": self.model, "temperature": temperature,
                                            "max_tokens": max_output_tokens,
                                            "messages": [{"role": "system", "content": system},
                                                         {"role": "user", "content": text}]})
        try:
            ch = r["choices"][0]
            out = ch["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise QAError(f"{self.model}: the answer holds no text: {str(r)[:300]}") from None
        if isinstance(out, list):
            out = "".join(p.get("text", "") for p in out if isinstance(p, dict))
        u = r.get("usage") or {}
        self.allowance += float(((u.get("opencode") or {}).get("allowance")) or 0)
        return GenResult(text=out, finish="MAX_TOKENS" if ch.get("finish_reason") == "length" else "STOP",
                         model=self.model, key=0, seconds=round(time.monotonic() - t0, 1),
                         usage={"promptTokenCount": u.get("prompt_tokens", 0),
                                "candidatesTokenCount": u.get("completion_tokens", 0),
                                "allowance": (u.get("opencode") or {}).get("allowance")})


def fix_lines(issues: list[dict]) -> str:
    """The redraw's extra prompt text: every issue as one instruction."""
    fixes = []
    for i in issues:
        f = str(i.get("fix") or "").strip() or f"avoid this {i.get('kind', 'problem')}: {i.get('where', '')}".strip()
        if f not in fixes:
            fixes.append(f)
    return ("FIX (an earlier try had these problems, avoid them): " + " ".join(
        f.rstrip(".") + "." for f in fixes)) if fixes else ""


FINAL = ("pass", "kept", "fixed", "failed", "unchecked")
# every state a picture's line in the status list (qa.json) goes through:
#   queued -> drawing -> checking -> pass | kept (referee overruled the panel) | unchecked (no model answered)
#                                 -> redrawing -> rechecking -> fixed | failed
#   drawing -> draw_failed (the server could not make it; a later run tries again)


class Live:
    """QA right after each picture: `drawn()` hands a finished picture to a worker that checks it while the next one
    is drawn; a fail is redrawn at once (`redraw(item, fix_text)` makes the picture again at the same path, through the
    server's queue) and checked again. The status list is the record file (JSON by picture id: status, tries, the
    verdicts), saved at every change, so it can be read while the run goes on and a stop loses nothing."""

    def __init__(self, qa: QA, record: Path, redraw, say=print, parallel: int = 3):
        self.qa, self.record, self.redraw, self.say = qa, Path(record), redraw, say
        self.rec = json.loads(self.record.read_text(encoding="utf-8")) if self.record.exists() else {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(parallel)
        self.checks = 0                                             # checks in flight (handed over, not yet finished)

    def _save(self) -> None:
        self.record.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.record.with_name(self.record.name + ".tmp")
        tmp.write_text(json.dumps(self.rec, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.record)

    def set(self, ident: str, status: str, **kw) -> None:
        with self.lock:
            e = self.rec.setdefault(ident, {"tries": 0})
            e.update(kw, status=status)
            self._save()

    def checked(self, it: dict) -> bool:
        """This picture file already has a final verdict."""
        e = self.rec.get(it["id"]) or {}
        p = Path(it["path"])
        return e.get("status") in FINAL and p.exists() and e.get("mtime") == p.stat().st_mtime_ns

    def queued(self, ids) -> None:
        for i in ids:
            self.set(i, "queued")

    def drawing(self, ident: str) -> None:
        self.set(ident, "drawing")

    def drawn(self, it: dict) -> None:
        with self.lock:
            e = self.rec.setdefault(it["id"], {"tries": 0})
            e["tries"] = e.get("tries", 0) + 1
            self.checks += 1
        self.set(it["id"], "checking")
        self.pool.submit(self._run, it)

    def busy(self) -> int:
        """How many checks are still going (one may ask for a redraw), so the drawing side must not shut down yet."""
        return self.checks

    def _run(self, it: dict) -> None:
        try:
            self._check(it)
        finally:
            with self.lock:
                self.checks -= 1

    def _check(self, it: dict) -> None:
        ident, path = it["id"], Path(it["path"])
        try:
            j = self.qa.judge(small_jpeg(path), it["brief"])
            kinds = "; ".join(str(i.get("kind")) for i in j["issues"])
            if j["pass"] is not False:
                status = "unchecked" if j["pass"] is None else "kept" if j["referee"] else "pass"
                self.set(ident, status, first=j, mtime=path.stat().st_mtime_ns)
                self.say(f"  QA {ident}: {status}" + (f" (the panel saw: {kinds or 'a problem'})" if status == "kept"
                                                      else ""))
                return
            fix = fix_lines(j["issues"])
            self.set(ident, "redrawing", first=j, fix=fix)
            self.say(f"  QA {ident}: failed ({kinds}); redrawing with the fixes")
            self.redraw(it, fix)
            with self.lock:
                self.rec[ident]["tries"] = self.rec[ident].get("tries", 0) + 1
            self.set(ident, "rechecking")
            second = self.qa.recheck(small_jpeg(path), it["brief"] + ("\n\n" + fix if fix else ""))
            status = "fixed" if second["pass"] else "failed" if second["pass"] is False else "unchecked"
            self.set(ident, status, second=second, mtime=path.stat().st_mtime_ns)
            self.say(f"  QA {ident}: {status} after the redraw")
        except Exception as e:                                      # never lose the run over one picture
            self.set(ident, "failed", error=str(e)[:300], mtime=path.stat().st_mtime_ns if path.exists() else None)
            self.say(f"  QA {ident}: failed ({str(e)[:120]})")

    def wait(self) -> dict:
        self.pool.shutdown(wait=True)
        return self.rec


def check(items: list[dict], qa: QA, redraw, record: Path, say=print, parallel: int = 3) -> dict:
    """QA pictures already on disk (items {"id", "path", "brief"}) through Live; a picture whose file already has a
    final verdict is skipped. Returns the record."""
    live = Live(qa, record, redraw, say, parallel)
    for it in items:
        if Path(it["path"]).exists() and not live.checked(it):
            live.drawn(it)
    return live.wait()


def summary(rec: dict) -> str:
    counts: dict[str, int] = {}
    for e in rec.values():
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    line = ", ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda x: (x[0] not in FINAL, x[0])))
    redrawn = sum(1 for e in rec.values() if e.get("tries", 0) > 1)
    bad = sorted(k for k, e in rec.items() if e["status"] in ("failed", "draw_failed"))
    return line + f"; {redrawn} redrawn" + (f"; NEEDS A LOOK: {', '.join(bad)}" if bad else "")


def status_table(rec: dict) -> str:
    """One line per picture: id, status, tries, what QA found."""
    rows = []
    for k in sorted(rec):
        e = rec[k]
        issues = (e.get("second") or e.get("first") or {}).get("issues") or []
        what = "; ".join(f"{i.get('kind')}: {i.get('where', '')}" for i in issues)[:110]
        rows.append(f"  {k:6} {e['status']:12} tries {e.get('tries', 0)}  {what or e.get('error', '')}")
    return "\n".join(rows)


def probe(cfg: dict, key: str, picture: Path, say=print) -> int:
    """Ask every configured model about one picture: which ones see images and answer in JSON."""
    qa = QA(cfg, key, say=say)
    jpeg = small_jpeg(picture)
    bad = 0
    for m in dict.fromkeys(qa.panel + qa.spare + [qa.referee]):
        if not m:
            continue
        t0 = time.monotonic()
        v = qa._try(m, jpeg, "A single anime-style illustration.")
        took = time.monotonic() - t0
        if "error" in v:
            bad += 1
            say(f"  {m}: NO  {v['error']}")
        else:
            say(f"  {m}: ok  pass={v['pass']}  {len(v['issues'])} issues  {took:.1f}s")
    return 1 if bad else 0
