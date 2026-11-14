"""novel research [--book B] [--refresh] [--seed "text"]... [--max-age-days N]"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from novel.core import context, paths, secrets, settings
from novel.publish import research


def _clean(items) -> list[str]:
    if isinstance(items, str):
        items = [items]
    out = []
    for s in items or []:
        if isinstance(s, str) and s.strip():
            out.append(s.strip())
    return out


def main(argv, *, opener=None, pause=research.PAUSE_SECONDS) -> int:
    ap = argparse.ArgumentParser(prog="novel research")
    ap.add_argument("--book")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--seed", action="append", default=[])
    ap.add_argument("--max-age-days", type=float, default=7)
    a = ap.parse_args(argv)

    book, _ = context.resolve(a.book)
    bdir = paths.book_dir(book)
    if not bdir.is_dir():
        print(f"book '{book}' not found in {bdir.parent}.")
        return 1

    seeds = _clean(a.seed) or _clean(settings.get("publish.research_seeds", book=book, default=[]))
    if not seeds:
        print('No research seeds. Add publish.research_seeds to book.yaml or pass --seed "..."')
        return 1

    rdir = paths.research_dir(book)
    cached = research.newest_cache(rdir)
    if cached is not None and not a.refresh and research.age_days(cached) < a.max_age_days:
        n = len(research.load_cache(cached).get("phrases") or [])
        print(f"Using cached research from {cached} ({n} phrases)")
        return 0

    now = datetime.now(timezone.utc)
    got = research.collect(seeds, opener=opener, pause=pause)
    phrases = got["phrases"]
    if not phrases:
        if cached is not None:
            print(f"Offline or blocked: using older research from {cached}")
            return 0
        print("No research available (offline?)")
        return 1

    data = {
        "book": book,
        "fetched_at": now.strftime(research.TIME_FMT),
        "seeds": seeds,
        "queries": got["queries"],
        "empty_queries": got["empty_queries"],
        "phrases": phrases,
    }

    key = secrets.load().get("YOUTUBE_API_KEY", "")
    if key:
        videos, err = research.top_videos(seeds, key, opener=opener)
        data["top_videos"] = videos
        if err:
            data["top_videos_error"] = err

    path = research.save(rdir, data, now)
    print(f"Saved {path}")
    print(f"Phrases: {len(phrases)} (queries {got['queries']}, empty {got['empty_queries']})")
    if key:
        if err:
            print(f"YouTube top videos: {len(videos)} saved, error: {err}")
        else:
            print(f"YouTube top videos: {len(videos)} saved")
    print("Top 15:")
    for p in phrases[:15]:
        print(f"  {p['count']:>3}  {p['text']}")
    return 0
