"""Join rendered chapters into one episode file, with a copyright disclaimer first.

The disclaimer is a short card (white text on black, silent; wording, heading, length and font come
from the video.disclaimer settings) made per episode with the same encoder settings as the chapter
renders, so everything joins with a plain stream copy. It is the first chapter marker, "Disclaimer".
It is rebuilt when any of those settings change (video/disclaimer.json holds what it was made from).

Usage:
    py -m abpipe.episode_join --episode 1                      # all chapters -> E001.mp4
    py -m abpipe.episode_join --episode 1 --chapters 1-5       # partial, -> E001-c1-5.mp4
    py -m abpipe.episode_join --episode 1 --watermark logo.png  # burn a watermark (re-encodes); the
                                                                # video.logo setting needs no re-encode
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from . import episode as E
from .episode_render import FPS, H, W
from .episode_translate import parse_range
from .episode_video import card_fonts, logo_layer, logo_signature, paste_logo, probe, run, series_label
from .manifest import Manifest
from .package import _ffmetadata

PLACEMENTS = ("start-except-first-episode", "start", "end", "none")

GENERIC_TEXT = (
    "This is an unofficial, fan-made adaptation inspired by {work}. "
    "All original story rights, characters, and lore belong to the original author, {author}, "
    "and the respective official publishers. "
    "The translated Hindi narration and accompanying visuals in this video are AI-generated. "
    "This project is completely independent and is not affiliated with, endorsed by, "
    "or sponsored by the original creators or copyright holders. "
    "Please support the official releases.")


def disclaimer_text(ep_root: Path, cfg: dict | None = None) -> tuple[str, list[str]]:
    """(heading, paragraphs) from video.disclaimer. text: null builds the generic text from
    book.title and book.author; a text from settings is split into paragraphs at blank lines."""
    cfg = cfg or E.cfg(E.info(ep_root)["episode"])
    d, book = cfg["video"]["disclaimer"], cfg.get("book") or {}
    text = d.get("text")
    if not text:
        text = GENERIC_TEXT.format(work=f"the novel {book['title']}" if book.get("title") else "the original novel",
                                   author=book.get("author") or "the original author")
    paras = [" ".join(p.split()) for p in str(text).split("\n\n") if p.strip()]
    return str(d.get("heading") or ""), paras


def card_image(ep_root: Path, out: Path, cfg: dict | None = None) -> None:
    from PIL import Image, ImageDraw, ImageFont
    cfg = cfg or E.cfg(E.info(ep_root)["episode"])
    head, paras = disclaimer_text(ep_root, cfg)
    regular, bold = card_fonts(cfg)

    def font(size, is_bold=False):
        return ImageFont.truetype(str(bold if is_bold else regular), size)

    body_f, head_f = font(36), font(44, is_bold=True)
    lines = []                                         # (text, font, gap before)
    for i, para in enumerate(paras):
        cur = ""
        for word in para.split():
            trial = (cur + " " + word).strip()
            if body_f.getlength(trial) > 1240 and cur:
                lines.append((cur, body_f, 0))
                cur = word
            else:
                cur = trial
        lines.append((cur, body_f, 0))
        lines[-1] = (lines[-1][0], body_f, 34)         # a gap after each paragraph
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    heights = [64] + [54 + gap for _, _, gap in lines]
    y = (H - (sum(heights) - lines[-1][2])) / 2
    d.text((W / 2, y + 32), head, font=head_f, fill=(255, 255, 255), anchor="mm")
    d.line([(W / 2 - 120, y + 78), (W / 2 + 120, y + 78)], fill=(255, 255, 255), width=2)
    y += 64 + 40
    for text, f, gap in lines:
        d.text((W / 2, y + 27), text, font=f, fill=(255, 255, 255), anchor="mm")
        y += 54 + gap
    paste_logo(img, logo_layer(cfg["video"]["logo"], (W, H)))
    img.save(out)


def _disclaimer_key(ep_root: Path, cfg: dict, rate: int) -> str:
    """What the card is made from: heading, text, seconds, fonts (files too), logo, sample rate."""
    fonts = [(str(f), f.stat().st_mtime_ns) for f in card_fonts(cfg)]
    d = cfg["video"]["disclaimer"]
    blob = json.dumps([disclaimer_text(ep_root, cfg), float(d["seconds"]), fonts,
                       logo_signature(cfg["video"]["logo"]), rate], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def make_disclaimer(ep_root: Path, rate: int = 24000, cfg: dict | None = None) -> Path:
    cfg = cfg or E.cfg(E.info(ep_root)["episode"])
    out = ep_root / "video" / "disclaimer.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    key, stamp = _disclaimer_key(ep_root, cfg, rate), out.with_suffix(".json")
    if out.exists() and stamp.exists() and json.loads(stamp.read_text(encoding="utf-8")).get("key") == key:
        return out                                     # current: nothing it is made from changed
    png = out.with_suffix(".png")
    card_image(ep_root, png, cfg)
    p = run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-i", png,
             "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl=mono",
             "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
             "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-r", str(FPS),
             "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-c:a", "aac", "-b:a", "160k", "-t", str(float(cfg["video"]["disclaimer"]["seconds"])),
             "-movflags", "+faststart", out])
    if p.returncode:
        sys.exit(f"disclaimer failed: {p.stderr[-500:]}")
    stamp.write_text(json.dumps({"key": key}), encoding="utf-8")
    return out


def phase_title(title: str) -> str:
    """The marker and timestamp name of a chapter: "Chapter 2: Prank" -> "Phase 2: Prank"."""
    import re
    return re.sub(r"^\s*Chapter\b", "Phase", str(title), count=1, flags=re.I)


def join(episode: int, chapters: str | None, *, watermark: Path | None = None) -> Path:
    ep_root = E.root(episode)
    info = E.info(ep_root)
    man = Manifest.load(ep_root / "manifest.json")
    want = set(parse_range(chapters)) if chapters else {c.idx for c in man.chapters}
    parts = [(c, ep_root / "video" / f"c{c.idx:04d}.mp4") for c in man.chapters if c.idx in want]
    missing = [c.idx for c, p in parts if not p.exists()]
    if missing:
        sys.exit(f"chapters not rendered yet: {missing}")
    audio = next(x for x in probe(parts[0][1])["streams"] if x["codec_type"] == "audio")
    rate = int(audio.get("sample_rate", 24000))
    cfg = E.cfg(info["episode"])
    sub_cfg, series = cfg["video"]["subscribe"], series_label()
    card = make_disclaimer(ep_root, rate, cfg) if cfg["video"]["disclaimer"]["enabled"] else None

    class Part:                                        # what package._ffmetadata reads
        def __init__(self, title, seconds, listed, kind, chapter=None):
            self.title, self.seconds, self.listed, self.kind, self.chapter = title, seconds, listed, kind, chapter
    # Subscribe cards (episode_extras): the voiced one where video.subscribe.voiced.placement says
    # (default: at the start of every episode after the first, else at the end, never both); a silent
    # one after every video.subscribe.silent.every_chapters-th chapter (0 = none), forced or
    # suppressed per chapter by video.subscribe.silent_after in episode.yaml.
    from . import episode_extras
    cards = episode_extras.cards_ready(rate)
    if not cards:
        print("subscribe cards: skipped, the message is not narrated yet (see `novel extras voice`)")
    ep_no, ep_first, ep_last = info["episode"], info["start"], info["end"]
    placement = sub_cfg["voiced"]["placement"]
    if placement not in PLACEMENTS:
        sys.exit(f"video.subscribe.voiced.placement '{placement}': use one of {', '.join(PLACEMENTS)}")
    every = int(sub_cfg["silent"]["every_chapters"] or 0)
    files = [("Disclaimer", card, False, "disclaimer")] if card else []
    first_in, last_in = parts[0][0].idx == ep_first, parts[-1][0].idx == ep_last
    # The intro hook (episode_intro, `novel intro`): first of all, listed as "Intro" at 0:00.
    from . import episode_intro
    intro = episode_intro.clip(ep_no) if first_in else None
    if intro:
        files.insert(0, ("Intro", intro, True, "intro"))
    voiced_at_start = bool(cards and first_in and (placement == "start" or
                                                   (placement == "start-except-first-episode" and ep_no > 1)))
    if voiced_at_start:
        files.append(("Subscribe", cards["voiced"], False, "subscribe-voiced"))
    for c, p in parts:
        files.append((phase_title(c.title), p, True, "chapter", c.idx))
        force = E.cfg(ep_no, c.idx)["video"]["subscribe"].get("silent_after")
        due = (every > 0 and (c.idx - ep_first + 1) % every == 0 and c.idx != parts[-1][0].idx) if force is None \
            else bool(force)
        if cards and due:
            files.append(("Subscribe", cards["silent"], False, "subscribe-silent"))
    if cards and last_in and not voiced_at_start and placement in ("end", "start-except-first-episode"):
        files.append(("Subscribe", cards["voiced"], False, "subscribe-voiced"))
    marks = [Part(f[0], float(probe(f[1])["format"]["duration"]), f[2], f[3], *f[4:]) for f in files]
    files = [(f[0], f[1]) for f in files]
    work = ep_root / "video"
    (work / "concat.txt").write_text("\n".join(f"file '{p.resolve().as_posix()}'" for _, p in files),
                                     encoding="utf-8")
    (work / "chapters.txt").write_text(_ffmetadata(marks, f"{series} - Episode {info['episode']}"),
                                       encoding="utf-8")
    name = f"E{info['episode']:03d}" + (f"-c{chapters}" if chapters else "") + ".mp4"
    out = ep_root / name
    if watermark and watermark.exists():
        if logo_signature(cfg["video"]["logo"]):
            print("  note: video.logo is on and --watermark is given: both will show")
        print(f"  watermark: {watermark.name} burned into final video (re-encoding)")
        p = run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", work / "concat.txt",
                 "-i", str(watermark), "-i", work / "chapters.txt",
                 "-filter_complex", "[0][1]overlay=26:26",
                 "-map_metadata", "2",
                 "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
                 "-c:a", "copy", "-movflags", "+faststart", out])
    else:
        p = run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", work / "concat.txt",
                 "-i", work / "chapters.txt", "-map", "0", "-map_metadata", "1", "-c", "copy",
                 "-movflags", "+faststart", out])
    if p.returncode:
        sys.exit(f"join failed: {p.stderr[-800:]}")
    total = float(probe(out)["format"]["duration"])
    # YouTube needs the first chapter at 0:00 and every chapter 10 s or longer, so the disclaimer and
    # the subscribe cards are not listed: the first chapter is 0:00, the rest keep their real times.
    stamps, at = [], 0.0
    for m in marks:
        if not m.listed:
            at += m.seconds
            continue
        if not stamps:
            stamps.append(f"0:00 {m.title}")
            at += m.seconds
            continue
        h, rem = divmod(int(at), 3600)
        stamp = f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"
        stamps.append(f"{stamp} {m.title}")
        at += m.seconds
    stamp_file = out.with_suffix(".timestamps.txt")
    stamp_file.write_text("\n".join(stamps) + "\n", encoding="utf-8")
    # The same list with true positions in the final video, for tools: nothing is shifted.
    at, chap_rows, card_rows = 0.0, [], []
    for m in marks:
        if m.kind == "intro":
            card_rows.append({"kind": "intro", "start": round(at, 3), "seconds": round(m.seconds, 3)})
        elif m.kind == "chapter":
            chap_rows.append({"chapter": m.chapter, "title": m.title, "start": round(at, 3)})
        else:
            card_rows.append({"kind": m.kind, "start": round(at, 3), "seconds": round(m.seconds, 3)})
        at += m.seconds
    lead = chap_rows[0]["start"] if chap_rows else 0.0
    json_file = out.with_suffix(".timestamps.json")
    json_file.write_text(json.dumps({
        "episode": info["episode"], "duration": round(total, 3),
        "offset_note": (f"start values are true seconds in the video. timestamps.txt lists the first chapter at "
                        f"0:00 although it starts at {lead:.1f} s (the cards before it are not listed); every "
                        f"later chapter is listed at its true time."),
        "chapters": chap_rows, "cards": card_rows}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{out}\n  {total / 60:.1f} min, {out.stat().st_size / 1e6:.0f} MB, {len(marks)} markers "
          f"({len(parts)} chapters plus disclaimer and subscribe cards; parts sum to {sum(m.seconds for m in marks) / 60:.1f} min)")
    print(f"  YouTube timestamps: {stamp_file} (true positions: {json_file.name})")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--episode", type=int, required=True)
    ap.add_argument("--chapters", default=None, help="e.g. 1-5; default all of the episode")
    ap.add_argument("--watermark", type=Path, default=None,
                    help="PNG watermark to burn into the joined video (top-left)")
    a = ap.parse_args()
    join(a.episode, a.chapters, watermark=a.watermark)


if __name__ == "__main__":
    main()
