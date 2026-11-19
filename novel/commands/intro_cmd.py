"""novel intro init | shots | images | render: the spoken hook before an episode's first chapter (pseudo-episode E9NNN)."""

from __future__ import annotations

import argparse
from pathlib import Path

from novel.core import context


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel intro", description=(
        "init: store the Hindi hook text (one paragraph per picture); shots: Claude writes shots.json (one shot per "
        "paragraph); images: Qwen pictures on the Kaggle ComfyUI from shots.json; render: the full-screen clip the episode join puts first. Narrate it in between with "
        "`novel tts push --ep 9NNN --chapters 1` and `novel tts pull --ep 9NNN` (9000 + the episode number)."))
    ap.add_argument("what", choices=["init", "shots", "images", "render"])
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--text-file", type=Path, help="init: UTF-8 file with the Hindi hook")
    ap.add_argument("--only", help="images: e.g. s02,s04 (redoes them)")
    ap.add_argument("--seed", type=int, help="images: another seed for the shots done (default: shots.json or 202)")
    ap.add_argument("--redo", action="store_true", help="shots: write shots.json again (the old one is kept); images: make every picture again")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args(argv)
    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    from novel.stages import episode_intro as I
    print(context.header(book))
    if a.what == "init":
        if not a.text_file:
            raise SystemExit("init needs --text-file <file with the Hindi hook>")
        I.init(ep, a.text_file.read_text(encoding="utf-8"))
    elif a.what == "shots":
        I.write_shots(ep, redo=a.redo)
    elif a.what == "images":
        I.images(ep, [x.strip() for x in a.only.split(",")] if a.only else None, a.seed, a.redo)
    else:
        I.render(ep, a.workers)
    return 0
