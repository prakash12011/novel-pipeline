"""`novel kllm`: start and stop a coding LLM (llama.cpp, Qwen3.6-35B-A3B) on a Kaggle "GPU T4 x2" notebook, reached
through a Cloudflare quick tunnel. Mirrors novel/stages/ktrial_run.py (same Kaggle plumbing, same push guards).

State is global, not per book: <novel home>/kllm/server.json plus the staging folders under <novel home>/kllm/kaggle.

How the pieces meet:
  * `up` makes a fresh random API key and a random 128-bit address-relay topic, puts them (with the limits) in
    session.json inside a small private dataset (kllm-session), next to a copy of novel/kllm.py and a marker unique to
    this push, then pushes the kernel kllm-server (it also attaches the existing hf-token dataset).
  * The notebook downloads the model, starts llama-server, starts novel.kllm's proxy and cloudflared, and posts the
    tunnel address to ntfy.sh under the topic (only the address and a few numbers, never the key). Kaggle returns an
    empty log while a notebook runs, so the log cannot carry the address. `up` polls the topic, then asks the server
    for /__kllm/status with the key, and writes the address and key into Pi's models.json (provider `kaggle`).
  * The notebook ends by itself: after IDLE minutes without an inference request, on /__kllm/stop (`down`), at the
    max-hours cap, or if llama-server dies.

Test hooks: $NOVEL_KLLM_URL_OVERRIDE (skip the relay, use this address), $NOVEL_KLLM_NTFY_URL (the relay's base URL),
$NOVEL_PI_MODELS (the models.json to write), $NOVEL_KLLM_POLL (seconds between polls), $NOVEL_KLLM_WAIT_MINUTES.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from novel import kllm
from novel.core import paths
from novel.kllm import KllmError

from . import episode_tts
from .account import activate_token, owner, resolve_profile
from .kbatch import kaggle, log_text

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = REPO / "kaggle" / "llm_server.ipynb"
DATASET = "kllm-session"
KERNEL = "kllm-server"
ALIAS = "qwen-local"
PROVIDER = "kaggle"
LADDER = (131072, 65536, 32768)
SHOW_RE = re.compile(r"KLLM|FATAL|Error|Traceback|WATCHDOG")
URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+(:\d+)?")


class KllmRunError(Exception):
    """A fixable problem; the command prints it and returns 1."""


# ---------------------------------------------------------------- small helpers

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_dir() -> Path:
    return paths.home() / "kllm"


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_server() -> dict:
    return _read_json(state_dir() / "server.json", {}) or {}


def save_server(server: dict) -> None:
    _write_json(state_dir() / "server.json", server)


def _kernel_word(ref: str) -> tuple[str, str]:
    raw = kaggle("kernels", "status", ref, timeout=180).strip()
    line = raw.splitlines()[-1] if raw else ""
    m = re.search(r"KernelWorkerStatus\.(\w+)", line) or re.search(r'status "?(\w+)"?', line)
    return (m.group(1).upper() if m else "UNKNOWN"), line


def _ended(word: str) -> bool:
    return word == "ERROR" or word.startswith("CANCEL")


def notebook_page(kernel: str) -> str:
    return f"https://www.kaggle.com/code/{kernel}"


def clean_url(text: str) -> str:
    """'https://x.trycloudflare.com/' -> that address; a bare host gets https://. Plain http only for localhost (tests)."""
    t = str(text or "").strip().rstrip("/")
    if t and "://" not in t:
        t = "https://" + t
    m = URL_RE.fullmatch(t)
    if not m or (t.startswith("http://") and not re.match(r"http://(127\.0\.0\.1|localhost)\b", t)):
        raise KllmRunError(f"{text!r} is not an address like https://something.trycloudflare.com")
    return t


def _ctx_ladder(start: int) -> list[int]:
    ladder = [c for c in LADDER if c <= start] or [start]
    if start not in ladder:
        ladder.insert(0, start)
    return ladder


# ---------------------------------------------------------------- Pi's models.json

def pi_models_path() -> Path:
    env = os.environ.get("NOVEL_PI_MODELS", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".pi" / "agent" / "models.json"


def merge_pi_config(url: str, key: str, ctx: int, thinking: bool, path: Path | None = None) -> Path | None:
    """Add (or replace) the `kaggle` provider in Pi's models.json; every other provider and field stays. A file that
    is not valid JSON is left alone (returns None)."""
    path = path or pi_models_path()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
    providers = data.setdefault("providers", {})
    providers[PROVIDER] = {
        "baseUrl": url.rstrip("/") + "/v1", "api": "openai-completions", "apiKey": key,
        "models": [{"id": ALIAS, "name": "Qwen3.6 35B A3B (Kaggle)", "contextWindow": int(ctx),
                    "maxTokens": min(16384, int(ctx) // 4), "reasoning": bool(thinking)}]}
    _write_json(path, data)
    return path


# ---------------------------------------------------------------- up

def _stage_dataset(user: str, marker: str, session: dict) -> Path:
    st = state_dir() / "kaggle" / "dataset"
    if st.exists():
        shutil.rmtree(st)
    st.mkdir(parents=True)
    shutil.copy2(Path(kllm.__file__), st / "kllm.py")
    (st / "session.json").write_text(json.dumps(session, indent=1), encoding="utf-8")
    (st / marker).write_text("kllm", encoding="utf-8")
    (st / "dataset-metadata.json").write_text(json.dumps(
        {"title": DATASET, "id": f"{user}/{DATASET}", "licenses": [{"name": "unknown"}]}, indent=2), encoding="utf-8")
    return st


def _stage_kernel(user: str, smoke: bool) -> Path:
    kd = state_dir() / "kaggle" / "kernel"
    kd.mkdir(parents=True, exist_ok=True)
    for old in kd.glob("*.ipynb"):
        old.unlink()                 # kernels push rejects a folder with a stray notebook
    shutil.copy2(NOTEBOOK, kd / f"{KERNEL}.ipynb")
    meta = {"id": f"{user}/{KERNEL}", "title": KERNEL, "code_file": f"{KERNEL}.ipynb", "language": "python",
            "kernel_type": "notebook", "is_private": True, "enable_gpu": not smoke, "enable_tpu": False,
            "enable_internet": True, "keywords": [],
            "dataset_sources": [f"{user}/{DATASET}", f"{user}/hf-token"],
            "kernel_sources": [], "competition_sources": [], "model_sources": []}
    if not smoke:
        meta["machine_shape"] = "NvidiaTeslaT4"
    (kd / "kernel-metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return kd


def _minutes(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except ValueError:
        return default


def _find_address(server: dict) -> dict | None:
    """The newest message with a url from the relay (None when there is none, or the relay does not answer)."""
    over = os.environ.get("NOVEL_KLLM_URL_OVERRIDE", "").strip()
    if over:
        return {"url": clean_url(over)}
    try:
        msgs = [m for m in kllm.relay_fetch(server["topic"]) if m.get("url")]
    except KllmError:
        return None
    return msgs[-1] if msgs else None


def _wait_for_address(server: dict) -> int:
    """Poll the relay for the address, glance at the kernel now and then, then ask the server itself."""
    ref = server["kernel"]
    poll = _minutes("NOVEL_KLLM_POLL", 10)
    t0 = time.monotonic()
    deadline = t0 + 60 * _minutes("NOVEL_KLLM_WAIT_MINUTES", 45)
    print("waiting for the server (queue, then install, model download and load: about 5 to 10 minutes) ...",
          flush=True)
    last_k, running_seen, relay_down = 0.0, None, False
    while True:
        msg = _find_address(server)
        if msg:
            break
        now = time.monotonic()
        if now - last_k >= (0 if poll == 0 else (20 if running_seen is None else 60)):
            last_k = now
            word, _ = _kernel_word(ref)
            if word == "RUNNING" and running_seen is None:
                running_seen = now - t0
                server["running_seen_s"] = round(running_seen)
                print(f"the kernel started running after {running_seen / 60:.1f} min in the queue", flush=True)
            if _ended(word) or word == "COMPLETE":
                lines = log_text(kaggle("kernels", "logs", ref, timeout=600)).splitlines()
                print(f"the kernel is {word} and never posted an address"
                      + (f" (it was cancelled before it ever ran: queue or GPU quota)"
                         if running_seen is None and word.startswith("CANCEL") else "") + ". Its log:")
                print("\n".join([l for l in lines if SHOW_RE.search(l)][-15:]) or f"(empty) {notebook_page(ref)}")
                return 1
        if now >= deadline:
            print(f"no address after {(now - t0) / 60:.0f} minutes. Open {notebook_page(ref)} for the log; if it shows "
                  f"`KLLM URL https://...`, run `novel kllm url ADDRESS`.")
            return 1
        time.sleep(poll)
    url = msg["url"]
    server.update(url=url, ctx=msg.get("ctx"), model=msg.get("model"), kernel_ready_s=msg.get("ready_s"))
    save_server(server)
    err = ""
    for _ in range(int(_minutes("NOVEL_KLLM_PROBES", 24))):      # the name needs a moment to resolve from here
        try:
            st = kllm.server_status(url, server["key"], timeout=20)
            break
        except KllmError as e:
            err = str(e)
            time.sleep(0 if poll == 0 else 5)
    else:
        print(f"the address {url} was posted but does not answer yet ({err}). It stays stored; "
              f"`novel kllm status` looks again.")
        return 1
    total = time.time() - server["pushed_epoch"]
    server.update(ready_epoch=round(time.time()), ready_after_s=round(total), ctx=st.get("ctx"))
    save_server(server)
    boot = f", of which the kernel's own start to ready was {msg['ready_s'] / 60:.1f} min" if msg.get("ready_s") else ""
    print(f"the server answers at {url}\n  context {st.get('ctx')}, model {st.get('model')}; "
          f"{total / 60:.1f} min from push to ready{boot}")
    return 0


def cmd_up(args) -> int:
    smoke = bool(getattr(args, "smoke", False))
    old = load_server()
    dry = getattr(args, "dry_run", False)
    if old and not old.get("stopped") and not dry:
        activate_token(old["account"])
        word, line = _kernel_word(old["kernel"])
        if word in ("RUNNING", "QUEUED"):
            print(f"the server pushed at {old.get('pushed')} ({old['kernel']}) is still {word}. "
                  f"Use `novel kllm status` to look at it or `novel kllm down` to stop it first.")
            return 1
        if word == "UNKNOWN":
            print(f"note: could not read the status of {old['kernel']} ({line[-100:]}); pushing anyway")
    profile = resolve_profile(getattr(args, "account", None))
    user = owner(profile)
    idle = args.idle_minutes if args.idle_minutes is not None else 30
    hours = args.max_hours if args.max_hours is not None else 11.5
    pad = args.pad_kb * 1000 if args.pad_kb is not None else kllm.DEFAULT_PAD
    thinking = args.thinking == "on"
    marker = f"push-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.txt"
    session = {"key": "kllm-" + secrets.token_urlsafe(32), "topic": secrets.token_hex(16),
               "idle_minutes": float(idle), "max_hours": float(hours), "contexts": _ctx_ladder(args.context),
               "thinking": thinking, "alias": ALIAS, "pad_bytes": pad, "smoke": smoke}
    st = _stage_dataset(user, marker, session)
    kd = _stage_kernel(user, smoke)
    print(f"staged the {'CPU smoke' if smoke else 'GPU'} server for {user} (profile {profile}): idle {idle:g} min, "
          f"at most {hours:g} h, contexts {session['contexts']}, thinking {args.thinking}")
    if dry:
        print(f"dry run - nothing is called. Would upload dataset {user}/{DATASET} and push kernel {user}/{KERNEL}:")
        for f in sorted(st.rglob("*")):
            if f.is_file():
                print(f"  dataset  {f.relative_to(st).as_posix()}  ({f.stat().st_size} bytes)")
        for f in sorted(kd.glob("*")):
            print(f"  kernel   {f.name}")
        return 0
    activate_token(profile)
    episode_tts.upload(st, f"{user}/{DATASET}", marker)
    out = kaggle("kernels", "push", "-p", ".", cwd=kd, timeout=600)
    if "successfully pushed" not in out:
        print(f"push failed:\n{out.strip()[-400:]}")
        return 1
    print("  " + [l for l in out.splitlines() if "pushed" in l][0].strip())
    server = {"account": profile, "user": user, "kernel": f"{user}/{KERNEL}", "dataset": f"{user}/{DATASET}",
              "key": session["key"], "topic": session["topic"], "pushed": marker[5:-4], "pushed_epoch": round(time.time()),
              "smoke": smoke, "thinking": thinking, "url": None}
    save_server(server)
    if getattr(args, "no_wait", False):
        print("pushed. `novel kllm status` shows the address once the server is ready.")
        return 0
    rc = _wait_for_address(server)
    if rc == 0 and not smoke:
        path = merge_pi_config(server["url"], server["key"], int(server.get("ctx") or 32768), thinking)
        if path:
            print(f"Pi is configured ({path}). Start it with:  pi --model {PROVIDER}/{ALIAS}")
        else:
            print(f"{pi_models_path()} is not valid JSON, so it was left alone; add the provider by hand.")
    return rc


# ---------------------------------------------------------------- status, url, down

def cmd_status(args) -> int:
    server = load_server()
    if not server:
        print("no server was started (`novel kllm up`)")
        return 1
    activate_token(server["account"])
    word, line = _kernel_word(server["kernel"])
    print(line or f"{server['kernel']}: {word}")
    if server.get("stopped"):
        print(f"stop was requested at {server['stopped']}")
    over = word == "COMPLETE" or _ended(word)
    if over:
        lines = log_text(kaggle("kernels", "logs", server["kernel"], timeout=600)).splitlines()
        print("\n".join([l for l in lines if SHOW_RE.search(l)][-12:]))
    if not server.get("url"):
        msg = None if over else _find_address(server)
        if msg:
            server["url"] = msg["url"]
            save_server(server)
        else:
            print("address: none yet" + ("" if over else f" (the kernel may still be starting: {notebook_page(server['kernel'])})"))
            return 0
    print(f"address: {server['url']}")
    if over:
        return 0
    try:
        st = kllm.server_status(server["url"], server["key"])
    except KllmError as e:
        print(f"the server does not answer: {e}")
        return 0
    print(f"the server answers: context {st.get('ctx')}, model {st.get('model')}, up {st['uptime_s'] // 60} min, "
          f"idle {st['idle_s'] // 60} of {st['idle_minutes']:g} min, {st['requests']} requests")
    return 0


def cmd_url(args) -> int:
    server = load_server()
    if not server:
        print("no server was started: run `novel kllm up` first")
        return 1
    try:
        server["url"] = clean_url(args.address)
    except KllmRunError as e:
        print(e)
        return 1
    save_server(server)
    print(f"address stored: {server['url']}")
    try:
        st = kllm.server_status(server["url"], server["key"])
    except KllmError as e:
        print(f"the server does not answer yet: {e}\nThe address stays stored; `novel kllm status` looks again.")
        return 1
    print("the server answers")
    if not server.get("smoke"):
        path = merge_pi_config(server["url"], server["key"], int(st.get("ctx") or 32768), server.get("thinking", False))
        if path:
            print(f"Pi is configured ({path}): pi --model {PROVIDER}/{ALIAS}")
    return 0


def cmd_down(args) -> int:
    server = load_server()
    if not server:
        print("no server was started")
        return 0
    if not server.get("url"):
        print("no address is recorded, so there is no stop request to send. The kernel ends by itself after its idle "
              "limit; to stop it now, cancel it on kaggle.com.")
        return 1
    try:
        kllm.request_stop(server["url"], server["key"])
        print("stop requested: the kernel shuts down within a minute and frees the GPU quota.")
        server["stop_sent"] = True
    except KllmError as e:
        print(f"the server does not answer ({e}): it has probably stopped already. If the kernel still shows RUNNING "
              f"on kaggle.com it ends after its idle limit.")
        server["stop_sent"] = False
    server["stopped"] = _now()
    save_server(server)
    if not server["stop_sent"] or getattr(args, "no_wait", False):
        return 0
    activate_token(server["account"])
    poll = _minutes("NOVEL_KLLM_POLL", 10)
    end = time.monotonic() + 60 * _minutes("NOVEL_KLLM_DOWN_MINUTES", 4)
    while True:
        word, _ = _kernel_word(server["kernel"])
        if word not in ("RUNNING", "QUEUED"):
            print(f"the kernel has ended ({word.lower()})")
            return 0
        if time.monotonic() >= end:
            print(f"the kernel still shows {word} after the wait; `novel kllm status` looks again.")
            return 0
        time.sleep(poll)
