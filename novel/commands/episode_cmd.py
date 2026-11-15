"""novel episode add [--book B] [--chapters A-B] [--ep N]  |  novel episode list [--book B]"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from novel.core import context, paths, settings
from novel.stages import episode as ep_stage

EXTRAS_FROM = ep_stage.EXTRAS_FROM

EPISODE_YAML = """\
# Settings for THIS episode only. Anything here beats book.yaml.
published: false      # set to true after the video is uploaded

# To change a setting for the whole episode, take the # off the lines and edit them, for example:
# video:
#   captions: {{enabled: false}}      # no subtitles anywhere in this episode
# images:
#   flow_project: "https://flow.google.com/project/..."
# tts:
#   voice: Anirban

# To change a setting for ONE chapter only (the number is the chapter number; {example} is the first one here).
# These per-chapter keys really change the video: video.logo, video.captions, video.subscribe.silent_after.
# (images.max_seconds and images.paced_seconds are also allowed, but they only change the wording of the
# Gemini prompt.) Any other key is refused with "cannot be overridden per chapter".
# chapters:
#   {example}:
#     video:
#       logo: {{enabled: false}}              # no logo on this chapter
#       captions: {{enabled: false}}          # no subtitles on this chapter
#       subscribe: {{silent_after: true}}     # a silent subscribe card after this chapter (false = none)
"""


def _err(msg: str) -> int:
    print(f"novel episode: {msg}", file=sys.stderr)
    return 1


def _chapter_numbers(chapters_dir: Path) -> list[int]:
    nums = []
    for f in chapters_dir.glob("*.txt"):
        m = re.match(r"^(\d+)_", f.name)
        if m:
            nums.append(int(m.group(1)))
    return sorted(set(nums))


def existing_episodes(book: str) -> list[tuple[int, int | None, int | None]]:
    """[(number, start, end)] for episodes below 900, by number."""
    out = []
    d = paths.episodes_dir(book)
    if not d.is_dir():
        return out
    for p in sorted(d.iterdir()):
        m = re.match(r"^E(\d+)$", p.name)
        if not (m and p.is_dir()) or int(m.group(1)) >= EXTRAS_FROM:
            continue
        start = end = None
        try:
            info = json.loads((p / "episode.json").read_text(encoding="utf-8"))
            start, end = int(info["start"]), int(info["end"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        out.append((int(m.group(1)), start, end))
    return out


def _ranges(nums: list[int]) -> str:
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def _parse_range(text: str) -> tuple[int, int]:
    m = re.match(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$", text)
    if not m:
        raise ValueError(f"--chapters '{text}' is not a range. Use A-B, for example --chapters 21-25")
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    if a < 1 or b < a:
        raise ValueError(f"--chapters {text}: the first chapter must be 1 or more and not above the last")
    return a, b


def add(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel episode add",
                                 description="Create the next episode folder from the extracted chapters.")
    ap.add_argument("--book")
    ap.add_argument("--chapters", metavar="A-B", help="chapter range, e.g. 21-25")
    ap.add_argument("--ep", type=int, metavar="N", help="episode number (default: next free one)")
    a = ap.parse_args(argv)

    book, _ = context.resolve(a.book)
    context.activate(book)
    chapters_dir = paths.chapters_dir(book)
    have = _chapter_numbers(chapters_dir) if chapters_dir.is_dir() else []
    eps = existing_episodes(book)
    cpe = int((ep_stage.cfg().get("book") or {}).get("chapters_per_episode") or 10)

    if a.ep is not None:
        if a.ep < 1 or a.ep >= EXTRAS_FROM:
            return _err(f"--ep {a.ep}: use a number from 1 to {EXTRAS_FROM - 1} "
                        f"(900 and above are extras)")
        n = a.ep
    else:
        n = max([e[0] for e in eps], default=0) + 1
    ep_root = paths.ep_root(book, n)
    if ep_root.exists():
        return _err(f"{ep_root} already exists. Nothing was changed. "
                    f"Pick another number with --ep, or delete that folder yourself first.")

    if not have:
        return _err(f"there are no chapters in {chapters_dir} yet.\n"
                    f"Put the EPUB in {paths.source_dir(book)} and run: novel extract")

    covered = sorted({c for _, s, e in eps if s is not None for c in range(s, e + 1)})
    if a.chapters:
        try:
            start, end = _parse_range(a.chapters)
        except ValueError as e:
            print(f"novel episode: {e}", file=sys.stderr)
            return 2
        for num, s, e in eps:
            if s is not None and not (end < s or start > e):
                if (s, e) == (start, end):
                    return _err(f"chapters {start}-{end} are already episode {num} ({ep_root.parent / f'E{num:03d}'}). "
                                f"Nothing was overwritten.")
                return _err(f"chapters {start}-{end} overlap episode {num} (chapters {s}-{e}). "
                            f"Choose a range that does not use chapters already in an episode.")
        missing = [c for c in range(start, end + 1) if c not in have]
        if missing:
            return _err(f"chapters {_ranges(missing)} are not in {chapters_dir}. "
                        f"Run: novel extract (or choose a range that exists: {_ranges(have)})")
        gap = [c for c in range(1, start) if c not in covered]
        if gap:
            print(f"warning: chapters {_ranges(gap)} are not in any episode (this episode "
                  f"starts at chapter {start}). Continuing.")
    else:
        start = max(covered, default=0) + 1
        left = [c for c in have if c >= start]
        if not left:
            return _err(f"no chapters are left: all {len(have)} extracted chapters "
                        f"({_ranges(have)}) are already in episodes.\n"
                        f"If the book has more chapters, add the EPUB to {paths.source_dir(book)} "
                        f"and run: novel extract. Otherwise the book is finished.")
        if left[0] != start:
            return _err(f"chapter {start} is not in {chapters_dir} (the next chapter found is "
                        f"{left[0]}). Run: novel extract, or choose a range with --chapters A-B")
        run = 1
        while run < len(left) and left[run] == left[run - 1] + 1:
            run += 1
        if run < cpe:
            last = left[run - 1]
            return _err(f"only {run} chapter(s) are left ({start}-{last}) but an episode needs "
                        f"{cpe}. To make a shorter episode, run:\n"
                        f"  novel episode add --chapters {start}-{last}")
        end = start + cpe - 1

    count = ep_stage.create_episode(ep_root, n, start, end, chapters_dir)
    (ep_root / "episode.yaml").write_text(
        EPISODE_YAML.format(example=start), encoding="utf-8")
    context.write_state(book, n)

    print(f"Created episode E{n:03d}: chapters {start}-{end} ({count} files) in {ep_root}")
    print(f"'{book}' episode {n} is now current.\n")
    print("Next step (the Gemini API writes the Hindi, the bible and the picture lines):")
    print("  novel write")
    print("Or by hand, pasting into the Gemini app:")
    print(f"  novel prompt --chapters {start}-{end} --copy")
    return 0


def _count(d: Path, pattern: str) -> int:
    return len(list(d.glob(pattern))) if d.is_dir() else 0


def _locked(ep_root: Path) -> int:
    f = ep_root / "hindi.lock.json"
    try:
        return len(json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return 0


def _published(ep_root: Path) -> bool:
    try:
        data = yaml.safe_load((ep_root / "episode.yaml").read_text(encoding="utf-8")) or {}
        return bool(data.get("published"))
    except (OSError, yaml.YAMLError, AttributeError):
        return False


def list_cmd(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel episode list")
    ap.add_argument("--book")
    a = ap.parse_args(argv)
    book, cur = context.resolve(a.book)
    eps = existing_episodes(book)
    if not eps:
        print(f"[{book}] no episodes yet. Add the first one with: novel episode add")
        return 0
    print(f"[{book}]")
    for n, s, e in eps:
        r = paths.ep_root(book, n)
        rng = f"{s}-{e}" if s is not None else "?"
        files = _count(r / "chapters", "*.txt")
        print(f"E{n:03d}  ch {rng:<9} {files:>3} chapter files  "
              f"hindi {_count(r / 'hindi', '*.txt')}  locked {_locked(r)}  "
              f"audio {_count(r / 'audio', '*.mp3')}  "
              f"published {'yes' if _published(r) else 'no'}")
    return 0


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: novel episode add [--book B] [--chapters A-B] [--ep N]\n"
              "       novel episode list [--book B]\n\n"
              "  add   make the next episode folder from the extracted chapters\n"
              "  list  show the episodes of the book")
        return 0 if argv else 2
    sub, rest = argv[0], argv[1:]
    if sub == "add":
        return add(rest)
    if sub == "list":
        return list_cmd(rest)
    print(f"novel episode: unknown action '{sub}'. Use add or list.", file=sys.stderr)
    return 2
