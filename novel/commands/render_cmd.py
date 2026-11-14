"""novel render --chapter N [--book B] [--ep E] [--end SECONDS] [--no-hd] [--no-captions] [--out FILE]

Renders one chapter to video/cNNNN.mp4 (a preview, video/cNNNN-firstNs.mp4, when --end is given).
Checks first that everything the render needs exists; if not, prints each missing item with the command
that makes it and exits 1 without rendering. Exit code 1 also when the render report says it failed."""

from __future__ import annotations

import argparse
import multiprocessing as mp
import shutil
from pathlib import Path

from novel.core import context

USAGE = ("usage: novel render --chapter N [--book B] [--ep E] [--end SECONDS] [--no-hd] [--no-captions] "
         "[--out FILE]")


def main(argv) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel render", usage=USAGE)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--end", type=float, help="render only the first N seconds (a preview)")
    ap.add_argument("--no-hd", action="store_true", help="use the cleaned downloads, not the hd pictures")
    ap.add_argument("--no-captions", action="store_true", help="leave out the English subtitles")
    ap.add_argument("--out", type=Path, help="also copy the finished video to this file")
    a = ap.parse_args(argv)

    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    print(context.header(book, ep))
    if a.end is not None and a.end <= 0:
        raise SystemExit("--end must be more than 0 seconds")

    from novel.stages import episode as E
    from novel.stages import episode_build as B
    from novel.stages import episode_hd
    from novel.stages.episode_render import render
    from novel.stages.manifest import Manifest

    ep_root = E.root(ep).resolve()
    chapters = [c.idx for c in Manifest.load(ep_root / "manifest.json").chapters]
    if a.chapter not in chapters:
        print(f"chapter {a.chapter} is not in episode {ep}"
              + (f" (it has chapters {chapters[0]}-{chapters[-1]})" if chapters else " (no manifest.json yet)"))
        return 1

    captions = not a.no_captions and B.captions_on(ep, a.chapter)
    hd = episode_hd.hd_plan(E.cfg(ep, a.chapter), no_hd=a.no_hd, needs_tool=False)["use_pictures"]
    missing = B.missing_inputs(ep_root, ep, a.chapter, captions=captions)
    short = {a.chapter: missing} if missing else {}
    if short:
        B.print_missing(short)
        print("nothing was rendered.")
        return 1

    try:
        report = render(ep, a.chapter, "20", "fast", max(1, mp.cpu_count() - 2), a.end, None, captions, hd)
    except SystemExit as e:
        print(f"render failed: {e}")
        return 1
    out = ep_root / "video" / report["file"]
    print(f"\n{out}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, a.out)
        print(f"copied to {a.out}")
    print(f"  duration {report['seconds']:.2f} s (expected {report['expected_seconds']:.2f} s)")
    for k, v in report.items():
        print(f"  {k:18} {v}")
    print(f"  result: {'PASS' if report['pass'] else 'FAIL'}")
    return 0 if report["pass"] else 1
