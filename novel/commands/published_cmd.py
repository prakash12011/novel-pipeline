"""novel published [--book B] [--ep N] --url URL

Sets `published: true` and `published_url: <url>` in the episode's episode.yaml, editing the
text line by line so every other line and comment stays as it is.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import yaml

from novel.core import context, paths

YOUTUBE_URL = re.compile(
    r"^https?://(?:(?:www|m|music)\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)[A-Za-z0-9_-]{11}(?:[&?#]\S*)?"
    r"|youtu\.be/[A-Za-z0-9_-]{11}(?:[?#]\S*)?)$")

_KEY = {"published": re.compile(r"^(published[ \t]*:[ \t]*)(.*?)([ \t]+#.*)?$"),
        "published_url": re.compile(r"^(published_url[ \t]*:[ \t]*)(.*?)([ \t]+#.*)?$")}


def is_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_URL.match(url.strip()))


def set_keys(text: str, values: dict) -> str:
    """Set top-level keys line-wise: replace a key's value (keeping a trailing comment), add
    the key at the end when absent. Line endings of the file are kept."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    done = set()
    for i, raw in enumerate(lines):
        body = raw.rstrip("\r\n")
        for key, val in values.items():
            m = _KEY[key].match(body) if key not in done else None
            if m:
                lines[i] = f"{m.group(1)}{val}{m.group(3) or ''}{raw[len(body):]}"
                done.add(key)
                break
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    for key, val in values.items():
        if key not in done:
            lines.append(f"{key}: {val}{nl}")
    return "".join(lines)


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel published")
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--url", required=True)
    a = ap.parse_args(argv)

    url = a.url.strip()
    if not is_youtube_url(url):
        print(f"'{a.url}' does not look like a YouTube video link "
              f"(https://youtu.be/ID or https://www.youtube.com/watch?v=ID).", file=sys.stderr)
        return 1
    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        print("No episode. Pass --ep N, or pick one with: novel use <book> --ep N", file=sys.stderr)
        return 1
    ep_dir = paths.ep_root(book, ep)
    if not ep_dir.is_dir():
        print(f"episode folder not found: {ep_dir}", file=sys.stderr)
        return 1
    f = ep_dir / "episode.yaml"
    old = f.read_bytes().decode("utf-8") if f.is_file() else ""
    new = set_keys(old, {"published": "true", "published_url": json.dumps(url)})
    try:
        data = yaml.safe_load(new) or {}
        assert data.get("published") is True and data.get("published_url") == url
    except Exception:
        print(f"refusing to write {f}: the result would not be valid YAML (file untouched)", file=sys.stderr)
        return 1
    with open(f, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
    print(f"{context.header(book, ep)} marked as published: {url}")
    print(f"Next episode: novel episode add --book {book}")
    return 0
