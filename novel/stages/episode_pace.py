"""Waiting and pacing for `novel build --when-ready [--per-hour N]`.

While images (or narration) are still being made, a build waits until every input of every chapter it
has to render exists, then runs the HD step, then renders the chapters in order, never starting one
sooner than one slot after the previous one began: --per-hour 2 means a new chapter every 30 minutes.
Chapters that are current are skipped, and at the end the episode is joined.

This module holds only the two waits and the log; episode_build.build_episode drives them, all in this
process (no subprocesses, so nothing depends on PYTHONPATH or the working directory):

    novel build --ep 2 --chapters 11-20 --when-ready --per-hour 2

Log: <episode folder>/pace.log (every line is also printed). Safe to stop (Ctrl+C) and rerun.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Callable

MAX_WAIT = 10 * 3600       # give up on chapters whose inputs never arrive (a refused shot, a Flow block)
CHECK_EVERY = 60           # seconds between two checks for missing inputs
DEFAULT_PER_HOUR = 2.0     # chapters started per hour when --when-ready is given without --per-hour


def make_log(ep_root: Path | None, to_file: bool = True) -> Callable[[str], None]:
    """A log(msg) that prints a time-stamped line and, with to_file, appends it to <ep_root>/pace.log."""
    def log(msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        if to_file and ep_root is not None:
            try:
                with open(ep_root / "pace.log", "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
    return log


def wait_until_ready(check: Callable[[list[int]], dict], chapters: list[int], log: Callable[[str], None], *,
                     sleep: Callable[[float], None] = time.sleep, interval: float = CHECK_EVERY,
                     max_wait: float = MAX_WAIT) -> tuple[list[int], dict]:
    """Call check(chapters) -> {chapter: [missing items]} until nothing is missing for any of them.

    Prints one line every time it checks. After max_wait seconds the chapters still missing inputs are
    given up on. Returns (chapters that are ready, {chapter: missing} of those given up on)."""
    waited = 0.0
    while True:
        short = check(chapters)
        if not short:
            return list(chapters), {}
        if waited >= max_wait:
            log(f"giving up on chapters {sorted(short)} after {max_wait / 3600:g} h: inputs still missing")
            return [n for n in chapters if n not in short], short
        what = "; ".join(f"c{n:04d} {', '.join(m['kind'] for m in items)}" for n, items in sorted(short.items()))
        log(f"waiting for inputs (waited {waited / 60:.0f} min, next check in {interval:g} s): {what}")
        sleep(interval)
        waited += interval


def wait_for_slot(last_start: float | None, gap: float, log: Callable[[str], None], label: str, *,
                  sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> None:
    """Sleep until one slot (gap seconds) after the previous render started. No wait before the first."""
    if last_start is None:
        return
    wait = last_start + gap - clock()
    if wait > 0:
        log(f"{label}: next slot in {wait / 60:.0f} min")
        sleep(wait)
