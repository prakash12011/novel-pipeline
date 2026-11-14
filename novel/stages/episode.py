"""Episodes: a run of chapters turned into one video, kept apart from the audiobook.

An episode lives entirely under workspace/episodes/E001/ and never touches the
master manifest, the ledger, workspace/hindi or workspace/timing. Its folder has
the same shape as the audiobook root (manifest.json, chapters/, hindi/, audio/,
timing/), so tools that take a --root work on it unchanged.

Unlike the master manifest, idx IS the chapter number here: chapter 1 is
hindi/00001.txt, not 00000. Episodes start at chapter 1, so the master
convention (idx = chapter - 802) would make no sense.

Usage:
    python -m abpipe.episode init --episode 1          # chapters 1-10; --start/--end override
    python -m abpipe.episode status --episode 1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .manifest import Chapter, Manifest

EPISODES = Path("workspace/episodes")
CHAPTERS_PER_EPISODE = 10      # episode N = chapters 10(N-1)+1 .. 10N (a YouTube video each)


def chapter_range(episode: int, size: int = CHAPTERS_PER_EPISODE) -> tuple[int, int]:
    return size * (episode - 1) + 1, size * episode


EXTRAS_FROM = 900              # numbers from here on are extras (E900 = the subscribe message)


def root(episode: int) -> Path:
    """Episode folder. Numbers >= 900 are extras: <book>/extras/E900, not under episodes/."""
    if int(episode) >= EXTRAS_FROM:
        return EPISODES.parent / "extras" / f"E{int(episode):03d}"
    return EPISODES / f"E{episode:03d}"


def cfg(ep: int | None = None, ch: int | None = None) -> dict:
    """Effective settings for the active book (and an episode / chapter)."""
    from novel.core import context, settings
    return settings.effective(context.active_book(), ep, ch)


def series_label() -> str:
    """Series name on title cards and in metadata: book.series_label, else book.title."""
    b = cfg().get("book") or {}
    return b.get("series_label") or b.get("title") or ""


def info(ep_root: Path) -> dict:
    return json.loads((ep_root / "episode.json").read_text(encoding="utf-8"))


def chapter_no(source: str) -> int:
    return int(source.split("_", 1)[0])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lock_path(ep_root: Path) -> Path:
    return ep_root / "hindi.lock.json"


def check_lock(ep_root: Path, idx: int) -> None:
    """Refuse to work from a Hindi file that changed after it was locked.

    Scenes point at paragraph numbers, and audio and timing are made from the
    exact text. An edit after the lock silently shifts every one of them.
    """
    lock = json.loads(lock_path(ep_root).read_text(encoding="utf-8")) \
        if lock_path(ep_root).exists() else {}
    name = f"{idx:05d}.txt"
    want = lock.get(name)
    if want is None:
        raise SystemExit(f"hindi/{name} is not locked - run "
                         f"`novel approve --chapters {idx}` first")
    if sha256(ep_root / "hindi" / name) != want:
        raise SystemExit(f"hindi/{name} changed after it was locked")


def create_episode(ep_root: Path, episode: int, start: int, end: int, chapters_dir: Path) -> int:
    """Copy chapters start..end from chapters_dir into ep_root/chapters and write
    manifest.json and episode.json (flow_project, voice, translate_model from settings)."""
    ep_root = Path(ep_root)
    if (ep_root / "manifest.json").exists():
        sys.exit(f"{ep_root} already exists - refusing to overwrite it")
    src = sorted(f for f in Path(chapters_dir).glob("*.txt") if start <= chapter_no(f.name) <= end)
    if len(src) != end - start + 1:
        sys.exit(f"expected {end - start + 1} chapters ({start}-{end}) in {chapters_dir}, "
                 f"found {len(src)}")
    (ep_root / "chapters").mkdir(parents=True, exist_ok=True)
    chapters = []
    for f in src:
        shutil.copyfile(f, ep_root / "chapters" / f.name)
        first = f.read_text(encoding="utf-8").split("\n", 1)[0].strip()
        chapters.append(Chapter(idx=chapter_no(f.name), title=first[:120], source=f.name))
    Manifest(path=ep_root / "manifest.json", chapters=chapters).save()
    c = cfg()
    (ep_root / "episode.json").write_text(json.dumps({
        "episode": episode, "start": start, "end": end,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "flow_project": (c.get("images") or {}).get("flow_project"),
        "voice": (c.get("tts") or {}).get("voice"),
        "translate_model": (c.get("gemini") or {}).get("api_model"),
    }, indent=2), encoding="utf-8")
    return len(chapters)


def cmd_init(args) -> None:
    lo, hi = chapter_range(args.episode)
    args.start = lo if args.start is None else args.start
    args.end = hi if args.end is None else args.end
    ep_root = root(args.episode)
    chapters_dir = Path(args.chapters_dir) if args.chapters_dir else \
        EPISODES.parent / "chapters"
    n = create_episode(ep_root, args.episode, args.start, args.end, chapters_dir)
    print(f"\nepisode {args.episode}: {n} chapters in {ep_root}")


def cmd_status(args) -> None:
    ep_root = root(args.episode)
    man = Manifest.load(ep_root / "manifest.json")
    lock = json.loads(lock_path(ep_root).read_text(encoding="utf-8")) \
        if lock_path(ep_root).exists() else {}
    print(f"{'ch':>4}  {'hindi':6} {'qa':4} {'lock':5} {'scenes':7} {'images':7} "
          f"{'audio':6} {'video':6} title")
    for ch in man.chapters:
        n = ch.idx
        name = f"{n:05d}"
        imgs = len(list((ep_root / "images" / f"c{n:04d}").glob("s*-2k.jpg")))
        mark = lambda p: "yes" if p.exists() else "-"
        print(f"{n:>4}  {mark(ep_root / 'hindi' / f'{name}.txt'):6} "
              f"{mark(ep_root / 'qa' / f'{name}.json'):4} "
              f"{'yes' if f'{name}.txt' in lock else '-':5} "
              f"{mark(ep_root / 'scenes' / f'{name}.json'):7} {imgs:>3}/22   "
              f"{mark(ep_root / 'audio' / f'{name}.mp3'):6} "
              f"{mark(ep_root / 'video' / f'c{n:04d}.mp4'):6} {ch.title[:40]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--episode", type=int, required=True)
    p.add_argument("--start", type=int, default=None, help="default: first chapter of this episode")
    p.add_argument("--end", type=int, default=None, help="default: last chapter of this episode")
    p.add_argument("--chapters-dir", default=None, help="default: <book>/chapters")
    p.set_defaults(fn=cmd_init)
    s = sub.add_parser("status")
    s.add_argument("--episode", type=int, required=True)
    s.set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
