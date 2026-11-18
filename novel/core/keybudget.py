"""The Gemini free tier, counted per key: at most `per_minute` calls in any 60 seconds and `per_day` calls per Pacific
day (Google resets the daily quota at midnight Pacific). Every call of every command is counted in one JSON file,
<novel home>/gemini_usage.json, so separate runs share the budget. Keys are stored as a short hash, never in full.

Before a call the client asks take(): a key with room (the current one first), else how long until the per-minute
window frees one, else None: every key has spent its day, and the client falls back to OpenRouter.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
FILE = "gemini_usage.json"
_LOCK = threading.Lock()        # shortcut: threads of one process only; two commands at once may each pass a full key


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class KeyBudget:
    def __init__(self, path: Path, keys: list[str], model: str, per_minute: int = 5, per_day: int = 20,
                 now=time.time):
        self.path, self.keys, self.model = Path(path), keys, model
        self.per_minute, self.per_day, self.now = per_minute, per_day, now

    def _day(self, t: float) -> str:
        return datetime.fromtimestamp(t, PACIFIC).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _row(self, data: dict, i: int, t: float) -> dict:
        """This key's entry for this model, reset when the Pacific day changed, with calls older than 60 s dropped."""
        row = data.setdefault(f"{key_id(self.keys[i])}:{self.model}", {})
        if row.get("day") != self._day(t):
            row.clear()
            row.update(day=self._day(t), count=0, recent=[], hold_until=0)
        row["recent"] = [x for x in row["recent"] if t - x < 60]
        return row

    def take(self, prefer: int = 0) -> int | float | None:
        """Reserve one call: a key index, else seconds to wait (a key still has calls today), else None."""
        with _LOCK:
            t, data = self.now(), self._load()
            waits = []
            for i in [(prefer + j) % len(self.keys) for j in range(len(self.keys))]:
                row = self._row(data, i, t)
                if row["count"] >= self.per_day:
                    continue
                if row["hold_until"] > t:
                    waits.append(row["hold_until"] - t)
                elif len(row["recent"]) >= self.per_minute:
                    waits.append(60 - (t - row["recent"][0]))
                else:
                    row["count"] += 1
                    row["recent"].append(t)
                    self._save(data)
                    return i
            self._save(data)
            return round(min(waits), 1) + 0.5 if waits else None

    def limited(self, i: int, whole_day: bool = False) -> None:
        """Google said 429 for this key: hold it a minute, or for the rest of the day when the daily quota is gone."""
        with _LOCK:
            t, data = self.now(), self._load()
            row = self._row(data, i, t)
            if whole_day:
                row["count"] = self.per_day
            else:
                row["hold_until"] = t + 60
            self._save(data)

    def status(self) -> list[tuple[int, int, int]]:
        """(key position 1.., calls today, calls in the last minute) per key."""
        with _LOCK:
            t, data = self.now(), self._load()
            return [(i + 1, self._row(data, i, t)["count"], len(self._row(data, i, t)["recent"]))
                    for i in range(len(self.keys))]
