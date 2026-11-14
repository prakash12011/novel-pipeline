"""Daily batch runner.

Usage:
    python -m abpipe.runner --config config.yaml --n 40
    python -m abpipe.runner --config config.yaml --init chapters/

Models load once and are reused across the whole batch. Loading sarvam-translate
and IndicF5 costs a few minutes each; doing it per chapter would waste most of
a Kaggle session.
"""

from __future__ import annotations

import argparse
import time
import traceback
from pathlib import Path

import yaml

from . import timing as TM
from . import translate as T
from . import tts as S
from .manifest import Manifest, build_from_dir, PENDING, TRANSLATED, DONE, FAILED


def run_batch(cfg: dict, n: int) -> None:
    root = Path(cfg["paths"]["root"])
    man = Manifest.load(root / "manifest.json")
    if not man.chapters:
        raise SystemExit("empty manifest - run with --init chapters/ first")

    todo = man.claim(
        n,
        worker=cfg["run"].get("worker"),
        workers_total=cfg["run"].get("workers_total", 1),
    )
    if not todo:
        print("nothing left to do")
        return

    print(f"{man.done_count()}/{len(man.chapters)} done. "
          f"this batch: {len(todo)} chapters")

    glossary = T.load_glossary(cfg["paths"]["glossary"])

    # Translation for the whole batch first, then TTS. Keeping only one model
    # resident at a time matters on a 16GB Kaggle GPU.
    needs_translation = [c for c in todo if c.status in (PENDING, FAILED)]
    if needs_translation:
        backend = T.make_backend(cfg["translate"])
        group_size = max(1, int(cfg["translate"].get("batch_chapters", 1)))

        # Chapters are translated in groups so their blocks batch together on
        # the GPU. The manifest is still saved per chapter, so a crash costs
        # at most the group in flight, not the session.
        for start in range(0, len(needs_translation), group_size):
            group = needs_translation[start:start + group_size]
            t0 = time.monotonic()
            try:
                srcs = [(root / "chapters" / c.source).read_text(encoding="utf-8")
                        for c in group]
                hindis = (T.translate_chapters_batched(srcs, backend, glossary,
                                                       cfg["translate"])
                          if group_size > 1 else
                          [T.translate_chapter(srcs[0], backend, glossary,
                                               cfg["translate"])])
            except Exception:
                # One bad group must not sink the rest of the batch.
                for ch in group:
                    ch.status = FAILED
                    ch.attempts += 1
                    ch.error = traceback.format_exc(limit=2)
                    print(f"  FAILED translate {ch.idx}")
                man.save()
                continue

            # Cost is measured per group (blocks batch across chapters, so a
            # single chapter's share is not separable) and divided evenly.
            per_chapter = (time.monotonic() - t0) / max(1, len(group))
            for ch, hindi in zip(group, hindis):
                out = root / "hindi" / f"{ch.idx:05d}.txt"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(hindi, encoding="utf-8")
                ch.hindi = out.name
                ch.status = TRANSLATED
                ch.translate_seconds = round(per_chapter, 1)
                ch.error = None
                print(f"  translated {ch.idx:>5}  {ch.title[:50]}"
                      f"  [{per_chapter/60:.1f} min]")
            man.save()          # after every group, not every batch
        del backend

    needs_audio = [c for c in todo if c.status == TRANSLATED]
    if needs_audio:
        backend = S.make_backend(cfg["tts"])
        consecutive_failures = 0
        max_consecutive = int(cfg["run"].get("max_consecutive_failures", 2))
        for ch in needs_audio:
            try:
                hindi = (root / "hindi" / ch.hindi).read_text(encoding="utf-8")
                t0 = time.monotonic()
                audio, sr, chunks = S.synth_chapter(hindi, backend, cfg["tts"])
                tts_secs = time.monotonic() - t0
                dest = root / "audio" / f"{ch.idx:05d}.mp3"
                secs = S.write_mp3(
                    audio, sr, dest, ch.title,
                    bitrate=cfg["output"]["bitrate"],
                    channels=cfg["output"]["channels"],
                )
                ch.audio = dest.name
                ch.seconds = secs

                tmap = root / "timing" / f"{ch.idx:05d}.json"
                TM.write_map(tmap, ch, sr, chunks, measured=True)
                ch.timing = tmap.name
                ch.tts_seconds = round(tts_secs, 1)
                # Real-time factor: compute seconds per second of audio. This
                # is the number to watch when tuning; everything else is
                # downstream of it.
                ch.rtf = round(tts_secs / secs, 2) if secs else None
                ch.status = DONE
                ch.error = None
                print(f"  spoke      {ch.idx:>5}  {secs/60:5.1f} min audio  "
                      f"in {tts_secs/60:5.1f} min  RTF {ch.rtf}")
                consecutive_failures = 0
            except Exception:
                ch.status = FAILED
                ch.attempts += 1
                ch.error = traceback.format_exc(limit=2)
                consecutive_failures += 1
                print(f"  FAILED tts {ch.idx}  "
                      f"({consecutive_failures} in a row)")
            man.save()

            # Isolating failures per chapter is right for a one-off bad
            # chapter, but with no cap a systemic fault - a broken model, an
            # unusable GPU, a bad config - burns the entire batch before
            # anyone sees it. Two in a row is not bad luck; stop and keep the
            # remaining GPU time. Everything already finished is saved.
            if consecutive_failures >= max_consecutive:
                print(f"\nABORTING: {consecutive_failures} consecutive TTS "
                      f"failures. Something is systematically wrong - fix it "
                      f"rather than spending the rest of the session on it.\n"
                      f"Last error:\n{ch.error or '(none)'}")
                break

    hrs = man.total_seconds() / 3600
    print(f"\n{man.done_count()}/{len(man.chapters)} chapters, {hrs:.1f} hrs audio")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--init", metavar="CHAPTERS_DIR")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    root = Path(cfg["paths"]["root"])

    if args.init:
        m = build_from_dir(args.init, root / "manifest.json")
        print(f"manifest built: {len(m.chapters)} chapters")
        return

    run_batch(cfg, args.n or cfg["run"]["chapters_per_batch"])


if __name__ == "__main__":
    main()
