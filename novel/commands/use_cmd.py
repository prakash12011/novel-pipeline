"""novel use <book> [--ep N]"""

from __future__ import annotations

import argparse
import sys

from novel.core import context, paths


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel use")
    ap.add_argument("book")
    ap.add_argument("--ep", type=int)
    a = ap.parse_args(argv)
    root = paths.data_dir()
    if not (root / a.book).is_dir():
        have = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []
        print(f"book '{a.book}' not found in {root}.", file=sys.stderr)
        print("Books there: " + (", ".join(have) if have else "(none yet)"), file=sys.stderr)
        return 1
    context.write_state(a.book, a.ep)
    print(context.header(a.book, a.ep))
    return 0
