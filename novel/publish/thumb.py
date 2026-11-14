"""Episode thumbnail drawing for `novel thumb`: 1280x720, one face + a 2-3 line hook, same layout every episode.

Ported from the COI thumb.py (Pillow). Layout, colours, sizes and the hook markup are the original's; the
differences are the font (passed in, see fit), optional badge/tag, and the library API (no CLI, no paths).

Hook markup: "|" starts a new line, *word* is the yellow keyword. Each JPG is kept under 2 MB.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

W, H = 1280, 720
WHITE, YELLOW, RED, BLACK = (255, 255, 255), (255, 214, 10), (227, 36, 43), (0, 0, 0)
BOX_W, BOX_TOP, BOX_BOT = 610, 160, 670
MAX_BYTES = 2_000_000


class ThumbError(Exception):
    """A problem the user can fix: hook too long, file still too big, bad hook count."""


def cover(img, w, h, fx=0.5, fy=0.5):
    s = max(w / img.width, h / img.height)
    im = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
    x = min(max(round(im.width * fx - w / 2), 0), im.width - w)
    y = min(max(round(im.height * fy - h / 2), 0), im.height - h)
    return im.crop((x, y, x + w, y + h))


def background(art, focus, zoom, face):
    fx, fy = focus
    bg = cover(art, W, H, fx, fy).filter(ImageFilter.GaussianBlur(28))
    bg = ImageEnhance.Brightness(bg).enhance(0.42)
    # the art, big, with the face at 70 % (or 30 %) across and 40 % down
    s = round(H * zoom)
    a = art.resize((round(art.width * s / art.height), s), Image.LANCZOS)
    a = ImageEnhance.Contrast(a).enhance(1.12)
    a = ImageEnhance.Color(a).enhance(1.12)
    ox = round(W * (0.70 if face == "right" else 0.30) - fx * a.width)
    oy = round(H * 0.40 - fy * a.height)
    mask = Image.new("L", a.size, 255)
    md = ImageDraw.Draw(mask)
    feather = 240
    for i in range(feather):  # soft inner edge, so the art melts into the blurred copy
        v = round(255 * (i / feather) ** 1.6)
        x = i if face == "right" else a.width - 1 - i
        md.line([(x, 0), (x, a.height)], fill=v)
    bg.paste(a, (ox, oy), mask)
    return bg


def shade(img, face):
    """Dark ramp behind the text, red glow in the text corner, vignette."""
    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    for x in range(W):
        t = x / 700 if face == "right" else (W - 1 - x) / 700
        if t < 1:
            d.line([(x, 0), (x, H)], fill=(0, 0, 0, round(205 * (1 - t) ** 1.3)))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gx = 120 if face == "right" else W - 120
    ImageDraw.Draw(glow).ellipse([gx - 420, H - 260, gx + 420, H + 380], fill=RED + (120,))
    over = Image.alpha_composite(over, glow.filter(ImageFilter.GaussianBlur(110)))
    # spotlight: everything away from the face goes darker, so the face pops off a busy background
    vig = Image.new("L", (W, H), 0)
    cx = W * (0.70 if face == "right" else 0.30)
    ImageDraw.Draw(vig).ellipse([cx - 400, H * 0.40 - 380, cx + 400, H * 0.40 + 420], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(140))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    dark.putalpha(vig.point(lambda v: round((255 - v) * 0.62)))
    out = Image.alpha_composite(img.convert("RGBA"), over)
    return Image.alpha_composite(out, dark)


def parse(hook):
    """'THIS VILLAGE|IS *NOT NORMAL*' -> [[(text, colour), ...], ...]"""
    lines = []
    for raw in hook.upper().split("|"):
        segs = [(t, YELLOW if i % 2 else WHITE) for i, t in enumerate(re.split(r"\*", raw.strip())) if t]
        lines.append(segs)
    return lines


def fit(lines, box_w, box_h, font, top=190, low=80):
    """Largest size (top down to low, step 2) at which every line fits box_w and the block fits box_h.

    Returns (ImageFont, size, per-line widths). Raises ThumbError when even `low` does not fit.
    """
    for size in range(top, low - 1, -2):
        f = ImageFont.truetype(str(font), size)
        widths = [sum(f.getlength(t) for t, _ in segs) for segs in lines]
        if max(widths) <= box_w and size * 1.04 * len(lines) <= box_h:
            return f, size, widths
    raise ThumbError(f"hook too long for the thumbnail: {lines} (use 2-5 short words)")


def draw_text(img, x, y, segs, f, size, shadow=True):
    sw = max(5, round(size * 0.065))
    if shadow:
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd, cx = ImageDraw.Draw(sh), x
        for t, _ in segs:
            sd.text((cx + size * 0.05, y + size * 0.07), t, font=f, fill=(0, 0, 0, 170), stroke_width=sw, stroke_fill=(0, 0, 0, 170))
            cx += f.getlength(t)
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(size * 0.03)))
    d, cx = ImageDraw.Draw(img), x
    for t, col in segs:
        d.text((cx, y), t, font=f, fill=col, stroke_width=sw, stroke_fill=BLACK)
        cx += f.getlength(t)


def badges(img, ep_t, ch_t, face, font):
    """Red EP badge and yellow CH tag in the top corner. Either one may be None."""
    if not ep_t and not ch_t:
        return
    d = ImageDraw.Draw(img)
    f1, f2 = ImageFont.truetype(str(font), 66), ImageFont.truetype(str(font), 54)
    w1 = f1.getlength(ep_t) + 44 if ep_t else 0
    w2 = f2.getlength(ch_t) if ch_t else 0
    total = w1 + (22 if ep_t and ch_t else 0) + w2
    x = 46 if face == "right" else W - 46 - total
    if ep_t:
        d.rounded_rectangle([x, 38, x + w1, 38 + 88], radius=14, fill=RED, outline=BLACK, width=4)
        d.text((x + 22, 44), ep_t, font=f1, fill=WHITE)
    if ch_t:
        tag_x = x + w1 + 22 if ep_t else x
        draw_text(img, tag_x, 50, [(ch_t, YELLOW)], f2, 54, shadow=False)


def render(art, badge, tag, hook, focus, zoom, face, font):
    img = shade(background(art, focus, zoom, face), face)
    badges(img, badge, tag, face, font)
    lines = parse(hook)
    box_w, box_top, box_bot = BOX_W, BOX_TOP, BOX_BOT
    f, size, widths = fit(lines, box_w, box_bot - box_top, font)
    lh = size * 1.04
    y = box_top + (box_bot - box_top - lh * len(lines)) / 2 - size * 0.08
    for segs, w in zip(lines, widths):
        x = 52 if face == "right" else W - 52 - w
        draw_text(img, x, y, segs, f, size)
        y += lh
    return img.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))


def save(img, path):
    """JPEG under 2 MB: the highest quality step that fits. Returns the quality used."""
    for q in (92, 88, 84, 80, 75):
        img.save(path, quality=q, optimize=True)
        if path.stat().st_size < MAX_BYTES:
            return q
    raise ThumbError(f"{path} is still over 2 MB")


def make_thumbs(art, hooks, out, *, font, badge, tag, focus=(0.53, 0.40), zoom=1.5, face="right"):
    """Write thumb-a.jpg (b, c ...), one per hook in order, and sheet.jpg into `out`.

    Every hook is rendered before any file is written, so a bad hook writes nothing.
    Returns the written paths (the JPGs, then sheet.jpg).
    """
    if not 1 <= len(hooks) <= 3:
        raise ThumbError("use 1 to 3 hooks (YouTube Test & compare takes 3)")
    rows = [render(art, badge, tag, hook, focus, zoom, face, font) for hook in hooks]
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for i, img in enumerate(rows):
        p = out / f"thumb-{'abc'[i]}.jpg"
        save(img, p)
        written.append(p)
    # review sheet: full size at half scale + the phone-feed size
    sheet = Image.new("RGB", (640 + 24 + 246 + 48, len(rows) * 384 + 24), (24, 24, 24))
    for i, img in enumerate(rows):
        sheet.paste(img.resize((640, 360), Image.LANCZOS), (24, 24 + i * 384))
        sheet.paste(img.resize((246, 138), Image.LANCZOS), (24 + 640 + 24, 24 + i * 384))
    sheet_path = out / "sheet.jpg"
    sheet.save(sheet_path, quality=90)
    written.append(sheet_path)
    return written
