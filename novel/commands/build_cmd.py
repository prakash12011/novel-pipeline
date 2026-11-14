"""novel build [--book B] [--ep E] [--chapters A-B] [--force] [--when-ready] [--no-hd] [--per-hour N]

Builds the episode video: the optional HD step (video.hd.enabled), every chapter that is not current
rendered, the join, the timestamps. Stops before any work and lists what is missing, unless --when-ready,
which waits for it (a check every minute, a line each time) and then renders a chapter every 60/N minutes
(--per-hour N, default 2). Ctrl+C stops cleanly (exit 130). Exit code 1 if anything is missing or failed."""

from __future__ import annotations

import argparse

from novel.core import context

USAGE = ("usage: novel build [--book B] [--ep E] [--chapters A-B] [--force] [--when-ready] [--no-hd] "
         "[--per-hour N]")


def main(argv, *, sleep=None, clock=None, interval=None, max_wait=None) -> int:
    """sleep / clock / interval / max_wait are for tests: the wait loop's sleep, clock, check period, give-up time."""
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel build", usage=USAGE)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--chapters", help="e.g. 3 or 2-8 (default: every chapter of the episode); a partial "
                                       "episode is written as E00N-cA-B.mp4")
    ap.add_argument("--force", action="store_true", help="render the listed chapters again even if current")
    ap.add_argument("--when-ready", action="store_true",
                    help="wait for missing inputs instead of stopping, then pace the renders")
    ap.add_argument("--no-hd", action="store_true", help="skip the HD step and use the cleaned downloads")
    ap.add_argument("--per-hour", type=float, help="chapters started per hour while pacing (default 2 with --when-ready)")
    a = ap.parse_args(argv)
    if a.per_hour is not None and a.per_hour <= 0:
        raise SystemExit("--per-hour must be more than 0")

    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    print(context.header(book, ep))

    from novel.stages import episode_build as B
    from novel.stages import episode_pace as P
    kw = {k: v for k, v in (("sleep", sleep), ("clock", clock)) if v is not None}
    try:
        r = B.build_episode(ep, a.chapters, force=a.force, hd=not a.no_hd, when_ready=a.when_ready,
                            per_hour=a.per_hour, interval=P.CHECK_EVERY if interval is None else interval,
                            max_wait=P.MAX_WAIT if max_wait is None else max_wait, **kw)
    except KeyboardInterrupt:
        print("\ninterrupted: stopped. Run the same command again to carry on (finished chapters are kept).")
        return 130

    print()
    for c in r["chapters"]:
        rep = c["report"]
        if c["action"] == "current":
            print(f"  chapter {c['chapter']}: current (not rendered), {rep.get('seconds', '?')} s")
        else:
            res = "pass" if rep.get("pass") else "FAIL"
            extra = "" if rep.get("pass") else f"  jerk {rep.get('jerk')}, blank frames {rep.get('blank_frames')}" + \
                (f", {rep['error'][-200:]}" if rep.get("error") else "")
            print(f"  chapter {c['chapter']}: rendered ({c['why']}), {res}, {rep.get('seconds', 0)} s, {rep.get('mb', 0)} MB{extra}")
    if r["status"] == "built":
        print(f"\nvideo:       {r['video']}")
        print(f"duration:    {r['duration']:.1f} s ({r['duration'] / 60:.1f} min)")
        print("timestamps:  " + "\n             ".join(r["timestamps"]))
        print("\nnext: novel meta")
        return 0
    if r["status"] == "missing":
        return 1
    print(f"\nnot built: status {r['status']}")
    return 1
