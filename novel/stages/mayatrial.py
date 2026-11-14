"""Narrate one already-translated chapter with Maya 2, on Kaggle, as a trial.

This is deliberately NOT kbatch. kbatch refuses chapters that are already done,
and rightly: but a trial WANTS a done chapter, so the result can be compared
with its IndicF5 audio. Everything here stays out of the production path:

  * its own private dataset and kernel, because re-pushing a kernel destroys
    the previous run's output, and the daily kernels may hold unpulled runs
  * no ledger entry and no merge: the master manifest and the IndicF5 audio
    are never touched
  * the book goes to the library under a name that says what it is

Usage:
    python -m abpipe.mayatrial push --chapter 1056
    python -m abpipe.mayatrial status
    python -m abpipe.mayatrial pull
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time

import yaml

from .account import load_profiles
from .kbatch import (LIBRARY, PROJECT, PULL, WS, chapter_no, kaggle, log_text,
                     m4b_ok, master_chapters, use_account)

TRIAL = PULL / "maya2-trial"
DATASET_SLUG = "audiobook-maya2-trial"
KERNEL_SLUG = "maya2-trial"
NOTEBOOK = PROJECT / "kaggle_maya2_trial.ipynb"


def owner(profile: str) -> str:
    from .account import owner as _owner      # one implementation, shared with the episode flow
    return _owner(profile)


def stage(number: int, user: str) -> tuple:
    """A one-chapter dataset: the existing Hindi and the code, no English."""
    picked = [c for c in master_chapters() if chapter_no(c["source"]) == number]
    if not picked or not picked[0].get("hindi"):
        sys.exit(f"chapter {number} has no Hindi translation to narrate")
    ch = picked[0]

    st = TRIAL / "dataset"
    if st.exists():
        shutil.rmtree(st)
    (st / "hindi").mkdir(parents=True)
    shutil.copy2(WS / "hindi" / ch["hindi"], st / "hindi" / ch["hindi"])

    # "translated" is what sends the runner straight to narration.
    slice_ch = {"idx": ch["idx"], "title": ch["title"], "source": ch["source"],
                "status": "translated", "hindi": ch["hindi"], "audio": None,
                "seconds": None, "error": None, "attempts": 0,
                "translate_seconds": None, "tts_seconds": None, "rtf": None}
    (st / "manifest.json").write_text(
        json.dumps({"version": 1, "chapters": [slice_ch]}, ensure_ascii=False,
                   indent=1), encoding="utf-8")

    shutil.copytree(PROJECT / "abpipe", st / "abpipe",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(PROJECT / "config.yaml", st / "config.yaml")
    (st / "dataset-metadata.json").write_text(
        json.dumps({"title": DATASET_SLUG, "id": f"{user}/{DATASET_SLUG}",
                    "licenses": [{"name": "unknown"}]}, indent=2),
        encoding="utf-8")
    return st, ch


def upload(st, user: str, number: int) -> None:
    ds = f"{user}/{DATASET_SLUG}"
    # A dataset that does not exist answers 403, not 404.
    exists = "ready" in kaggle("datasets", "status", ds, timeout=180)
    # From INSIDE the folder, and never -d: see kbatch. A new dataset is
    # private unless --public is passed, which this never does.
    if exists:
        up = kaggle("datasets", "version", "-p", ".", "-m", f"chapter {number}",
                    "-r", "zip", cwd=st, timeout=1800)
    else:
        up = kaggle("datasets", "create", "-p", ".", "-r", "zip", cwd=st,
                    timeout=1800)
    if "is being created" not in up and "successfully" not in up.lower():
        sys.exit(f"upload did not complete:\n{up.strip()[-400:]}")

    # "being created" is not "live", and the kernel would mount the old version.
    hindi = next((st / "hindi").glob("*.txt")).name
    for _ in range(40):
        status = kaggle("datasets", "status", ds, timeout=180)
        files = kaggle("datasets", "files", ds, "--page-size", "200", timeout=180)
        if "ready" in status and hindi in files:
            print(f"  dataset live: {ds}")
            return
        time.sleep(15)
    sys.exit(f"dataset {ds} did not go live - refusing to push the kernel")


def cmd_push(args) -> None:
    use_account(args.account)
    user = owner(args.account)
    st, ch = stage(args.chapter, user)
    print(f"staged chapter {args.chapter}: {ch['hindi']}  account {user}")
    if args.dry_run:
        print(f"dry run - not uploading. look in {st}")
        return
    upload(st, user, args.chapter)

    kd = TRIAL / "kernel"
    kd.mkdir(parents=True, exist_ok=True)
    shutil.copy2(NOTEBOOK, kd / f"{KERNEL_SLUG}.ipynb")
    (kd / "kernel-metadata.json").write_text(json.dumps({
        "id": f"{user}/{KERNEL_SLUG}", "title": KERNEL_SLUG,
        "code_file": f"{KERNEL_SLUG}.ipynb", "language": "python",
        "kernel_type": "notebook", "is_private": True, "enable_gpu": True,
        "enable_tpu": False, "enable_internet": True, "keywords": [],
        "dataset_sources": [f"{user}/{DATASET_SLUG}", f"{user}/hf-token"],
        "kernel_sources": [], "competition_sources": [], "model_sources": [],
        "machine_shape": "NvidiaTeslaT4",
    }, indent=2), encoding="utf-8")

    out = kaggle("kernels", "push", "-p", ".", cwd=kd, timeout=600)
    if "successfully pushed" not in out:
        sys.exit(f"push failed:\n{out.strip()[-400:]}")
    print("  " + [l for l in out.splitlines() if "pushed" in l][0].strip())
    (TRIAL / "last.json").write_text(json.dumps(
        {"account": args.account, "user": user, "chapter": args.chapter}))


def last() -> dict:
    p = TRIAL / "last.json"
    if not p.exists():
        sys.exit("no trial has been pushed")
    return json.loads(p.read_text())


def cmd_status(args) -> None:
    rec = last()
    use_account(rec["account"])
    kern = f"{rec['user']}/{KERNEL_SLUG}"
    print(kaggle("kernels", "status", kern, timeout=180).strip())
    text = log_text(kaggle("kernels", "logs", kern, timeout=600))
    print("\n".join([l for l in text.splitlines() if re.match(
        r"\s*(\[maya2\]|probe|spoke|FAILED|ABORT|building)", l)][-15:]))


def cmd_pull(args) -> None:
    rec = last()
    use_account(rec["account"])
    kern = f"{rec['user']}/{KERNEL_SLUG}"
    status = kaggle("kernels", "status", kern, timeout=180)
    if "COMPLETE" not in status:
        sys.exit(f"not finished: {status.strip()[-200:]}")

    dest = TRIAL / f"out{rec['chapter']}"
    dest.mkdir(parents=True, exist_ok=True)
    print(kaggle("kernels", "output", kern, "-p", str(dest), "-o",
                 "--file-pattern", r"\.(m4b|json|wav)$", "--page-size", "200",
                 timeout=1800).strip()[-200:])

    books = sorted(dest.rglob("*.m4b"))
    if not books:
        sys.exit("no .m4b in the output")
    mins = m4b_ok(books[0])
    if mins <= 0:
        sys.exit(f"{books[0]} is not playable")

    # Not COI_NNNN-NNNN.m4b: that name is the IndicF5 book, and the library
    # is append-only.
    cfg = yaml.safe_load((PROJECT / "config.yaml").read_text(encoding="utf-8"))
    voice = cfg["tts"]["maya2"]["voice"]
    target = LIBRARY / f"TRIAL_COI_{rec['chapter']:04d}_maya2-{voice}.m4b"
    LIBRARY.mkdir(exist_ok=True)
    if target.exists():
        print(f"library: {target.name} already present, left alone")
    else:
        shutil.copy2(books[0], target)
        print(f"library: added {target.name}")
    print(f"{mins:.1f} min, {target.stat().st_size / 1048576:.1f} MB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push")
    p.add_argument("--chapter", type=int, required=True)
    p.add_argument("--account", default="prakash")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_push)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("pull").set_defaults(fn=cmd_pull)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
