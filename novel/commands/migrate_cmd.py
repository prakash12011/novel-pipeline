"""novel migrate coi --from "D:/COI Audiobook" [--dry-run] [--with-media]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from novel import migrate


def main(argv) -> int:
    ap = argparse.ArgumentParser(
        prog="novel migrate",
        description="Copy the old COI Audiobook project into the new layout (copy only; never changes the old folder).")
    ap.add_argument("book", help="book slug (only 'coi' is supported)")
    ap.add_argument("--from", dest="old", required=True, help="the old project folder")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--with-media", action="store_true",
                    help="also copy the finished E001/E002 videos and thumbnails")
    a = ap.parse_args(argv)
    if a.book != "coi":
        print(f"novel migrate: only 'coi' can be migrated (got '{a.book}')", file=sys.stderr)
        return 2
    old = Path(a.old).expanduser()
    if not old.is_dir():
        print(f"novel migrate: {old} is not a folder", file=sys.stderr)
        return 2
    return migrate.run(old, a.book, a.dry_run, a.with_media)
