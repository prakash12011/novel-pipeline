"""novel extras voice | cards: the subscribe message shared by every episode (pseudo-episode E900)."""

from __future__ import annotations

import argparse

from novel.core import context


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel extras")
    ap.add_argument("what", choices=["voice", "cards"],
                    help="voice: write the message and print the narration commands; "
                         "cards: build the voiced and silent card videos")
    ap.add_argument("--book")
    a = ap.parse_args(argv)
    book, _ = context.resolve(a.book)
    context.activate(book)
    from novel.stages import episode_extras as X
    print(context.header(book))
    if a.what == "voice":
        X.setup_message()
    else:
        X.make_cards()
    return 0
