"""novel captions [--book B] [--ep N] [--chapters A-B] [--force]

Translates the English subtitle text for every locked chapter that has no current captions file.
Needs only the locked Hindi (no narration, no timing); the times are applied when a chapter is
rendered. Exit code 1 if any chapter failed."""

from __future__ import annotations

import argparse

from novel.core import context

USAGE = "usage: novel captions [--book B] [--ep N] [--chapters A-B] [--force]"


def main(argv) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel captions", usage=USAGE)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--chapters", help="e.g. 3 or 2-8 (default: every chapter of the episode)")
    ap.add_argument("--force", action="store_true", help="translate again even if the captions are current")
    a = ap.parse_args(argv)

    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    print(context.header(book, ep))

    from novel.stages import episode as E
    from novel.stages import episode_captions as C
    from novel.stages.episode_translate import parse_range
    from novel.stages.manifest import Manifest

    conf = (E.cfg(ep).get("video") or {}).get("captions") or {}
    if conf.get("enabled") is False:
        print("captions are switched off (video.captions.enabled: false): nothing to do")
        return 0

    ep_root = E.root(ep)
    in_episode = [c.idx for c in Manifest.load(ep_root / "manifest.json").chapters]
    wanted = parse_range(a.chapters) if a.chapters else in_episode
    model = ((E.cfg(ep).get("gemini") or {}).get("api_model")) or C.MODEL

    g = None
    failed = 0
    for n in wanted:
        name = f"{n:05d}"
        if n not in in_episode:
            print(f"chapter {n}: skipped: not in this episode")
            continue
        if not (ep_root / "hindi" / f"{name}.txt").exists():
            print(f"chapter {n}: skipped: not locked (no Hindi yet)")
            continue
        try:
            E.check_lock(ep_root, n)
        except SystemExit:
            print(f"chapter {n}: skipped: not locked")
            continue
        if not a.force and not C.stale(ep_root, n):
            old = " (old format)" if C.is_legacy(C.load(ep_root, n)) else ""
            print(f"chapter {n}: already current{old}")
            continue
        try:
            g = g or C.make_gemini(ep_root, model)
            C.translate_chapter(ep_root, n, g)
            print(f"chapter {n}: made")
        except (SystemExit, Exception) as e:      # one bad chapter must not stop the others
            failed += 1
            print(f"chapter {n}: FAILED: {e}")
    return 1 if failed else 0
