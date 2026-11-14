"""novel status [--book B] [--ep N] [--all] [--json] [--chapters A-B]

Is this episode ready, and if not, exactly what is missing and which command fixes it? Reads files only;
writes nothing. Exit code 0 when the verdict is READY TO BUILD, READY TO PUBLISH or PUBLISHED, else 1.
"""

from __future__ import annotations

import argparse
import json
import sys

from novel import status as ST
from novel.core import context, paths


def _fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel status", description=__doc__.split("\n\n")[0])
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--all", action="store_true", help="one line per episode of the book, newest first")
    ap.add_argument("--json", action="store_true", help="print the whole result as JSON")
    ap.add_argument("--chapters", help="e.g. 24 or 21-25: limit the matrix and the verdict to these chapters")
    a = ap.parse_args(argv)

    try:
        book, ep = context.resolve(a.book, a.ep)
    except SystemExit as e:
        return _fail(str(e.code))
    state = context.read_state()
    state_key = (state.get("book"), state.get("episode"))
    deps = ST.default_deps(book)

    if a.all:
        rows = ST.collect_all(book, deps, state=state_key)
        if a.json:
            print(json.dumps({"book": book, "episodes": rows}, ensure_ascii=False, indent=2))
        else:
            print(ST.format_all(book, rows))
        return 0

    if ep is None:
        return _fail("no current episode. Pass --ep N, or run: novel use <book> --ep N")
    root = paths.ep_root(book, ep)
    if not root.is_dir():
        return _fail(f"episode folder not found: {root}")
    chapters = None
    if a.chapters:
        from novel.stages.episode_translate import parse_range
        try:
            chapters = parse_range(a.chapters)
        except ValueError:
            return _fail(f"--chapters must look like 24 or 21-25, not {a.chapters!r}")
        have = {c.idx for c in ST.chapter_numbers(root)[0]}
        if not set(chapters) & have:
            return _fail(f"none of chapters {a.chapters} are in episode {ep}")
        chapters = [c for c in chapters if c in have]
    res = ST.collect(root, book, ep, deps, chapters=chapters, state=state_key)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print(ST.format_text(res))
    return res["exit_code"]
