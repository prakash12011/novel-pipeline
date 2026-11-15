"""novel approve --chapters A-B [--book B] [--ep N] [--force] [--dry-run]

Run once an episode's text is accepted: locks the Hindi (if not locked yet), checks the scene file and the
QA result, writes one Flow prompt file per picture and merges the chapters into the episode's flow/plan.json.
All checks of a chapter come first, then its writes; a chapter that fails a check writes nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from novel import bible as B
from novel import flowrun as F
from novel import prompts as P
from novel.core import context

USAGE = "usage: novel approve --chapters A-B [--book B] [--ep N] [--force] [--dry-run]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def _virtual_lock(E, ep_root: Path, extra: dict):
    """check_chapter reads the lock file; a chapter about to be locked is not in it yet. Point the lock path at
    a temporary copy that has the entry, for the duration of the checks only (nothing is written in ep_root)."""
    import tempfile
    lock = _read_json(E.lock_path(ep_root)) or {}
    lock.update(extra)
    real = E.lock_path
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "hindi.lock.json"
        f.write_text(json.dumps(lock), encoding="utf-8")
        E.lock_path = lambda _root: f
        try:
            yield
        finally:
            E.lock_path = real


class Chapter:
    def __init__(self, n: int):
        self.n = n
        self.refused: list[str] = []      # each entry: what is wrong + the fix
        self.notes: list[str] = []
        self.lock_now = False
        self.sha = None
        self.qa_force = False
        self.doc = None
        self.prompts: dict[int, str] = {}     # scene id -> new text
        self.written = 0
        self.kept: list[tuple[str, Path]] = []
        self.unchanged = 0
        self.shots: list[dict] = []
        self.refs = None


def _qa_problem(ep_root: Path, n: int) -> str | None:
    """Why QA does not pass, or None."""
    name = f"{n:05d}"
    qa = _read_json(ep_root / "qa" / f"{name}.json")
    if not isinstance(qa, dict) or qa.get("version") != 2:
        return "QA has not been run for this chapter" + (" (the QA file is an old-format leftover)" if qa else "")
    errs = qa.get("errors") or []
    if errs:
        return f"QA found {len(errs)} error(s), e.g. {errs[0] if isinstance(errs[0], str) else json.dumps(errs[0], ensure_ascii=False)}"
    rev = _read_json(ep_root / "qa" / f"{name}.review.json")
    if isinstance(rev, dict) and str(rev.get("verdict", "")).upper() == "FIX":
        return f"the QA review says FIX (see qa/{name}.review.json)"
    return None


def _mark_forced(ep_root: Path, n: int) -> None:
    f = ep_root / "qa" / f"{n:05d}.json"
    qa = _read_json(f)
    if not isinstance(qa, dict) or qa.get("version") != 2:
        if f.exists():
            old = f.with_name(f"{n:05d}.old-format.json")
            if not old.exists():
                os.replace(f, old)
        qa = {"version": 2, "chapter": n, "errors": [], "warnings": [], "checked_at": None}
    qa["forced"] = True
    qa["forced_at"] = _now()
    _atomic(f, json.dumps(qa, ensure_ascii=False, indent=1) + "\n")


def check_chapter(E, S, ep_root: Path, n: int, a, chars_doc: dict, locs_doc: dict, settings: dict) -> Chapter:
    c = Chapter(n)
    name = f"{n:05d}"
    hindi = ep_root / "hindi" / f"{name}.txt"
    # 1. the Hindi file and its lock
    if not hindi.exists():
        c.refused.append(f"hindi/{name}.txt does not exist. Fix: translate and import chapter {n} first")
        return c
    sha = E.sha256(hindi)
    c.sha = sha
    lock = _read_json(E.lock_path(ep_root)) or {}
    if name + ".txt" in lock:
        if lock[name + ".txt"] != sha:
            c.refused.append(f"hindi/{name}.txt changed after it was locked (scenes, audio and timing were made "
                             f"from the old text). Fix: put the old text back; or, if the change is on purpose, "
                             f"delete \"{name}.txt\" from {E.lock_path(ep_root)}, import the scenes for chapter "
                             f"{n} again, then run `novel approve --chapters {n}`")
            return c
    else:
        c.lock_now = True
    # 2. the scene file
    sf = S.scenes_path(ep_root, n)
    if not sf.exists():
        c.refused.append(f"no scene file at {sf}. Fix: write the scenes for chapter {n} (`novel write`, or by hand "
                         f"`novel prompt` then `novel import`)")
        return c
    doc = _read_json(sf)
    if not isinstance(doc, dict):
        c.refused.append(f"{sf} is not valid JSON")
        return c
    if doc.get("version") != 2:
        c.refused.append(f"chapter {n} has a v1 scene file: `novel approve` is for the new flow (scene file v2, "
                         f"written by `novel import`). Finished v1 chapters keep using the old tools")
        return c
    with _virtual_lock(E, ep_root, {name + ".txt": sha}):
        try:
            problems = S.check_chapter(ep_root, n)
        except SystemExit as e:
            problems = [str(e)]
    if problems:
        c.refused.append(f"the scene file has {len(problems)} problem(s): " + "; ".join(problems[:6]) +
                         f". Fix: import corrected scenes for chapter {n}")
        return c
    c.doc = doc
    # 3. QA
    why = _qa_problem(ep_root, n)
    if why:
        if a.force:
            c.qa_force = True
            c.notes.append(f"FORCED past QA ({why})")
        else:
            c.refused.append(f"{why}. Fix: run `novel qa --chapters {n}` (or approve anyway with --force)")
            return c
    # 4. prompts (all of them build, or the chapter is refused)
    try:
        pos = P.positions(doc)
        for s in doc["scenes"]:
            c.prompts[s["id"]] = P.flow_prompt(s, doc, n, chars_doc, locs_doc, settings, pos[s["id"]])
        c.refs = P.reference_plan(doc, n, chars_doc)
    except P.PromptError as e:
        c.refused.append(f"cannot build the prompts: {e}. Fix: correct the bible or re-import the scenes")
        return c
    img_dir = ep_root / "images" / f"c{n:04d}"
    for s in doc["scenes"]:
        stem = img_dir / f"s{s['id']:02d}"
        prompt_file = stem.with_suffix(".prompt.txt")
        text = c.prompts[s["id"]]
        old = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else None
        image = Path(f"{stem.as_posix()}-2k.jpg")
        keep = old is not None and old != text and image.exists()
        if keep:
            c.kept.append((f"c{n:04d}-s{s['id']:02d}", image))
        elif old != text:
            c.written += 1
        else:
            c.unchanged += 1
        c.shots.append({"id": f"c{n:04d}-s{s['id']:02d}", "chars": P.flow_names(s, n, chars_doc),
                        "prompt": prompt_file.resolve().as_posix(), "out": image.resolve().as_posix(),
                        "meta": stem.with_suffix(".flow.json").resolve().as_posix(),
                        "used": stem.with_suffix(".used.txt").resolve().as_posix(),
                        "_text": text, "_keep": keep})
    return c


def write_chapter(E, T, ep_root: Path, c: Chapter) -> None:
    name = f"{c.n:05d}"
    if c.lock_now:
        with contextlib.redirect_stdout(io.StringIO()):
            T.cmd_lock(SimpleNamespace(episode=E_episode(ep_root), chapters=str(c.n)))
    if c.qa_force:
        _mark_forced(ep_root, c.n)
    for s in c.shots:
        if not s["_keep"]:
            p = Path(s["prompt"])
            old = p.read_text(encoding="utf-8") if p.exists() else None
            if old != s["_text"]:
                _atomic(p, s["_text"])


def E_episode(ep_root: Path) -> int:
    return int(ep_root.name[1:])


PATH_KEYS = ("prompt", "out", "meta", "used")


def _portable(shot: dict, old_plan: dict, ep_root: Path) -> dict:
    """The shot with its paths relative to the episode folder (through the shared plan-path helper), so the
    plan works wherever the data folder is. A path that lies outside the episode folder stays as it was."""
    out = dict(shot)
    for k in PATH_KEYS:
        if out.get(k):
            out[k] = F.relative_plan_path(F.resolve_plan_path(out[k], old_plan, ep_root), ep_root)
    return out


def merge_plan(ep_root: Path, ok: list[Chapter], settings: dict) -> tuple[Path, int, bool]:
    plan_path = ep_root / "flow" / "plan.json"
    old = _read_json(plan_path) if plan_path.exists() else None
    mine = {f"c{c.n:04d}-" for c in ok}
    keep = [_portable(s, old or {}, ep_root) for s in (old or {}).get("shots", [])
            if not any(s["id"].startswith(m) for m in mine)]
    fresh = [_portable({k: v for k, v in s.items() if not k.startswith("_")}, {}, ep_root)
             for c in ok for s in c.shots]
    shots = sorted(keep + fresh, key=lambda s: s["id"])
    from novel.core import chromelock
    plan = {"project": settings["flow_project"],
            "image_model": settings.get("flow_model"),
            "cdp": settings.get("cdp") or chromelock.DEFAULT_URL,
            "header_mark": P.HEADER_MARK,
            "journal": "flow/journal.jsonl",
            "stop_file": "flow/STOP",
            "shots": shots}
    had_prev = plan_path.exists()
    if had_prev:
        _atomic(plan_path.with_name("plan.prev.json"), plan_path.read_bytes().decode("utf-8"))
    _atomic(plan_path, json.dumps(plan, ensure_ascii=False, indent=1) + "\n")
    return plan_path, len(shots), had_prev


def _make_keys(pairs) -> str:
    return ",".join(k if v == "base" else f"{k}:{v}" for k, v in pairs)


def main(argv) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel approve", usage=USAGE)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--chapters", required=True, help="e.g. 21 or 21-22")
    ap.add_argument("--force", action="store_true", help="approve although QA has not passed (recorded)")
    ap.add_argument("--dry-run", action="store_true", help="check everything and print; write nothing")
    a = ap.parse_args(argv)

    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    print(context.header(book, ep))

    from novel.core import settings as cfgmod
    from novel.stages import episode as E
    from novel.stages import episode_scenes as S
    from novel.stages import episode_translate as T

    eff = cfgmod.effective(book, ep)
    images = eff.get("images") or {}
    if not images.get("flow_project"):
        raise SystemExit("images.flow_project is not set: put the Flow project URL under `images:` / "
                         "`flow_project:` in the book's book.yaml (or episode.yaml), then run this again")
    flow_settings = {"flow_project": images["flow_project"], "flow_model": images.get("flow_model"),
                     "cdp": (eff.get("chrome") or {}).get("cdp_url")}
    ep_root = E.root(ep).resolve()
    if not ep_root.exists():
        raise SystemExit(f"episode folder {ep_root} does not exist")
    chars_doc = B.load_characters(B.bible_dir(book))
    locs_doc = B.load_locations(B.bible_dir(book))
    nums = T.parse_range(a.chapters)

    results = [check_chapter(E, S, ep_root, n, a, chars_doc, locs_doc, eff) for n in nums]
    ok = [c for c in results if not c.refused]
    dry = a.dry_run
    if dry:
        print("DRY RUN: every check is made, nothing is written.")
    for c in ok:
        if not dry:
            write_chapter(E, T, ep_root, c)
    plan_path, total, had_prev = (None, 0, False)
    if ok and not dry:
        plan_path, total, had_prev = merge_plan(ep_root, ok, flow_settings)

    for c in results:
        if c.refused:
            for r in c.refused:
                print(f"chapter {c.n}: REFUSED, nothing written. {r}")
            continue
        todo = sum(1 for s in c.shots if not Path(s["out"]).exists())
        extra = ""
        if c.lock_now:
            extra += " Hindi " + ("would be locked." if dry else "locked now.")
        print(f"chapter {c.n}: {len(c.doc['scenes'])} scenes, {c.written} prompts "
              f"{'to write' if dry else 'written'}, {c.unchanged} unchanged, {len(c.kept)} kept, "
              f"{todo} pictures still to generate.{extra}")
        for note in c.notes:
            print(f"  note: {note}" + ("" if dry else "; recorded as forced in the QA file"))
        if c.kept:
            print("  kept: image exists, prompt changed (the old prompt stays; to regenerate a picture, delete "
                  f"its -2k.jpg, then run `novel approve --chapters {c.n}` again):")
            for sid, image in c.kept:
                print(f"    {sid}: delete {image.as_posix()}")

    if plan_path:
        print(f"{plan_path}: {total} shots in the plan" + (" (previous plan kept as plan.prev.json)" if had_prev else ""))
    elif ok:
        print(f"dry run: flow/plan.json would hold the shots of chapters {', '.join(str(c.n) for c in ok)}")

    create, recreate = [], []
    for c in ok:
        for pair in c.refs["not_made"]:
            if pair not in create:
                create.append(pair)
        for pair in c.refs["needs_new_reference"]:
            if pair not in c.refs["not_made"] and pair not in recreate:
                recreate.append(pair)
    if create:
        print("\nFlow Characters to create before generating:")
        for k, v in create:
            print(f"  {k} [{v}]")
        print(f"  novel characters make --keys {_make_keys(create)}")
    if recreate:
        print("\nFlow Characters to recreate (the old reference has a baked-in prop):")
        for k, v in recreate:
            print(f"  {k} [{v}]")
        print(f"  novel characters make --keys {_make_keys(recreate)}")
    if ok:
        lo, hi = min(c.n for c in ok), max(c.n for c in ok)
        rng = str(lo) if lo == hi else f"{lo}-{hi}"
        print(f"\nNext: {'novel characters make first, then ' if (create or recreate) else ''}"
              f"`novel run` (narration and captions in the background, pictures in this window), or only the pictures: "
              f"`novel images --chapters {rng}`")
    bad = [c.n for c in results if c.refused]
    if bad:
        print(f"\nrefused: chapter(s) {', '.join(map(str, bad))}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
