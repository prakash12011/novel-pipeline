"""novel hd [--book B] [--ep N] [--chapters A-B] [--limit N] [--force] [--marks-only]

Makes the full-HD copies of a chapter's pictures (images/cNNNN/hd/sNN.jpg, 1920x1080) with chaiNNer: the
Gemini mark is handled the way images.watermark says (crop: cut off; remove: `novel clean` runs first and its
clean copies are used; none: left as it is), a star painted into the picture gets a second trim, 1K pictures go
through the 2x model, and every picture is resized to exactly 1920x1080. This is the same step `novel build`
runs before rendering; `novel render` only uses copies that already exist.

Pictures with an up-to-date hd copy are skipped (--force redoes them). --limit N does only the first N (a
test). --marks-only redoes only the pictures that still show a painted star. Refuses an episode marked
`published: true` or under an `_archive` folder. Exit code 1 when nothing could be done."""

from __future__ import annotations

import argparse

from novel.core import context

USAGE = "usage: novel hd [--book B] [--ep N] [--chapters A-B] [--limit N] [--force] [--marks-only]"


def main(argv) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel hd", usage=USAGE)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--chapters", help="e.g. 24 or 21-25 (default: every chapter of the episode that has pictures)")
    ap.add_argument("--limit", type=int, help="only the first N pictures (a test)")
    ap.add_argument("--force", action="store_true", help="redo pictures that already have an up-to-date hd copy")
    ap.add_argument("--marks-only", action="store_true",
                    help="redo only the pictures that still show a painted star, with a second trim")
    a = ap.parse_args(argv)

    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    print(context.header(book, ep))

    from novel.stages import episode as E
    from novel.stages import episode_clean as CL
    from novel.stages import episode_hd as H
    from novel.stages.episode_translate import parse_range
    from novel.stages.manifest import Manifest

    ep_root = E.root(ep).resolve()
    in_ep = [c.idx for c in Manifest.load(ep_root / "manifest.json").chapters] if (ep_root / "manifest.json").exists() else []
    want = parse_range(a.chapters) if a.chapters else in_ep
    outside = [n for n in want if n not in in_ep]
    if outside:
        print(f"chapters {outside} are not in episode {ep}" + (f" (it has {in_ep[0]}-{in_ep[-1]})" if in_ep else " (no manifest.json yet)"))
        return 1
    missing = H.hd_missing()
    if missing:
        print(f"cannot make hd pictures: {missing}")
        return 1
    if H.hd_mode(E.cfg(ep)) == "false":
        print("note: video.hd.enabled is false, so `novel build` and `novel render` do not use these copies. "
              "Set video.hd.enabled: auto (novel settings) to use them.")

    ready = []
    for n in want:
        why = CL.refusal(ep_root, n, force=True)          # with force only `published` and `_archive` remain
        if why:
            print(f"chapter {n}: not touched: {why}")
            continue
        if not CL.raw_pictures(ep_root, n):
            if a.chapters:
                print(f"chapter {n}: no downloaded pictures in images/c{n:04d}/ (novel images --ep {ep} --chapters {n})")
            continue
        ready.append(n)
    if not ready:
        print("nothing to do: no chapter with downloaded pictures")
        return 1
    print(f"chapters {', '.join(map(str, ready))}: making hd copies")
    H.make_hd(ep_root, ready, limit=a.limit, force=a.force, marks_only=a.marks_only)
    return 0
