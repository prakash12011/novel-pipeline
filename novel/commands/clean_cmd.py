"""novel clean [--book B] [--ep N] [--chapters A-B] [--force] [--sheet] [--open]

Removes the Gemini mark from a chapter's downloaded pictures with the npm package @pilio/gemini-watermark-remover
(installed by `npm ci` in flow/) and writes images/cNNNN/clean/sNN.jpg plus clean/clean.json. Every picture is
checked after the tool ran; one that still shows the mark, a dark ghost or a bright rim is CROPPED instead, the
way `images.watermark: crop` does it, so no clean copy ever carries a visible mark. Pictures that already have an
up-to-date clean copy are skipped (--force redoes them). --sheet writes clean/sheet.html (before/after of every
picture's corner, rejected ones first); --open shows it in the browser.

Works whatever images.watermark says; `novel build` / `novel render` use the copies only when it is `remove`.

Old pictures are never touched by accident. It refuses (and says why) an episode marked `published: true`, anything
under an `_archive` folder, and a chapter that already has pictures or a video made the old crop way, unless you
give --force together with an explicit --chapters (that lifts only the last case; a published or archived episode
is never cleaned). Exit code 1 only when nothing could be done (no episode, a chapter not in it, the tool not
installed, every chapter refused)."""

from __future__ import annotations

import argparse
import webbrowser

from novel.core import context

USAGE = "usage: novel clean [--book B] [--ep N] [--chapters A-B] [--force] [--sheet] [--open]"


def main(argv) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel clean", usage=USAGE)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--chapters", help="e.g. 11 or 11-14 (default: every chapter of the episode that has pictures)")
    ap.add_argument("--force", action="store_true", help="redo pictures that already have an up-to-date clean copy")
    ap.add_argument("--sheet", action="store_true", help="write clean/sheet.html: before / after of each picture's corner")
    ap.add_argument("--open", action="store_true", help="open the sheet in the default browser (implies --sheet)")
    a = ap.parse_args(argv)

    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    print(context.header(book, ep))

    from novel.stages import episode as E
    from novel.stages import episode_clean as CL
    from novel.stages.episode_translate import parse_range
    from novel.stages.manifest import Manifest

    ep_root = E.root(ep).resolve()
    in_ep = [c.idx for c in Manifest.load(ep_root / "manifest.json").chapters] if (ep_root / "manifest.json").exists() else []
    want = parse_range(a.chapters) if a.chapters else in_ep
    outside = [n for n in want if n not in in_ep]
    if outside:
        print(f"chapters {outside} are not in episode {ep}" + (f" (it has {in_ep[0]}-{in_ep[-1]})" if in_ep else " (no manifest.json yet)"))
        return 1
    missing = CL.tool_missing()
    if missing:
        print(f"cannot remove the mark: {missing}")
        return 1
    mode = CL.setting_for(ep_root)
    if mode != "remove":
        print(f"note: images.watermark is '{mode}', so `novel build` and `novel render` do not use these copies yet. "
              f"Set images.watermark: remove (novel settings) to use them.")
    did = refused = 0
    sheets = []
    for n in want:
        if not CL.raw_pictures(ep_root, n):
            print(f"chapter {n}: no downloaded pictures in images/c{n:04d}/ (novel images --ep {ep} --chapters {n})")
            continue
        try:
            r = CL.clean_chapter(ep_root, n, force=a.force, allow_old=bool(a.force and a.chapters))
        except CL.Refused as e:
            print(e)
            if a.force and not a.chapters:
                print(f"  (--force lifts a refusal only together with an explicit --chapters)")
            elif "published" not in str(e) and "_archive" not in str(e):
                print(f"  (to clean it anyway: novel clean --chapters {n} --force)")
            refused += 1
            continue
        except SystemExit as e:
            print(f"chapter {n}: {e}")
            return 1
        did += 1
        s = CL.summary(r["pictures"])
        made = len(r["done"])
        print(f"chapter {n}: {s['total']} pictures; made {made}, already clean {s['total'] - made}")
        print(f"  {CL.summary_line(s)}")
        if a.sheet or a.open:
            f = CL.write_sheet(ep_root, n)
            sheets.append(f)
            print(f"  sheet: {f}")
    if a.open:
        for f in sheets:
            webbrowser.open(f.as_uri())
    return 0 if did else 1
