"""novel extract [--book B] [--source-dir DIR] [--out-dir DIR] [--check] [--force]"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from novel.core import context, paths, settings
from novel.stages import extract as ex

SHORT_WORDS = 200      # a chapter under this many words is suspicious (parsing problem?)


def _ranges(gaps) -> str:
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in gaps)


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel extract",
                                 description="Turn the EPUBs (or .txt files) in source/ into chapter text files.")
    ap.add_argument("--book")
    ap.add_argument("--source-dir")
    ap.add_argument("--out-dir")
    ap.add_argument("--check", action="store_true",
                    help="report only, write nothing; exit 1 on gaps, duplicates or short chapters")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing chapter files whose content differs")
    a = ap.parse_args(argv)

    book, _ = context.resolve(a.book)
    src = Path(a.source_dir) if a.source_dir else paths.source_dir(book)
    out = Path(a.out_dir) if a.out_dir else paths.book_dir(book) / "chapters"
    regex = settings.get("book.chapter_regex", book=book, default=None)
    volumes = settings.get("book.volumes", book=book, default=[]) or []

    try:
        sc = ex.scan(src, regex, volumes)
    except (ValueError, re.error) as e:  # bad chapter regex
        print(f"novel extract: {e}", file=sys.stderr)
        return 2

    if not sc.epubs:
        print(f"No EPUB (or .txt) files found in {src}\n"
              f"Put the book's EPUB file(s) there, then run: novel extract", file=sys.stderr)
        return 1

    nums = sc.numbers
    print(f"source: {src}")
    print(f"EPUBs/files found: {len(sc.epubs)}")
    for name in sc.epubs:
        print(f"  {name}")
    print(f"chapters found: {len(sc.chapters)}")
    print(f"skipped documents (not chapters): {sc.skipped}")
    if nums:
        print(f"first chapter: {min(nums)}   last chapter: {max(nums)}")
    vols: dict[int, int] = {}
    for c in sc.chapters:
        vols[c.volume] = vols.get(c.volume, 0) + 1
    if len(vols) > 1 or volumes:
        print("volumes: " + ", ".join(f"{v}: {n} chapters" for v, n in sorted(vols.items())))
    print("gaps: " + (_ranges(sc.gaps) if sc.gaps else "none"))
    print("duplicates: " + (", ".join(map(str, sc.duplicates)) if sc.duplicates else "none"))
    shortest = sorted(sc.chapters, key=lambda c: (c.words, c.num))[:5]
    if shortest:
        print("shortest chapters:")
        for c in shortest:
            print(f"  {c.num:>5}  {c.words:>6} words  {c.title}")
    short = [c for c in sc.chapters if c.words < SHORT_WORDS]
    if short:
        print(f"suspiciously short (< {SHORT_WORDS} words): {len(short)}")

    if a.check:
        bad = bool(sc.gaps or sc.duplicates or short)
        print("check: " + ("PROBLEMS FOUND" if bad else "ok") + " (nothing written)")
        return 1 if bad else 0

    changed = ex.differing(sc.chapters, out)
    if changed and not a.force:
        print(f"\n{len(changed)} existing chapter file(s) in {out} differ from the new extraction:",
              file=sys.stderr)
        for p in changed[:20]:
            print(f"  {p.name}", file=sys.stderr)
        if len(changed) > 20:
            print(f"  ... and {len(changed) - 20} more", file=sys.stderr)
        print("Nothing written. Use --force to overwrite them.", file=sys.stderr)
        return 1
    written = ex.write_chapters(sc.chapters, out)
    print(f"chapters written: {written} (of {len(sc.chapters)}; unchanged files left alone) -> {out}")
    return 0
