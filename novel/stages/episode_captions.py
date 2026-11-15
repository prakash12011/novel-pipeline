"""English subtitles for the Hindi narration, burned into the chapter video.

Two steps, so captions never wait for the narration:

  translate  needs only the locked Hindi. The chapter is split into sentences exactly as the TTS
             kernel splits it into chunks (tts.chunk_hindi), and Gemini translates sentence for
             sentence. Saved as captions/NNNNN.json (schema below): the Hindi, the English, and
             where each sentence sits (paragraph, chunk, character offset in the chunk), plus the
             sha256 of the Hindi file, so a later edit marks the captions stale.
  time       needs timing/NNNNN.json (measured start and end of every spoken chunk). Each sentence
             gets a share of its chunk's time by character count. Done in memory whenever the ASS
             file is written (timed_lines / write_ass); NOT stored in the captions file, so
             narration that is redone never leaves old times behind. ffmpeg (libass) burns the ASS.

captions/NNNNN.json, version 2:
    {"version": 2, "chapter": 1, "model": "...", "hindi_sha256": "...", "chunks": 7,
     "lines": [{"para": 0, "chunk": 0, "off": 0, "len": 21, "hi": "...", "en": "..."}, ...]}
  para  paragraph (0 = the chapter heading, which the title card shows);
  chunk index of the narration chunk the sentence is in; off, len  character offset / length of
  the sentence inside that chunk's text. Files without "version" are the old one-step format
  (start and end already inside each line); they are used exactly as they are.

By hand:  novel captions [--book B] [--ep N] [--chapters A-B] [--force]
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import episode as E
from .episode_render import LEAD, H, W
from .episode_translate import MODEL, Gemini, parse_range
from .scenewrite import _extract_json

CHUNK_CHARS = 300          # what the narration kernel uses (tts.chunk_hindi, cfg chars_per_chunk)
MAX_LINE = 84              # defaults when settings are not available: video.captions.max_line
SIZE = 38
VERSION = 2
NAME_LIMIT = 150           # send every name up to this many; above it, only names the chapter contains

PROMPT = """You translate a Hinglish (Hindi in Devanagari) story narration into natural, simple English subtitles.
You get a JSON list of numbered sentences. Return JSON {"en": [...]} with EXACTLY one English line per input
sentence, same order, same count, never merged or split. Keep each line short and spoken, like film subtitles.@@NAMES_LINE@@
Reported speech must read naturally in English: "लुमियान कहता है कि मैं बहुत आम इंसान हूं" becomes
"Lumian says he's a very ordinary person", never "Lumian says that I am...". A line spoken by a
character in the first person stays first person ("I did not want them to throw him in the river.").@@NAMES_BLOCK@@"""


def build_prompt(names: list[str]) -> str:
    if not names:
        return PROMPT.replace("@@NAMES_LINE@@", "").replace("@@NAMES_BLOCK@@", "")
    return (PROMPT.replace("@@NAMES_LINE@@", "\nKeep names as the English spellings below.")
            .replace("@@NAMES_BLOCK@@", "\n\nNAMES: " + ", ".join(names) + "."))


# ------------------------------------------------------------------ names

def book_names(text: str = "", book: str | None = None) -> list[str]:
    """Character display names (bible v2) and English names (names.json) of the current book.

    Up to NAME_LIMIT names all go in; beyond that only those whose Hindi spelling is in `text`.
    No book or no bible gives an empty list."""
    from novel import bible
    from novel.core import context, paths
    try:
        book = book or context.active_book()
        d = paths.bible_dir(book)
    except SystemExit:
        return []
    if not d.exists():
        return []
    found: list[tuple[str, str]] = []          # (English, Hindi or "")
    for c in (bible.load_characters(d).get("characters") or {}).values():
        if c.get("display"):
            found.append((str(c["display"]).strip(), str(c.get("hi") or "").strip()))
    for en, hi in (bible.load_names(d) or {}).items():
        found.append((str(en).strip(), str(hi or "").strip()))
    seen: set[str] = set()
    uniq = []
    for en, hi in found:
        if en and en.lower() not in seen:
            seen.add(en.lower())
            uniq.append((en, hi))
    if len(uniq) > NAME_LIMIT:
        uniq = [(en, hi) for en, hi in uniq if hi and hi in text]
    return [en for en, _ in uniq]


# ------------------------------------------------------------------ sentences and chunks

def sentences(para: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[।?!.])\s+", para) if s.strip()]


def chunks(text: str) -> list[tuple[int, list[str]]]:
    """(paragraph, sentences) per narration chunk, the same split the TTS kernel made."""
    out = []
    for pi, para in enumerate(p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()):
        cur: list[str] = []
        for s in sentences(para):
            if cur and len(" ".join(cur)) + len(s) + 1 > CHUNK_CHARS:
                out.append((pi, cur))
                cur = [s]
            else:
                cur.append(s)
        if cur:
            out.append((pi, cur))
    return out


def caption_rows(text: str) -> list[dict]:
    """One row per sentence, with the place it has in the narration (no times, no English yet)."""
    rows = []
    for ci, (pi, ss) in enumerate(chunks(text)):
        off = 0
        for s in ss:
            rows.append({"para": pi, "chunk": ci, "off": off, "len": len(s), "hi": s})
            off += len(s) + 1
    return rows


def time_rows(rows: list[dict], timing: list[dict]) -> list[dict]:
    """Add start/end to every row: a sentence takes its chunk's measured time by character share.

    The arithmetic is the old one-step code's (accumulating `at` sentence by sentence)."""
    nchunks = (max(r["chunk"] for r in rows) + 1) if rows else 0
    if nchunks != len(timing):
        raise SystemExit(f"{nchunks} chunks in the text, {len(timing)} in the timing map")
    by_chunk: dict[int, list[dict]] = {}
    for r in rows:
        by_chunk.setdefault(r["chunk"], []).append(r)
    out = []
    for ci, t in enumerate(timing):
        group = by_chunk.get(ci, [])
        total = sum(r["len"] for r in group)
        at = t["start_sec"]
        for r in group:
            dur = (t["end_sec"] - t["start_sec"]) * r["len"] / total
            out.append({**r, "start": round(at, 3), "end": round(at + dur, 3)})
            at += dur
    return out


def timed_sentences(ep_root: Path, n: int) -> list[dict]:
    """The old API: sentences of the Hindi with start/end from the timing map."""
    name = f"{n:05d}"
    text = (ep_root / "hindi" / f"{name}.txt").read_text(encoding="utf-8")
    timing = json.loads((ep_root / "timing" / f"{name}.json").read_text(encoding="utf-8"))["chunks"]
    try:
        return time_rows(caption_rows(text), timing)
    except SystemExit as e:
        raise SystemExit(f"chapter {n}: {e}")


# ------------------------------------------------------------------ files and staleness

def caption_path(ep_root: Path, n: int) -> Path:
    return ep_root / "captions" / f"{n:05d}.json"


def load(ep_root: Path, n: int) -> dict:
    return json.loads(caption_path(ep_root, n).read_text(encoding="utf-8"))


def is_legacy(doc: dict) -> bool:
    """The old one-step format: no version, start/end inside every line."""
    return doc.get("version") != VERSION


def hindi_sha(ep_root: Path, n: int) -> str:
    return E.sha256(ep_root / "hindi" / f"{n:05d}.txt")


def stale(ep_root: Path, n: int) -> bool:
    """No captions yet, or (new format) they were made from other Hindi. Old-format files count
    as current: they are used as they are."""
    if not caption_path(ep_root, n).exists():
        return True
    doc = load(ep_root, n)
    return False if is_legacy(doc) else doc.get("hindi_sha256") != hindi_sha(ep_root, n)


def fix_command(ep_root: Path, n: int) -> str:
    m = re.fullmatch(r"E(\d+)", ep_root.name)
    return f"novel captions --ep {int(m[1]) if m else '<N>'} --chapters {n} --force"


# ------------------------------------------------------------------ gemini

class CaptionGemini(Gemini):
    """The episode Gemini client. The endpoint is replaceable: NOVEL_GEMINI_URL is a URL template with {model}
    (tests point it at a local server); Gemini._post reads it."""


def _ep_number(ep_root: Path) -> int | None:
    m = re.fullmatch(r"E(\d+)", ep_root.name)
    return int(m[1]) if m else None


def make_gemini(ep_root: Path, model: str | None = None) -> Gemini:
    (ep_root / "captions").mkdir(parents=True, exist_ok=True)
    return CaptionGemini(model or MODEL, ep_root / "captions" / "gemini.log.jsonl")


def ensure(ep_root: Path, n: int, model: str = MODEL) -> Path:
    """Captions for the chapter, translated now if missing or made from other Hindi."""
    if stale(ep_root, n):
        translate_chapter(ep_root, n, make_gemini(ep_root, model))
    return caption_path(ep_root, n)


def translate(episode: int, chapters: str, model: str) -> None:
    ep_root = E.root(episode)
    g = make_gemini(ep_root, model)
    for n in parse_range(chapters):
        translate_chapter(ep_root, n, g)


def translate_chapter(ep_root: Path, n: int, g, names: list[str] | None = None) -> Path:
    """Step 1: needs only hindi/NNNNN.txt. `g` is anything with ask(system, text, json_out=,
    temperature=) and .model (the Gemini class, or a fake in tests)."""
    (ep_root / "captions").mkdir(parents=True, exist_ok=True)
    out = caption_path(ep_root, n)
    f = ep_root / "hindi" / f"{n:05d}.txt"
    text = f.read_text(encoding="utf-8")
    rows = caption_rows(text)
    if not rows:
        raise SystemExit(f"chapter {n}: no sentences in {f}")
    system = build_prompt(book_names(text) if names is None else names)
    for attempt in range(3):
        raw = g.ask(system, json.dumps([{"n": i, "hi": r["hi"]} for i, r in enumerate(rows)],
                                       ensure_ascii=False), json_out=True, temperature=0.2)
        en = _extract_json(raw).get("en", [])
        if len(en) == len(rows):
            break
        print(f"  chapter {n}: got {len(en)} lines for {len(rows)} sentences, asking again")
    else:
        raise SystemExit(f"chapter {n}: Gemini would not keep one line per sentence")
    for r, e in zip(rows, en):
        r["en"] = str(e).strip()
    doc = {"version": VERSION, "chapter": n, "model": g.model, "hindi_sha256": E.sha256(f),
           "chunks": rows[-1]["chunk"] + 1, "lines": rows}
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)
    print(f"  chapter {n}: {len(rows)} subtitle lines (Gemini) -> {out}")
    return out


# ------------------------------------------------------------------ step 2: time

def timed_lines(ep_root: Path, n: int) -> list[dict]:
    """Lines with start/end, ready for the ASS file. Refuses stale or untimed new-format captions."""
    p = caption_path(ep_root, n)
    if not p.exists():
        raise SystemExit(f"chapter {n}: no captions yet: run {fix_command(ep_root, n).replace(' --force', '')}")
    doc = load(ep_root, n)
    if is_legacy(doc):
        return doc["lines"]
    if doc.get("hindi_sha256") != hindi_sha(ep_root, n):
        raise SystemExit(f"chapter {n}: captions/{n:05d}.json were made from different Hindi than "
                         f"hindi/{n:05d}.txt now holds. Redo them with: {fix_command(ep_root, n)}")
    tf = ep_root / "timing" / f"{n:05d}.json"
    if not tf.exists():
        raise SystemExit(f"chapter {n}: no timing map yet ({tf}): pull the narration first (novel tts pull)")
    timing = json.loads(tf.read_text(encoding="utf-8"))["chunks"]
    try:
        return time_rows(doc["lines"], timing)
    except SystemExit as e:
        raise SystemExit(f"chapter {n}: {e}")


# ------------------------------------------------------------------ style and font

def caption_settings(ep_root: Path, n: int | None = None) -> dict:
    """video.captions for this chapter (chapter overrides apply). Falls back to the repo defaults
    when no book is active."""
    ep = _ep_number(ep_root)
    try:
        cfg = E.cfg(ep, n)
    except SystemExit:
        from novel.core import settings
        cfg = settings._read(settings.defaults_file())
    return dict((cfg.get("video") or {}).get("captions") or {})


def font_family(path: Path) -> str:
    """The family name a TTF declares (name table ID 1: what libass and fontconfig match on)."""
    d = Path(path).read_bytes()
    count = struct.unpack(">H", d[4:6])[0]
    for i in range(count):
        tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i:28 + 16 * i])
        if tag == b"name":
            break
    else:
        raise ValueError(f"{path}: no name table")
    cnt, so = struct.unpack(">HH", d[off + 2:off + 6])
    best = None
    for j in range(cnt):
        plat, _, _, nid, ln, o = struct.unpack(">6H", d[off + 6 + 12 * j:off + 18 + 12 * j])
        if nid != 1:
            continue
        raw = d[off + so + o:off + so + o + ln]
        s = raw.decode("utf-16-be") if plat in (0, 3) else raw.decode("latin-1")
        if best is None or plat == 3:
            best = s
    if not best:
        raise ValueError(f"{path}: no family name")
    return best


def font_choice(cfg: dict) -> tuple[str, Path | None]:
    """(family name for the ASS style, folder libass should search). The family is read from
    font_file; font_name is used only when there is no font file."""
    from novel.core import settings
    ff = cfg.get("font_file")
    if ff:
        p = settings.asset_path(ff)
        if p.exists():
            return font_family(p), p.parent
        raise SystemExit(f"video.captions.font_file {p} does not exist")
    return str(cfg.get("font_name") or "Noto Sans SemiBold"), None


def _quote(path: str) -> str:
    """One path as an ffmpeg filter option value: forward slashes, drive colon escaped, the
    whole thing in single quotes (spaces and commas are safe inside them)."""
    s = str(path).replace("\\", "/").replace("'", "'\\\\\\''")
    return "'" + s.replace(":", "\\:") + "'"


def ass_filter(ass: Path, fontsdir: Path | None = None) -> str:
    """The complete ffmpeg filter for burning `ass`: ass='C\\:/dir with space/x.ass':fontsdir='...'.
    Use as the video filter, e.g. f"[0:v]...,{ass_filter(ass, fontsdir)}[v]"."""
    if fontsdir is None:
        from novel.core import paths
        fontsdir = paths.fonts_dir()
    return f"ass={_quote(Path(ass).resolve())}:fontsdir={_quote(Path(fontsdir).resolve())}"


def chapter_ass_filter(ep_root: Path, n: int, ass: Path) -> str:
    """ass_filter with the fonts folder this chapter's settings name."""
    return ass_filter(ass, font_choice(caption_settings(ep_root, n))[1])


# ------------------------------------------------------------------ ASS

def _pieces(text: str, max_line: int = MAX_LINE) -> list[str]:
    """Split a long line at a comma, or between words, into pieces of <= max_line characters."""
    if len(text) <= max_line:
        return [text]
    words, best = text.split(), None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        score = abs(len(a) - len(b)) - (40 if a.endswith((",", ";", ":")) else 0)
        if best is None or score < best[0]:
            best = (score, a, b)
    if best is None:                       # one word longer than the line: nothing to split
        return [text]
    return _pieces(best[1], max_line) + _pieces(best[2], max_line)


def _ts(t: float) -> str:
    cs = int(round(max(t, 0) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_ass(ep_root: Path, n: int, out: Path) -> int:
    cfg = caption_settings(ep_root, n)
    family, _ = font_choice(cfg)
    size = int(cfg.get("size") or SIZE)
    max_line = int(cfg.get("max_line") or MAX_LINE)
    lines = timed_lines(ep_root, n)
    para1 = next((r["start"] for r in lines if r["para"] == 1), 3.0)
    title_end = min(10.0, max(4.0, LEAD + para1 + 2.5))    # the title card's band, as episode_render
    events = []
    for r in lines:
        if r["para"] == 0:                  # the chapter heading: the title card shows it
            continue
        start, end = LEAD + r["start"], LEAD + r["end"]
        ps = _pieces(r["en"], max_line)
        total = sum(len(p) for p in ps)
        for p in ps:
            dur = (end - start) * len(p) / total
            a, b = max(start, title_end), start + dur
            if b - a > 0.3:
                events.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{p}")
            start += dur
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{family},{size},&H00FFFFFF,&H00FFFFFF,&H40000000,&H40000000,0,0,0,0,100,100,0,0,3,7,0,2,240,240,64,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    out.write_text(head + "\n".join(events) + "\n", encoding="utf-8")
    return len(events)


def burn(episode: int, n: int, end: float | None) -> Path:
    from .episode_video import probe
    ep_root = E.root(episode)
    vid = ep_root / "video"
    src = vid / f"c{n:04d}.mp4"
    if not src.exists():
        sys.exit(f"{src} not rendered yet")
    ass = vid / f"c{n:04d}.en.ass"
    count = write_ass(ep_root, n, ass)
    out = vid / f"c{n:04d}-cc.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", src.name, "-vf", chapter_ass_filter(ep_root, n, ass),
           "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-c:a", "copy", "-movflags", "+faststart"]
    if end:
        cmd += ["-t", str(end)]
    p = subprocess.run(cmd + [out.name], cwd=vid, capture_output=True, text=True)
    if p.returncode:
        sys.exit(f"burn failed: {p.stderr[-800:]}")
    d = float(probe(out)["format"]["duration"])
    print(f"{out}\n  {count} subtitle events, {d:.1f} s, {out.stat().st_size / 1e6:.0f} MB")
    return out
