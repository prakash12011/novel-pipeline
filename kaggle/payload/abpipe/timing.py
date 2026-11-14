"""Per-chunk timing maps: when each paragraph is actually spoken.

The manifest records a chapter's total duration. That is enough to place
chapter markers in an .m4b, but not enough to put an image on screen at the
moment its paragraph is read - for that you need the boundary of every chunk
inside the chapter.

Two ways to get one, and they must be distinguishable:

  measured   Written by synth_chapter() from real sample counts, accurate to a
             sample. This is what every chapter narrated from now on gets.

  estimated  Reconstructed for chapters that were already narrated before this
             existed. Chunk durations are apportioned by character count out of
             the chapter's known total. Re-narrating 38 finished chapters to
             replace this would cost about 8 hours of T4 quota.

The estimate is anchored at both ends - it is scaled to a duration that is
already known, not accumulated forward - so there is no drift across the ~99
chunks in a chapter. It is still an estimate: characters are assumed to take
uniform time, which a chunk of dense punctuation or one long proper noun
violates locally. Hence `measured` in every file. A consumer that cannot tell
the two apart cannot know whether to expect +/-0.05s or +/-1s.

Measured error, 399 paragraph boundaries across 5 backfilled chapters, by
decoding the mp3 and asking where the real pauses are:

    68% of predicted boundaries already sit inside a real pause
    93% are within 0.5s of one
    99% are within 1.0s of one
    worst case 1.26s

Good enough to place a slide that stays up for 30-60s. Not good enough for
anything that needs to cut on a word.

Usage:
    python -m abpipe.timing --backfill                    # dry run
    python -m abpipe.timing --backfill --apply
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .manifest import Manifest, DONE
from .tts import SHORT_GAP_MS, chunk_hindi

VERSION = 1


def gap_seconds(ends_para: bool, cfg: dict) -> float:
    """Silence appended after a chunk. Must match synth_chapter's layout."""
    ms = cfg["paragraph_gap_ms"] if ends_para else SHORT_GAP_MS
    return ms / 1000.0


def write_map(dest: Path, chapter, sample_rate: int, chunks: list[dict],
              measured: bool) -> None:
    """Serialise one chapter's timing map.

    Shared by the live path and the backfill so the two cannot drift into
    writing subtly different shapes.
    """
    total = chunks[-1]["end_sec"] if chunks else 0.0
    payload = {
        "version": VERSION,
        "source": chapter.source,
        "measured": measured,
        "sample_rate": sample_rate,
        "total_seconds": round(chapter.seconds or total, 3),
        "chunks": chunks,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def estimate(hindi: str, seconds: float, cfg: dict) -> list[dict]:
    """Rebuild a timing map for audio that has already been rendered.

    The gaps are not estimated - their lengths are fixed by config and known
    exactly - so they are subtracted first and only the speech is apportioned.
    That also means the final end_sec plus its gap comes back to `seconds`
    exactly, rather than approximately.
    """
    chunks = chunk_hindi(hindi, cfg["chars_per_chunk"])
    if not chunks:
        return []

    gaps = [gap_seconds(c.ends_para, cfg) for c in chunks]
    speech = seconds - sum(gaps)
    total_chars = sum(len(c.text) for c in chunks)
    if speech <= 0 or total_chars == 0:
        # More silence than audio means the chapter's `seconds` and its Hindi
        # text disagree about what was narrated. Estimating from that would
        # produce negative durations that look like data.
        raise ValueError(
            f"cannot estimate: {seconds:.1f}s of audio but {sum(gaps):.1f}s of "
            f"gaps across {len(chunks)} chunks - the text and the audio do not "
            f"match"
        )

    out: list[dict] = []
    pos = 0.0
    for i, (c, gap) in enumerate(zip(chunks, gaps)):
        dur = speech * len(c.text) / total_chars
        out.append({
            "chunk": i,
            "para": c.para,
            "start_sec": round(pos, 3),
            "end_sec": round(pos + dur, 3),
            "chars": len(c.text),
        })
        pos += dur + gap
    return out


def backfill(root: Path, cfg: dict, apply: bool) -> None:
    man = Manifest.load(root / "manifest.json")
    done = [c for c in man.chapters
            if c.status == DONE and c.audio and c.seconds]
    if not done:
        raise SystemExit("no finished chapters with audio in the manifest")

    written = skipped = 0
    for ch in done:
        dest = root / "timing" / f"{ch.idx:05d}.json"
        if dest.exists():
            # Never overwrite. A measured map is strictly better than anything
            # this function can produce, and clobbering one silently downgrades
            # it to an estimate.
            existing = json.loads(dest.read_text(encoding="utf-8"))
            kind = "measured" if existing.get("measured") else "estimated"
            print(f"  skip  {ch.idx:>5}  {ch.source[:34]:34} ({kind} map exists)")
            skipped += 1
            continue

        hindi = (root / "hindi" / ch.hindi).read_text(encoding="utf-8")
        chunks = estimate(hindi, ch.seconds, cfg)
        rate = ch.seconds / max(1, sum(c["chars"] for c in chunks))
        print(f"  write {ch.idx:>5}  {ch.source[:34]:34} "
              f"{len(chunks):>3} chunks, {max(c['para'] for c in chunks) + 1:>3} paras, "
              f"{rate:.4f} s/char")
        if apply:
            write_map(dest, ch, cfg_sample_rate(cfg), chunks, measured=False)
            ch.timing = dest.name
        written += 1

    if apply:
        man.save()
        print(f"\nwrote {written} estimated maps, skipped {skipped}")
    else:
        print(f"\ndry run - nothing written. {written} would be written, "
              f"{skipped} skipped. re-run with --apply")


def cfg_sample_rate(cfg: dict) -> int:
    """The sample rate the audio was rendered at, per the active TTS backend."""
    return cfg[cfg["backend"]]["sample_rate"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--root", default=None, help="default: paths.root from config")
    ap.add_argument("--backfill", action="store_true",
                    help="estimate maps for chapters narrated before timing existed")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    root = Path(args.root) if args.root else Path(cfg["paths"]["root"])

    if not args.backfill:
        ap.error("nothing to do - pass --backfill")
    backfill(root, cfg["tts"], args.apply)


if __name__ == "__main__":
    main()
