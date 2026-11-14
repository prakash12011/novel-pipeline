"""novel import [--book B] [--file F | --inbox] [--batch ID] [--dry-run] [--replace] [--allow-revise]

Files a Gemini reply: from the clipboard (default), a saved reply (--file) or the files saved by hand in
<book>/inbox/ (--inbox). See novel/gemini/importer.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def read_clipboard() -> str | None:
    """The clipboard text (tests replace this function)."""
    from novel.commands.prompt_cmd import get_clipboard
    return get_clipboard()


def main(argv) -> int:
    from novel.core import context
    from novel.gemini import importer as I

    ap = argparse.ArgumentParser(prog="novel import",
                                 description="File a Gemini reply (clipboard by default) and say what to type next.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--file", help="a reply saved as a text file")
    src.add_argument("--inbox", action="store_true", help="import the files saved by hand in <book>/inbox/")
    ap.add_argument("--book")
    ap.add_argument("--batch", help="the batch id (B021-030) when it cannot be found from the blocks")
    ap.add_argument("--dry-run", action="store_true", help="check everything, write nothing (not even the archive)")
    ap.add_argument("--replace", action="store_true",
                    help="replace a chapter that was already imported (old files move to old/vN/)")
    ap.add_argument("--allow-revise", action="store_true", help="accept BIBLE 'revise' lines")
    a = ap.parse_args(argv)

    source_args = (f'--file "{a.file}"' if a.file else "--inbox" if a.inbox else "")
    if a.batch:
        source_args += f" --batch {a.batch}"
    try:
        book, _ = context.resolve(a.book)
        imp = I.Importer(book, dry_run=a.dry_run, replace=a.replace, allow_revise=a.allow_revise,
                         batch=a.batch, source_args=source_args.strip())
        if a.inbox:
            return imp.run_inbox()
        if a.file:
            f = Path(a.file)
            if not f.is_file():
                print(f"novel import: no such file: {f}", file=sys.stderr)
                return 1
            return imp.run_text(f.read_text(encoding="utf-8-sig"), f"file {f}")
        text = read_clipboard()
        if text is None:
            print("novel import: the clipboard has no text (or is not available here). Copy Gemini's reply, "
                  "or save it to a file and use --file.", file=sys.stderr)
            return 1
        return imp.run_text(text, "the clipboard")
    except I.ImportFailure as e:
        print(f"novel import: {e}", file=sys.stderr)
        return 1
