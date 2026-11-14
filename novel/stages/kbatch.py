"""The whole Kaggle cycle in two commands: pull finished work, push new work.

Doing this by hand is ~25 commands per round and has failed in five distinct
ways, every one of which cost real GPU time. Each guard below exists because
of a specific incident, not as defensive habit:

  * A dataset upload that printed "Starting upload" and never landed. All
    three kernels mounted the PREVIOUS dataset version and re-narrated
    chapters that were already done: ~21 GPU-hours. `push` now refuses to
    push a kernel until it has seen a chapter number from the new range in
    `datasets files` on the live dataset.

  * `dispatch next` returns chapters from the FRONT of the book (802-849 is
    an old unstarted gap), so it silently proposes the wrong batch when what
    you want is to keep moving forward. `push` takes an explicit --start and
    never consults it.

  * A staged notebook carrying `BATCH = 5` from a batch months earlier would
    have narrated a third of each batch and looked like a clean run. `push`
    rewrites BATCH from the range it is actually sending.

  * `kaggle datasets version -p some/nested/dir` builds its temp filename out
    of the path, so a "/" in it makes the CLI write into a directory that does
    not exist. Every upload runs from INSIDE the staging folder with `-p .`.

  * An m4b that arrived as 0 bytes after the connection dropped mid-transfer.
    It would have merged as "done" and sat in the library as a dead file.
    Every m4b is checked for a plausible duration with ffprobe, and re-fetched
    once before the run is allowed to merge.

`-d` is NEVER passed to `datasets version`: it means --delete-old-versions.

Usage:
    python -m abpipe.kbatch pull
    python -m abpipe.kbatch pull --run seema-1002
    python -m abpipe.kbatch push --start 1047 --count 15
    python -m abpipe.kbatch push --start 1047 --count 15 --accounts seema,shashi
    python -m abpipe.kbatch push --start 1047 --count 15 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
WS = PROJECT / "workspace"
PULL = PROJECT / ".kaggle-pull"
LIBRARY = PROJECT / "audiobook-library"
KERNEL_DIR = PULL / "kernel"

# Order matters only for readability; each account gets its own slice.
ACCOUNTS = ["seema", "shashi", "prakash"]


# ---------------------------------------------------------------- helpers


def kaggle_home() -> Path:
    """The folder that holds the Kaggle tokens: $NOVEL_KAGGLE_HOME, else ~/.kaggle."""
    env = os.environ.get("NOVEL_KAGGLE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".kaggle"


def kaggle_cmd() -> list[str]:
    """THE one place that decides how the Kaggle CLI is started.

    1. $NOVEL_KAGGLE_CMD when set (tests: a fake), split like a command line;
    2. `kaggle` when it is on PATH;
    3. this Python's `-m kaggle` (the package is installed, the script is not on PATH).
    """
    env = os.environ.get("NOVEL_KAGGLE_CMD", "").strip()
    if env:
        import shlex
        parts = [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t
                 for t in shlex.split(env, posix=False)]
        if parts:
            return parts
    exe = shutil.which("kaggle")
    if exe:
        return [exe]
    return [sys.executable, "-m", "kaggle"]


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> str:
    """Run a command with UTF-8 forced.

    Without this the Kaggle CLI dies on Windows with a cp1252 charmap error
    the moment a log line contains Devanagari - which every log does, since
    the chapter titles are printed as they are narrated.
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    if os.environ.get("NOVEL_KAGGLE_HOME"):
        env["KAGGLE_CONFIG_DIR"] = os.environ["NOVEL_KAGGLE_HOME"]   # the CLI reads its token there
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return (p.stdout or "") + (p.stderr or "")


def kaggle(*args: str, cwd: Path | None = None, timeout: int = 900) -> str:
    """Every Kaggle CLI call in the episode flow goes through here."""
    return run([*kaggle_cmd(), *args], cwd=cwd, timeout=timeout)


def abpipe(*args: str, timeout: int = 900) -> str:
    return run([sys.executable, "-m", *args], timeout=timeout)


def use_account(profile: str) -> None:
    out = abpipe("novel.stages.account", "use", profile, timeout=180)
    if "kernel id" not in out and "notebook staged" not in out:
        sys.exit(f"account switch to {profile} failed:\n{out}")


def ledger() -> list[dict]:
    p = PROJECT / "runs.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("runs", [])


def master_chapters() -> list[dict]:
    return json.loads((WS / "manifest.json").read_text(encoding="utf-8"))["chapters"]


def chapter_no(source: str) -> int:
    return int(source[:4])


def log_text(raw: str) -> str:
    """Kaggle returns logs as a JSON array of {stream_name,time,data}."""
    try:
        return "".join(r.get("data", "") for r in json.loads(raw))
    except Exception:
        return raw


# ------------------------------------------------------------------ pull


def fetch(kern: str, dest: Path, pattern: str, force: bool = False) -> str:
    """Download the output files matching `pattern`.

    --page-size 200 is load-bearing. The default is 20, so a 15-chapter batch
    - which carries an english source, a hindi translation and a timing map
    per chapter, plus the m4b files - is listed across several pages and only
    the first is fetched. The download reports success either way; the failure
    only surfaces later as a merge complaining about one absent file.
    """
    args = ["kernels", "output", kern, "-p", str(dest),
            "--file-pattern", pattern, "--page-size", "200"]
    if force:
        args.append("-o")
    return kaggle(*args, timeout=1800)


def m4b_ok(path: Path) -> float:
    """Duration in minutes, or 0.0 if this is not playable audio.

    Size alone is not enough: a dropped connection leaves a 0-byte file that
    merges happily and is silent when you open it.
    """
    if not path.exists() or path.stat().st_size < 1024:
        return 0.0
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)], timeout=120).strip()
    try:
        return float(out.splitlines()[0]) / 60.0
    except Exception:
        return 0.0


def pull_run(rec: dict, apply: bool = True) -> bool:
    rid, prof = rec["id"], rec["account"]
    kern = f"{rec['kaggle_user']}/{rec['kernel']}"
    lo, hi = rec["start"], rec["end"]
    print(f"\n=== {rid}  {kern}  chapters {lo}-{hi} ===")

    use_account(prof)

    status = kaggle("kernels", "status", kern, timeout=180)
    if "COMPLETE" not in status:
        print(f"  not finished yet: {status.strip()[-60:]}")
        return False

    # Verify from the LOGS which chapters actually ran before spending
    # bandwidth on the output. This is the step whose absence let three
    # kernels re-narrate old chapters for 21 GPU-hours.
    text = log_text(kaggle("kernels", "logs", kern, timeout=600))
    seen = sorted({int(m) for m in re.findall(r"Chapter (\d{3,4}):", text)})
    spoke = len([l for l in text.splitlines() if l.strip().startswith("spoke")])
    if not seen or seen[0] != lo or seen[-1] != hi:
        print(f"  RANGE MISMATCH: ledger says {lo}-{hi}, log says "
              f"{seen[:1]}..{seen[-1:]} - refusing to merge")
        return False
    if spoke != (hi - lo + 1):
        print(f"  only {spoke} of {hi-lo+1} chapters narrated - refusing")
        return False
    print(f"  log OK: {lo}-{hi}, {spoke} narrated")

    dest = PULL / f"final{lo}"
    dest.mkdir(parents=True, exist_ok=True)
    fetch(kern, dest, r"\.m4b$")
    fetch(kern, dest, r"\.(json|txt)$")

    # Every file the batch manifest names has to be on disk. merge.py does
    # catch this, but only after the m4b work is done, and its message names
    # one missing file rather than the truncated download behind it.
    # Measured: 1 of 15 timing maps arrived before --page-size was raised.
    def missing_files() -> list[str]:
        bm = json.loads((dest / "audiobook" / "manifest.json")
                        .read_text(encoding="utf-8"))["chapters"]
        return [f"{k}/{c[k]}" for c in bm for k in ("hindi", "timing")
                if c.get(k) and not (dest / "audiobook" / k / c[k]).exists()]

    gone = missing_files()
    if gone:
        print(f"  {len(gone)} files the manifest names are absent "
              f"(e.g. {gone[0]}) - re-fetching")
        fetch(kern, dest, r"\.(json|txt)$", force=True)
        gone = missing_files()
        if gone:
            print(f"  still missing {len(gone)} ({gone[:2]}) - refusing to merge")
            return False

    books = sorted((dest / "audiobook" / "books").glob("*.m4b"))
    if not books:
        print("  no .m4b in output - refusing")
        return False
    for b in books:
        mins = m4b_ok(b)
        if mins <= 0:
            print(f"  {b.name} is not playable, re-fetching")
            b.unlink(missing_ok=True)
            kaggle("kernels", "output", kern, "-p", str(dest), "-o",
                   "--file-pattern", re.escape(b.name) + "$", timeout=1800)
            mins = m4b_ok(b)
            if mins <= 0:
                print(f"  {b.name} STILL not playable - refusing to merge")
                return False
        print(f"  {b.name}  {mins:.1f} min  {b.stat().st_size/1048576:.1f} MB")

    if not apply:
        print("  dry run - not merging")
        return False

    out = abpipe("novel.stages.merge", "--master", str(WS),
                 "--batch", str(dest / "audiobook"), "--no-audio", "--apply")
    tail = [l for l in out.splitlines() if "merged." in l]
    print("  " + (tail[-1] if tail else out.strip()[-160:]))
    if not tail:
        print("  merge did not report success - stopping before the library")
        return False

    # audiobook-library is append-only by standing instruction: copy, never
    # move, and never overwrite something already sitting there.
    LIBRARY.mkdir(exist_ok=True)
    for b in books:
        target = LIBRARY / b.name
        if target.exists():
            print(f"  library: {b.name} already present, left alone")
        else:
            shutil.copy2(b, target)
            print(f"  library: added {b.name}")

    abpipe("novel.stages.dispatch", "set", "--id", rid, "--status", "done")
    print(f"  ledger: {rid} -> done")
    return True


def cmd_pull(args) -> None:
    runs = [r for r in ledger() if r.get("status") == "running"]
    if args.run:
        runs = [r for r in runs if r["id"] == args.run]
    if not runs:
        sys.exit("no running runs in the ledger")
    ok = sum(pull_run(r, apply=not args.dry_run) for r in runs)
    print(f"\n{ok}/{len(runs)} runs pulled")
    print(abpipe("novel.stages.dispatch", "status").split("RUNS")[0].strip())


# ------------------------------------------------------------------ push


def stage(lo: int, hi: int, owner: str) -> Path:
    """Build one account's dataset folder: a slice manifest plus the code."""
    chapters = master_chapters()
    picked = sorted((c for c in chapters if lo <= chapter_no(c["source"]) <= hi),
                    key=lambda c: chapter_no(c["source"]))
    if len(picked) != hi - lo + 1:
        sys.exit(f"expected {hi-lo+1} chapters for {lo}-{hi}, found {len(picked)}")
    done = [c["source"] for c in picked if c.get("status") == "done"]
    if done:
        sys.exit(f"{len(done)} of {lo}-{hi} are already done ({done[:2]}) - "
                 f"a slice carrying done chapters is skipped by the runner "
                 f"and produces an empty, fast, successful-looking run")

    st = PULL / f"ds{lo}"
    if st.exists():
        shutil.rmtree(st)
    (st / "chapters").mkdir(parents=True)

    slice_ch = []
    for i, c in enumerate(picked):
        shutil.copy2(WS / "chapters" / c["source"], st / "chapters" / c["source"])
        slice_ch.append({"idx": i, "title": c["title"], "source": c["source"],
                         "status": "pending", "hindi": None, "audio": None,
                         "seconds": None, "error": None, "attempts": 0,
                         "translate_seconds": None, "tts_seconds": None,
                         "rtf": None})
    (st / "manifest.json").write_text(
        json.dumps({"version": 1, "chapters": slice_ch}, ensure_ascii=False,
                   indent=1), encoding="utf-8")

    shutil.copytree(PROJECT / "abpipe", st / "abpipe",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("config.yaml", "glossary.json"):
        shutil.copy2(PROJECT / f, st / f)
    shutil.copytree(WS / "ref", st / "ref")
    if not (st / "ref" / "narrator.wav").exists():
        sys.exit("ref/narrator.wav missing - TTS would fail on Kaggle")

    (st / "dataset-metadata.json").write_text(
        json.dumps({"title": "audiobook-data", "id": f"{owner}/audiobook-data",
                    "licenses": [{"name": "unknown"}]}, indent=2),
        encoding="utf-8")
    return st


def set_batch(nb_path: Path, count: int, lo: int, hi: int) -> None:
    """Rewrite BATCH from the range actually being sent.

    The cell is one string with embedded newlines, so this substitutes inside
    the joined text rather than per source-list element - matching per element
    silently matches nothing and leaves a stale BATCH in place.
    """
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    hits = 0
    for c in nb["cells"]:
        text = "".join(c["source"])
        if re.search(r"^BATCH\s*=", text, re.M):
            text = re.sub(r"^BATCH\s*=.*$",
                          f"BATCH = {count}  # chapters {lo}-{hi}", text, flags=re.M)
            c["source"] = text.splitlines(keepends=True)
            hits += 1
    if not hits:
        sys.exit(f"no BATCH line in {nb_path}")
    nb_path.write_text(json.dumps(nb, ensure_ascii=False, indent=1),
                       encoding="utf-8")


def push_one(profile: str, lo: int, hi: int, dry: bool) -> bool:
    print(f"\n=== {profile}  chapters {lo}-{hi} ===")
    use_account(profile)
    meta = json.loads((KERNEL_DIR / "kernel-metadata.json").read_text(encoding="utf-8"))
    owner = meta["id"].split("/")[0]
    st = stage(lo, hi, owner)
    print(f"  staged {st.name}: {hi - lo + 1} chapters, owner {owner}")

    if dry:
        print("  dry run - not uploading or pushing")
        return False

    rid = f"{profile}-{lo}"
    add = abpipe("novel.stages.dispatch", "add", "--account", profile,
                 "--user", owner, "--kernel", meta["id"].split("/")[1],
                 "--start", str(lo), "--end", str(hi),
                 "--gpu", "T4", "--precision", "fp32", "--id", rid)
    if "recorded" not in add:
        print(f"  ledger refused the range:\n{add.strip()[-200:]}")
        return False

    # From INSIDE the folder: a "/" in the -p path makes the CLI build a temp
    # filename containing a directory that does not exist. And never -d.
    up = kaggle("datasets", "version", "-p", ".", "-m", f"chapters {lo}-{hi}",
                "-r", "zip", cwd=st, timeout=1800)
    if "version is being created" not in up:
        print(f"  upload did not complete:\n{up.strip()[-300:]}")
        abpipe("novel.stages.dispatch", "set", "--id", rid, "--status", "failed")
        return False

    # "being created" is NOT "live". Confirm the new range is actually
    # readable on the dataset before any kernel is allowed to mount it.
    files = kaggle("datasets", "files", f"{owner}/audiobook-data",
                   "--page-size", "200", timeout=300)
    if f"chapters/{lo}_" not in files:
        print(f"  dataset does NOT yet serve chapters/{lo}_ - refusing to push")
        abpipe("novel.stages.dispatch", "set", "--id", rid, "--status", "failed")
        return False
    print(f"  dataset live: chapters/{lo}_ present")

    nb = KERNEL_DIR / (meta["code_file"])
    set_batch(nb, hi - lo + 1, lo, hi)
    print(f"  BATCH = {hi-lo+1}  # chapters {lo}-{hi}")

    out = kaggle("kernels", "push", "-p", ".", cwd=KERNEL_DIR, timeout=600)
    if "successfully pushed" not in out:
        print(f"  push failed:\n{out.strip()[-300:]}")
        abpipe("novel.stages.dispatch", "set", "--id", rid, "--status", "failed")
        return False
    print("  " + [l for l in out.splitlines() if "pushed" in l][0].strip())
    print("  " + kaggle("kernels", "status", meta["id"], timeout=180).strip())
    return True


def cmd_push(args) -> None:
    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
    n = args.count
    lo = args.start
    plan = []
    for a in accounts:
        plan.append((a, lo, lo + n - 1))
        lo += n
    print("plan:")
    for a, x, y in plan:
        print(f"  {a:8} {x}-{y}")

    ok = sum(push_one(a, x, y, args.dry_run) for a, x, y in plan)
    print(f"\n{ok}/{len(plan)} accounts pushed")
    if not args.dry_run:
        print(abpipe("novel.stages.dispatch", "status").split("RUNS")[0].strip())


# ------------------------------------------------------------------ main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pull", help="download, verify, merge and shelve finished runs")
    p.add_argument("--run", help="only this ledger run id")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_pull)

    q = sub.add_parser("push", help="stage, upload, verify and queue new chapters")
    q.add_argument("--start", type=int, required=True, help="first chapter")
    q.add_argument("--count", type=int, default=15, help="chapters per account")
    q.add_argument("--accounts", default=",".join(ACCOUNTS))
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(fn=cmd_push)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
