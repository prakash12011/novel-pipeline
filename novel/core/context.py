"""The current book and episode (state.json) and how the copied modules find them."""

from __future__ import annotations

import json
from pathlib import Path

from . import paths

_ACTIVE_BOOK: str | None = None


def read_state() -> dict:
    f = paths.state_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8")) or {}
    except ValueError:
        return {}


def write_state(book: str, episode=None) -> None:
    f = paths.state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    state = {"book": book}
    if episode is not None:
        state["episode"] = int(episode)
    f.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def resolve(book=None, ep=None):
    """(book, episode) from arguments, else from state.json. The episode may be None;
    the book may not (clear error when neither the arguments nor state give one)."""
    st = read_state()
    book = book or st.get("book")
    if not book:
        raise SystemExit("no current book. Pick one with: novel use <book> [--ep N] "
                         "(or pass --book)")
    if ep is None and st.get("book") == book:
        ep = st.get("episode")
    return book, (int(ep) if ep is not None else None)


def active_book() -> str:
    """The book activate() was called for, else the one in state.json."""
    return _ACTIVE_BOOK or resolve()[0]


def activate(book: str) -> Path:
    """Point the copied modules (novel.stages.episode.EPISODES) at this book's data."""
    global _ACTIVE_BOOK
    from novel.stages import episode

    episode.EPISODES = paths.episodes_dir(book)
    _ACTIVE_BOOK = book
    return episode.EPISODES


def header(book: str, ep=None) -> str:
    if ep is None:
        return f"[{book}]"
    parts = [book, f"E{int(ep):03d}"]
    f = paths.ep_root(book, ep) / "episode.json"
    if f.exists():
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
            if "start" in info and "end" in info:
                parts.append(f"ch {info['start']}-{info['end']}")
        except ValueError:
            pass
    return "[" + " · ".join(parts) + "]"
