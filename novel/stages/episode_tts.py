"""Narrate an episode's locked Hindi with Maya 2 on Kaggle, and bring the audio home.

Its own private dataset and kernel (<book>-e001-tts for episode 1), so nothing here
can touch the daily audiobook kernels, the ledger or the master manifest -
the same separation as mayatrial.py, which this grows out of.

What the video needs and the audiobook flow throws away: the per-chapter mp3s
(kbatch keeps only the m4b) and the measured timing maps. Both are pulled.

Guards, each for a way this has gone wrong before:
  * Only locked Hindi is narrated, and the kernel reports the hash of what it
    read. Audio of a text that was edited afterwards would put every scene in
    the wrong place.
  * A push is refused while the previous run is unpulled: re-pushing a kernel
    destroys its output.
  * The upload is confirmed live by a marker file unique to this push, not by
    a chapter file an older version also had.

A kernel that ended in ERROR (or was cancelled) leaves a run that can never be pulled; `novel tts reset`
marks it abandoned (tts-state.json keeps it under "history") so that push works again.

Run it through `novel tts push|status|pull|watch|reset` (novel/commands/tts_cmd.py). Every
Kaggle call goes through kbatch.kaggle(), which honours $NOVEL_KAGGLE_CMD; the
login is chosen by account.activate_token() (token file only).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import episode as E
from .account import activate_token, owner, resolve_profile
from .episode_translate import parse_range
from .kbatch import fetch, kaggle, log_text, run
from .manifest import DONE, Manifest

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = REPO / "kaggle" / "episode_tts.ipynb"


def book_slug() -> str:
    """The book's name in Kaggle slugs: settings book.slug, else the book's folder name."""
    from novel.core import context, settings
    book = context.active_book()
    name = settings.get("book.slug", book=book, default=None) or book
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def slug(episode: int) -> str:
    """The episode's dataset: <book>-e007-tts (coi: coi-e001-tts, as before)."""
    return f"{book_slug()}-e{episode:03d}-tts"


def kernel_slug(episode: int) -> str:
    """The episode's notebook. Not the dataset's name: Kaggle answered 409 Conflict when
    asked to create a kernel with the same slug as a dataset on the account."""
    return f"{book_slug()}-e{episode:03d}-narrate"


def work_dir(episode: int) -> Path:
    from novel.core import context, paths
    return paths.kaggle_work_dir(context.active_book()) / slug(episode)


def tts_settings(episode: int) -> dict:
    """voice, model and bitrate from settings (tts.*); only Maya 2 is wired up."""
    from novel.core import context, settings
    book = context.active_book()

    def get(k):
        return settings.get(f"tts.{k}", book=book, ep=episode, default=None)

    if get("engine") not in (None, "maya2"):
        sys.exit(f"tts.engine is '{get('engine')}' but the episode notebook only runs maya2")
    return {"voice": get("voice"), "model": get("model"), "bitrate": get("bitrate")}


def is_locked(ep_root: Path, idx: int) -> bool:
    """Locked = hindi.lock.json has the chapter AND the file still matches its hash
    (the same test episode.check_lock enforces)."""
    try:
        E.check_lock(ep_root, idx)
        return True
    except (SystemExit, FileNotFoundError):
        return False


def has_valid_audio(ep_root: Path, c) -> bool:
    name = f"{c.idx:05d}"
    return (c.status == DONE and (ep_root / "audio" / f"{name}.mp3").exists()
            and (ep_root / "timing" / f"{name}.json").exists())


def default_chapters(episode: int) -> list[int]:
    """Chapters of the episode that are locked and have no valid audio yet."""
    ep_root = E.root(episode)
    man = Manifest.load(ep_root / "manifest.json")
    return [c.idx for c in man.chapters
            if is_locked(ep_root, c.idx) and not has_valid_audio(ep_root, c)]


def state_path(episode: int) -> Path:
    return E.root(episode) / "tts-state.json"


def load_state(episode: int) -> dict:
    p = state_path(episode)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_state(episode: int, state: dict) -> None:
    state_path(episode).write_text(json.dumps(state, indent=1), encoding="utf-8")


# Kaggle kernel words that mean "this run is over and has no output to pull".
ENDED = ("ERROR", "CANCEL_ACKNOWLEDGED", "CANCEL_REQUESTED")


def run_waiting(state: dict) -> bool:
    """A run was pushed and has neither been pulled nor abandoned (`novel tts reset`)."""
    return bool(state.get("pushed") and not state.get("pulled") and not state.get("abandoned"))


def note_status(episode: int, state: dict, word: str) -> None:
    """Remember the last status read from Kaggle, so `novel status` (which never calls Kaggle) can name
    `novel tts reset` when the run ended in error."""
    if state.get("last_status") != word:
        state["last_status"] = word
        state["last_status_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_state(episode, state)


def write_config(dest: Path, episode: int) -> None:
    """The staged config.yaml: the payload's, with voice and model from settings.
    (The notebook itself sets output.bitrate to 128k, so tts.bitrate is not written.)
    The payload file is never modified."""
    cfg = yaml.safe_load((REPO / "kaggle" / "payload" / "config.yaml").read_text(encoding="utf-8"))
    t = tts_settings(episode)
    if t["voice"]:
        cfg["tts"]["maya2"]["voice"] = t["voice"]
    if t["model"]:
        cfg["tts"]["maya2"]["model_id"] = t["model"]
    if t["bitrate"] and str(t["bitrate"]) != "128k":
        print(f"  note: tts.bitrate {t['bitrate']} is ignored; the notebook encodes at 128k")
    dest.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")


def stage(episode: int, chapters: list[int], user: str, marker: str) -> Path:
    ep_root = E.root(episode)
    man = Manifest.load(ep_root / "manifest.json")
    rows = [c for c in man.chapters if c.idx in chapters]
    if len(rows) != len(chapters):
        sys.exit(f"chapters {sorted(set(chapters) - {c.idx for c in rows})} are not in the episode")
    for c in rows:
        E.check_lock(ep_root, c.idx)

    st = work_dir(episode) / "dataset"
    if st.exists():
        shutil.rmtree(st)
    (st / "hindi").mkdir(parents=True)
    slice_ch = []
    for c in rows:
        shutil.copy2(ep_root / "hindi" / f"{c.idx:05d}.txt", st / "hindi")
        # Always "translated": a "failed" row would send runner.py into the
        # translation path, which this kernel has no model for.
        slice_ch.append({"idx": c.idx, "title": c.title, "source": c.source,
                         "status": "translated", "hindi": f"{c.idx:05d}.txt",
                         "audio": None, "timing": None, "seconds": None, "error": None,
                         "attempts": 0, "translate_seconds": None, "tts_seconds": None,
                         "rtf": None})
    (st / "manifest.json").write_text(json.dumps(
        {"version": 1, "chapters": slice_ch}, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.copytree(REPO / "kaggle" / "payload" / "abpipe", st / "abpipe",
                    ignore=shutil.ignore_patterns("__pycache__"))
    write_config(st / "config.yaml", episode)
    (st / marker).write_text(" ".join(map(str, chapters)), encoding="utf-8")
    (st / "dataset-metadata.json").write_text(json.dumps(
        {"title": slug(episode), "id": f"{user}/{slug(episode)}",
         "licenses": [{"name": "unknown"}]}, indent=2), encoding="utf-8")
    return st


def stage_kernel(episode: int, user: str) -> Path:
    kd = work_dir(episode) / "kernel"
    kd.mkdir(parents=True, exist_ok=True)
    ks = kernel_slug(episode)
    for old in kd.glob("*.ipynb"):
        old.unlink()                 # kernels push rejects a folder with a stray notebook
    nb = kd / f"{ks}.ipynb"
    shutil.copy2(NOTEBOOK, nb)
    # The notebook finds its dataset by this slug.
    nb.write_text(nb.read_text(encoding="utf-8").replace("@DATASET@", slug(episode)),
                  encoding="utf-8")
    (kd / "kernel-metadata.json").write_text(json.dumps({
        "id": f"{user}/{ks}", "title": ks,
        "code_file": f"{ks}.ipynb", "language": "python",
        "kernel_type": "notebook", "is_private": True, "enable_gpu": True,
        "enable_tpu": False, "enable_internet": True, "keywords": [],
        "dataset_sources": [f"{user}/{slug(episode)}", f"{user}/hf-token"],
        "kernel_sources": [], "competition_sources": [], "model_sources": [],
        "machine_shape": "NvidiaTeslaT4",
    }, indent=2), encoding="utf-8")
    return kd


def upload(st: Path, ds: str, marker: str) -> None:
    # A dataset that does not exist answers 403, not 404. From INSIDE the
    # folder, and never -d (see kbatch). New datasets are private by default.
    if "ready" in kaggle("datasets", "status", ds, timeout=180):
        up = kaggle("datasets", "version", "-p", ".", "-m", marker, "-r", "zip",
                    cwd=st, timeout=1800)
    else:
        up = kaggle("datasets", "create", "-p", ".", "-r", "zip", cwd=st, timeout=1800)
    if "is being created" not in up and "successfully" not in up.lower():
        sys.exit(f"upload did not complete:\n{up.strip()[-400:]}")
    for _ in range(40):
        status = kaggle("datasets", "status", ds, timeout=180)
        files = kaggle("datasets", "files", ds, "--page-size", "200", timeout=180)
        if "ready" in status and marker in files:
            print(f"  dataset live: {ds} ({marker})")
            return
        time.sleep(float(os.environ.get("NOVEL_TTS_UPLOAD_WAIT", "15")))
    sys.exit(f"dataset {ds} never showed {marker} - refusing to push the kernel")


def cmd_push(args) -> int:
    state = load_state(args.episode)
    blocked = run_waiting(state)
    msg = (f"the run pushed at {state.get('pushed')} (chapters {state.get('chapters')}) "
           f"has not been pulled. Pushing again would destroy its output: run `pull` first. "
           f"(If the Kaggle run ended in ERROR and has nothing to pull: `novel tts reset`.)")
    if blocked and not args.dry_run:
        sys.exit(msg)
    chapters = parse_range(args.chapters) if args.chapters else default_chapters(args.episode)
    if not chapters:
        print("nothing to narrate: no chapter is locked and still without audio")
        return 0
    profile = resolve_profile(args.account)
    user = owner(profile)
    marker = f"push-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.txt"
    st = stage(args.episode, chapters, user, marker)
    kd = stage_kernel(args.episode, user)
    print(f"staged chapters {','.join(map(str, chapters))} for {user} (profile {profile})")
    if args.dry_run:
        print(f"dry run - nothing is called. Would upload dataset {user}/{slug(args.episode)} "
              f"and push kernel {user}/{kernel_slug(args.episode)}:")
        for f in sorted(st.rglob("*")):
            if f.is_file():
                print(f"  dataset  {f.relative_to(st).as_posix()}  ({f.stat().st_size} bytes)")
        for f in sorted(kd.glob("*")):
            print(f"  kernel   {f.name}")
        if blocked:
            print(f"  WARNING: a real push would be refused: {msg}")
        print(f"look in {work_dir(args.episode)}")
        return 0
    activate_token(profile)         # selects the login, so only once we really upload
    upload(st, f"{user}/{slug(args.episode)}", marker)
    out = kaggle("kernels", "push", "-p", ".", cwd=kd, timeout=600)
    if "successfully pushed" not in out:
        sys.exit(f"push failed:\n{out.strip()[-400:]}")
    print("  " + [l for l in out.splitlines() if "pushed" in l][0].strip())
    new = {"account": profile, "user": user, "chapters": chapters,
           "marker": marker, "pushed": marker[5:-4], "pulled": False}
    if state.get("history"):
        new["history"] = state["history"]          # abandoned runs stay on record
    save_state(args.episode, new)
    return 0


def kernel(state: dict, episode: int) -> str:
    return f"{state['user']}/{kernel_slug(episode)}"


def kernel_state(state: dict, episode: int) -> tuple[str, str]:
    """(STATUS_WORD, the CLI's status line), e.g. ("COMPLETE", '... has status "..."')."""
    raw = kaggle("kernels", "status", kernel(state, episode), timeout=180).strip()
    line = raw.splitlines()[-1] if raw else ""
    m = re.search(r"KernelWorkerStatus\.(\w+)", line) or re.search(r'status "?(\w+)"?', line)
    return (m.group(1).upper() if m else "UNKNOWN"), line


def log_lines(state: dict, episode: int) -> list[str]:
    return log_text(kaggle("kernels", "logs", kernel(state, episode), timeout=600)).splitlines()


def cmd_status(args) -> int:
    state = load_state(args.episode)
    if not state:
        sys.exit("nothing has been pushed for this episode")
    activate_token(state["account"])
    word, line = kernel_state(state, args.episode)
    print(line)
    if run_waiting(state) and word != "UNKNOWN":
        note_status(args.episode, state, word)
    if run_waiting(state) and word in ENDED:
        print(RESET_HINT)
    print("\n".join([l for l in log_lines(state, args.episode) if re.match(
        r"\s*(\[maya2\]|probe|spoke|FAILED|ABORT|venv)", l)][-15:]))
    return 0


RESET_HINT = ("This run ended without output to pull. To narrate again: `novel tts reset` "
              "(clears the failed run), then `novel tts push`.")


def duration(path: Path) -> tuple[float, int]:
    """Seconds and audio bitrate, from ffprobe."""
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=bit_rate",
               "-of", "json", str(path)], timeout=120)
    d = json.loads(out)
    return float(d["format"]["duration"]), int(d["streams"][0].get("bit_rate", 0))


def cmd_pull(args) -> int:
    state = load_state(args.episode)
    if not state or not run_waiting(state):
        sys.exit("no unpulled run for this episode" + (
            " (the last run was abandoned with `novel tts reset`)" if state.get("abandoned") else ""))
    activate_token(state["account"])
    k = kernel(state, args.episode)
    status = kaggle("kernels", "status", k, timeout=180)
    if "COMPLETE" not in status:
        hint = f" {RESET_HINT}" if any(w in status for w in ENDED) else ""
        sys.exit(f"not finished: {status.strip()[-200:]}{hint}")

    dest = work_dir(args.episode) / f"out-{state['pushed']}"
    if dest.exists():
        shutil.rmtree(dest)      # a stale folder could mix an older run's files in
    dest.mkdir(parents=True)
    fetch(k, dest, r"\.(mp3|json)$")
    out = dest / "audiobook"
    if not (out / "manifest.json").exists():
        sys.exit(f"no manifest in the output - see {dest}")

    ep_root = E.root(args.episode)
    man = Manifest.load(ep_root / "manifest.json")
    remote = {c["idx"]: c for c in json.loads(
        (out / "manifest.json").read_text(encoding="utf-8"))["chapters"]}
    hashes = json.loads((out / "hashes.json").read_text(encoding="utf-8"))
    lock = json.loads(E.lock_path(ep_root).read_text(encoding="utf-8"))
    (ep_root / "audio").mkdir(exist_ok=True)
    (ep_root / "timing").mkdir(exist_ok=True)

    took = 0
    for c in man.chapters:
        if c.idx not in state["chapters"]:
            continue
        name = f"{c.idx:05d}"
        r = remote.get(c.idx, {})
        problems = []
        if r.get("status") != DONE:
            problems.append(f"status {r.get('status')}: {(r.get('error') or '')[-200:]}")
        mp3, tmap = out / "audio" / f"{name}.mp3", out / "timing" / f"{name}.json"
        if hashes.get(f"{name}.txt") != lock.get(f"{name}.txt"):
            problems.append("narrated text is not the locked text")
        if not problems:
            if not mp3.exists() or not tmap.exists():
                problems.append("mp3 or timing map missing from the download")
            else:
                secs, bitrate = duration(mp3)
                m = json.loads(tmap.read_text(encoding="utf-8"))
                if abs(secs - r["seconds"]) > 0.1:
                    problems.append(f"mp3 is {secs:.2f}s, manifest says {r['seconds']:.2f}s")
                if abs(secs - m.get("total_seconds", secs)) > 0.1:
                    problems.append(f"mp3 is {secs:.2f}s, timing map says {m['total_seconds']:.2f}s")
                if not m.get("measured"):
                    problems.append("timing map is not measured")
                if bitrate < 120_000:
                    problems.append(f"bitrate {bitrate} - expected 128k")
        if problems:
            print(f"  {c.idx:>3}  NOT TAKEN: {'; '.join(problems)}")
            continue
        shutil.copy2(mp3, ep_root / "audio" / f"{name}.mp3")
        shutil.copy2(tmap, ep_root / "timing" / f"{name}.json")
        c.audio, c.timing, c.status = f"{name}.mp3", f"{name}.json", DONE
        c.seconds, c.tts_seconds, c.rtf = r["seconds"], r["tts_seconds"], r["rtf"]
        took += 1
        print(f"  {c.idx:>3}  ok  {c.seconds / 60:.1f} min  RTF {c.rtf}")
    man.save()
    state["pulled"] = True
    save_state(args.episode, state)
    print(f"{took}/{len(state['chapters'])} chapters brought home")
    return 0 if took == len(state["chapters"]) else 1


def cmd_watch(args) -> int:
    """Poll the kernel; pull when it completes; on an error show the log tail and return 1."""
    state = load_state(args.episode)
    if not state or not run_waiting(state):
        sys.exit("no unpulled run for this episode")
    from novel.core import settings
    interval = args.interval_seconds
    if interval is None:
        interval = 60 * float(settings.get("tts.poll_minutes", default=10))
    activate_token(state["account"])
    unknown = 0
    try:
        while True:
            word, line = kernel_state(state, args.episode)
            print(f"{datetime.now():%H:%M:%S}  {kernel(state, args.episode)}  {word}", flush=True)
            if word == "COMPLETE":
                return cmd_pull(args)
            if word in ENDED:
                note_status(args.episode, state, word)
                print(f"kernel ended with {word}. Last 15 log lines:")
                print("\n".join(log_lines(state, args.episode)[-15:]))
                print(RESET_HINT)
                return 1
            unknown = unknown + 1 if word == "UNKNOWN" else 0
            if unknown >= 5:
                print(f"status unreadable 5 times in a row: {line[-200:]}")
                return 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped watching; nothing changed (run `novel tts watch` or `pull` again)")
        return 130


def _local_time(stamp: str) -> str:
    """'20261110T174906' (UTC, the push marker) -> 'YYYY-MM-DD HH:MM' local; anything else as it is."""
    try:
        t = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return t.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(stamp)


def cmd_reset(args, ask=input) -> int:
    """Give up a Kaggle run that ended in ERROR (or was cancelled): mark it abandoned, keep it in the history,
    delete nothing. Refused while the kernel is running or finished with output waiting."""
    state = load_state(args.episode)
    if not state or not state.get("pushed"):
        print("nothing to reset: no Kaggle run is recorded for this episode.")
        return 0
    if state.get("abandoned"):
        print(f"nothing to reset: the run pushed at {_local_time(state['pushed'])} was already abandoned "
              f"({state['abandoned']}). `novel tts push` works.")
        return 0
    if state.get("pulled"):
        print(f"nothing to reset: the run pushed at {_local_time(state['pushed'])} was pulled. `novel tts push` works.")
        return 0
    print(f"Recorded run: pushed {_local_time(state['pushed'])}, "
          f"chapters {', '.join(map(str, state.get('chapters') or []))}, kernel {kernel(state, args.episode)} "
          f"(account {state.get('account')}).")
    activate_token(state["account"])
    try:
        word, line = kernel_state(state, args.episode)
    except SystemExit as e:
        word, line = "UNKNOWN", f"status call failed: {str(e)[-200:]}"
    print(f"Kaggle says now: {word}" + (f"  ({line.strip()[-160:]})" if line else ""))
    if word == "COMPLETE":
        print("REFUSED: the kernel finished and its output is waiting. Bring it home with `novel tts pull` "
              "(nothing is lost); do not reset.")
        return 1
    if word not in ENDED and word != "UNKNOWN":
        print(f"REFUSED: the kernel is still {word}. Wait for it with `novel tts watch` (it pulls when done); "
              f"reset is only for a run that ended in error or was cancelled.")
        return 1
    if word == "UNKNOWN":
        print("WARNING: Kaggle's status could not be read. If the run actually finished, resetting would "
              "leave its output on Kaggle unpulled.")
    print("Audio already pulled is never touched. The run is only marked abandoned in tts-state.json "
          "(kept under \"history\").")
    if not getattr(args, "yes", False):
        try:
            reply = ask("Abandon this run so that `novel tts push` works again? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            reply = ""
        if str(reply).strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old = {k: v for k, v in state.items() if k != "history"}
    old.update(abandoned=now, abandoned_status=word)
    state = {**old, "history": list(state.get("history") or []) + [dict(old)]}
    save_state(args.episode, state)
    print(f"Abandoned. Now narrate again: novel tts push --chapters "
          f"{','.join(map(str, old.get('chapters') or [])) or 'A-B'}")
    return 0
