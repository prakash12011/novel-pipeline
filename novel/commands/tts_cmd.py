"""novel tts push | status | pull | watch | reset"""

from __future__ import annotations

import argparse

from novel.core import context

USAGE = ("usage: novel tts push [--book B] [--ep N] [--chapters A-B] [--account NAME] [--dry-run]\n"
         "       novel tts status|pull [--book B] [--ep N]\n"
         "       novel tts watch [--book B] [--ep N] [--interval-seconds S]\n"
         "       novel tts reset [--book B] [--ep N] [--yes]")


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub not in ("push", "status", "pull", "watch", "reset"):
        print(f"novel tts: unknown subcommand '{sub}' (push | status | pull | watch | reset)")
        return 2
    ap = argparse.ArgumentParser(prog=f"novel tts {sub}")
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    if sub == "push":
        ap.add_argument("--chapters", help="e.g. 3 or 2-8 (default: locked chapters without audio)")
        ap.add_argument("--account", help="Kaggle profile (default: settings tts.kaggle_profile, "
                                          "then kaggle.account)")
        ap.add_argument("--dry-run", action="store_true",
                        help="stage everything and list it; call nothing")
    if sub == "watch":
        ap.add_argument("--interval-seconds", type=float, default=None,
                        help="poll period (default: settings tts.poll_minutes)")
    if sub == "reset":
        ap.add_argument("--yes", action="store_true",
                        help="do not ask for confirmation (a run that is still going, or finished, is still refused)")
    a = ap.parse_args(rest)

    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    print(context.header(book, ep))
    a.episode = ep
    a.account = getattr(a, "account", None)
    a.chapters = getattr(a, "chapters", None)
    a.dry_run = getattr(a, "dry_run", False)
    a.interval_seconds = getattr(a, "interval_seconds", None)
    a.yes = getattr(a, "yes", False)

    from novel.stages import episode_tts as T
    return {"push": T.cmd_push, "status": T.cmd_status, "pull": T.cmd_pull,
            "watch": T.cmd_watch, "reset": T.cmd_reset}[sub](a)
