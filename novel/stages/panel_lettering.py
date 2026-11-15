"""How a caption looks inside a comic panel: the manga narration box of the Shorts reels.

The same lettering as reelkit's manga style (tools/reelkit/styles/manga.mjs, the `.mg-bx` boxes): each line is
its own white box with a thick black border and a hard black drop shadow, Archivo Black in capitals, the box
hugging its text, square corners. Lines of one caption stack downward. A caption sits at the top left of the
active panel, tilted a little (seeded per caption, so a re-render looks the same), and pops in: from 85% size
and transparent to full over 0.09 s, growing from its left edge.

The reel is 1080 px wide with 34 px text; every size here is that box scaled by SIZE / 34, so the proportions
match.

Pure Pillow; panel_captions decides the text and the times, episode_panels calls `paste`."""

from __future__ import annotations

import math
import random
import re
from functools import lru_cache
from pathlib import Path

FONT = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "ArchivoBlack-Regular.ttf"
SIZE = 22                  # text px (the reel: 34 px on a 1080 px wide frame; 44 looked far too big here)
_S = SIZE / 34
BOX_H = round(74 * _S)     # one box, border included (the reel's box-sizing: border-box)
PAD = round(20 * _S)       # text to border, left and right
BORDER = max(3, round(4 * _S))
SHADOW = round(5 * _S)     # hard shadow, right and down, no blur
STEP = round(88 * _S)      # top of one line's box to the next
INSET = round(36 * _S)     # box to the panel's top and left edge
CUE_GAP = 14               # px between one caption and the next stacked under it (a comic's narration boxes)
TILT = 1.6                 # degrees, at most, either way
POP = 0.09                 # seconds: pop-in
POP_FROM = 0.85            # scale at the start of the pop-in
FADE_OUT = 0.1             # seconds: fade at the end of a caption followed by a gap (not before the next one)
INK, PAPER = (0, 0, 0, 255), (255, 255, 255, 255)
_TRAIL = re.compile(r"[.,!?;:]+$")


@lru_cache(maxsize=4)
def font(size: int = SIZE):
    from PIL import ImageFont
    return ImageFont.truetype(str(FONT), size)


def clean(line: str) -> str:
    """The text as lettered: capitals, no full stop or comma at the end of a box (the reel strips them)."""
    return _TRAIL.sub("", line.strip()).upper()


def measure(text: str) -> float:
    """Width in px of one line's box, border and padding included (what panel_captions fits to a panel)."""
    return font().getlength(clean(text)) + 2 * (PAD + BORDER)


def tilt_for(cue: dict) -> float:
    """A small fixed tilt per caption, the same on every render."""
    return (random.Random(f"{cue['k']}:{cue['t0']}").random() - 0.5) * 2 * TILT


def box_image(lines: list[str], tilt: float = 0.0):
    """The caption as one RGBA image: a box per line, stacked, shadows included, then tilted.

    Returns (image, (px, py), h): (px, py) is where the caption's left middle (its pivot) lies in the
    image, h the untilted height. Put the pivot at (x, y + h / 2) and the untilted top left is at (x, y)."""
    from PIL import Image, ImageDraw
    f = font()
    texts = [clean(l) for l in lines if clean(l)]
    widths = [round(f.getlength(t)) + 2 * (PAD + BORDER) for t in texts]
    w = max(widths) + SHADOW
    h = STEP * (len(texts) - 1) + BOX_H + SHADOW
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cap = -f.getbbox("H", anchor="ls")[1]                  # cap height: capitals are centred in the box
    for i, (t, bw) in enumerate(zip(texts, widths)):
        y = i * STEP
        d.rectangle([SHADOW, y + SHADOW, SHADOW + bw - 1, y + SHADOW + BOX_H - 1], fill=INK)
        d.rectangle([0, y, bw - 1, y + BOX_H - 1], fill=PAPER, outline=INK, width=BORDER)
        d.text((BORDER + PAD, y + (BOX_H + cap) / 2), t, font=f, fill=INK, anchor="ls")
    if abs(tilt) < 1e-3:
        return im, (0.0, h / 2), h
    # Turned about its left middle (the reel's transform origin), so the caption's left edge stays put.
    # Pillow turns counter-clockwise about the centre; with y pointing down a point (dx, dy) from the
    # centre goes to (dx cos a + dy sin a, -dx sin a + dy cos a).
    rot = im.rotate(tilt, resample=Image.BICUBIC, expand=True)
    a, lx = math.radians(tilt), -w / 2                     # the left middle, from the centre
    return rot, (lx * math.cos(a) + rot.width / 2, -lx * math.sin(a) + rot.height / 2), h


def height(cue: dict) -> int:
    """Untilted height of a caption, shadow included."""
    return STEP * (len(cue["lines"]) - 1) + BOX_H + SHADOW


def stack(cues: list[dict], t: float, panel_h: int, at: str = "top") -> list[tuple[dict, int]]:
    """The captions a panel shows at time t, as (cue, y from the panel's top): every caption of the panel
    that has started stays, the next one stacked under it CUE_GAP below, as narration boxes fill a comic
    panel. When the stack would not fit the panel, the oldest leave from the top. at "bottom": the stack
    sits on the panel's bottom edge instead (newest lowest), the older ones pushed up."""
    shown = [c for c in cues if c["t0"] <= t]
    room = panel_h - 2 * INSET
    while len(shown) > 1 and sum(height(c) for c in shown) + CUE_GAP * (len(shown) - 1) > room:
        shown.pop(0)
    out, y = [], INSET
    if at == "bottom":
        y = panel_h - INSET - (sum(height(c) for c in shown) + CUE_GAP * (len(shown) - 1))
    for c in shown:
        out.append((c, y))
        y += height(c) + CUE_GAP
    return out


def offset(cue: dict, panel_h: int) -> tuple[int, int]:
    """The caption's untilted top-left corner from its panel's top-left: INSET in from the left, and INSET
    down from the top, or (cue "at": "bottom") its last box INSET up from the bottom."""
    if cue.get("at") == "bottom":
        return INSET, panel_h - INSET - SHADOW - (STEP * (len(cue["lines"]) - 1) + BOX_H)
    return INSET, INSET


def pop(cue: dict, t: float) -> tuple[float, float]:
    """(scale, opacity) of a caption at video time t: pop-in at the start, a fade at the end when it has one."""
    u = (t - cue["t0"]) / POP
    if u < 1:
        e = 1 - (1 - max(0.0, u)) ** 3                     # power3.out, as the reel's GSAP ease
        scale, alpha = POP_FROM + (1 - POP_FROM) * e, e
    else:
        scale, alpha = 1.0, 1.0
    if cue.get("fade_out", True):
        alpha *= min(1.0, max(0.0, (cue["t1"] - t) / FADE_OUT))
    return scale, alpha


def paste(canvas, x: int, y: int, cue: dict, t: float, cached=None, dim: float = 0.0,
          paper: tuple = (243, 240, 232)) -> None:
    """Draw `cue` as it looks at time t, its untilted top-left corner at (x, y) on canvas (panel's top left
    plus INSET). `cached(key, make)` may cache the drawn caption (episode_panels passes its LRU)."""
    from PIL import Image
    key = ("caption", cue["k"], cue["t0"], tuple(cue["lines"]))
    make = lambda: box_image(cue["lines"], tilt_for(cue))
    im, (px, py), h = cached(key, make) if cached else make()
    scale, alpha = pop(cue, t)
    if alpha <= 0.004:
        return
    if scale < 0.999:                                      # grows from the pivot, which stays in place
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.BILINEAR)
        px, py = px * scale, py * scale
    if dim > 0.004:                                        # on a panel that is not narrated: faded with it
        a = im.getchannel("A")
        im = Image.blend(im.convert("RGB"), Image.new("RGB", im.size, paper), dim).convert("RGBA")
        im.putalpha(a)
    if alpha < 0.999:
        im = im.copy()
        im.putalpha(im.getchannel("A").point(lambda v: round(v * alpha)))
    canvas.paste(im, (round(x - px), round(y + h / 2 - py)), im)
