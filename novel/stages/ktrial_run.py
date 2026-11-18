"""The GPU side of the picture trial: start and stop the Kaggle ComfyUI server, make the pictures from a plan
file, and write the comparison report (Flow picture next to the FLUX.3 [klein] picture, plus the panel cutouts).

Everything lives in one trial folder (`<trial>`, see novel/stages/ktrial.py for the layout); nothing here touches the
production episode folders. The Kaggle side reuses the TTS plumbing (account, upload wait, kbatch.kaggle) but has its
own dataset (<book>-ktrial) and kernel (<book>-ktrial-comfy), so it cannot disturb a narration run.

Guards, each for a way this could go wrong:
  * `up` refuses while the recorded kernel is still running: a second push would replace a server that may be
    mid-picture and burn another session of the weekly GPU quota.
  * The dataset is confirmed live by a marker unique to this push (episode_tts.upload), as for TTS.
  * Pictures are written to a temp file and renamed, so Ctrl+C or a crash never leaves half a JPEG that a later
    run would take as finished.
  * A re-roll or redo moves the old picture to images/old/<stamp>/ once the new one exists: nothing is ever overwritten.
  * Three failures in a row stop the run: that is a dead tunnel or a full GPU, not a bad prompt, and the rest of
    the pictures would only fail the same way.
  * The server's address is never read from the kernel's log: Kaggle returns an EMPTY log while a notebook is still
    running. With ngrok the tunnel is opened on the account's fixed dev domain, so the address is known before the
    server starts; without a domain the user copies the address from the notebook page (`novel ktrial url`).

`up --model minimax-h3` starts the same server with MiniMax-H3-Image (GGUF) instead of FLUX.3 klein: the model is stored
in the session and in server.json (FLUX stays the default), and `run` then makes the pictures into
<trial>/minimax-h3/ (plates/, images/, log.jsonl; each with its sent prompt and meta file), so the FLUX pictures stay.
`up --model qwen-image-turbo` does the same for Qwen-Image-2.1-Turbo (INT8): pictures go to <trial>/qwen-image-turbo/,
with the place plate sent as the first reference (Qwen edits `image_1`). `up --ngrok-dataset SLUG` takes the ngrok token
from another private Kaggle dataset of the account (default `ngrok-token`), for a second ngrok account.

Test hook: $NOVEL_KTRIAL_URL_OVERRIDE replaces the address computed from a domain (`https://<host>`) in `up` and `url`,
so a test can point the "domain" at a local fake ComfyUI. Never set it for real use.

`run --via openrouter` makes the same pictures with the hosted API (novel/korouter.py) instead of the Kaggle server.
It needs no server.json, writes to <trial>/openrouter/ (plates/, images/, log.jsonl) so the Kaggle pictures stay for
comparison, and stops cleanly once the money spent in this run plus one more picture would pass --budget.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import secrets
import shutil
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from novel import kcomfy, korouter
from novel.kcomfy import Comfy, ComfyError
from novel.korouter import RouterError

from . import episode_tts
from .account import activate_token, owner, resolve_profile
from .episode_panels import SHAPES, shape_for
from .kbatch import kaggle, kaggle_home, log_text

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = REPO / "kaggle" / "comfy_server.ipynb"

JPEG_QUALITY = 92
SMALL_BYTES = 50_000            # below this a picture is kept but flagged (a very dark one can be small)
MAX_FAILS_IN_ROW = 3
# the --via openrouter pictures, next to the Kaggle ones in the trial folder; $NOVEL_OPENROUTER_DIR replaces the name
# (set it together with $NOVEL_OPENROUTER_MODEL so one model's pictures never overwrite another's)
OR_DIR = os.environ.get("NOVEL_OPENROUTER_DIR", "").strip() or "nanobanana21"
                                # (was "openrouter": the FLUX.3 klein pictures stay there)
DEFAULT_BUDGET = 1.50           # dollars per run
RUN_ERRORS = (ComfyError, RouterError, OSError, ValueError)
CUT_HEIGHT = 420                # px, panel cutouts in the report
PROJECT_PICTURES = 400          # the number the go/no-go projection is made for
HOST_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
HOST_RE = re.compile(HOST_LABEL + r"(\." + HOST_LABEL + ")*")
DOMAIN_FILE = "ngrok-domain"            # in the Kaggle home, next to the access tokens
NGROK_DOMAINS_PAGE = "https://dashboard.ngrok.com/domains"
SHOW_RE = re.compile(r"probe|KTRIAL|Error|Traceback|ASSERT")


# ---------------------------------------------------------------- small helpers

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


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


def _valid_jpeg(path: Path) -> bool:
    """Starts with FFD8, ends with FFD9, not a stub. Deliberately NOT flowrun.jpeg_ok: that one also demands 50 kB,
    and a legitimately dark picture under that size must still count as done, or every run would redo it."""
    try:
        x = Path(path).read_bytes()
    except OSError:
        return False
    return len(x) > 1000 and x[:2] == b"\xff\xd8" and x[-2:] == b"\xff\xd9"


def _meta_path(out: Path) -> Path:
    return out.with_suffix(".meta.json")


def _book(args, plan: dict | None = None) -> str:
    name = getattr(args, "book", None) or (plan or {}).get("book") or episode_tts.book_slug()
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def dataset_slug(book: str) -> str:
    return f"{book}-ktrial"


def kernel_slug(book: str) -> str:
    """Not the dataset's name: Kaggle answered 409 Conflict for a kernel slug equal to a dataset slug."""
    return f"{book}-ktrial-comfy"


def server_path(trial: Path) -> Path:
    return trial / "server.json"


def load_server(trial: Path) -> dict:
    return _read_json(server_path(trial), {}) or {}


def save_server(trial: Path, server: dict) -> None:
    _write_json(server_path(trial), server)


def _kernel_word(ref: str) -> tuple[str, str]:
    """(STATUS_WORD, the CLI's status line); the same parse as episode_tts.kernel_state, which is tied to an episode."""
    raw = kaggle("kernels", "status", ref, timeout=180).strip()
    line = raw.splitlines()[-1] if raw else ""
    m = re.search(r"KernelWorkerStatus\.(\w+)", line) or re.search(r'status "?(\w+)"?', line)
    return (m.group(1).upper() if m else "UNKNOWN"), line


def _kernel_lines(ref: str) -> list[str]:
    return log_text(kaggle("kernels", "logs", ref, timeout=600)).splitlines()


def _ended(word: str) -> bool:
    return word == "ERROR" or word.startswith("CANCEL")


def _comfy(server: dict, **kw) -> Comfy:
    return Comfy(server["url"], server.get("comfy_user"), server.get("password"), **kw)


def notebook_page(kernel: str) -> str:
    """The kernel's page on kaggle.com; its log can be read there while the notebook runs."""
    return f"https://www.kaggle.com/code/{kernel}"


# ---------------------------------------------------------------- the ngrok dev domain

def normalise_host(text: str, what: str = "domain") -> str:
    """'https://Some-Words.ngrok-free.dev/' -> 'some-words.ngrok-free.dev'. Anything that is not a plain host name
    (letters, digits, dots, hyphens) exits with a clear message."""
    host = str(text or "").strip()
    host = re.sub(r"^https?://", "", host, flags=re.I).rstrip("/").lower()
    if not host or not HOST_RE.fullmatch(host):
        sys.exit(f"{what} {str(text).strip()!r} is not a plain host name (letters, digits, dots and hyphens only, "
                 f"like something-words.ngrok-free.dev; no port, no path). Your ngrok domain is listed at "
                 f"{NGROK_DOMAINS_PAGE}")
    return host


def domain_file() -> Path:
    return kaggle_home() / DOMAIN_FILE


def ngrok_domain(args, save: bool = True) -> str | None:
    """The ngrok dev domain to open the tunnel on: --domain, else $NOVEL_NGROK_DOMAIN, else the file `ngrok-domain`
    in the Kaggle home (one line). None when there is none. A --domain is remembered in that file (when `save`)."""
    given = getattr(args, "domain", None)
    if given:
        host = normalise_host(given, "--domain")
        if save and not getattr(args, "ngrok_dataset", None):     # a second account's domain is not remembered
            # (the file holds the first account's: a later plain `up` would try it with the wrong token)
            f = domain_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(host + "\n", encoding="utf-8")
            print(f"ngrok domain {host} saved in {f}: the next `up` uses it without --domain")
        return host
    env = os.environ.get("NOVEL_NGROK_DOMAIN", "").strip()
    if env:
        return normalise_host(env, "$NOVEL_NGROK_DOMAIN")
    if getattr(args, "ngrok_dataset", None):
        return None     # another ngrok account's token: the stored domain is the first account's and would not open
    f = domain_file()
    try:
        text = f.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return normalise_host(text.splitlines()[0], f"the domain in {f}") if text else None


def _url_for(host: str) -> str:
    return os.environ.get("NOVEL_KTRIAL_URL_OVERRIDE", "").strip() or f"https://{host}"


def _manual_steps(server: dict, chapter) -> None:
    """What to do when the address is not known: Kaggle shows no log of a running notebook through its API, but the
    notebook page on kaggle.com does."""
    n = chapter if chapter is not None else "N"
    print(f"The address of the server is not known yet. Open {notebook_page(server['kernel'])}\n"
          f"and wait for the line `KTRIAL URL https://...` in its log (about 7 minutes), then run:\n"
          f"  novel ktrial url --chapter {n} <address>")
    print(f"To avoid this next time: novel ktrial up --chapter {n} --domain <your ngrok dev domain> "
          f"(the domain is listed at {NGROK_DOMAINS_PAGE})")


# ---------------------------------------------------------------- up

def _stage_dataset(trial: Path, book: str, user: str, marker: str, args,
                   domain: str | None = None) -> tuple[Path, dict]:
    st = trial / "kaggle" / "dataset"
    if st.exists():
        shutil.rmtree(st)
    st.mkdir(parents=True)
    session = {"user": "ktrial", "password": secrets.token_urlsafe(24),
               "idle_minutes": getattr(args, "idle_minutes", None) or 20,
               "max_hours": getattr(args, "max_hours", None) or 6,
               "gpu": getattr(args, "gpu", None) if getattr(args, "gpu", None) is not None else 1,
               "tunnel": getattr(args, "tunnel", None) or "ngrok"}
    if getattr(args, "model", None) not in (None, kcomfy.FLUX):
        session["model"] = args.model
    if getattr(args, "comfy_arg", None):
        session["comfy_args"] = list(args.comfy_arg)
    if getattr(args, "ngrok_dataset", None):
        slug = str(args.ngrok_dataset).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            sys.exit(f"--ngrok-dataset {slug!r} is not a Kaggle dataset slug (lower-case letters, digits, hyphens)")
        session["ngrok_dataset"] = slug         # the notebook reads the token from this dataset
    if domain:
        session["domain"] = domain
    if getattr(args, "cf_hostname", None):
        session["cf_hostname"] = args.cf_hostname      # the named Cloudflare tunnel: ngrok's fallback, fixed address
    shutil.copy2(Path(kcomfy.__file__), st / "kcomfy.py")
    (st / "session.json").write_text(json.dumps(session, indent=1), encoding="utf-8")
    (st / marker).write_text("ktrial", encoding="utf-8")
    (st / "dataset-metadata.json").write_text(json.dumps(
        {"title": dataset_slug(book), "id": f"{user}/{dataset_slug(book)}",
         "licenses": [{"name": "unknown"}]}, indent=2), encoding="utf-8")
    return st, session


CF_SLUG, CF_FILE = "cloudflare-tunnel", "CLOUDFLARE_TUNNEL_TOKEN"     # the named tunnel's token (images.cf_hostname)


def ensure_ngrok_dataset(profile: str, user: str, slug: str = "ngrok-token", name: str = "NGROK_AUTHTOKEN") -> None:
    """The image server reads a token from the private dataset <user>/<slug>: upload kaggle/<name> there when the
    account (picked because it was free) does not have it yet. Used for the ngrok token and the Cloudflare tunnel
    token. The ngrok domain stays the stored one."""
    from .account import kaggle_as
    if name in kaggle_as(profile, "datasets", "files", f"{user}/{slug}"):
        return
    src = NOTEBOOK.parent / name
    if not src.is_file():
        sys.exit(f"{user} has no {slug} dataset and {src} is missing: add the token there")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy2(src, Path(tmp) / name)
        (Path(tmp) / "dataset-metadata.json").write_text(json.dumps(
            {"title": slug, "id": f"{user}/{slug}", "licenses": [{"name": "CC0-1.0"}]}), encoding="utf-8")
        print(f"uploading the ngrok token to {user}/{slug} (private): "
              + kaggle_as(profile, "datasets", "create", "-p", tmp).strip()[-160:])


def _stage_kernel(trial: Path, book: str, user: str, tunnel: str, ngrok_dataset: str = "ngrok-token",
                  cloudflare: bool = False) -> Path:
    kd = trial / "kaggle" / "kernel"
    kd.mkdir(parents=True, exist_ok=True)
    ks = kernel_slug(book)
    for old in kd.glob("*.ipynb"):
        old.unlink()                 # kernels push rejects a folder with a stray notebook
    nb = kd / f"{ks}.ipynb"
    shutil.copy2(NOTEBOOK, nb)
    nb.write_text(nb.read_text(encoding="utf-8").replace("@DATASET@", dataset_slug(book)), encoding="utf-8")
    sources = [f"{user}/{dataset_slug(book)}", f"{user}/hf-token"]
    if tunnel == "ngrok":
        sources.append(f"{user}/{ngrok_dataset}")    # only the ngrok tunnel reads it
    if cloudflare:
        sources.append(f"{user}/{CF_SLUG}")         # the named tunnel's token
    (kd / "kernel-metadata.json").write_text(json.dumps({
        "id": f"{user}/{ks}", "title": ks, "code_file": f"{ks}.ipynb", "language": "python",
        "kernel_type": "notebook", "is_private": True, "enable_gpu": True,
        "enable_tpu": False, "enable_internet": True, "keywords": [],
        "dataset_sources": sources, "kernel_sources": [], "competition_sources": [], "model_sources": [],
        "machine_shape": "NvidiaTeslaT4",
    }, indent=2), encoding="utf-8")
    return kd


def _wait_for_server(trial: Path, server: dict) -> int:
    """Wait for the server to answer at its (known) address. Polls the SERVER, not the log; between tries it looks at
    the kernel and stops on a dead one. Gives up after NOVEL_KTRIAL_WAIT_MINUTES (default 40)."""
    ref, url = server["kernel"], server["url"]
    poll = float(os.environ.get("NOVEL_KTRIAL_POLL", "30"))
    minutes = float(os.environ.get("NOVEL_KTRIAL_WAIT_MINUTES", "40"))
    t0 = time.monotonic()
    deadline = t0 + 60 * minutes
    print(f"waiting for the server at {url} (install and model download take about 10 minutes) ...", flush=True)
    tries = 0
    while True:
        live = answering(server)
        if live is not None:
            if live["url"] != url:
                print("the first tunnel did not come up; the notebook opened its fallback tunnel")
                save_server(trial, live)
            print(f"the server answers at {live['url']}")
            return 0
        last = "no answer"
        word, _ = _kernel_word(ref)
        if _ended(word) or word == "COMPLETE":
            print(f"the kernel is {word} and the server never answered ({last[:150]}).\n"
                  f"Open the notebook page on kaggle.com and read its log: {notebook_page(ref)}")
            return 1
        if time.monotonic() >= deadline:
            print(f"the server at {url} did not answer within {minutes:g} minutes ({last[:150]}). The kernel is "
                  f"still {word}: read its log at {notebook_page(ref)} ; `novel ktrial status` looks again.")
            return 1
        tries += 1
        if tries % 4 == 0:
            print(f"still starting, {(time.monotonic() - t0) / 60:.0f} min", flush=True)
        if tries == 32 and server.get("tunnel", "ngrok") == "ngrok" and not server.get("fallback_url"):          # ~16 min: longer than a normal start
            print(f"no answer on the ngrok address yet. If ngrok failed, the notebook opened a Cloudflare tunnel "
                  f"instead: on {notebook_page(ref)} find the line `KTRIAL FALLBACK cloudflared https://...` and run "
                  f"`novel ktrial url --chapter <N> <that address>`", flush=True)
        time.sleep(poll)


def cmd_up(args, trial: Path) -> int:
    trial = Path(trial)
    plan = _read_json(trial / "plan.json", {})
    book = _book(args, plan)
    dry = getattr(args, "dry_run", False)
    tunnel = getattr(args, "tunnel", None) or "ngrok"
    domain = None
    if tunnel == "ngrok":
        domain = ngrok_domain(args, save=not dry)        # a bad value stops here, before anything is staged or called
    elif getattr(args, "domain", None):
        print("note: --domain is only used with the ngrok tunnel; ignored")
    old = load_server(trial)
    if old and not old.get("stopped") and not dry:
        activate_token(old["account"])
        word, line = _kernel_word(old["kernel"])
        if word in ("RUNNING", "QUEUED"):
            print(f"the server pushed at {old.get('pushed')} ({old['kernel']}) is still {word}. "
                  f"Use `novel ktrial status` to look at it or `novel ktrial down` to stop it first.")
            return 1
        if word == "UNKNOWN":
            print(f"note: could not read the status of {old['kernel']} ({line[-100:]}); pushing anyway")
    from novel.core import settings
    from .account import free_profile
    profile = getattr(args, "account", None) or free_profile(settings.get("images.kaggle_profile", default=None))
    user = owner(profile)
    if tunnel == "ngrok" and not getattr(args, "ngrok_dataset", None) and not getattr(args, "dry_run", False) \
            and settings.get("kaggle.pick_free", default=False):
        ensure_ngrok_dataset(profile, user)
    cf = normalise_host(str(settings.get("images.cf_hostname", default="") or ""), "cf_hostname") \
        if settings.get("images.cf_hostname", default=None) and tunnel == "ngrok" else None
    if cf and not dry:
        ensure_ngrok_dataset(profile, user, CF_SLUG, CF_FILE)
    args.cf_hostname = cf
    marker = f"push-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.txt"
    st, session = _stage_dataset(trial, book, user, marker, args, domain)
    kd = _stage_kernel(trial, book, user, tunnel, session.get("ngrok_dataset") or "ngrok-token", cloudflare=bool(cf))
    print(f"staged server for {user} (profile {profile}), tunnel {tunnel}"
          + (f", domain {domain}" if domain else "") + f", model {session.get('model', kcomfy.FLUX)}")
    if dry:
        print(f"dry run - nothing is called. Would upload dataset {user}/{dataset_slug(book)} and push kernel "
              f"{user}/{kernel_slug(book)}:")
        for f in sorted(st.rglob("*")):
            if f.is_file():
                print(f"  dataset  {f.relative_to(st).as_posix()}  ({f.stat().st_size} bytes)")
        for f in sorted(kd.glob("*")):
            print(f"  kernel   {f.name}")
        print(f"look in {trial / 'kaggle'}")
        return 0
    activate_token(profile)         # selects the login, so only once we really upload
    episode_tts.upload(st, f"{user}/{dataset_slug(book)}", marker)
    out = kaggle("kernels", "push", "-p", ".", cwd=kd, timeout=600)
    if "successfully pushed" not in out:
        print(f"push failed:\n{out.strip()[-400:]}")
        return 1
    print("  " + [l for l in out.splitlines() if "pushed" in l][0].strip())
    server = {"account": profile, "user": user, "kernel": f"{user}/{kernel_slug(book)}",
              "dataset": f"{user}/{dataset_slug(book)}", "comfy_user": session["user"],
              "password": session["password"], "tunnel": session["tunnel"], "pushed": marker[5:-4],
              "url": _url_for(cf or domain) if (cf or domain) else None}
    if domain:
        server["domain"] = domain
    if cf and domain:
        server["fallback_url"] = _url_for(domain)      # the named Cloudflare tunnel first, ngrok second
    if session.get("model"):
        server["model"] = session["model"]
    save_server(trial, server)
    if not server["url"]:                              # no fixed address (no ngrok domain, no Cloudflare hostname)
        print("pushed.")
        _manual_steps(server, getattr(args, "chapter", None))
        return 0
    if getattr(args, "no_wait", False):
        print(f"pushed. The address is {server['url']}; `novel ktrial status` shows whether the server answers.")
        return 0
    return _wait_for_server(trial, server)


# ---------------------------------------------------------------- status and down

def cmd_status(args, trial: Path) -> int:
    """Kernel state, the stored address, whether the server answers. The log is read only once the kernel has
    ended: Kaggle returns an empty log for a running one."""
    trial = Path(trial)
    server = load_server(trial)
    if not server:
        print("no server was started for this trial (`novel ktrial up`)")
        return 1
    activate_token(server["account"])
    word, line = _kernel_word(server["kernel"])
    print(line or f"{server['kernel']}: {word}")
    if server.get("stopped"):
        print(f"stop was requested at {server['stopped']}")
    over = word == "COMPLETE" or _ended(word)
    if over:
        lines = _kernel_lines(server["kernel"])
        print("\n".join([l for l in lines if SHOW_RE.search(l)][-15:]))
    if not server.get("url"):
        if over:
            print("address: none stored (the kernel has ended)")
        else:
            _manual_steps(server, getattr(args, "chapter", None))
        return 0
    print(f"address: {server['url']}")
    try:
        _comfy(server, timeout=15, retries=1, backoff=0.5).ping()
        print("the server answers")
    except ComfyError as e:
        print(f"the server does not answer: {e}")
    return 0


def cmd_url(args, trial: Path) -> int:
    """Store the server's address by hand (no fixed domain): `novel ktrial url --chapter N ADDRESS`."""
    trial = Path(trial)
    server = load_server(trial)
    if not server:
        print("no server was started for this trial: run `novel ktrial up` first")
        return 1
    url = _url_for(normalise_host(args.address, "the address"))
    server["url"] = url
    save_server(trial, server)
    print(f"address stored: {url}")
    try:
        _comfy(server, timeout=15, retries=2, backoff=1.0).ping()
    except ComfyError as e:
        print(f"the server does not answer yet: {e}\nThe address stays stored; `novel ktrial status` looks again.")
        return 1
    print("the server answers")
    return 0


def cmd_down(args, trial: Path) -> int:
    trial = Path(trial)
    server = load_server(trial)
    if not server:
        print("no server was started for this trial")
        return 0
    if not server.get("url"):
        print("no URL is recorded, so there is no stop file to send. The kernel ends by itself after its idle "
              "limit; to stop it now, cancel it on kaggle.com.")
        return 1
    try:
        kcomfy.request_stop(_comfy(server, timeout=15, retries=1, backoff=0.5))
        print("stop file sent: the kernel shuts down within a minute and frees the GPU quota.")
        server["stop_sent"] = True
    except ComfyError as e:
        print(f"the server does not answer ({e}): it has probably stopped already. If the kernel still shows "
              f"RUNNING on kaggle.com it ends after its idle limit.")
        server["stop_sent"] = False
    server["stopped"] = _now()
    save_server(trial, server)
    return 0


# ---------------------------------------------------------------- run

_LOG_LOCK = threading.Lock()          # two cards: two workers append to one log


def _log(root: Path, entry: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK, open(root / "log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _short(shot: dict) -> str:
    """'s05' for a shot whose picture is images/s05.jpg."""
    return Path(shot["out"]).stem


def _select(plan: dict, only: str | None, limit: int | None) -> tuple[list[dict], set[str]]:
    """The shots to make, in plan order, and the group ids named explicitly in --only."""
    shots = plan["shots"]
    named_groups: set[str] = set()
    if only:
        tokens = [t.strip() for t in only.split(",") if t.strip()]
        groups = {p["group"] for p in plan["plates"]} | {s["group"] for s in shots}
        shorts = {_short(s) for s in shots}
        ids = {s["id"] for s in shots}
        bad = [t for t in tokens if t not in groups and t not in shorts and t not in ids]
        if bad:
            sys.exit(f"--only: unknown {', '.join(bad)}. Shots: {', '.join(sorted(shorts))}; "
                     f"groups: {', '.join(sorted(groups))}")
        named_groups = {t for t in tokens if t in groups}
        shots = [s for s in shots
                 if _short(s) in tokens or s["id"] in tokens or s["group"] in named_groups]
    if limit is not None:
        shots = shots[:max(limit, 0)]
    return shots, named_groups


def _next_seed(out: Path, plan_seed, reroll: bool) -> int:
    """The previous picture's seed (its meta file, else the plan's), plus one for a re-roll."""
    prev = _read_json(_meta_path(out), {}) or {}
    seed = prev.get("seed")
    return int(plan_seed if seed is None else seed) + (1 if reroll else 0)


def _archive(trial: Path, files: list[Path], folder: Path, stamp: str) -> None:
    """Move the files that exist into <folder>/old/<stamp>/ (never overwrite, never delete): a second move within
    the same second goes to <stamp>-2, so a quick re-roll after a redo keeps both old pictures."""
    present = [f for f in files if f.exists()]
    if not present:
        return
    k, dest = 1, folder / "old" / stamp
    while any((dest / f.name).exists() for f in present):
        k += 1
        dest = folder / "old" / f"{stamp}-{k}"
    dest.mkdir(parents=True, exist_ok=True)
    for f in present:
        shutil.move(str(f), str(dest / f.name))


def _to_jpeg(data: bytes) -> tuple[bytes, tuple[int, int]]:
    """ComfyUI hands back PNG; the pipeline works with JPEG, as the Flow pictures are."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGB")
    except (OSError, ValueError) as e:
        raise ComfyError(f"ComfyUI returned something that is not a picture: {e}") from None
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), img.size


class _Run:
    """One `run`: the client, what has been uploaded, and the running numbers."""

    def __init__(self, comfy: Comfy | None, trial: Path, plan: dict, model: str = kcomfy.FLUX):
        self.comfy, self.trial, self.plan, self.model = comfy, trial, plan, model
        sub = {kcomfy.MINIMAX: kcomfy.MM_DIR, kcomfy.QWEN: kcomfy.QW_DIR}.get(model)
        self.root = trial / sub if sub else trial   # pictures, meta files, log.jsonl
        self.names: dict[str, str] = {}         # plan file -> name on the server
        self.secs: list[float] = []
        self.made = self.skipped = self.failed = 0
        self.total = 0
        self.done = 0                           # pictures attempted, of self.total
        self.t0 = time.monotonic()
        self.live = None                        # kqa.Live: picture QA right after each shot (cmd_run with qa)

    def upload(self, ref: dict) -> str:
        """Upload a reference once per run. The picture on disk is the truth: a missing one is an error."""
        file = ref["file"]
        if file in self.names:
            return self.names[file]
        # a place reference is the plate of THIS model's run (self.root; the trial itself for FLUX)
        path = (self.root if ref.get("role") == "place" else self.trial) / file
        if not path.is_file():
            raise ComfyError(f"reference {file} is missing (a place picture is made by its plate first)")
        if ref.get("role") == "place" and ref.get("group"):
            name = f"ktrial-c{int(self.plan['chapter']):04d}-{ref['group']}.jpg"
        else:
            name = f"ktrial-{path.name}"
        self.names[file] = self.comfy.upload_image(path, name)
        return self.names[file]

    def make(self, kind: str, ident: str, item: dict, text: str, seed: int, refs: list[dict],
             replace_stamp: str | None = None) -> dict:
        """Generate, convert, write the picture and its meta file, log it. Raises ComfyError on failure.
        With `replace_stamp` the picture it replaces is moved to old/<stamp>/ only once the new one exists, so a
        failed re-roll leaves the old picture where it was."""
        if self.model == kcomfy.QWEN:           # the place plate is its canvas (image_1)
            text, refs = kcomfy.qwen_order(text, refs)
        names = [self.upload(r) for r in refs]
        sent = kcomfy.adapt_prompt(self.model, text)
        kw = {"timeout": kcomfy.picture_timeout(self.model), "model": self.model} if self.model != kcomfy.FLUX else {}
        data, took = self.comfy.generate(sent, seed, int(self.plan["width"]), int(self.plan["height"]), names,
                                         f"ktrial-c{int(self.plan['chapter']):04d}-{ident}-{seed}", **kw)
        jpeg, (w, h) = _to_jpeg(data)
        out = self.root / item["out"]
        out.parent.mkdir(parents=True, exist_ok=True)
        if replace_stamp:
            sidecar = [out.with_suffix(".prompt.txt")] if self.model != kcomfy.FLUX else []
            _archive(self.trial, [out, _meta_path(out), *sidecar], out.parent, replace_stamp)
        tmp = out.with_name(out.name + ".part")
        tmp.write_bytes(jpeg)
        os.replace(tmp, out)
        if self.model != kcomfy.FLUX:           # the prompt as sent (the FLUX one is written by `prompts`)
            out.with_suffix(".prompt.txt").write_bytes(sent.encode("utf-8"))
        meta = {"id": ident, "seed": seed, "seconds": round(took, 2), "refs": [Path(r["file"]).name for r in refs],
                "width": w, "height": h, "bytes": len(jpeg), **kcomfy.model_info(self.model), "at": _now()}
        _write_json(_meta_path(out), meta)
        _log(self.root, dict(meta, kind=kind))
        self.secs.append(took)
        self.made += 1
        return meta

    def prepare_plate(self, group: str, plate: dict) -> None:
        """After a plate is made: upload it, so the first shot of its group finds it on the server."""
        self.upload({"role": "place", "group": group, "file": plate["out"]})

    def progress(self, ident: str, meta: dict) -> None:
        self.done += 1
        avg = sum(self.secs) / len(self.secs)
        left = max(self.total - self.done, 0)
        warn = f"  WARNING small file ({meta['bytes'] // 1000} kB): check it is not black" \
            if meta["bytes"] < SMALL_BYTES else ""
        print(f"  {ident}  {meta['seconds']:.1f}s  avg {avg:.1f}s  {left} left, ~{left * avg / 60:.0f} min{warn}",
              flush=True)

    def summary(self) -> None:
        print(f"made {self.made}, skipped {self.skipped}, failed {self.failed}")
        if self.secs:
            avg = sum(self.secs) / len(self.secs)
            print(f"seconds per picture: average {avg:.1f}, median {statistics.median(self.secs):.1f}; "
                  f"{sum(self.secs) / 60:.1f} min of generation; {PROJECT_PICTURES} pictures would take about "
                  f"{avg * PROJECT_PICTURES / 3600:.1f} h")


class _BudgetStop(Exception):
    """The next picture would take the run past --budget."""


class _RouterRun(_Run):
    """The same run, made by the hosted API: pictures go under <trial>/openrouter/, references are read from disk
    (a place reference is the OpenRouter plate of its group), and every request is counted against the budget."""

    def __init__(self, router: korouter.OpenRouter, trial: Path, plan: dict, resolution: str, budget: float):
        super().__init__(None, trial, plan)
        self.router, self.resolution, self.budget = router, resolution, budget
        self.root = trial / OR_DIR
        self.spent = 0.0                        # dollars spent in THIS run
        self.next_cost = korouter.estimate(resolution)

    def upload(self, ref: dict) -> str:        # nothing is uploaded: references travel inside the request
        raise NotImplementedError

    def prepare_plate(self, group: str, plate: dict) -> None:
        pass

    def ref_path(self, ref: dict) -> Path:
        base = self.root if ref.get("role") == "place" else self.trial
        return base / ref["file"]

    def make(self, kind: str, ident: str, item: dict, text: str, seed: int, refs: list[dict],
             replace_stamp: str | None = None) -> dict:
        """Generate, convert, write the picture and its meta file, log it. Raises RouterError on failure. With
        `replace_stamp` the picture it replaces is moved to old/<stamp>/ only once the new one exists."""
        paths = []
        for r in refs:
            p = self.ref_path(r)
            if not p.is_file():
                raise RouterError(f"reference {r['file']} is missing (a place picture is made by its plate first)")
            paths.append(p)
        if self.spent + self.next_cost > self.budget + 1e-9:
            raise _BudgetStop(f"stopping: ${self.spent:.4f} spent in this run + ${self.next_cost:.4f} for one more "
                              f"picture would pass --budget ${self.budget:.2f}")
        data, took, info = self.router.generate(korouter.hosted_text(text), paths, aspect_ratio="16:9",
                                                resolution=self.resolution, seed=seed)
        cost = info["cost"] if info.get("cost") is not None else korouter.COST_PER_IMAGE
        self.spent += cost                      # money is spent even when the picture turns out unusable
        try:
            jpeg, (w, h) = _to_jpeg(data)
        except ComfyError:
            raise RouterError("OpenRouter returned something that is not a picture") from None
        out = self.root / item["out"]
        out.parent.mkdir(parents=True, exist_ok=True)
        if replace_stamp:
            _archive(self.trial, [out, _meta_path(out)], out.parent, replace_stamp)
        tmp = out.with_name(out.name + ".part")
        tmp.write_bytes(jpeg)
        os.replace(tmp, out)
        meta = {"id": ident, "seed": seed, "seconds": round(took, 2), "refs": [Path(r["file"]).name for r in refs],
                "width": w, "height": h, "bytes": len(jpeg), "model": korouter.MODEL, "resolution": self.resolution,
                "cost": round(cost, 6), "via": "openrouter", "at": _now()}
        _write_json(_meta_path(out), meta)
        _log(self.root, dict(meta, kind=kind))
        self.secs.append(took)
        self.made += 1
        return meta

    def progress(self, ident: str, meta: dict) -> None:
        self.done += 1
        warn = f"  WARNING small file ({meta['bytes'] // 1000} kB): check it is not black" \
            if meta["bytes"] < SMALL_BYTES else ""
        left = max(self.total - self.done, 0)
        print(f"  {ident}  {meta['seconds']:.1f}s  ${meta['cost']:.4f}  total ${self.spent:.4f} of "
              f"${self.budget:.2f}  {left} left{warn}", flush=True)

    def summary(self) -> None:
        print(f"made {self.made}, skipped {self.skipped}, failed {self.failed}")
        if self.secs:
            print(f"seconds per picture: average {sum(self.secs) / len(self.secs):.1f}, "
                  f"median {statistics.median(self.secs):.1f}")
        print(f"cost of this run: ${self.spent:.4f} (budget ${self.budget:.2f})")


def _router_client() -> korouter.OpenRouter:
    """The hosted-API client; the base URL can be replaced by $NOVEL_OPENROUTER_URL (test hook)."""
    base = os.environ.get("NOVEL_OPENROUTER_URL", "").strip() or korouter.BASE_URL
    return korouter.OpenRouter(korouter.find_key(), base)


SHEET_SEED = 202
SHEET_LAYOUT = ("LAYOUT: the same single character drawn four times in full body, left to right: front view, "
                "three-quarter view, side view, back view, each standing straight in a relaxed neutral pose with arms "
                "at the sides, feet visible, all the same size and lined up on one ground line. On the far right, two "
                "larger head-and-shoulders close-ups of the face: front view and three-quarter view. Identical face, "
                "hair and clothes in every view.")
SHEET_END = "No background scenery, no text, no letters, no labels, no captions, no borders."


def sheet_prompt(bible: dict, key: str) -> str:
    """The text of one Qwen character sheet: the book's picture style on a plain sheet, the bible look, four views."""
    from novel import kprompts
    c = bible["characters"][key]
    style = kprompts._style(bible).replace("16:9 illustration.", "16:9 character reference sheet on a plain, flat, "
                                           "light neutral gray background.", 1)
    look = kprompts._one(c.get("look") or "").rstrip(" .")
    return f"{style}\n\nCHARACTER: {c['display']}" + (f", {look}" if look else "") + f".\n\n{SHEET_LAYOUT}\n\n{SHEET_END}\n"


def sheet_keys(args) -> list[str]:
    """--keys a,b, plus with --missing the chapter's referenced people (a Flow name on their base look) that have no
    sheet yet. The cast comes from the production scene file, else the trial's."""
    from novel import bible as B
    from . import episode_scenes as S
    from . import ktrial as K
    book, chapter, _, ep_root, trial = K._setup(args)
    bdir = B.bible_dir(book)
    chars = B.load_characters(bdir)["characters"]
    keys = [k.strip() for k in (getattr(args, "keys", None) or "").split(",") if k.strip()]
    if getattr(args, "missing", False):
        f = S.scenes_path(ep_root, chapter)
        f = f if f.exists() else K.scenes_file(trial, chapter)
        doc = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"scenes": []}
        cast = {c["key"] for s in doc["scenes"] for c in s.get("cast", [])}
        keys += sorted(k for k in cast if k in chars and ((chars[k].get("variants") or {}).get("base") or {}).get("flow_name")
                       and not K.qwen_sheet(trial.parent, k) and k not in keys)
    unknown = [k for k in keys if k not in chars]
    if unknown:
        sys.exit(f"not in the bible: {', '.join(unknown)}")
    return keys


def cmd_charsheet(args, trial: Path) -> int:
    """Make Qwen character sheets in <book>/bible/qwen/<key>_sheet_seed202.jpg on the running server (no reference:
    text only, from the bible look). The picture prompts attach them as each person's reference."""
    from novel import bible as B
    keys = sheet_keys(args)
    if not keys:
        print("no character sheet to make")
        return 0
    server = load_server(trial)
    if not server.get("url") or server.get("model") != kcomfy.QWEN:
        print("no Qwen server recorded for this chapter: run `novel ktrial up --model qwen-image-turbo` first")
        return 1
    bdir = B.bible_dir(args.book)
    bible, out_dir = B.load_characters(bdir), bdir / "qwen"
    run = _Run(_comfy(server), out_dir, {"width": 1376, "height": 768, "chapter": 0}, kcomfy.QWEN)
    run.root = out_dir                          # sheets live in the bible, not in a model subfolder
    run.total = len(keys)
    manifest = _read_json(out_dir / "manifest.json", {}) or {}
    for k in keys:
        name = f"{k}_sheet_seed{SHEET_SEED}.jpg"
        try:
            meta = run.make("sheet", k, {"out": name}, sheet_prompt(bible, k), SHEET_SEED, [])
        except ComfyError as e:
            print(f"  {k}: failed: {e}")
            run.failed += 1
            continue
        manifest[k] = {"file": name, "seed": SHEET_SEED, "display": bible["characters"][k]["display"]}
        _write_json(out_dir / "manifest.json", manifest)
        print(f"  {k}: {name} ({meta['seconds']:.0f}s)")
    print(f"character sheets: made {run.made}, failed {run.failed} -> {out_dir}")
    return 1 if run.failed else 0


def cmd_qa(args, trial: Path) -> int:
    """Picture QA by hand of the chapter's Qwen pictures that have no verdict yet (the same flow `novel images` runs
    right after each picture). --status only prints the status list (<trial>/qwen-image-turbo/qa.json)."""
    from novel import kqa
    record = trial / kcomfy.QW_DIR / "qa.json"
    if getattr(args, "status", False):
        rec = _read_json(record, {}) or {}
        print(kqa.status_table(rec) or "no status list yet")
        print(f"{kqa.summary(rec)} ({record})" if rec else "")
        return 0
    plan = _read_json(trial / "plan.json")
    server = answering(load_server(trial))
    if not plan or server is None:
        print("picture QA: no plan.json or no answering server for this chapter (a redraw needs the server)")
        return 1
    run = _Run(_comfy(server), trial, plan, kcomfy.QWEN)
    return _qa_finish(_qa_live(args, trial, plan, run), run, plan, 0)


def cmd_run(args, trial: Path) -> int:
    trial = Path(trial)
    plan = _read_json(trial / "plan.json")
    if not plan:
        print(f"no plan.json in {trial}: run `novel ktrial prompts` first")
        return 1
    via = getattr(args, "via", None) or "kaggle"
    if via == "openrouter":
        budget = getattr(args, "budget", None)
        try:
            run = _RouterRun(_router_client(), trial, plan, getattr(args, "resolution", None) or "1K",
                             float(DEFAULT_BUDGET if budget is None else budget))
        except RouterError as e:
            print(e)
            return 1
        print(f"via OpenRouter, model {korouter.MODEL}, resolution {run.resolution}, budget ${run.budget:.2f}; "
              f"pictures go to {run.root}")
    else:
        server = load_server(trial)
        if not server.get("url"):
            print("no server address recorded: run `novel ktrial up` (or `novel ktrial url ADDRESS` to store it by hand)")
            return 1
        model = server.get("model") or kcomfy.FLUX
        live = answering(server, retries=2)                      # a dead tunnel should not cost half a minute
        if live is None:
            print(f"the server at {server['url']} does not answer"
                  + (f" (nor at {server['fallback_url']})" if server.get("fallback_url") else "") + ".\n"
                  f"Check it with `novel ktrial status`; if it has stopped, start a new one with `novel ktrial up`.")
            return 1
        if live["url"] != server["url"]:
            save_server(trial, live)
        server = live
        comfy = _comfy(server)
        run = _Run(comfy, trial, plan, model)
        if model != kcomfy.FLUX:
            info = kcomfy.model_info(model)
            print(f"model {info['model']} + {info['text_encoder']}; pictures go to {run.root}")
        second = _second_card(server) if model == kcomfy.QWEN else None
        live = _qa_live(args, trial, plan, run) if getattr(args, "qa", False) and model == kcomfy.QWEN else None
        if second is not None and not plan["plates"]:
            two = _Run(second, trial, plan, model)
            run.live = two.live = live
            rc = _execute_two([run, two], args, plan)
        else:
            run.live = live
            rc = _execute(run, args, plan)
        return _qa_finish(live, run, plan, rc)
    return _execute(run, args, plan)


def _qa_item(run: _Run, s: dict) -> dict:
    """What the picture QA gets for one shot: its id, its picture, the prompt as sent (else the plan's prompt)."""
    out = run.root / s["out"]
    sent = out.with_suffix(".prompt.txt")
    brief = (sent if sent.exists() else run.trial / s["prompt"]).read_text(encoding="utf-8").strip()
    return {"id": _short(s), "path": out, "brief": brief}


def _qa_live(args, trial: Path, plan: dict, run: _Run):
    """A kqa.Live for this chapter (status list: <run root>/qa.json), every shot without a verdict marked queued;
    None (said why) without an OpenCode key or with images.qa.enabled off."""
    from novel import kqa
    from novel.core import settings
    cfg = (settings.effective(getattr(args, "book", None)).get("images") or {}).get("qa") or {}
    key = kqa.find_key()
    if not cfg.get("enabled", True) or not key:
        print(f"picture QA skipped ({'images.qa.enabled is off' if key else f'no {kqa.KEY_VAR} in secrets.env'})")
        return None
    shots = {_short(s): s for s in plan["shots"]}

    def redraw(it, fix):
        s = shots[it["id"]]
        text = (trial / s["prompt"]).read_text(encoding="utf-8").strip() + ("\n\n" + fix if fix else "")
        seed = _next_seed(run.root / s["out"], s["seed"], False) + 1000
        run.make("shot", it["id"], s, text, seed, s["refs"], _stamp())

    live = kqa.Live(kqa.QA(cfg, key), run.root / "qa.json", redraw)
    live.queued([i for i, s in shots.items()
                 if not (_valid_jpeg(run.root / s["out"]) and live.checked(_qa_item(run, s)))])
    print(f"picture QA on: each picture is checked as soon as it is drawn; status list {run.root / 'qa.json'}")
    return live


def _qa_finish(live, run: _Run, plan: dict, rc: int) -> int:
    """Check the pictures that were already there and never checked, wait for the workers, print the status."""
    if live is None:
        return rc
    from novel import kqa
    for s in plan["shots"]:
        e = live.rec.get(_short(s)) or {}
        if _valid_jpeg(run.root / s["out"]) and e.get("status") == "queued":
            live.drawn(_qa_item(run, s))
    rec = live.wait()
    print(f"picture QA: {kqa.summary(rec)} (status list: {run.root / 'qa.json'})")
    return rc


def answering(server: dict, retries: int = 1) -> dict | None:
    """The server record with the address that answers: its url (ngrok), else its fallback_url (the named Cloudflare
    tunnel the notebook opens when ngrok fails). None when neither answers."""
    for url in filter(None, (server.get("url"), server.get("fallback_url"))):
        try:
            _comfy(dict(server, url=url), timeout=15, retries=retries, backoff=0.5).ping()
            return dict(server, url=url)
        except ComfyError:
            continue
    return None


def _second_card(server: dict) -> Comfy | None:
    """The server's second ComfyUI (the other T4) behind <url>/g2, or None when it is not running."""
    two = dict(server, url=server["url"].rstrip("/") + kcomfy.SECOND_PREFIX)
    try:
        _comfy(two, retries=0, backoff=0.5).ping()
    except ComfyError:
        return None
    return _comfy(two)


def _execute_two(runs: list, args, plan: dict) -> int:
    """Both cards at once: the chosen pictures alternate between them, each card works through its own half."""
    from concurrent.futures import ThreadPoolExecutor
    shots, _ = _select(plan, getattr(args, "only", None), getattr(args, "limit", None))
    ids = [_short(s) for s in shots]
    halves = [h for h in (ids[0::2], ids[1::2]) if h]
    print(f"two cards: {' + '.join(str(len(h)) for h in halves)} pictures")
    jobs = [(r, SimpleNamespace(**{**vars(args), "only": ",".join(h), "limit": None})) for r, h in zip(runs, halves)]
    with ThreadPoolExecutor(len(jobs)) as pool:
        codes = [f.result() for f in [pool.submit(_execute, r, a, plan) for r, a in jobs]]
    return max(codes, default=0)


def _execute(run: _Run, args, plan: dict) -> int:
    """The plates, then the shots: the same for both ways of making a picture. Everything is written under run.root."""
    root = run.root
    reroll, redo = getattr(args, "reroll", False), getattr(args, "redo", False)
    shots, named_groups = _select(plan, getattr(args, "only", None), getattr(args, "limit", None))
    stamp = _stamp()
    plates = {p["group"]: p for p in plan["plates"]}
    wanted = [g for g in plates if g in {s["group"] for s in shots}]
    regen = named_groups if (reroll or redo) else set()
    shot_todo = [s for s in shots if reroll or redo or not _valid_jpeg(root / s["out"])]
    plate_todo = [g for g in wanted if g in regen or not _valid_jpeg(root / plates[g]["out"])]
    if getattr(args, "plates_only", False):
        shots, shot_todo = [], []
    run.total = len(plate_todo) + len(shot_todo)
    run.skipped = (len(wanted) - len(plate_todo)) + (len(shots) - len(shot_todo))
    print(f"{len(wanted)} places ({len(plate_todo)} to make), {len(shots)} pictures ({len(shot_todo)} to make)")

    fails = 0
    broken_groups: set[str] = set()
    try:
        for g in wanted:
            p = plates[g]
            out = root / p["out"]
            if g not in plate_todo:
                continue                        # reused: uploaded when the first picture of its group needs it
            seed = _next_seed(out, p["seed"], reroll) if g in regen else int(p["seed"])
            try:
                text = (run.trial / p["prompt"]).read_text(encoding="utf-8").strip()
                meta = run.make("plate", g, p, text, seed, [], stamp if g in regen else None)
                run.prepare_plate(g, p)
            except RUN_ERRORS as e:
                fails = _failed(run, "plate", g, e, fails)
                if not _valid_jpeg(out):
                    broken_groups.add(g)
                if fails >= MAX_FAILS_IN_ROW:
                    return _stop_after_failures(run)
                continue
            fails = 0
            run.progress(g, meta)

        for s in shot_todo:
            ident = _short(s)
            if s["group"] in broken_groups:
                print(f"  {ident}: skipped, its place picture {s['group']} is not available")
                run.failed += 1
                run.done += 1
                continue
            out = root / s["out"]
            seed = _next_seed(out, s["seed"], reroll) if reroll or redo else int(s["seed"])
            if run.live:
                run.live.drawing(ident)
            try:
                text = (run.trial / s["prompt"]).read_text(encoding="utf-8").strip()
                meta = run.make("shot", ident, s, text, seed, s["refs"], stamp if reroll or redo else None)
            except RUN_ERRORS as e:
                if run.live:
                    run.live.set(ident, "draw_failed", error=str(e)[:300])
                fails = _failed(run, "shot", ident, e, fails)
                if fails >= MAX_FAILS_IN_ROW:
                    return _stop_after_failures(run)
                continue
            fails = 0
            run.progress(ident, meta)
            if run.live:
                run.live.drawn(_qa_item(run, s))
    except _BudgetStop as e:
        print(f"  {e}")
        run.summary()
        return 0
    except KeyboardInterrupt:
        print("\nstopped by Ctrl+C; finished pictures stay, run again to continue")
        run.summary()
        return 130
    run.summary()
    return 1 if run.failed else 0


def _failed(run: _Run, kind: str, ident: str, err: Exception, fails: int) -> int:
    print(f"  {ident}  FAILED: {err}", flush=True)
    _log(run.root, {"id": ident, "kind": kind, "error": str(err)[:500], "at": _now()})
    run.failed += 1
    run.done += 1
    return fails + 1


def _stop_after_failures(run: _Run) -> int:
    where = "Look at the messages above" if isinstance(run, _RouterRun) else "Look at `novel ktrial status`"
    print(f"{MAX_FAILS_IN_ROW} failures in a row: stopping. {where}; finished pictures "
          f"stay and a new run continues.")
    run.summary()
    return 1


# ---------------------------------------------------------------- report

def crop_box(width: int, height: int, aspect: float, focus) -> tuple[int, int, int, int]:
    """The largest window of `aspect` (width / height) inside the picture, centred on `focus` ([x, y] in 0..1,
    default the centre) and clamped to the picture. Returns (left, top, right, bottom) in pixels."""
    try:
        fx, fy = (min(max(float(v), 0.0), 1.0) for v in focus)
    except (TypeError, ValueError):
        fx = fy = 0.5
    if width / height > aspect:
        cw, ch = height * aspect, float(height)
    else:
        cw, ch = float(width), width / aspect
    left = min(max(fx * width - cw / 2, 0.0), width - cw)
    top = min(max(fy * height - ch / 2, 0.0), height - ch)
    return round(left), round(top), min(round(left + cw), width), min(round(top + ch), height)


def _cutout(src: Path, dst: Path, aspect: float, focus) -> None:
    with Image.open(src) as im:
        im = im.convert("RGB")
        box = crop_box(im.width, im.height, aspect, focus)
        crop = im.crop(box)
        crop = crop.resize((max(round(CUT_HEIGHT * crop.width / crop.height), 1), CUT_HEIGHT), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        crop.save(dst, "JPEG", quality=88)


def _find_scene(scenes: list[dict], n: int, sid: str | None = None) -> dict | None:
    """The scene of window n: by id (the number, or the plan's shot id), else by position."""
    for s in scenes:
        if str(s.get("id")) in (str(n), str(sid)):
            return s
    return scenes[n - 1] if 0 < n <= len(scenes) else None


def _shape(scene: dict | None) -> tuple[str, float]:
    """Panel shape name and aspect; a scene without `characters` gets them from `cast`."""
    scene = dict(scene or {})
    if "characters" not in scene:
        scene["characters"] = [c.get("key") if isinstance(c, dict) else c for c in scene.get("cast") or []]
    try:
        name = shape_for(scene)
    except SystemExit:
        name = "landscape"
    return name, SHAPES[name]


def _cast_names(shot: dict, scene: dict | None) -> list[str]:
    names = [r.get("display") or r.get("key") for r in shot.get("refs", []) if r.get("role") == "character"]
    if names:
        return [n for n in names if n]
    out = []
    for c in (scene or {}).get("cast") or []:
        out.append(str(c.get("display") or c.get("name") or c.get("key")) if isinstance(c, dict) else str(c))
    return out


def _stats(trial: Path, plan: dict) -> dict:
    metas = [m for s in plan["shots"] if (m := _read_json(_meta_path(trial / s["out"]))) and m.get("seconds") is not None]
    secs = [float(m["seconds"]) for m in metas]
    log = [e for e in (_read_json_lines(trial / "log.jsonl")) if e.get("seconds") is not None and not e.get("error")]
    total = sum(float(e["seconds"]) for e in log) if log else sum(secs) + sum(
        float(m["seconds"]) for p in plan["plates"] if (m := _read_json(_meta_path(trial / p["out"])))
        and m.get("seconds") is not None)
    avg = sum(secs) / len(secs) if secs else None
    return {"done": sum(1 for s in plan["shots"] if _valid_jpeg(trial / s["out"])), "of": len(plan["shots"]),
            "avg": avg, "median": statistics.median(secs) if secs else None, "total_min": total / 60,
            "hours": avg * PROJECT_PICTURES / 3600 if avg is not None else None}


def _or_stats(trial: Path, plan: dict) -> dict:
    """Count, average seconds and total cost of the OpenRouter pictures (the cost counts every picture made,
    re-rolls included, as the log has them; without a log it adds the meta files)."""
    root = trial / OR_DIR
    metas = [m for s in plan["shots"] if _valid_jpeg(root / s["out"]) and (m := _read_json(_meta_path(root / s["out"])))]
    secs = [float(m["seconds"]) for m in metas if m.get("seconds") is not None]
    log = [e for e in _read_json_lines(root / "log.jsonl") if e.get("cost") is not None and not e.get("error")]
    if log:
        cost = sum(float(e["cost"]) for e in log)
    else:
        cost = sum(float(m["cost"]) for m in metas if m.get("cost") is not None) + sum(
            float(m["cost"]) for p in plan["plates"] if (m := _read_json(_meta_path(root / p["out"])))
            and m.get("cost") is not None)
    return {"done": len(metas), "of": len(plan["shots"]), "avg": sum(secs) / len(secs) if secs else None,
            "cost": cost}


def _read_json_lines(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


CSS = """
body{font-family:system-ui,Segoe UI,sans-serif;margin:0;padding:16px 24px;background:#f4f1ea;color:#1c1c1c}
h1{margin:0 0 4px}h2{margin:28px 0 8px;font-size:1.15em}
.sum{display:flex;flex-wrap:wrap;gap:8px 24px;margin:8px 0 16px}.sum b{font-size:1.2em}
.group{display:flex;gap:16px;align-items:flex-start;background:#fff;border:1px solid #d9d4c7;padding:12px;margin:8px 0}
.group img{max-width:360px;height:auto}.win{background:#fff;border:1px solid #d9d4c7;padding:12px;margin:14px 0}
.win h3{margin:0 0 6px}.hi{font-size:1.05em;line-height:1.6;margin:0 0 8px}.cast{color:#555;margin:0 0 8px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}.cols3{grid-template-columns:1fr 1fr 1fr}
.col h4{margin:0 0 6px}
.col img{max-width:100%;height:auto;display:block;border:1px solid #ccc}
.cut{margin-top:8px}.cut img{max-height:420px;width:auto}
.missing{background:#e7e3d8;color:#666;padding:36px 12px;text-align:center;border:1px dashed #aaa}
.small{margin-top:8px}.small b{color:#a40}.note{color:#555;font-size:.9em;margin:4px 0}
.warn{color:#a40}pre{white-space:pre-wrap;background:#f4f1ea;padding:8px;margin:4px 0}
@media(max-width:800px){.cols,.cols3{grid-template-columns:1fr}}
"""


def _box(rel: str | None, label: str, missing: str) -> str:
    if not rel:
        return f'<div class="missing">{html.escape(missing)}</div>'
    r = html.escape(rel, quote=True)
    return f'<a href="{r}"><img src="{r}" alt="{html.escape(label, quote=True)}" loading="lazy"></a>'


def cmd_report(args, trial: Path) -> int:
    trial = Path(trial)
    plan = _read_json(trial / "plan.json")
    if not plan:
        print(f"no plan.json in {trial}: run `novel ktrial prompts` first")
        return 1
    trial_scenes = (_read_json(trial / plan.get("trial_scenes", ""), {}) or {}).get("scenes") or []
    prod_scenes = (_read_json(Path(plan.get("production_scenes") or ""), {}) or {}).get("scenes") or []
    flow_dir = Path(plan["flow_images"]) if plan.get("flow_images") else None
    cut_dir = trial / "report" / "cut"
    if cut_dir.exists():
        shutil.rmtree(cut_dir)        # a cutout of a picture that has since been replaced must not linger
    st = _stats(trial, plan)
    or_root = trial / OR_DIR
    has_or = (or_root / "images").is_dir()               # the third column exists only once a run made it
    ost = _or_stats(trial, plan) if has_or else None
    f1 = lambda v: "-" if v is None else f"{v:.1f}"      # noqa: E731
    h = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Picture trial, chapter '
         f'{html.escape(str(plan.get("chapter")))}</title><style>{CSS}</style></head><body>',
         f'<h1>Picture trial: {html.escape(str(plan.get("book")))} chapter {html.escape(str(plan.get("chapter")))}'
         f' (episode {html.escape(str(plan.get("episode")))})</h1>',
         f'<div class="sum"><span>model <b>{html.escape(kcomfy.UNET_NAME)}</b>, {kcomfy.STEPS} steps, '
         f'{plan.get("width")}x{plan.get("height")}</span>'
         f'<span>pictures <b>{st["done"]}/{st["of"]}</b></span>'
         f'<span>average <b>{f1(st["avg"])} s</b></span><span>median <b>{f1(st["median"])} s</b></span>'
         f'<span>GPU time <b>{st["total_min"]:.1f} min</b> (with places and re-rolls)</span>'
         f'<span>{PROJECT_PICTURES} pictures would take <b>{f1(st["hours"])} h</b></span></div>']
    if ost is not None:
        h.append(f'<div class="sum"><span>OpenRouter {html.escape(korouter.MODEL)}: '
                 f'pictures <b>{ost["done"]}/{ost["of"]}</b></span>'
                 f'<span>average <b>{f1(ost["avg"])} s</b></span>'
                 f'<span>total cost <b>${ost["cost"]:.4f}</b></span></div>')

    h.append("<h2>Places</h2>")
    for p in plan["plates"]:
        pm = _read_json(_meta_path(trial / p["out"]), {}) or {}
        have = _valid_jpeg(trial / p["out"])
        info = f'{html.escape(str(p.get("name", "")))}<br>{html.escape(str(p.get("time", "")))}, ' \
               f'{html.escape(str(p.get("light", "")))}'
        if pm:
            info += f'<br>{pm.get("seconds")} s, seed {pm.get("seed")}'
        or_box = ""
        if has_or:
            opm = _read_json(_meta_path(or_root / p["out"]), {}) or {}
            ohave = _valid_jpeg(or_root / p["out"])
            oinfo = f'<div class="note">OpenRouter: {opm.get("seconds")} s, ${float(opm.get("cost") or 0):.4f}</div>' \
                if opm else ""
            orel = f"{OR_DIR}/{p['out']}" if ohave else None
            or_box = f'<div>{_box(orel, p["group"] + " (OpenRouter)", "not generated yet")}{oinfo}</div>'
        h.append(f'<div class="group"><div style="min-width:260px"><b>{html.escape(str(p["group"]))}</b><br>{info}</div>'
                 f'{_box(p["out"] if have else None, p["group"], "not generated yet")}{or_box}</div>')

    h.append("<h2>Windows</h2>")
    shots = {int(s["n"]): s for s in plan["shots"]}
    for key in sorted(plan.get("windows", {}), key=int):
        n = int(key)
        shot = shots.get(n)
        scene = _find_scene(trial_scenes, n, shot["id"] if shot else None)
        pscene = _find_scene(prod_scenes, n)
        cast = _cast_names(shot, scene) if shot else []
        h.append(f'<div class="win"><h3>Window {n}</h3><p class="hi" lang="hi">'
                 f'{html.escape(str(plan["windows"][key]))}</p>')
        if shot:
            h.append(f'<p class="cast">{html.escape(str(shot["id"]))}: {html.escape(str(shot.get("shot", "")))}'
                     f'{", " + html.escape(", ".join(cast)) if cast else ", no people"}'
                     f' (group {html.escape(str(shot["group"]))})</p>')
        # Flow column
        flow_rel = flow_cut = None
        flow_src = flow_dir / f"s{n:02d}-2k.jpg" if flow_dir else None
        pshape, paspect = _shape(pscene or scene)
        if flow_src is not None and flow_src.is_file():
            flow_rel = flow_src.resolve().as_uri()
            try:
                _cutout(flow_src, cut_dir / f"flow-s{n:02d}.jpg", paspect, (pscene or scene or {}).get("focus"))
                flow_cut = f"report/cut/flow-s{n:02d}.jpg"
            except OSError:
                pass
        # trial column
        trial_rel = trial_cut = None
        meta = {}
        tshape, taspect = _shape(scene)
        if shot and _valid_jpeg(trial / shot["out"]):
            trial_rel = shot["out"]
            meta = _read_json(_meta_path(trial / shot["out"]), {}) or {}
            try:
                _cutout(trial / shot["out"], cut_dir / f"trial-s{n:02d}.jpg", taspect, (scene or {}).get("focus"))
                trial_cut = f"report/cut/trial-s{n:02d}.jpg"
            except OSError:
                pass
        prompt = ""
        if shot:
            try:
                prompt = (trial / shot["prompt"]).read_text(encoding="utf-8").strip()
            except OSError:
                pass
        info = f'{meta.get("seconds")} s, seed {meta.get("seed")}' if meta else (
            f'seed {shot.get("seed")}' if shot else "")
        small = f'<p class="small warn">small file ({meta["bytes"] // 1000} kB): check it is not black</p>' \
            if meta and meta.get("bytes", SMALL_BYTES) < SMALL_BYTES else ""
        or_col = ""
        if has_or:
            or_rel = or_cut = None
            ometa = {}
            if shot and _valid_jpeg(or_root / shot["out"]):
                or_rel = f"{OR_DIR}/{shot['out']}"
                ometa = _read_json(_meta_path(or_root / shot["out"]), {}) or {}
                try:
                    _cutout(or_root / shot["out"], cut_dir / f"or-s{n:02d}.jpg", taspect, (scene or {}).get("focus"))
                    or_cut = f"report/cut/or-s{n:02d}.jpg"
                except OSError:
                    pass
            oinfo = f'{ometa.get("seconds")} s, ${float(ometa.get("cost") or 0):.4f}, seed {ometa.get("seed")}'                 if ometa else ""
            or_col = (f'<div class="col"><h4>FLUX.3 klein (OpenRouter)</h4>'
                      f'{_box(or_rel, "OpenRouter picture", "not generated yet")}'
                      f'<div class="cut"><div class="note">panel cutout: {tshape}</div>'
                      f'{_box(or_cut, "OpenRouter cutout", "not generated yet")}</div>'
                      f'<p class="note">{html.escape(oinfo)}</p>'
                      f'<details><summary>prompt sent</summary><pre>{html.escape(korouter.hosted_text(prompt))}</pre>'
                      f'</details></div>')
        h.append(f'<div class="cols{" cols3" if has_or else ""}">'
                 f'<div class="col"><h4>Flow</h4>{_box(flow_rel, "Flow picture", "no Flow picture")}'
                 f'<div class="cut"><div class="note">panel cutout: {pshape}</div>'
                 f'{_box(flow_cut, "Flow cutout", "no Flow picture")}</div></div>'
                 f'<div class="col"><h4>FLUX.3 klein (Kaggle T4)</h4>{_box(trial_rel, "trial picture", "not generated yet")}'
                 f'<div class="cut"><div class="note">panel cutout: {tshape}</div>'
                 f'{_box(trial_cut, "trial cutout", "not generated yet")}</div>'
                 f'<p class="note">{html.escape(info)}</p>{small}'
                 f'<details><summary>prompt</summary><pre>{html.escape(prompt)}</pre></details></div>'
                 f'{or_col}</div></div>')
    h.append("</body></html>")
    out = trial / "report.html"
    out.write_text("\n".join(h), encoding="utf-8")
    print(f"report: {out}  ({st['done']}/{st['of']} pictures)")
    return 0
