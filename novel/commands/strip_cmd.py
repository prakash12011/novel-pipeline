"""novel strip-heading: take the spoken chapter heading out of an episode's Hindi, audio, timing, captions, scenes."""

from __future__ import annotations

import argparse

from novel.core import context


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel strip-heading", description=(
        "Remove the 'चैप्टर ...: ...' heading line from each chapter's Hindi and, when the chapter is narrated, cut it "
        "out of the mp3 (no new Kaggle run) and move timing, captions and scene paragraphs up. Originals are copied to "
        "old/heading/. Chapters without a heading are left alone."))
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--chapters", help="e.g. 1-10 (default: the whole episode)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    from novel.stages.episode_strip import strip_episode
    from novel.stages.episode_translate import parse_range
    print(context.header(book))
    strip_episode(ep, parse_range(a.chapters) if a.chapters else None, a.dry_run)
    return 0
