"""Chapter state tracking.

The manifest is the only thing that survives a crashed session, so it is
written after every stage rather than every chapter. If TTS dies on chapter 7,
that chapter's translation is already on disk and is not redone.

Writes are atomic (temp file + os.replace) because a session killed mid-write
would otherwise leave you with a truncated manifest and no way to resume.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterator

PENDING = "pending"
TRANSLATED = "translated"
DONE = "done"
FAILED = "failed"


@dataclass
class Chapter:
    idx: int
    title: str
    source: str          # relative path to English text
    status: str = PENDING
    hindi: str | None = None      # relative path to Hindi text
    audio: str | None = None      # relative path to mp3
    timing: str | None = None     # relative path to the per-chunk timing map
    seconds: float | None = None
    error: str | None = None
    attempts: int = 0
    # Per-stage cost. Recorded permanently, not as diagnostic scaffolding:
    # without it a slow batch only tells you the combined per-chapter time,
    # and which of translation or TTS ate the hour is left to guesswork.
    # rtf = tts_seconds / seconds, the standard real-time factor - 1.0 means
    # synthesis takes exactly as long as the audio it produces.
    # All default to None so manifests written before this existed still load.
    translate_seconds: float | None = None
    tts_seconds: float | None = None
    rtf: float | None = None


@dataclass
class Manifest:
    path: Path
    chapters: list[Chapter] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        path = Path(path)
        if not path.exists():
            return cls(path=path, chapters=[])
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(path=path, chapters=[Chapter(**c) for c in raw["chapters"]])

    def save(self) -> None:
        payload = {"version": 1, "chapters": [asdict(c) for c in self.chapters]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def claim(
        self,
        n: int,
        worker: str | None = None,
        workers_total: int = 1,
        max_attempts: int = 3,
    ) -> list[Chapter]:
        """Return the next n chapters needing work, in reading order.

        Partitioning is by stride rather than by a lock. Google Drive is not
        transactional, so two workers writing one manifest will clobber each
        other. Stride assignment means each worker touches a disjoint set and
        no coordination is needed. With one machine, leave worker as None.
        """
        out: list[Chapter] = []
        for i, ch in enumerate(self.chapters):
            if len(out) >= n:
                break
            if ch.status == DONE:
                continue
            if ch.status == FAILED and ch.attempts >= max_attempts:
                continue
            if worker is not None and workers_total > 1:
                if i % workers_total != _worker_slot(worker, workers_total):
                    continue
            out.append(ch)
        return out

    def pending_count(self) -> int:
        return sum(1 for c in self.chapters if c.status != DONE)

    def done_count(self) -> int:
        return sum(1 for c in self.chapters if c.status == DONE)

    def total_seconds(self) -> float:
        return sum(c.seconds or 0.0 for c in self.chapters)

    def __iter__(self) -> Iterator[Chapter]:
        return iter(self.chapters)


def _worker_slot(worker: str, total: int) -> int:
    return sum(ord(c) for c in worker) % total


def build_from_dir(chapters_dir: str | Path, manifest_path: str | Path) -> Manifest:
    """Create a manifest from a directory of numbered .txt chapter files.

    Expects filenames that sort correctly and start with the chapter number,
    e.g. 0821_infatuation.txt. Zero-pad them or the ordering will be wrong.
    """
    chapters_dir = Path(chapters_dir)
    files = sorted(chapters_dir.glob("*.txt"))
    if not files:
        raise SystemExit(f"no .txt files found in {chapters_dir}")

    chapters = []
    for i, f in enumerate(files):
        first = f.read_text(encoding="utf-8").lstrip().split("\n", 1)[0].strip()
        chapters.append(
            Chapter(idx=i, title=first[:120] or f.stem, source=f.name)
        )

    m = Manifest(path=Path(manifest_path), chapters=chapters)
    m.save()
    return m
