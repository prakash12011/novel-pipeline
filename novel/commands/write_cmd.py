"""novel write [--book B] [--ep N] [--chapters A-B] [--max-chapters N] [--model M] [--parallel N] [--dry-run]
              [--no-translate] [--recap] [--redo N] [--fix N] [--translate N[,M...]] [--restore N]

The Hindi, the bible and the picture lines written by the Gemini API (your keys) and filed by the same importer
as `novel import`. See novel/gemini/write.py.
"""

from __future__ import annotations

import argparse
import sys


def main(argv) -> int:
    from novel.core import context
    from novel.gemini import prompt as P
    from novel.gemini import write as W

    ap = argparse.ArgumentParser(
        prog="novel write",
        description="Write the Hindi, the bible and the picture lines with the Gemini API and file them. "
                    "The hand-paste path (novel prompt, then novel import) still works.")
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--chapters", help="first-last, e.g. 21-25 (default: the chapters of the episode that need work)")
    ap.add_argument("--max-chapters", type=int, metavar="N",
                    help="chapters per run (default: settings gemini.chapters_per_run, 5)")
    ap.add_argument("--model", help="Gemini model (default: settings gemini.api_model)")
    ap.add_argument("--parallel", type=int, metavar="N",
                    help="chapters asked at the same time after the bible call (default: settings gemini.parallel, 1)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what it would do and the size of each request; send and write nothing")
    ap.add_argument("--no-translate", action="store_true",
                    help="refuse a chapter that has no Hindi instead of translating it")
    ap.add_argument("--recap", action="store_true",
                    help="only (re)write recap.txt, the very short recap of the whole episode for the next intro")
    ap.add_argument("--redo", metavar="N", help="write the scenes of chapter N again (the old files move to old/vN/)")
    ap.add_argument("--fix", type=int, metavar="N",
                    help="send the QA fix message of chapter N to the API and file the PATCH it returns")
    ap.add_argument("--translate", "--retranslate", dest="translate", metavar="SPEC",
                    help="translate chapters (e.g. 23 or 21,23-25). A chapter that already has a translation keeps "
                         "its old Hindi, audio, timing, captions, scenes, images and QA in old/vK/ (nothing is deleted) "
                         "and is translated again; new narration is then needed: novel tts push --chapters N. "
                         "Undo with --restore N. (--retranslate is the same option.)")
    ap.add_argument("--no-scenes", action="store_true",
                    help="with --translate: translate (and recap) only; do not write the picture lines")
    ap.add_argument("--restore", type=int, metavar="N",
                    help="bring back the most recent old/vK/ of chapter N that has a RESTORE.json; the current "
                         "version is itself set aside first, so restoring is reversible. Makes no API call.")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):                    # Hindi names in a message must not stop the run
        sys.stdout.reconfigure(errors="replace")
    if a.fix is not None and (a.redo or a.chapters):
        print("novel write: --fix N works on one chapter; do not combine it with --redo or --chapters", file=sys.stderr)
        return 2
    if (a.translate is not None or a.restore is not None) and (
            a.fix is not None or a.redo is not None or a.chapters is not None
            or (a.translate is not None and a.restore is not None)):
        print("novel write: --translate and --restore are one-chapter modes; do not combine them with --fix, "
              "--redo, --chapters or each other", file=sys.stderr)
        return 2
    for name in ("max_chapters", "parallel"):
        if getattr(a, name) is not None and getattr(a, name) < 1:
            print(f"novel write: --{name.replace('_', '-')} must be 1 or more", file=sys.stderr)
            return 2
    try:
        book, ep = context.resolve(a.book, a.ep)
        if ep is None:
            print("novel write: no current episode. Pass --ep N or run: novel use <book> --ep N", file=sys.stderr)
            return 2
        runner = W.Runner(book, ep, a)
        if a.recap:
            if a.dry_run:        # the recap has no dry-run of its own: it used to send and overwrite recap.txt
                print(f"Dry run: would write the recap of episode {ep} (up to 3 calls) to "
                      f"{runner.ep_root / 'recap.txt'}; nothing sent, nothing written.")
                return 0
            return runner.recap_only()
        if a.fix is not None:
            return runner.run_fix(a.fix)
        if a.translate is not None:
            return runner.run_retranslate(a.translate)
        if a.restore is not None:
            return runner.run_restore(a.restore)
        return runner.run()
    except P.PromptError as e:
        print(f"novel write: {e}", file=sys.stderr)
        return 1
