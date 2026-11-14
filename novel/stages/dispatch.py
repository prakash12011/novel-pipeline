"""Chapter dispatch ledger across multiple Kaggle accounts.

The master manifest records what is DONE. It cannot record what is
IN FLIGHT - a batch running on someone's account has claimed chapters that
are not finished and must not be handed to another account. Without that
second piece, picking the next range is guesswork, and the same chapters get
narrated twice on two accounts (which has already happened here once).

This ledger closes that gap. `next` derives the next free range from
DONE (master manifest) + IN FLIGHT (this file), so double-assignment is
arithmetic rather than memory.

Statuses:
    running   - dispatched, still executing; its chapters are reserved
    failed    - terminal failure; chapters released back to the pool
    done      - finished AND merged into the master manifest
    abandoned - manually released

Usage:
    python -m abpipe.dispatch status
    python -m abpipe.dispatch next --count 5
    python -m abpipe.dispatch add --account shashi --user karanprakash62 \
        --kernel karanprakash62/hindi-audiobook-daily --start 892 --end 896 \
        --gpu P100 --precision fp32
    python -m abpipe.dispatch set --id <id> --status done
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LEDGER = PROJECT / "runs.json"
MANIFEST = PROJECT / "workspace" / "manifest.json"

# The scope of this project: chapters 802-1180 of the source epubs.
FIRST_CHAPTER = 802
LAST_CHAPTER = 1180

RESERVING = ("running",)          # statuses that hold chapters


def load_ledger() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    return {"runs": []}


def save_ledger(led: dict) -> None:
    LEDGER.write_text(json.dumps(led, indent=2), encoding="utf-8")


def chapter_no(source: str) -> int:
    return int(source.split("_", 1)[0])


def done_chapters() -> set[int]:
    if not MANIFEST.exists():
        return set()
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    chs = raw["chapters"] if isinstance(raw, dict) else raw
    return {chapter_no(c["source"]) for c in chs if c.get("status") == "done"}


def reserved_chapters(led: dict) -> dict[int, str]:
    """chapter -> run id, for every chapter held by a live run."""
    out = {}
    for r in led["runs"]:
        if r["status"] in RESERVING:
            for n in range(r["start"], r["end"] + 1):
                out[n] = r["id"]
    return out


def _fmt_ranges(nums) -> str:
    """Collapse a sorted set into compact ranges: 850-886, 892-896."""
    nums = sorted(nums)
    if not nums:
        return "(none)"
    out, lo, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append((lo, prev))
        lo = prev = n
    out.append((lo, prev))
    return ", ".join("%d" % a if a == b else "%d-%d" % (a, b) for a, b in out)


def cmd_status(led: dict) -> None:
    done = done_chapters()
    res = reserved_chapters(led)
    total = LAST_CHAPTER - FIRST_CHAPTER + 1

    print("CHAPTERS %d-%d  (%d total)" % (FIRST_CHAPTER, LAST_CHAPTER, total))
    print("  done      %4d   %s" % (len(done), _fmt_ranges(done)))
    print("  in flight %4d   %s" % (len(res), _fmt_ranges(res)))
    print("  remaining %4d" % (total - len(done) - len(res)))
    print()
    if not led["runs"]:
        print("no runs recorded yet")
        return
    print("RUNS")
    print("  %-16s %-10s %-34s %-11s %-6s %-5s %s"
          % ("id", "account", "kernel", "chapters", "gpu", "prec", "status"))
    for r in led["runs"]:
        print("  %-16s %-10s %-34s %-11s %-6s %-5s %s" % (
            r["id"], r["account"], r["kernel"],
            "%d-%d" % (r["start"], r["end"]),
            r.get("gpu", "?"), r.get("precision", "?"), r["status"]))

    # Overlap check: two live runs must never hold the same chapter.
    seen, clash = {}, []
    for r in led["runs"]:
        if r["status"] not in RESERVING:
            continue
        for n in range(r["start"], r["end"] + 1):
            if n in seen and seen[n] != r["id"]:
                clash.append((n, seen[n], r["id"]))
            seen[n] = r["id"]
    print()
    if clash:
        print("  *** OVERLAP: %d chapters claimed by two live runs ***" % len(clash))
        for n, a, b in clash[:5]:
            print("      ch %d held by %s and %s" % (n, a, b))
    else:
        print("  no overlap between live runs")


def cmd_next(led: dict, count: int) -> None:
    done = done_chapters()
    res = reserved_chapters(led)
    picked = []
    for n in range(FIRST_CHAPTER, LAST_CHAPTER + 1):
        if len(picked) >= count:
            break
        if n in done or n in res:
            continue
        picked.append(n)
    if not picked:
        print("nothing left to dispatch")
        return
    # Report contiguity explicitly: a gap means earlier chapters are still
    # reserved or done, and the caller may prefer to wait rather than
    # narrate out of order.
    contiguous = picked == list(range(picked[0], picked[0] + len(picked)))
    print("next %d free chapters: %s" % (len(picked), _fmt_ranges(picked)))
    print("  contiguous:", "yes" if contiguous else "NO - the range has gaps")
    if not contiguous:
        print("  (gaps mean those chapters are done or held by a live run)")


def cmd_add(led: dict, args) -> None:
    res = reserved_chapters(led)
    done = done_chapters()
    want = set(range(args.start, args.end + 1))
    conflict_live = sorted(want & set(res))
    conflict_done = sorted(want & done)
    if conflict_live or conflict_done:
        print("REFUSING to record this run:")
        if conflict_live:
            print("  already held by a live run:", _fmt_ranges(conflict_live))
        if conflict_done:
            print("  already finished:", _fmt_ranges(conflict_done))
        print("  use `next` to get a free range")
        raise SystemExit(1)
    rid = args.id or "%s-%d" % (args.account, args.start)
    if any(r["id"] == rid for r in led["runs"]):
        print("run id %r already exists" % rid)
        raise SystemExit(1)
    led["runs"].append({
        "id": rid,
        "account": args.account,
        "kaggle_user": args.user,
        "kernel": args.kernel,
        "start": args.start,
        "end": args.end,
        "gpu": args.gpu,
        "precision": args.precision,
        "started": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "status": "running",
    })
    save_ledger(led)
    print("recorded %s: chapters %d-%d on %s (%s, %s)"
          % (rid, args.start, args.end, args.kernel, args.gpu, args.precision))


def cmd_set(led: dict, rid: str, status: str) -> None:
    for r in led["runs"]:
        if r["id"] == rid:
            old = r["status"]
            r["status"] = status
            save_ledger(led)
            print("%s: %s -> %s" % (rid, old, status))
            if status not in RESERVING:
                print("  chapters %d-%d released back to the pool"
                      % (r["start"], r["end"]))
            return
    print("no run with id %r" % rid)
    raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    n = sub.add_parser("next"); n.add_argument("--count", type=int, default=5)
    a = sub.add_parser("add")
    a.add_argument("--account", required=True)
    a.add_argument("--user", required=True)
    a.add_argument("--kernel", required=True)
    a.add_argument("--start", type=int, required=True)
    a.add_argument("--end", type=int, required=True)
    a.add_argument("--gpu", default="?")
    a.add_argument("--precision", default="fp32")
    a.add_argument("--id")
    s = sub.add_parser("set")
    s.add_argument("--id", required=True)
    s.add_argument("--status", required=True,
                   choices=["running", "failed", "done", "abandoned"])
    args = ap.parse_args()

    led = load_ledger()
    if args.cmd == "next":
        cmd_next(led, args.count)
    elif args.cmd == "add":
        cmd_add(led, args)
    elif args.cmd == "set":
        cmd_set(led, args.id, args.status)
    else:
        cmd_status(led)


if __name__ == "__main__":
    main()
