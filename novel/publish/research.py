"""YouTube search-suggestion research for a book.

Collects autocomplete phrases for a set of seeds (each seed plus "seed a" ... "seed z"),
ranks them by how many queries returned them, and optionally adds the top YouTube
videos for the first seeds when YOUTUBE_API_KEY is set.

Every network call goes through an `opener`: a function taking a URL and returning
the reply as bytes (or str). Tests pass a fake; the default uses urllib.
"""

from __future__ import annotations

import json
import string
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

SUGGEST_URL = ("https://suggestqueries.google.com/complete/search"
               "?client=firefox&ds=yt&hl={hl}&gl={gl}&q={q}")
API_URL = ("https://www.googleapis.com/youtube/v3/search?part=snippet&type=video"
           "&maxResults=10&order=viewCount&regionCode=IN&relevanceLanguage=hi"
           "&q={q}&key={key}")
TIMEOUT_SECONDS = 10
PAUSE_SECONDS = 0.3
STOP_AFTER_EMPTY = 5
TOP_API_SEEDS = 2
FILE_PREFIX = "autocomplete-"
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _default_opener(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return resp.read()


def _decode(raw) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def norm(text) -> str:
    """Case-insensitive, trimmed, single-spaced form used for counting."""
    return " ".join(str(text).split()).lower()


def fetch_suggestions(query, hl="hi", gl="IN", opener=None) -> list[str]:
    """YouTube autocomplete suggestions for one query. Any error gives []."""
    url = SUGGEST_URL.format(hl=hl, gl=gl, q=quote_plus(query))
    try:
        data = json.loads(_decode((opener or _default_opener)(url)))
        items = data[1]
        return [s for s in items if isinstance(s, str) and s.strip()]
    except Exception:
        return []


def build_queries(seed: str) -> list[str]:
    return [seed] + [f"{seed} {letter}" for letter in string.ascii_lowercase]


def collect(seeds, opener=None, pause=PAUSE_SECONDS) -> dict:
    """Run every query for every seed and rank the phrases.

    Stops early after STOP_AFTER_EMPTY requests in a row return nothing.
    """
    found: dict[str, dict] = {}
    jobs = [(seed, q) for seed in seeds for q in build_queries(seed)]
    made = empty = empty_run = 0
    for i, (seed, query) in enumerate(jobs):
        if i and pause > 0:
            time.sleep(pause)
        hits = {norm(s) for s in fetch_suggestions(query, opener=opener)}
        hits.discard("")
        made += 1
        if not hits:
            empty += 1
            empty_run += 1
            if empty_run >= STOP_AFTER_EMPTY:
                break
            continue
        empty_run = 0
        for text in hits:
            entry = found.setdefault(text, {"queries": set(), "seeds": set()})
            entry["queries"].add(query)
            entry["seeds"].add(seed)
    phrases = [{"text": text, "count": len(e["queries"]), "seeds": sorted(e["seeds"])}
               for text, e in found.items()]
    phrases.sort(key=lambda p: (-p["count"], p["text"]))
    return {"queries": made, "empty_queries": empty, "phrases": phrases}


def list_cache(research_dir: Path) -> list[Path]:
    """Cache files, oldest first (the names carry YYYYMMDD)."""
    if not research_dir.is_dir():
        return []
    return sorted(p for p in research_dir.glob(FILE_PREFIX + "*.json") if p.is_file())


def newest_cache(research_dir: Path) -> Path | None:
    files = list_cache(research_dir)
    return files[-1] if files else None


def load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def age_days(path: Path, now: datetime | None = None) -> float:
    """Age from fetched_at inside the file, else from its modification time."""
    now = now or datetime.now(timezone.utc)
    try:
        raw = load_cache(path).get("fetched_at")
        stamp = datetime.strptime(raw, TIME_FMT).replace(tzinfo=timezone.utc)
    except Exception:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return (now - stamp).total_seconds() / 86400


def save(research_dir: Path, data: dict, now: datetime) -> Path:
    research_dir.mkdir(parents=True, exist_ok=True)
    path = research_dir / f"{FILE_PREFIX}{now:%Y%m%d}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _reason(exc: Exception) -> str:
    """Short error text that never contains the request URL or the key."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return type(exc).__name__


def fetch_top_videos(seed: str, key: str, opener=None) -> list[dict]:
    url = API_URL.format(q=quote_plus(seed), key=quote_plus(key))
    data = json.loads(_decode((opener or _default_opener)(url)))
    videos = []
    for item in data.get("items") or []:
        snip = item.get("snippet") or {}
        videos.append({
            "seed": seed,
            "title": snip.get("title", ""),
            "channel": snip.get("channelTitle", ""),
            "video_id": (item.get("id") or {}).get("videoId", ""),
        })
    return videos


def top_videos(seeds, key: str, opener=None) -> tuple[list[dict], str | None]:
    """Top videos for the first TOP_API_SEEDS seeds. Returns (videos, error or None)."""
    videos: list[dict] = []
    reasons: list[str] = []
    for seed in list(seeds)[:TOP_API_SEEDS]:
        try:
            videos.extend(fetch_top_videos(seed, key, opener))
        except Exception as exc:
            reason = _reason(exc)
            if reason not in reasons:
                reasons.append(reason)
    return videos, ("; ".join(reasons) or None)
