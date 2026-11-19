"""Take the spoken chapter heading ("चैप्टर एक: फॉरेनर्स") out of chapters that already have one.

Per chapter, when hindi/NNNNN.txt opens with a heading line:
  hindi     the heading paragraph is removed; hindi.lock.json and every JSON that held the old sha256 are updated
  audio     when narrated: the mp3 is cut where the story starts (a little before the first story chunk), no
            new Kaggle run; timing/NNNNN.json drops the heading chunk and moves the rest up
  captions  the heading line is dropped, the rest moves up (times in the old format, chunk numbers in the new)
  scenes    paragraph numbers move up by one
Every file it changes is first copied to old/heading/ (never overwritten there). Chapters without a heading are
left alone, so running it twice changes nothing.

    novel strip-heading --ep 1 [--chapters 1-10] [--dry-run]
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import episode as E
from .heading import body_start
from .scenebuild import paragraphs

MARGIN = 0.15          # seconds of the silence before the story that stay in the cut mp3


def _backup(ep_root: Path, f: Path) -> None:
    dest = ep_root / "old" / "heading" / f.relative_to(ep_root)
    if f.exists() and not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)


def _duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(p)],
                         capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def _cut_mp3(src: Path, dest: Path, at: float, bitrate: str) -> None:
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ss", f"{at:.3f}",
                        "-c:a", "libmp3lame", "-b:a", bitrate, str(dest)], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"ffmpeg could not cut {src.name}: {p.stderr[-400:]}")


def strip_chapter(ep_root: Path, n: int, dry_run: bool = False) -> str:
    name = f"{n:05d}"
    hf = ep_root / "hindi" / f"{name}.txt"
    raw = hf.read_bytes().decode("utf-8")
    paras = paragraphs(raw)
    if not body_start(paras):
        return "no heading"
    E.check_lock(ep_root, n)                           # only a locked, unchanged text is edited
    nl = "\r\n" if "\r\n" in raw else "\n"
    new_text = (nl + nl).join(paras[1:]) + nl
    mp3, tf = ep_root / "audio" / f"{name}.mp3", ep_root / "timing" / f"{name}.json"
    cf, sf = ep_root / "captions" / f"{name}.json", ep_root / "scenes" / f"{name}.json"
    cut = None
    if mp3.exists() and tf.exists():
        timing = json.loads(tf.read_text(encoding="utf-8"))
        head = [c for c in timing["chunks"] if c["para"] == 0]
        story = [c for c in timing["chunks"] if c["para"] >= 1]
        cut = max(max(c["end_sec"] for c in head) if head else 0.0, story[0]["start_sec"] - MARGIN)
    what = f"heading '{paras[0]}' removed" + (f", audio cut at {cut:.2f} s" if cut is not None else ", no audio yet")
    if dry_run:
        return what + " (dry run)"
    old_sha = E.sha256(hf)
    for f in (hf, mp3, tf, cf, sf, E.lock_path(ep_root)):
        _backup(ep_root, f)
    # text
    tmp = hf.with_suffix(".tmp")
    tmp.write_bytes(new_text.encode("utf-8"))
    tmp.replace(hf)
    new_sha = E.sha256(hf)
    # audio and timing
    shift = 0.0
    removed_chunks = 0
    if cut is not None:
        bitrate = str((E.cfg(E.info(ep_root)["episode"]).get("tts") or {}).get("bitrate") or "128k")
        out = mp3.with_suffix(".cut.mp3")
        _cut_mp3(ep_root / "old" / "heading" / mp3.relative_to(ep_root), out, cut, bitrate)
        out.replace(mp3)
        shift = cut
        removed_chunks = sum(1 for c in timing["chunks"] if c["para"] == 0)
        chunks = []
        for c in timing["chunks"]:
            if c["para"] == 0:
                continue
            c = dict(c, para=c["para"] - 1, chunk=c["chunk"] - removed_chunks,
                     start_sec=round(max(0.0, c["start_sec"] - shift), 3),
                     end_sec=round(max(0.0, c["end_sec"] - shift), 3))
            chunks.append(c)
        timing["chunks"] = chunks
        timing["total_seconds"] = round(_duration(mp3), 3)
        timing["heading_removed"] = {"cut_at": round(cut, 3)}
        tf.write_text(json.dumps(timing, ensure_ascii=False, indent=1), encoding="utf-8")
    # captions
    if cf.exists():
        doc = json.loads(cf.read_text(encoding="utf-8"))
        heads = sum(1 for r in doc.get("lines", []) if r.get("para") == 0)
        lines = []
        for r in doc.get("lines", []):
            if r.get("para") == 0:
                continue
            r = dict(r, para=r["para"] - 1)
            for k in ("start", "end"):
                if k in r:
                    r[k] = round(max(0.0, r[k] - shift), 3)
            if "chunk" in r:
                r["chunk"] = r["chunk"] - (removed_chunks or heads)
            lines.append(r)
        doc["lines"] = lines
        if "chunks" in doc and isinstance(doc["chunks"], int):
            doc["chunks"] -= removed_chunks or heads
        cf.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    # scenes
    if sf.exists():
        doc = json.loads(sf.read_text(encoding="utf-8"))
        for s in doc.get("scenes", []):
            if isinstance(s.get("para"), int):
                s["para"] = max(0, s["para"] - 1)
        sf.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    # every stored hash of the old text (lock, scenes, captions, QA, prompts) now names the new one
    for f in ep_root.rglob("*.json"):
        if "old" in f.relative_to(ep_root).parts:
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        if old_sha in txt:
            f.write_text(txt.replace(old_sha, new_sha), encoding="utf-8")
    for f in (ep_root / "scenes").glob(f"{name}*.md"):
        txt = f.read_text(encoding="utf-8")
        if old_sha in txt:
            f.write_text(txt.replace(old_sha, new_sha), encoding="utf-8")
    E.check_lock(ep_root, n)
    return what


def strip_episode(ep: int, chapters: list[int] | None = None, dry_run: bool = False) -> None:
    from .manifest import Manifest
    ep_root = E.root(ep)
    man = Manifest.load(ep_root / "manifest.json")
    for c in man.chapters:
        if chapters and c.idx not in chapters:
            continue
        if not (ep_root / "hindi" / f"{c.idx:05d}.txt").exists():
            print(f"  chapter {c.idx}: no Hindi yet")
            continue
        print(f"  chapter {c.idx}: {strip_chapter(ep_root, c.idx, dry_run)}", flush=True)
