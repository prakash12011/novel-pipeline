"""Build an episode video: every chapter rendered (with English subtitles), then joined behind the
copyright disclaimer, with chapter markers and timestamps. Driven by `novel build` (commands/build_cmd.py).

A chapter is rendered again only when it has no video yet, its video has no subtitles,
its last render failed a check, its logo settings changed, or its text, narration, timing,
scenes, captions or hd pictures changed after the render. Everything else is reused, so
re-running after a fix is cheap.

    novel build --ep 1 --chapters 1-5           # -> E001-c1-5.mp4
    novel build --ep 1                          # all chapters -> E001.mp4
    novel build --ep 1 --chapters 3 --force     # re-render chapter 3 anyway
    novel build --ep 2 --when-ready --per-hour 2    # wait for missing inputs, a chapter every 30 min
"""

from __future__ import annotations

import json
import multiprocessing as mp
import shutil
import time
from pathlib import Path
from typing import Callable

from . import episode as E
from . import episode_pace as P
from .episode_join import join
from .episode_render import render
from .episode_translate import parse_range
from .manifest import Manifest

MIN_IMAGE_BYTES = 1          # a download in progress is written under another name; empty means not there


def why_render(ep_root: Path, n: int) -> str | None:
    """The reason chapter n needs rendering, or None if its video is current."""
    mp4 = ep_root / "video" / f"c{n:04d}.mp4"
    rep = mp4.with_suffix(".render.json")
    if not mp4.exists():
        return "no video yet"
    if not rep.exists():
        return "no render report"
    r = json.loads(rep.read_text(encoding="utf-8"))
    if not r.get("captions"):
        return "no subtitles"
    if not r.get("pass"):
        return "last render failed a check"
    from .episode_video import DEFAULT_TITLE_FONT, logo_signature
    ep = E.info(ep_root)["episode"]
    if r.get("logo") != logo_signature(E.cfg(ep, n)["video"]["logo"]):   # old reports have no key: None
        return "logo settings changed"
    from .episode_video import title_font_setting
    if r.get("title_font", DEFAULT_TITLE_FONT) != title_font_setting(E.cfg(ep, n)):    # old reports: the default
        return "title font setting changed"
    made = mp4.stat().st_mtime
    name = f"{n:05d}"
    for rel in (f"hindi/{name}.txt", f"audio/{name}.mp3", f"timing/{name}.json",
                f"scenes/{name}.json", f"captions/{name}.json"):
        f = ep_root / rel
        if f.exists() and f.stat().st_mtime > made:
            return f"{rel} changed"
    for img in (ep_root / "images" / f"c{n:04d}" / "hd").glob("s*.jpg"):
        if img.stat().st_mtime > made:
            return f"hd picture {img.name} changed"
    return None


# ------------------------------------------------------------------ the inputs a render needs


def captions_on(ep: int, n: int) -> bool:
    """video.captions.enabled for this chapter (default on)."""
    return ((E.cfg(ep, n).get("video") or {}).get("captions") or {}).get("enabled", True) is not False


def _item(kind: str, what: str, fix: str) -> dict:
    return {"kind": kind, "what": what, "fix": fix}


def missing_inputs(ep_root: Path, ep: int, n: int, *, captions: bool = True) -> list[dict]:
    """Everything chapter n's render still lacks, as [{"kind", "what", "fix"}]; empty means ready.

    Checked: Hindi text and its lock, scene file, every image the scene file expects (the 2k download),
    narration mp3, timing map, English captions (or a Gemini key to make them), ffmpeg, the logo file.
    `fix` is the command that produces the missing thing."""
    from . import episode_captions as C
    from novel.core import secrets
    name, out = f"{n:05d}", []
    c = f"--ep {ep} --chapters {n}"
    hindi = ep_root / "hindi" / f"{name}.txt"
    if not hindi.exists():
        out.append(_item("hindi", f"hindi/{name}.txt does not exist", f"novel prompt {c}   (then: novel import)"))
    else:
        lock = E.lock_path(ep_root)
        locked = json.loads(lock.read_text(encoding="utf-8")).get(f"{name}.txt") if lock.exists() else None
        if locked is None:
            out.append(_item("lock", f"hindi/{name}.txt is not locked", f"novel approve {c}"))
        elif E.sha256(hindi) != locked:
            out.append(_item("lock", f"hindi/{name}.txt changed after it was locked", f"novel approve {c}"))
    scenes = ep_root / "scenes" / f"{name}.json"
    doc = None
    try:
        doc = json.loads(scenes.read_text(encoding="utf-8")) if scenes.exists() else None
        ids = [int(s["id"]) for s in doc["scenes"]] if doc else []
    except (ValueError, KeyError, TypeError):
        doc, ids = None, []
        out.append(_item("scenes", f"scenes/{name}.json is not a valid scene file", f"novel prompt {c} --mode scenes   (then: novel import)"))
    else:
        if doc is None:
            out.append(_item("scenes", f"scenes/{name}.json does not exist", f"novel prompt {c} --mode scenes   (then: novel import)"))
    if doc is not None:
        img_dir = ep_root / "images" / f"c{n:04d}"
        gone = [f"s{i:02d}-2k.jpg" for i in ids
                if not (img_dir / f"s{i:02d}-2k.jpg").exists() or (img_dir / f"s{i:02d}-2k.jpg").stat().st_size < MIN_IMAGE_BYTES]
        if gone:
            shown = ", ".join(gone[:6]) + (f" and {len(gone) - 6} more" if len(gone) > 6 else "")
            out.append(_item("images", f"{len(gone)} of {len(ids)} images missing in images/c{n:04d}/: {shown}",
                             f"novel images {c}"))
    if not (ep_root / "audio" / f"{name}.mp3").exists():
        out.append(_item("audio", f"audio/{name}.mp3 does not exist", f"novel tts push {c}   (then: novel tts pull --ep {ep})"))
    if not (ep_root / "timing" / f"{name}.json").exists():
        out.append(_item("timing", f"timing/{name}.json does not exist", f"novel tts pull --ep {ep}"))
    if captions and hindi.exists():
        try:
            stale = C.stale(ep_root, n)
        except (ValueError, KeyError):
            stale = True
        if stale and not secrets.gemini_keys():
            have = (ep_root / "captions" / f"{name}.json").exists()
            out.append(_item("captions", f"captions/{name}.json is {'out of date' if have else 'missing'} and there is no "
                             f"GEMINI_API_KEY in {secrets_path()} to make them", f"novel captions {c}   (needs a Gemini key)"))
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        out.append(_item("ffmpeg", "ffmpeg / ffprobe are not on PATH", "install ffmpeg, then run: novel doctor"))
    try:
        from .episode_video import logo_signature
        logo_signature(E.cfg(ep, n)["video"]["logo"])
    except SystemExit as e:
        out.append(_item("logo", str(e), "add the logo file, or set video.logo.enabled: false (novel settings)"))
    return out


def secrets_path() -> str:
    from novel.core import paths
    return str(paths.secrets_file())


def check_chapters(ep_root: Path, ep: int, chapters: list[int]) -> dict[int, list[dict]]:
    """{chapter: missing items} for the chapters that lack something."""
    short = {n: missing_inputs(ep_root, ep, n, captions=captions_on(ep, n)) for n in chapters}
    return {n: m for n, m in short.items() if m}


def print_missing(short: dict[int, list[dict]]) -> None:
    """Every missing item with the command that produces it, one per line."""
    for n, items in sorted(short.items()):
        print(f"chapter {n}: cannot render yet, {len(items)} thing{'s' if len(items) != 1 else ''} missing")
        for m in items:
            print(f"  - {m['kind']}: {m['what']}")
            print(f"      fix: {m['fix']}")


# ------------------------------------------------------------------ the build


def _existing_report(ep_root: Path, n: int) -> dict:
    f = ep_root / "video" / f"c{n:04d}.render.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def build_episode(ep: int, chapters: str | None = None, *, force: bool = False, hd: bool = True,
                  when_ready: bool = False, per_hour: float | None = None, workers: int | None = None,
                  interval: float = P.CHECK_EVERY, max_wait: float = P.MAX_WAIT,
                  sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None) -> dict:
    """Run the whole build in this process. Returns
    {"ok", "status", "video", "duration", "chapters": [{"chapter", "action", "why", "report"}],
     "timestamps", "missing", "skipped", "failed"}.
    status: "built", "missing" (inputs missing, nothing done), "failed" (a render failed a check, not joined),
    "skipped" (when_ready gave up on some chapters, not joined). Raises SystemExit for a bad request."""
    from . import episode_hd
    from .episode_video import probe
    sleep, clock = sleep or time.sleep, clock or time.time
    ep_root = E.root(ep).resolve()
    if not (ep_root / "manifest.json").exists():
        raise SystemExit(f"episode {ep} has no manifest.json at {ep_root}: add it with `novel episode add`")
    in_ep = [c.idx for c in Manifest.load(ep_root / "manifest.json").chapters]
    want = parse_range(chapters) if chapters else in_ep
    outside = [n for n in want if n not in in_ep]
    if outside:
        raise SystemExit(f"chapters {outside} are not in episode {ep} (it has {in_ep[0]}-{in_ep[-1]})")
    if not want:
        raise SystemExit(f"episode {ep} has no chapters to build")
    workers = workers or max(1, mp.cpu_count() - 2)
    result = {"ok": False, "status": "", "video": None, "duration": None, "chapters": [], "timestamps": [],
              "missing": {}, "skipped": [], "failed": []}
    paced = when_ready or per_hour is not None
    gap = 3600 / (per_hour or P.DEFAULT_PER_HOUR) if paced else 0.0
    log = P.make_log(ep_root, to_file=paced)

    plan = episode_hd.hd_plan(E.cfg(ep), no_hd=not hd)       # a missing tool with enabled: true stops here
    if plan["note"]:
        print(plan["note"])

    todo = []
    for n in want:
        why = "forced" if force else why_render(ep_root, n)
        if why:
            todo.append((n, why))
    result["chapters"] = [{"chapter": n, "action": "current", "why": None, "report": _existing_report(ep_root, n)}
                          for n in want if n not in dict(todo)]
    print(f"{len(want) - len(todo)} chapters current, {len(todo)} to render (~1 min of render per min of video)")

    # Before any work: every input of every chapter to render.
    need = [n for n, _ in todo]
    short = check_chapters(ep_root, ep, need) if need else {}
    if short and not when_ready:
        print_missing(short)
        print(f"nothing was rendered. Make the missing things, or run with --when-ready to wait for them.")
        result.update(status="missing", missing=short)
        return result
    if short:
        log(f"pace: episode {ep}, chapters {want[0]}-{want[-1]}, {per_hour or P.DEFAULT_PER_HOUR:g} an hour "
            f"(a slot every {gap / 60:.0f} min)")
        ready, given_up = P.wait_until_ready(lambda ch: check_chapters(ep_root, ep, ch), need, log,
                                             sleep=sleep, interval=interval, max_wait=max_wait)
        result["skipped"], result["missing"] = sorted(given_up), given_up
        todo = [(n, why) for n, why in todo if n in ready]
        log(f"all inputs ready for chapters {ready}" if ready else "nothing to render")

    # The optional HD step, one pass over the chapters about to be rendered.
    if plan["run"] and todo:
        log(f"hd: making full-HD pictures for chapters {[n for n, _ in todo]}")
        t0 = time.time()
        try:
            episode_hd.make_hd(ep_root, [n for n, _ in todo])
            log(f"hd ok in {(time.time() - t0) / 60:.1f} min")
        except SystemExit as e:
            if plan["mode"] == "true":
                raise SystemExit(f"hd failed (video.hd.enabled is true): {e}")
            log(f"hd FAILED, the renderer falls back to cleaned downloads: {str(e)[-300:]}")

    # Render what is not current.
    failed, last_start = [], None
    for i, (n, why) in enumerate(todo, 1):
        if paced:
            P.wait_for_slot(last_start, gap, log, f"c{n:04d}", sleep=sleep, clock=clock)
        last_start = clock()
        print(f"[{i}/{len(todo)}] chapter {n}: {why}", flush=True)
        r, err = None, ""
        for attempt in range(2 if paced else 1):
            t0 = time.time()
            try:
                r = render(ep, n, "20", "fast", workers, None, None, captions_on(ep, n), plan["use_pictures"])
            except SystemExit as e:
                r, err = None, str(e)
            if r and r["pass"]:
                break
            if paced:
                log(f"c{n:04d}: render attempt {attempt + 1} {'failed a check' if r else 'failed: ' + err[-400:]}")
        if r is None:
            print(f"  FAIL: {err}", flush=True)
            r = {"pass": False, "error": err, "seconds": 0, "mb": 0}
        else:
            print(f"  {'pass' if r['pass'] else 'FAIL'}: {r['seconds']:.0f} s video in "
                  f"{(time.time() - t0) / 60:.1f} min, {r['mb']} MB", flush=True)
        result["chapters"].append({"chapter": n, "action": "rendered", "why": why, "report": r})
        if not r["pass"]:
            failed.append(n)
    result["chapters"].sort(key=lambda c: c["chapter"])
    result["failed"] = failed
    if failed:
        print(f"chapters {failed} failed their checks (see video/cNNNN.render.json); not joining")
        result["status"] = "failed"
        return result
    if result["skipped"]:
        print(f"chapters {result['skipped']} were given up on (inputs never arrived); not joining")
        result["status"] = "skipped"
        return result
    out = join(ep, chapters)
    result.update(ok=True, status="built", video=str(out), duration=float(probe(out)["format"]["duration"]),
                  timestamps=[str(out.with_suffix(".timestamps.txt")), str(out.with_suffix(".timestamps.json"))])
    return result
