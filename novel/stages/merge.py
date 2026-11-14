"""Merge a finished batch back into the master manifest.

Each Kaggle batch runs against a *slice* manifest holding only that batch's
chapters, renumbered from idx 0. That keeps the upload small, but it means
chapter 850 comes back as idx 0 with files named 00000.mp3 / 00000.txt. The
master manifest numbers the same chapter 48 (it starts at chapter 802).

Merging the slice back verbatim would therefore be silently destructive: the
master would point at 00000.mp3 for chapter 850, and the *next* batch would
write its own 00000.mp3 over it. So the merge renames files to their master
index as it goes, and matches chapters by `source` filename rather than by
idx - source is the only identifier stable across the two numbering schemes.

Usage:
    python -m abpipe.merge --batch <dir>            # dry run, prints the plan
    python -m abpipe.merge --batch <dir> --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def load_chapters(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["chapters"] if isinstance(data, dict) else data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default="workspace", help="root holding the 379-chapter manifest")
    ap.add_argument("--batch", required=True, help="root of the downloaded batch output")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    # The per-chapter mp3s and the .m4b are the SAME audio: downloading both
    # doubles the transfer for nothing. When only the .m4b was fetched, the
    # mp3s are absent and the normal "refusing to merge, file missing" guard
    # would block recording that the chapter is done - leaving the manifest
    # stale, which is how a range gets dispatched twice.
    #
    # Only the AUDIO pair is dropped. The hindi text and the timing map are
    # small and are merged either way: the timing map is what the visual layer
    # joins scenes to, so skipping it here would quietly cost every downstream
    # image its timestamp.
    ap.add_argument("--no-audio", action="store_true",
                    help="merge metadata only; per-chapter mp3s were not "
                         "downloaded (audio is in the .m4b)")
    args = ap.parse_args()

    master_root, batch_root = Path(args.master), Path(args.batch)
    master_path = master_root / "manifest.json"

    master_doc = json.loads(master_path.read_text(encoding="utf-8"))
    master = master_doc["chapters"] if isinstance(master_doc, dict) else master_doc
    batch = load_chapters(batch_root / "manifest.json")

    by_source = {c["source"]: c for c in master}

    planned, skipped = [], []
    for b in batch:
        src = b["source"]
        m = by_source.get(src)
        if m is None:
            skipped.append((src, "not in master manifest"))
            continue
        if b.get("status") != "done" or not b.get("audio"):
            skipped.append((src, f"status={b.get('status')}, no audio"))
            continue
        planned.append((b, m))

    print(f"batch: {len(batch)} chapters, {len(planned)} mergeable, {len(skipped)} skipped")
    for src, why in skipped:
        print(f"  skip {src}: {why}")
    print()

    for b, m in planned:
        mi = m["idx"]
        moves = []
        kinds = (("hindi", ".txt"), ("timing", ".json"))
        if not args.no_audio:
            kinds = (("audio", ".mp3"),) + kinds
        for kind, ext in kinds:
            name = b.get(kind)
            if not name:
                continue
            src_f = batch_root / kind / name
            dst_f = master_root / kind / f"{mi:05d}{ext}"
            moves.append((kind, src_f, dst_f))

        print(f"  {b['source']}  batch idx {b['idx']} -> master idx {mi}")
        for kind, src_f, dst_f in moves:
            exists = "ok" if src_f.exists() else "MISSING"
            clobber = "  (OVERWRITES EXISTING)" if dst_f.exists() else ""
            print(f"      {kind:5} {src_f.name} -> {dst_f.name}  [{exists}]{clobber}")

        if not args.apply:
            continue

        for kind, src_f, dst_f in moves:
            if not src_f.exists():
                raise SystemExit(f"refusing to merge: {src_f} is missing")
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_f, dst_f)

        m["status"] = b["status"]
        m["seconds"] = b.get("seconds")
        m["error"] = b.get("error")
        m["attempts"] = b.get("attempts", 0)
        # With --no-audio the mp3 was never fetched, so recording a filename
        # for it would be a lie: the manifest would name a file that is not on
        # disk. Leave it null and let the .m4b be the record of the audio.
        m["audio"] = None if args.no_audio else (
            f"{mi:05d}.mp3" if b.get("audio") else None)
        m["hindi"] = f"{mi:05d}.txt" if b.get("hindi") else None
        m["timing"] = f"{mi:05d}.json" if b.get("timing") else None
        # Per-stage cost is measured on Kaggle and exists nowhere else. Not
        # copying it here meant every batch's timing breakdown was discarded on
        # merge, which is the whole reason it was recorded.
        for k in ("translate_seconds", "tts_seconds", "rtf"):
            if b.get(k) is not None:
                m[k] = b[k]

    if not args.apply:
        print("\ndry run - nothing written. re-run with --apply")
        return

    # Write via a temp file and replace, so an interrupted write can't leave a
    # truncated master manifest behind - it is the only record of what is done.
    tmp = master_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(master_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(master_path)

    done = sum(1 for c in master if c.get("status") == "done")
    secs = sum(c.get("seconds") or 0 for c in master)
    print(f"\nmerged. master now {done}/{len(master)} done, {secs/3600:.1f} hrs audio")


if __name__ == "__main__":
    main()
