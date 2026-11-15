"""Captions drawn inside the active comic panel: WHAT text is shown in WHICH panel WHEN.

Pure Python (no drawing, no fonts): the caller passes `measure`, the pixel width of one line of text in
the caption font, so this module stays testable and the renderer stays the only place that knows the font.

Rules:
- A subtitle sentence is shown in the panel being narrated. A sentence that spans a camera cut is split
  at the cut, each part going to its own panel: a caption never sits in a panel the camera has left.
  Words are placed by character-share timing (the sentence's characters spread evenly over its interval),
  which is how the rest of the pipeline times subtitles.
- A caption box is small, so a portion is cut into chunks that fit it: few lines, few words. Upright
  panels get short chunks because a narrow box turns a long chunk into a tall block of text. Chunks end
  on punctuation when they can, and never leave one lonely word for the last chunk.
- Time is never invented: a chunk too short to read (MIN_SECONDS) is not shown, and a chunk is never
  shown over the title card or past its panel's window.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Callable

BOX_SHARE = 0.84    # a caption line may use at most this share of the panel's width (the rest is margin)
# Per panel shape: (max lines, max words per chunk). Upright panels get short chunks so text never runs
# long in a narrow panel.
LIMITS = {"wide": (2, 9), "landscape": (2, 8), "square": (2, 6), "portrait": (2, 4), "tall": (2, 4)}
MIN_SECONDS = 0.35  # a caption on screen for less than this cannot be read: dropped, not shown
BREAK_CHARS = ",;:.!?-–—…"   # a word ending in one of these is a good place to end a chunk
# Words that lean on the next one: a chunk does not end on them (the reel's hype captions do the same).
SOFT = frozenset("a an the of to in on at for from with by and or but his her its their my your our "
                 "he she they we i it is was were be as that this these those who which".split())

Measure = Callable[[str], float]


# ------------------------------------------------------------------ wrapping and chunking


def wrap(words: list[str], max_width: float, measure: Measure) -> list[str]:
    """Word wrap into as few lines as greedy wrapping needs; two lines are balanced (the longer one as short
    as possible) so a second line is never one stray word. A word wider than max_width gets its own line:
    words are never split."""
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        if cur and measure(" ".join(cur + [w])) > max_width:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    if len(lines) == 2 and len(words) > 2:
        best = None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            wa, wb = measure(a), measure(b)
            if max(wa, wb) <= max_width and (best is None or max(wa, wb) < best[0]):
                best = (max(wa, wb), [a, b])
        if best:
            lines = best[1]
    return lines


def split_chunks(words: list[str], max_width: float, max_lines: int, max_words: int,
                 measure: Measure) -> list[list[str]]:
    """Split a run of words into chunks of <= max_words words that wrap into <= max_lines lines.

    Greedy: each chunk takes as many words as fit, then backs up to the last punctuation break if that
    leaves it at least half of max_words long. A last chunk of one word is topped up from the previous
    one (only when both still fit), since a lone word flashing up reads badly."""
    def fits(ws: list[str]) -> bool:
        return len(ws) <= max_words and len(wrap(ws, max_width, measure)) <= max_lines

    chunks: list[list[str]] = []
    i, n = 0, len(words)
    while i < n:
        j = i + 1                                   # a single word always makes a chunk, however wide
        while j < n and fits(words[i:j + 1]):
            j += 1
        if j < n:                                   # the run goes on: prefer to end on punctuation
            for p in range(j, i, -1):
                if 2 * (p - i) < max_words:
                    break
                if words[p - 1][-1] in BREAK_CHARS:
                    j = p
                    break
            # Never end on "the", "of", "his"...: it belongs with the next word ("NEAR THE" / "BODY").
            while j - i >= 2 and words[j - 1][-1] not in BREAK_CHARS and words[j - 1].lower() in SOFT:
                j -= 1
        chunks.append(words[i:j])
        i = j
    if len(chunks) >= 2 and len(chunks[-1]) == 1 and len(chunks[-2]) >= 3:
        moved = [chunks[-2][-1]] + chunks[-1]
        left = chunks[-2][-2]                       # what the chunk before would then end on
        if fits(moved) and (left[-1] in BREAK_CHARS or left.lower() not in SOFT):
            chunks[-2] = chunks[-2][:-1]
            chunks[-1] = moved
    return chunks


# ------------------------------------------------------------------ cues


def _portions(words: list[str], t0: float, t1: float, cuts: list[float]) -> list[tuple[int, list[str]]]:
    """(panel, words) per panel the sentence spans, by each word's character midpoint in time."""
    total = len(" ".join(words))
    n = len(cuts) - 1
    out: list[tuple[int, list[str]]] = []
    pos = 0
    for w in words:
        t = t0 + (pos + len(w) / 2) / total * (t1 - t0)
        pos += len(w) + 1
        k = min(n - 1, max(0, bisect_right(cuts, t) - 1))
        if out and out[-1][0] == k:
            out[-1][1].append(w)
        else:
            out.append((k, [w]))
    return out


def cues(lines: list[dict], pl: dict, measure: Measure, *, lead: float, title_end: float) -> list[dict]:
    """The captions of a chapter: [{"k", "t0", "t1", "lines", "text"}] in video seconds, sorted by t0
    and never overlapping. `lines` are sentences {"para", "en", "start", "end"} in narration seconds."""
    cuts = pl["cuts"]
    panels = pl["panels"]
    raw: list[tuple[float, float, int, list[str]]] = []
    for ln in lines:
        words = (ln.get("en") or "").split()
        if ln.get("para") == 0 or not words:
            continue
        t0, t1 = lead + ln["start"], lead + ln["end"]
        if t1 <= t0:
            continue
        for k, ws in _portions(words, t0, t1, cuts):
            lo, hi = max(t0, cuts[k]), min(t1, cuts[k + 1])
            if hi <= lo:
                continue
            width = BOX_SHARE * panels[k]["w"]
            max_lines, max_words = LIMITS[panels[k]["shape"]]
            chunks = split_chunks(ws, width, max_lines, max_words, measure)
            sizes = [len(" ".join(c)) for c in chunks]
            tot, acc = sum(sizes), 0
            for c, size in zip(chunks, sizes):
                a = lo + (hi - lo) * acc / tot
                acc += size
                b = lo + (hi - lo) * acc / tot
                raw.append((a, b, k, c))
    raw.sort(key=lambda r: (r[0], r[1]))
    out: list[dict] = []
    prev_end = title_end
    for a, b, k, c in raw:
        a = max(a, prev_end)                        # title card, and the cue before this one
        if b - a < MIN_SECONDS:
            continue
        out.append({"k": k, "t0": round(a, 3), "t1": round(b, 3),
                    "lines": wrap(c, BOX_SHARE * panels[k]["w"], measure), "text": " ".join(c)})
        prev_end = b
    return out


FADE_GAP = 0.05     # a caption followed by the next one sooner than this is swapped, not faded


def mark_fades(cues_list: list[dict]) -> list[dict]:
    """Set "fade_out" on each cue: True when no caption follows in the same panel right after it (a gap,
    or the camera moves on), so it fades instead of vanishing; False when the next one replaces it."""
    for a, b in zip(cues_list, cues_list[1:] + [None]):
        a["fade_out"] = b is None or b["k"] != a["k"] or b["t0"] - a["t1"] > FADE_GAP
    return cues_list


def at(cues_list: list[dict], t: float) -> dict | None:
    """The cue showing at video time t (t0 <= t < t1), by binary search; None in a gap."""
    lo, hi = 0, len(cues_list)                      # first cue whose t0 is > t
    while lo < hi:
        mid = (lo + hi) // 2
        if cues_list[mid]["t0"] <= t:
            lo = mid + 1
        else:
            hi = mid
    if lo and t < cues_list[lo - 1]["t1"]:
        return cues_list[lo - 1]
    return None
