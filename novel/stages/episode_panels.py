"""The "comic panels" render style: a chapter's pictures laid out as comic pages.

Instead of one full-screen picture at a time, every picture of the chapter is a
panel on a comic page. All panels have the same height (ROW_H, about 85% of the
frame), so the camera never zooms: it rests on the panel being narrated (the
"active" one) and slides to the next, with an eased start and stop and a little
motion blur. The screen never shows anything beyond the current page: at the
end of a row the camera stops one margin from the page edge, so the panel sits
off-centre with its neighbours filling the rest. Pages are separate sheets: the
next page comes with a page flip. Only the active panel is in full colour and moving: the picture
moves INSIDE its panel (slide, pan, zoom in, zoom out). Every other panel is a
still, faded toward the paper colour.

Rules:
- Shape: from the scene's cast and shot (shape_for), so a lone character gets a
  narrow panel and a crowd or an establishing shot a wide one; no two
  neighbours share a shape (shapes). A scene's "panel" key overrides the rule.
- Layout: panels go in reading order into rows, each row justified to ROW_W by
  stretching its panels' shapes by one factor (the picture is cropped to the
  panel, so this costs nothing; a panel that would pass MAX_PANEL_W stops there
  and the others share the rest); row breaks keep the factors closest to 1.
  Rows stack two to a page, ROW_GAP apart (wider than the paper above and below
  a panel on screen, so the other row never peeks in at rest).
- Timing: the same cuts as episode_render.plan. Each cut is a slide within a
  page (0.5 s for a neighbour, up to 0.9 s for a far one) or a page flip
  (FLIP_SECONDS), centred on the cut.
- Inner move: runs from the start of the slide into the panel to the end of
  the slide out of it, so the active panel never freezes. Narrow panels mostly
  SWEEP the window across the picture: the scene's focus point was written
  before the picture was drawn and is often wrong, so a sweep is the one move
  that is sure to show the character.

Frames are drawn like episode_render's: each moving panel is ONE float affine
transform from a copy of its picture pre-scaled with Lanczos, so moves glide
sub-pixel instead of stepping. Frozen panels are cached stills. The page is
composed at whole-pixel camera positions and then shifted by the fractional
part as a whole, so panel edges never wobble against each other. During a slide,
BLUR_SAMPLES copies along a 180 degree shutter are averaged and a short box blur
along the motion fills the gaps between them (four bare copies showed as four
ghosts at ~25 px apart).

`frame(S, t)` draws one frame inside a worker process (state in S);
`page_images` writes one preview JPG per page for a person to check the crops.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from pathlib import Path

from .episode_render import FPS, H, LEAD, TAIL, W

# ------------------------------------------------------------------ shapes and layout constants

# Only two sizes for now: "wide" (the full 16:9 picture) and "landscape" (4:3, one size below it). The others
# are kept, switched off; to bring them back restore these three lines and the shape_for rules below.
# SHAPES = {"wide": 16 / 9, "landscape": 4 / 3, "square": 1.0, "portrait": 3 / 4, "tall": 2 / 3}   # width / height
# ALTERNATE = {"portrait": "tall", "tall": "portrait", "landscape": "square", "square": "landscape", "wide": "landscape"}
# NARROW = ("portrait", "tall")
SHAPES = {"wide": 16 / 9, "landscape": 4 / 3}          # width / height
ALTERNATE = {"landscape": "wide", "wide": "landscape"}
NARROW = ()                                            # no upright panels: NARROW_MOVES / MAX_PORTRAIT_PER_PAGE idle
SWITCHED_OFF = {"square": "landscape", "portrait": "landscape", "tall": "landscape"}   # a scene's old "panel" key

# One row per page; a panel is 95% of the screen (1026 of 1080 px high, a wide one 1823 of 1920 px wide), so
# the next panel peeks in by only GUTTER-wide paper plus the ~24 px of screen the wide panel leaves each side.
# Before (two rows per page, 94% high): ROW_H 1012, ROW_W 2750, GUTTER 30, ROW_GAP 40, PAGE_MARGIN 34, MAX_PANEL_W 1860.
ROW_H = 1026           # panel height: 27 px of paper above and below it on screen
ROW_W = 6400           # only the justified layout (_layout_justified, switched off) uses this
GUTTER = 24            # between the panels of a row
ROW_GAP = 14           # between rows: less than the 27 px left on screen, so the next row peeks in by ~13 px
BORDER = 4             # drawn inside the panel's rect
ROWS_PER_PAGE = 2
PAGE_PANELS = 6        # a sheet holds six pictures, in two rows of ROW_PANELS (three) each
ROW_PANELS = 3
# Rows of one sheet, picked at random (the same for the same chapter): a row may hold a single panel.
ASPECT_RANGE = {"wide": (1.62, 16 / 9), "landscape": (4 / 3, 1.5)}   # width / height a panel of this shape may take
ROW_PATTERNS = [(2, 2, 2), (3, 1, 2), (1, 3, 2), (2, 1, 3), (2, 3, 1), (1, 2, 3), (3, 2, 1)]
PAGE_MARGIN = 27       # paper around the panels: an edge panel gets the same margin as above and below
MAX_PANEL_W = 1824     # 48 px of the screen left each side of the widest panel: gutter plus a ~24 px peek
STRETCH_MIN, STRETCH_MAX = 0.82, 1.25   # one factor per row, applied to its panels' natural widths
MAX_ROW_PANELS = 6
MAX_PORTRAIT_PER_PAGE = 2   # a page holds at most this many upright panels (portrait or tall, see NARROW)
OUT_OF_LIMITS = 1e4    # row cost per (stretch factor's distance past a limit) squared
RAGGED_COST = 0.03     # a ragged last row: about a justified row stretched by 17%

# ------------------------------------------------------------------ motion constants

PICTURE_ASPECT = 16 / 9    # plan() assumes it; frame() re-clamps on each picture's real size
FIRST_HOLD = 2.0           # seconds the first panel holds still at the start (the chapter title shows)
SLIDE_NEAR = 0.5           # camera slide to the next panel in the row, seconds
SLIDE_ROW = 0.8            # to the start of the next row
SLIDE_PAGE = 0.9           # the longest slide (a far one)
FLIP_SECONDS = 0.9         # page flip to the next page
SLIDE_SHARE = 0.4          # a slide or flip never takes more than this share of either neighbouring span
MAX_SLIDE_SPEED = 170      # screen px/s for a window sweeping across the picture
NARROW_ZOOM_RATE = 0.02    # zoom change per second in a narrow panel
NARROW_ZMAX = 1.10
TILT_Z = 1.18              # tilt_down: zoomed in just enough to travel top to bottom (not in the move lists)
ZOOM_RATE = 0.02           # wider panels (episode_render uses 0.015)
PAN_RATE = 0.014           # centre travel per second, in picture widths (episode_render: 0.0112)
ZMAX = 1.10
# Every move STARTS at zoom 1 (the panel's largest window): a panel that opens already zoomed in looks
# cropped and soft, since a zoom past 1 enlarges the picture beyond its own pixels. So no zoom_out, pull or
# tilt_down (they start zoomed); zooms only go in, and gently (ZMAX). Narrow panels: every other one sweeps
# across the picture (see the docstring for why).
NARROW_MOVES = ["slide_right", "zoom_in", "slide_left", "slide_right", "zoom_in", "slide_left"]
MOVES = ["push", "pan_right", "push", "pan_left"]

# ------------------------------------------------------------------ drawing constants

BLUR_SAMPLES = 4           # camera positions averaged during a slide (gaps filled by a box blur)
SHUTTER = 0.5              # 180 degrees: they span half a frame interval
BORDER_RGB = (24, 22, 20)
FLIP_TILT = 0.2            # page flip: the fold's slant mid-turn, top to bottom, in screen widths
FLIP_CURL = 0.85           # the turned flap's width relative to a flat fold (a curled sheet looks narrower)
FLIP_SHADOW = 0.5          # darkness of the shadow on the new page beside the fold, mid-turn
CACHE_MB = 150             # per worker: pre-scaled pictures and panel stills
FONT = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "NotoSans-SemiBold.ttf"


# ------------------------------------------------------------------ shapes


def shape_for(scene: dict) -> str:
    """The panel shape for one scene: from its cast and shot, or its "panel" key."""
    if scene.get("panel") is not None:
        p = SWITCHED_OFF.get(scene["panel"], scene["panel"])
        if p not in SHAPES:
            raise SystemExit(f"scene {scene.get('id')}: panel {scene['panel']!r} is not one of "
                             f"{', '.join(SHAPES)}")
        return p
    n = len(scene.get("characters") or [])
    shot = scene.get("shot", "")
    # Switched off: one character -> "tall" (close-up, low-angle, high-angle) or "portrait"; no cast -> "square".
    if n == 2:
        return "wide" if shot == "over-the-shoulder" else "landscape"
    if n >= 3:
        return "wide"
    return "wide" if shot in ("establishing", "wide") else "landscape"


def shapes(scenes: list[dict]) -> list[str]:
    """shape_for each scene, then one left-to-right pass so no two neighbours share a shape."""
    names = [shape_for(s) for s in scenes]
    fixed = [s.get("panel") is not None for s in scenes]
    n = len(names)
    for i in range(1, n):
        if names[i] != names[i - 1]:
            continue
        alt_i, alt_p = ALTERNATE[names[i]], ALTERNATE[names[i - 1]]
        # Swapping i would only move the clash onto a fixed panel after it: then swap the one before.
        can_i = not fixed[i] and not (i + 1 < n and fixed[i + 1] and names[i + 1] == alt_i)
        can_p = not fixed[i - 1] and not (i >= 2 and names[i - 2] == alt_p)
        if can_i:
            names[i] = alt_i
        elif can_p:
            names[i - 1] = alt_p
        elif not fixed[i]:
            names[i] = alt_i
    return names


# ------------------------------------------------------------------ layout


def _row_fit(nat: list[float], last: bool, row_w: int = ROW_W) -> tuple[float, list[float], bool]:
    """(cost, panel widths, justified) for a row of panels with these natural widths.

    One stretch factor for the whole row; a panel it would make wider than MAX_PANEL_W stops there
    and the others share the rest (a wide panel beside a narrow one: the row still fills)."""
    room = row_w - (len(nat) - 1) * GUTTER
    cap_w = MAX_PANEL_W - 1                                # 1 px slack for rounding
    capped = [False] * len(nat)
    while True:
        free = sum(x for x, c in zip(nat, capped) if not c)
        f = (room - cap_w * sum(capped)) / free if free else 0.0
        over = [i for i, x in enumerate(nat) if not capped[i] and x * f > cap_w]
        if not over:
            break
        for i in over:
            capped[i] = True
    ragged = (RAGGED_COST, list(nat), False)              # the last row may stay at its natural widths
    if not free:                           # every panel at its widest and the row still not full
        return ragged if last else (1e6, [cap_w] * len(nat), False)
    widths = [cap_w if c else x * f for x, c in zip(nat, capped)]
    cost = sum((w / x - 1) ** 2 for w, x in zip(widths, nat)) / len(nat)
    # Outside the limits costs steeply, but not infinitely: some runs (landscape, tall, landscape)
    # have no break that keeps every row inside them, and a row a little past a limit looks far
    # better than a lone panel in a ragged row.
    cost += OUT_OF_LIMITS * max(0.0, STRETCH_MIN - f, f - STRETCH_MAX) ** 2
    if last and f > 1 and ragged[0] < cost:
        return ragged
    return cost, widths, True


def layout(shape_names: list[str], fixed: list[bool] | None = None) -> dict:
    """Panels and pages in world pixels (1 world px = 1 output px), all whole numbers.

    At most MAX_PORTRAIT_PER_PAGE upright panels (portrait or tall) per page: a page of them looks like
    a row of doors. An extra one (not fixed by a scene's "panel" key) is changed to square, landscape or
    wide, the first that differs from its neighbours, and the layout is run again because the page
    breaks move. The final shapes are returned as "shapes"."""
    for s in shape_names:
        if s not in SHAPES:
            raise SystemExit(f"unknown panel shape {s!r}: one of {', '.join(SHAPES)}")
    names = list(shape_names)
    fixed = fixed or [False] * len(names)
    while True:
        lay = _layout(names)
        over = _excess_portraits(lay, fixed)
        if not over:
            lay["shapes"] = names
            return lay
        for k in over:               # each pass turns at least one upright panel into a wider one
            left = names[k - 1] if k > 0 else None
            right = names[k + 1] if k + 1 < len(names) else None
            names[k] = next(s for s in ("square", "landscape", "wide") if s not in (left, right))


def _excess_portraits(lay: dict, fixed: list[bool]) -> list[int]:
    """Scene indexes of the upright panels past the first MAX_PORTRAIT_PER_PAGE on each page."""
    by_page: dict[int, list[dict]] = {}
    for p in lay["panels"]:
        if p["shape"] in NARROW:
            by_page.setdefault(p["page"], []).append(p)
    over = []
    for ps in by_page.values():
        ps.sort(key=lambda p: (p["row"], p["x"]))
        over += [p["k"] for p in ps[MAX_PORTRAIT_PER_PAGE:] if not fixed[p["k"]]]
    return over


def _layout(shape_names: list[str]) -> dict:
    """Sheets of PAGE_PANELS panels in two rows of three. Panels vary in width (an aspect drawn within their
    shape's range; the picture is cropped to it) and every full row is stretched to the sheet's width (a wide
    one at most MAX_PANEL_W). A picture left alone at the very end gets a sheet of its own: full screen, no
    border ("full": True). The same chapter always gets the same layout."""
    import random
    rng = random.Random(f"{len(shape_names)}:{''.join(s[0] for s in shape_names)}")
    n = len(shape_names)
    panels, pages, r, k = [], [], 0, 0
    while k < n:
        page = len(pages)
        px = sum(p["w"] for p in pages)
        if k == n - 1:                                     # one picture left: full screen
            pages.append({"x": px, "y": 0, "w": W, "h": H, "panels": [k]})
            panels.append({"k": k, "shape": "wide", "x": px, "y": 0, "w": W, "h": H, "row": r, "page": page,
                           "full": True})
            break
        last = min(n, k + PAGE_PANELS)
        if last == n and (n - k) % ROW_PANELS == 1:        # a last row of one picture: that one goes full screen
            last = n - 1
        rows = []
        while k < last:
            c = min(ROW_PANELS, last - k)
            nat = [round(ROW_H * rng.uniform(*ASPECT_RANGE[s])) for s in shape_names[k:k + c]]
            rows.append((k, nat))
            k += c
        page_w = max(sum(w) + (len(w) - 1) * GUTTER for _, w in rows)
        rows = [(k0, [round(x) for x in _row_fit(nat, False, page_w)[1]]) for k0, nat in rows]   # fill the sheet's width
        page_w = max(W, page_w + 2 * PAGE_MARGIN)
        page_h = max(H, len(rows) * ROW_H + (len(rows) - 1) * ROW_GAP + 2 * PAGE_MARGIN)
        pages.append({"x": px, "y": 0, "w": page_w, "h": page_h, "panels": []})
        for m, (k0, widths) in enumerate(rows):
            x = px + PAGE_MARGIN
            y = PAGE_MARGIN + m * (ROW_H + ROW_GAP)
            for i, w in enumerate(widths):
                panels.append({"k": k0 + i, "shape": shape_names[k0 + i], "x": x, "y": y, "w": w, "h": ROW_H,
                               "row": r, "page": page})
                pages[page]["panels"].append(k0 + i)
                x += w + GUTTER
            r += 1
    return {"panels": panels, "pages": pages}


def _layout_justified(shape_names: list[str]) -> dict:
    """The earlier layout (switched off): rows justified to ROW_W by one stretch factor, two rows a sheet."""
    nat = [SHAPES[s] * ROW_H for s in shape_names]
    n = len(nat)
    # Row breaks: a small dynamic program over where each row ends.
    best, back = [0.0] + [math.inf] * n, [0] * (n + 1)
    for j in range(1, n + 1):
        for i in range(max(0, j - MAX_ROW_PANELS), j):
            c = best[i] + _row_fit(nat[i:j], j == n)[0]
            if c < best[j]:
                best[j], back[j] = c, i
    rows, j = [], n
    while j > 0:
        rows.append((back[j], j))
        j = back[j]
    rows.reverse()

    # Each page is its own sheet (pages change with a page flip, never side by side on screen); they
    # simply sit edge to edge in one coordinate space.
    page_w = ROW_W + 2 * PAGE_MARGIN
    page_h = ROWS_PER_PAGE * ROW_H + (ROWS_PER_PAGE - 1) * ROW_GAP + 2 * PAGE_MARGIN
    panels, pages = [], []
    for r, (i, j) in enumerate(rows):
        _, widths, justified = _row_fit(nat[i:j], j == n)
        page = r // ROWS_PER_PAGE
        if page == len(pages):
            pages.append({"x": page * page_w, "y": 0, "w": page_w, "h": page_h, "panels": []})
        x0 = pages[page]["x"] + PAGE_MARGIN
        y = pages[page]["y"] + PAGE_MARGIN + (r % ROWS_PER_PAGE) * (ROW_H + ROW_GAP)
        # Edges from rounded running sums: the widths add up exactly, gutters stay GUTTER.
        acc = 0.0
        for m, k in enumerate(range(i, j)):
            left = round(acc) + m * GUTTER
            acc += widths[m]
            right = round(acc) + m * GUTTER
            if justified and k == j - 1:
                right = ROW_W
            panels.append({"k": k, "shape": shape_names[k], "x": x0 + left, "y": y, "w": right - left,
                           "h": ROW_H, "row": r, "page": page})
            pages[page]["panels"].append(k)
    return {"panels": panels, "pages": pages}


# ------------------------------------------------------------------ plan


def _base_window(a: float, aspect: float) -> tuple[float, float]:
    """The largest window of aspect a in a picture of this aspect, as fractions (width, height)."""
    return (1.0, aspect / a) if a >= aspect else (a / aspect, 1.0)


def _inside(c: float, half: float) -> float:
    return min(max(c, half), 1 - half)


def _move(kind: str, a: float, w: int, focus, t0: float, t1: float) -> dict:
    """An inner move in normalised terms: zoom z >= 1 relative to the largest window of the
    panel's shape, centre (x, y) as fractions of the picture; both linear in time."""
    fx, fy = focus
    bw, bh = _base_window(a, PICTURE_ASPECT)
    d = max(0.0, t1 - t0)
    if kind in ("slide_right", "slide_left"):
        # Full picture height, as far across as MAX_SLIDE_SPEED allows: w / bw screen px per picture width.
        full = 1 - bw
        travel = min(full, MAX_SLIDE_SPEED * d / (w / bw))
        x0 = min(max(fx - travel / 2, bw / 2), 1 - bw / 2 - travel)
        y = _inside(fy, bh / 2)
        z, c = (1.0, 1.0), ((x0, y), (x0 + travel, y))
    elif kind in ("zoom_in", "zoom_out"):
        z1 = min(NARROW_ZMAX, 1 + NARROW_ZOOM_RATE * d)
        z = (1.0, z1)
        c = ((_inside(fx, bw / 2), _inside(0.5, bh / 2)), (_inside(fx, bw / 2 / z1), _inside(fy, bh / 2 / z1)))
    elif kind == "tilt_down":
        z = (TILT_Z, TILT_Z)
        hh = bh / 2 / TILT_Z
        x = _inside(fx, bw / 2 / TILT_Z)
        c = ((x, hh), (x, 1 - hh))
    elif kind in ("push", "pull"):
        z1 = min(ZMAX, 1 + ZOOM_RATE * d)
        z = (1.0, z1)
        c = ((_inside(0.5, bw / 2), _inside(0.5, bh / 2)), (_inside(fx, bw / 2 / z1), _inside(fy, bh / 2 / z1)))
    else:                                                  # pan_right, pan_left
        # Starts at zoom 1 and zooms in just enough, while it travels, to have room to move across.
        travel = PAN_RATE * d
        zz = min(ZMAX, max(1.04, bw / max(1e-6, 1 - travel) + 0.01))
        half = bw / 2 / zz
        travel = max(0.0, min(travel, 1 - 2 * half))
        x0 = min(max(fx - travel / 2, half), 1 - half - travel)
        z = (1.0, zz)
        c = ((_inside(x0, bw / 2), _inside(fy, bh / 2)), (x0 + travel, _inside(fy, bh / 2 / zz)))
    if kind in ("slide_left", "zoom_out", "pull"):
        z, c = z[::-1], c[::-1]
    elif kind == "pan_left":                               # travels the other way, still zooming IN
        (x0, y0), (x1, y1) = c
        hz = bw / 2 / z[1]
        c = ((_inside(x1, bw / 2), y0), (min(max(x0, hz), 1 - hz), y1))
    return {"kind": kind, "t0": t0, "t1": t1, "z": list(z), "c": [list(c[0]), list(c[1])]}


def plan(starts: list[float], audio: float, scenes: list[dict]) -> dict:
    """Panels, pages, camera slides and inner moves of the chapter, in video seconds."""
    total = LEAD + audio + TAIL
    cuts = [0.0] + [LEAD + s for s in starts[1:]] + [total]
    n = len(cuts) - 1
    if n != len(scenes):
        raise SystemExit(f"{len(scenes)} scenes but {n} narration windows")
    for k in range(n):
        span = cuts[k + 1] - cuts[k]
        if span < 2.0:
            raise SystemExit(f"image {k + 1} would be on screen for only {span:.1f}s")
    lay = layout(shapes(scenes), fixed=[s.get("panel") is not None for s in scenes])
    panels = [dict(p) for p in lay["panels"]]
    for p, s in zip(panels, scenes):
        pg = lay["pages"][p["page"]]
        p["id"] = s.get("id", p["k"] + 1)
        p["cx"], p["cy"] = p["x"] + p["w"] // 2, p["y"] + p["h"] // 2
        # The camera at rest: on the panel's centre, but never showing anything beyond its page, so a
        # panel at the end of a row sits one margin from the screen edge. Whole pixels: holds are sharp.
        p["cam"] = [min(max(p["cx"], pg["x"] + W // 2), pg["x"] + pg["w"] - W // 2),
                    min(max(p["cy"], pg["y"] + H // 2), pg["y"] + pg["h"] - H // 2)]
        p["t0"], p["t1"] = cuts[p["k"]], cuts[p["k"] + 1]

    slides, flips, trans = [], [], []          # trans[k]: the transition from panel k to k + 1
    for k in range(n - 1):
        a, b = panels[k], panels[k + 1]
        if a["page"] != b["page"]:
            dur = FLIP_SECONDS
        else:
            dist = math.hypot(b["cam"][0] - a["cam"][0], b["cam"][1] - a["cam"][1])
            floor = SLIDE_ROW if a["row"] != b["row"] else SLIDE_NEAR
            dur = min(SLIDE_PAGE, max(floor, SLIDE_NEAR + (dist - 1100) / 2750))   # grows with distance
        dur = min(dur, SLIDE_SHARE * (cuts[k + 1] - cuts[k]), SLIDE_SHARE * (cuts[k + 2] - cuts[k + 1]))
        tr = {"from": k, "to": k + 1, "t0": cuts[k + 1] - dur / 2, "t1": cuts[k + 1] + dur / 2}
        (flips if a["page"] != b["page"] else slides).append(tr)
        trans.append(tr)

    narrow = other = 0
    for k, p in enumerate(panels):
        # The first panel holds still while the chapter title is read, so the eye is on the title first.
        m0 = trans[k - 1]["t0"] if k > 0 else 0.0
        if k == 0:
            m0 = min(FIRST_HOLD, (trans[0]["t1"] if n > 1 else total) / 2)
        m1 = trans[k]["t1"] if k < n - 1 else total
        if p["shape"] in NARROW:
            kind = NARROW_MOVES[narrow % len(NARROW_MOVES)]
            narrow += 1
        else:
            kind = MOVES[other % len(MOVES)]
            other += 1
        fx, fy = (scenes[k].get("focus") or [0.5, 0.5])[:2]
        p["focus"] = [min(1.0, max(0.0, float(fx))), min(1.0, max(0.0, float(fy)))]
        p["move"] = _move(kind, p["w"] / p["h"], p["w"], p["focus"], m0, m1)
    return {"total": total, "cuts": cuts, "panels": panels, "pages": lay["pages"], "slides": slides,
            "flips": flips}


CAPTION_CLEAR = 0.12       # the subject's focus point this near a caption (in panel heights) moves it


def focus_in_panel(pan: dict, t: float) -> tuple[float, float]:
    """Where the scene's focus point is inside the panel at time t, as fractions of the panel (0..1 when
    visible): the inner move's window at that moment, as _window draws it (assuming a 16:9 picture)."""
    mv = pan["move"]
    q = 0.0 if mv["t1"] <= mv["t0"] else min(1.0, max(0.0, (t - mv["t0"]) / (mv["t1"] - mv["t0"])))
    z = mv["z"][0] + (mv["z"][1] - mv["z"][0]) * q
    (ax, ay), (bx, by) = mv["c"]
    cx, cy = ax + (bx - ax) * q, ay + (by - ay) * q
    bw, bh = _base_window(pan["w"] / pan["h"], PICTURE_ASPECT)
    ww, wh = bw / z, bh / z
    fx, fy = pan["focus"]
    return (fx - (cx - ww / 2)) / ww, (fy - (cy - wh / 2)) / wh


CALMER = 0.7              # the bottom-left area must have at most this share of the top-left's detail to win
DETAIL_W = 384            # pictures are judged at this width (grey, gradient magnitude)


def _detail_map(path: str, cache: dict):
    """A small grey copy of the picture as its gradient magnitude: high on faces, hair, figures and clutter,
    low on sky, fog and plain ground."""
    if path not in cache:
        import numpy as np
        from PIL import Image
        im = Image.open(path).convert("L")
        im = im.resize((DETAIL_W, max(1, round(im.height * DETAIL_W / im.width))), Image.BILINEAR)
        a = np.asarray(im, dtype=np.float32)
        g = np.zeros_like(a)
        g[:, 1:] += np.abs(np.diff(a, axis=1))
        g[1:, :] += np.abs(np.diff(a, axis=0))
        cache[path] = g
    return cache[path]


def _region_detail(g, pan: dict, t: float, u0: float, v0: float, u1: float, v1: float) -> float:
    """Mean detail of the panel area (u0, v0)-(u1, v1) (fractions of the panel) at time t."""
    mv = pan["move"]
    q = 0.0 if mv["t1"] <= mv["t0"] else min(1.0, max(0.0, (t - mv["t0"]) / (mv["t1"] - mv["t0"])))
    z = mv["z"][0] + (mv["z"][1] - mv["z"][0]) * q
    (ax, ay), (bx, by) = mv["c"]
    cx, cy = ax + (bx - ax) * q, ay + (by - ay) * q
    bw, bh = _base_window(pan["w"] / pan["h"], PICTURE_ASPECT)
    ww, wh = bw / z, bh / z
    gh, gw = g.shape
    x0, x1 = (cx - ww / 2 + u0 * ww) * gw, (cx - ww / 2 + u1 * ww) * gw
    y0, y1 = (cy - wh / 2 + v0 * wh) * gh, (cy - wh / 2 + v1 * wh) * gh
    x0, x1 = int(max(0, min(gw - 1, x0))), int(max(1, min(gw, math.ceil(x1))))
    y0, y1 = int(max(0, min(gh - 1, y0))), int(max(1, min(gh, math.ceil(y1))))
    part = g[y0:max(y1, y0 + 1), x0:max(x1, x0 + 1)]
    return float(part.mean()) if part.size else 0.0


def place_captions(pl: dict, cues: list[dict], images: list | None = None) -> list[dict]:
    """Set each caption's "at": "top" (the top left of its panel, as the reel's narration boxes) or "bottom"
    (bottom left), so a caption covers as little of the picture's subject as it can.

    With the pictures: the area each placement would cover is judged at the caption's middle moment (the
    inner move's window then); faces, hair and figures are full of detail, sky and fog are not. The bottom
    is chosen only when it is clearly calmer (CALMER): the top is the comic convention. Without pictures:
    the scene's focus point decides (bottom when it would sit under the top caption); it is often the
    default guess, so that is the weaker rule."""
    from . import panel_lettering as PL
    maps: dict = {}
    by_k: dict = {}
    for c in cues:
        by_k.setdefault(c["k"], []).append(c)
    # Captions stack up in their panel, so the choice is made once per panel, for the area the whole stack
    # covers, judged at the panel's middle caption.
    for k, mine in by_k.items():
        mid = mine[len(mine) // 2]
        c = {"k": k, "t0": mid["t0"], "t1": mid["t1"], "lines": [l for x in mine for l in x["lines"]]}
        pan = pl["panels"][k]
        tm = (c["t0"] + c["t1"]) / 2
        bw = max(PL.measure(l) for l in c["lines"]) + PL.SHADOW
        bh = min(pan["h"] - 2 * PL.INSET,
                 sum(PL.height(x) for x in mine) + PL.CUE_GAP * (len(mine) - 1))
        w, h = pan["w"], pan["h"]
        u0, u1 = PL.INSET / w, min(1.0, (PL.INSET + bw) / w)
        if images:
            g = _detail_map(str(images[c["k"]]), maps)
            top = _region_detail(g, pan, tm, u0, PL.INSET / h, u1, (PL.INSET + bh) / h)
            bottom = _region_detail(g, pan, tm, u0, 1 - (PL.INSET + bh) / h, u1, 1 - PL.INSET / h)
            c["at"] = "bottom" if bottom < CALMER * top else "top"
        else:
            u, v = focus_in_panel(pan, tm)
            m = CAPTION_CLEAR * h
            under = (PL.INSET - m <= u * w <= PL.INSET + bw + m) and (v * h <= PL.INSET + bh + m)
            c["at"] = "bottom" if under else "top"
        for x in mine:
            x["at"] = c["at"]
    return cues


def slide_windows(pl: dict) -> list[tuple[float, float]]:
    """(t0, t1) of every camera slide and page flip, in time order: where a big frame-to-frame
    change is by design."""
    return sorted((s["t0"], s["t1"]) for s in pl["slides"] + pl.get("flips", []))


def _transition(pl: dict, t: float, strict: bool = False):
    """(kind, transition) under way at t: ("slide", s), ("flip", f) or (None, None)."""
    for kind in ("slides", "flips"):
        for s in pl.get(kind, []):
            if (s["t0"] < t < s["t1"]) if strict else (s["t0"] <= t <= s["t1"]):
                return kind[:-1], s
    return None, None


def _ease(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)                             # smoothstep: zero speed at both ends


def _active(pl: dict, t: float) -> int:
    """The panel being narrated at t: the last cut at or before t."""
    cuts = pl["cuts"]
    lo, hi = 0, len(cuts) - 2
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cuts[mid] <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo


def camera(pl: dict, t: float):
    """(centre x, centre y, the slide under way or None, its eased progress). During a page flip:
    the camera of the page on top for the first half, then of the page underneath."""
    kind, tr = _transition(pl, t)
    if kind == "slide":
        p = _ease((t - tr["t0"]) / (tr["t1"] - tr["t0"]))
        (ax, ay), (bx, by) = pl["panels"][tr["from"]]["cam"], pl["panels"][tr["to"]]["cam"]
        return ax + (bx - ax) * p, ay + (by - ay) * p, tr, p
    if kind == "flip":
        k = tr["from"] if t < (tr["t0"] + tr["t1"]) / 2 else tr["to"]
    else:
        k = _active(pl, t)
    cx, cy = pl["panels"][k]["cam"]
    return cx, cy, None, 0.0


# ------------------------------------------------------------------ drawing (worker processes)


def _nbytes(im) -> int:
    if isinstance(im, tuple):                              # a caption: (image, pivot, height)
        im = im[0]
    return im.width * im.height * len(im.getbands())


def _cached(S: dict, key, make):
    """LRU inside S, bounded by bytes so a worker stays well under ~300 MB."""
    cache = S.setdefault("panel_cache", OrderedDict())
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    im = cache[key] = make()
    used = S.get("panel_cache_bytes", 0) + _nbytes(im)
    while used > CACHE_MB * 1e6 and len(cache) > 1:
        _, old = cache.popitem(last=False)
        used -= _nbytes(old)
    S["panel_cache_bytes"] = used
    return im


def _paper(S: dict) -> tuple[int, int, int]:
    from PIL import ImageColor
    return ImageColor.getrgb(S.get("paper") or "#f3f0e8")[:3]


def _size(S: dict, k: int) -> tuple[int, int]:
    from PIL import Image
    sizes = S.setdefault("panel_sizes", {})
    if k not in sizes:
        with Image.open(S["images"][k]) as im:             # reads the header only
            sizes[k] = im.size
    return sizes[k]


def _window(S: dict, pan: dict, p: float, box=None):
    """Panel `pan` at move progress p (0..1): its picture window, one bilinear affine transform from a
    copy pre-scaled so that at the move's largest zoom it maps 1:1 onto the panel (the transform only
    ever shrinks a little). box = (u0, v0, u1, v1) in panel pixels draws just that part."""
    from PIL import Image
    k, w, h, mv = pan["k"], pan["w"], pan["h"], pan["move"]
    z0, z1 = mv["z"]
    z = z0 + (z1 - z0) * p
    (ax, ay), (bx, by) = mv["c"]
    cx, cy = ax + (bx - ax) * p, ay + (by - ay) * p
    zmax = max(z0, z1)
    iw, ih = _size(S, k)
    a = w / h
    f = w * zmax / (iw if a >= iw / ih else ih * a)

    def scaled():
        full = Image.open(S["images"][k]).convert("RGB")
        return full.resize((max(1, round(iw * f)), max(1, round(ih * f))), Image.LANCZOS)
    src = _cached(S, ("src", k, round(f, 5)), scaled)
    sw, sh = src.size
    bw = sw if a >= sw / sh else sh * a                   # the panel-shaped window at z = 1, source px
    ww, wh = bw / z, bw / a / z
    s = w / ww                                            # output px per source px: z / zmax, <= 1
    x0 = min(max(cx * sw - ww / 2, 0.0), sw - ww)         # the real picture's size: clamp again
    y0 = min(max(cy * sh - wh / 2, 0.0), sh - wh)
    u0, v0, u1, v1 = box or (0, 0, w, h)
    return src.transform((u1 - u0, v1 - v0), Image.AFFINE,
                         (1 / s, 0, x0 + (u0 + 0.5) / s - 0.5, 0, 1 / s, y0 + (v0 + 0.5) / s - 0.5),
                         resample=Image.BILINEAR)


def _faded(im, paper, wgt: float):
    from PIL import Image
    return Image.blend(im, Image.new("RGB", im.size, paper), wgt) if wgt > 0 else im


def _compose(S: dict, t: float, mx: int = 0, my: int = 0):
    """One sharp view at time t (a hold or a slide), W x H plus a margin of mx, my px on each side."""
    pl = S["panels"]
    dim = S.get("dim", 0.45)
    cam_x, cam_y, sl, p = camera(pl, t)
    k = sl["from"] if sl else _active(pl, t)
    weights = {sl["from"]: dim * p, sl["to"]: dim * (1 - p)} if sl else {k: 0.0}
    return _view(S, t, (cam_x, cam_y), weights, pl["panels"][k]["page"], mx, my)


def _view(S: dict, t: float, cam, weights: dict, page: int, mx: int = 0, my: int = 0):
    """The panels of one page seen from camera centre `cam`; fade weight `dim` unless in `weights`."""
    from PIL import Image, ImageDraw
    pl = S["panels"]
    dim = S.get("dim", 0.45)
    paper = _paper(S)
    cam_x, cam_y = cam
    ox, oy = cam_x - W / 2 - mx, cam_y - H / 2 - my
    ix, iy = math.floor(ox), math.floor(oy)
    fx, fy = ox - ix, oy - iy
    # A fractional camera: compose one pixel larger at whole pixels, then shift the whole frame at once.
    sub = fx > 1e-4 or fy > 1e-4
    ow, oh = W + 2 * mx, H + 2 * my
    cw, ch = ow + sub, oh + sub
    canvas = _cached(S, ("paper", cw, ch, paper), lambda: Image.new("RGB", (cw, ch), paper)).copy()
    draw = ImageDraw.Draw(canvas)
    for pan in (pl["panels"][k] for k in pl["pages"][page]["panels"]):    # one sheet: never its neighbours
        x, y, w, h = pan["x"] - ix, pan["y"] - iy, pan["w"], pan["h"]
        if x >= cw or y >= ch or x + w <= 0 or y + h <= 0:
            continue
        k, mv = pan["k"], pan["move"]
        wgt = weights.get(k, dim)
        if mv["t0"] <= t <= mv["t1"] and mv["t1"] > mv["t0"]:
            q = (t - mv["t0"]) / (mv["t1"] - mv["t0"])
            box = (max(0, -x), max(0, -y), min(w, cw - x), min(h, ch - y))
            canvas.paste(_faded(_window(S, pan, q, box), paper, wgt), (x + box[0], y + box[1]))
        else:                                              # frozen on its first or last frame
            end = 0.0 if t < mv["t0"] else 1.0
            still = _cached(S, ("still", k, end, round(wgt, 4), paper),
                            lambda: _faded(_window(S, pan, end), paper, wgt))
            canvas.paste(still, (x, y))
        if not pan.get("full"):
            draw.rectangle([x, y, x + w - 1, y + h - 1], outline=BORDER_RGB, width=BORDER)
    if S.get("cues"):                                      # captions, lettered on their panels: they move with them
        from . import panel_lettering as PL
        if "cues_by_k" not in S:
            by_k: dict = {}
            for c in S["cues"]:
                by_k.setdefault(c["k"], []).append(c)
            S["cues_by_k"] = by_k
        for k in pl["pages"][page]["panels"]:
            pan = pl["panels"][k]
            mine = S["cues_by_k"].get(k)
            if not mine or t < mine[0]["t0"]:
                continue
            for cue, y in PL.stack(mine, t, pan["h"], mine[0].get("at", "top")):
                PL.paste(canvas, pan["x"] - ix + PL.INSET, pan["y"] - iy + y, cue, t,
                         lambda key, make: _cached(S, key, make), weights.get(k, dim), paper)
    if sub:
        canvas = canvas.transform((ow, oh), Image.AFFINE, (1, 0, fx, 0, 1, fy), resample=Image.BILINEAR)
    return canvas


def _box(a, n: int, axis: int):
    """Mean of every n consecutive values along axis (the result is n - 1 shorter), uint16 in and out.

    The running sum is uint16 and wraps around, which is harmless: a window's sum is a difference of two
    running sums and is right modulo 65536, and each window sum is below 65536 (n <= 64, values <= 1020).
    Half the memory of int32 arithmetic on a frame-sized array."""
    import numpy as np
    if n <= 1:
        return a
    c = np.cumsum(a, axis=axis, dtype=np.uint16)
    if axis == 0:
        out = c[n - 1:].copy()
        out[1:] -= c[:-n]
    else:
        out = c[:, n - 1:].copy()
        out[:, 1:] -= c[:, :-n]
    del c
    out += n // 2
    out //= n
    return out


def _turn(old, new, u: float, paper):
    """The page flip at progress u (0..1, linear in time): `old` is the top sheet, its right edge lifting
    and travelling left; `new` lies underneath and is revealed right of the fold.

    A screen-space model of a sheet folding back on itself: per row, a fold line (slanted, so the
    bottom corner leads, as when a hand turns a page) and the sheet's free edge, which lies left of the
    fold by a little less than the width already turned (the curl foreshortens it). Between them the
    sheet's back shows: plain paper, darker into the crease, a soft highlight beside it, lighter toward
    the free edge, so it reads as a curved sheet. The new page gets a soft shadow beside the fold and the
    old one a faint one under the free edge.

    Cost: only the columns the effect touches are computed; the two 1 px boundaries are exact at full
    resolution, the smooth shading at half resolution (scaled up), and the colour work is Pillow's."""
    import numpy as np
    from PIL import Image, ImageChops
    p = _ease(u)
    lift = max(0.0, math.sin(math.pi * u))                 # 0 at both ends: the first and last frames are plain
    sig = 20 + 110 * lift                                  # shadow width: widest mid-turn, when the sheet is high

    def lines(y):                                          # fold and free edge at row centres y (px)
        fold = W * (1 - p) + FLIP_TILT * W * lift * (0.5 - y / H)
        return fold, fold - FLIP_CURL * (W - fold)

    fold, edge = lines(np.arange(H, dtype=np.float32)[:, None] + 0.5)
    c0 = int(min(W, max(0, math.floor(float(edge.min())) - 120)))
    c1 = int(max(c0, min(W, math.ceil(float(fold.max()) + 6 * sig))))
    out = new.copy()
    if c0 > 0:
        out.paste(old.crop((0, 0, c0, H)), (0, 0))
    if c1 <= c0:
        return out
    bw = c1 - c0
    L = lambda a, size=None: (lambda im: im.resize(size, Image.BILINEAR) if size else im)(
        Image.fromarray(np.clip(a * 255 + 0.5, 0, 255).astype(np.uint8), "L"))
    # The boundaries, soft over 1 px: right of the fold the new page, between edge and fold the flap.
    # Built straight as 0..255 bytes (three passes each): they are the only full-resolution arrays.
    x255 = (np.arange(c0, c1, dtype=np.float32)[None, :] + 1.0) * 255   # (x + 0.5 + 0.5) * 255

    def step(line):
        m = x255 - line * 255
        return np.clip(m, 0, 255, out=m).astype(np.uint8)
    mB = step(fold)
    mFlap = np.minimum(step(edge), 255 - mB)
    # Shading on a grid of 2 x 2 blocks.
    hx = np.arange((bw + 1) // 2, dtype=np.float32)[None, :] * 2 + c0 + 1
    hfold, hedge = lines(np.arange((H + 1) // 2, dtype=np.float32)[:, None] * 2 + 1)
    dF, dE = hx - hfold, hx - hedge                        # > 0: right of the fold / of the free edge
    shB = 1 - FLIP_SHADOW * math.sqrt(lift) * np.exp(-np.maximum(dF, 0) / sig)
    shA = 1 - 0.3 * lift * np.exp(-np.maximum(-dE, 0) / 18)
    v = np.clip(-dF / np.maximum(hfold - hedge, 1), 0, 1)  # across the back: 0 at the fold, 1 at the edge
    shade = 0.72 + 0.26 * v * v * (3 - 2 * v) + 0.14 * np.exp(-((v - 0.16) / 0.07) ** 2)
    shade *= (1 - 0.35 * np.exp(-np.maximum(-dF, 0) / 5)) * (1 - 0.2 * np.exp(-np.maximum(dE, 0) / 2))
    back = Image.fromarray(np.clip(shade[..., None] * np.array(paper, np.float32) + 0.5, 0, 255)
                           .astype(np.uint8), "RGB").resize((bw, H), Image.BILINEAR)
    size = (bw, H)
    a = ImageChops.multiply(old.crop((c0, 0, c1, H)), L(shA, size).convert("RGB"))
    b = ImageChops.multiply(new.crop((c0, 0, c1, H)), L(shB, size).convert("RGB"))
    mid = Image.composite(back, Image.composite(b, a, Image.fromarray(mB, "L")), Image.fromarray(mFlap, "L"))
    out.paste(mid, (c0, 0))
    return out


def frame(S: dict, t: float):
    """The frame at video time t (RGB, W x H). S: this worker's state ("panels" = plan(), "images",
    "paper", "dim"); caches are kept in it.

    During a page flip: the old and the new screen, each a hold, combined by _turn.

    During a slide: motion blur over a 180 degree shutter centred on t. BLUR_SAMPLES copies of the page,
    each where the camera is at its point of the shutter, are averaged, and a box blur one copy-spacing
    long along the motion fills the gaps between them, so a fast slide smears instead of showing four
    ghosts (at peak speed the camera moves ~25 px between copies). The copies are shifts of ONE view
    composed at t: during a slide only the camera moves fast enough to matter."""
    from PIL import Image
    import numpy as np
    pl = S["panels"]
    t = min(max(t, 0.0), pl["total"])
    kind, sl = _transition(pl, t, strict=True)
    if kind == "flip":                       # each screen is a plain hold on its own page, moving inside
        a, b = pl["panels"][sl["from"]], pl["panels"][sl["to"]]
        old = _view(S, t, a["cam"], {a["k"]: 0.0}, a["page"])
        new = _view(S, t, b["cam"], {b["k"]: 0.0}, b["page"])
        return _turn(old, new, (t - sl["t0"]) / (sl["t1"] - sl["t0"]), _paper(S))
    if sl is None:
        return _compose(S, t)
    half = SHUTTER / FPS / 2
    xa, ya = camera(pl, max(sl["t0"], t - half))[:2]
    xb, yb = camera(pl, min(sl["t1"], t + half))[:2]
    dx, dy = xb - xa, yb - ya
    n = BLUR_SAMPLES
    # Copy offsets, rounded symmetrically so their average stays exactly on the camera at t.
    rnd = lambda v: int(math.copysign(math.floor(abs(v) + 0.5), v))
    offs = [(rnd(((i + 0.5) / n - 0.5) * dx), rnd(((i + 0.5) / n - 0.5) * dy)) for i in range(n)]
    lx, ly = (min(64, max(1, round(abs(v) / n))) for v in (dx, dy))
    if all(o == (0, 0) for o in offs) and lx == ly == 1:
        return _compose(S, t)                              # too slow to smear: the sharp frame
    bx, by = (lx - 1) // 2, (ly - 1) // 2
    mx = max(abs(o[0]) for o in offs) + lx
    my = max(abs(o[1]) for o in offs) + ly
    big = np.asarray(_compose(S, t, mx, my))
    gw, gh = W + lx - 1, H + ly - 1                        # the region the box blur reads from
    acc = None
    for ox, oy in offs:
        x0, y0 = mx + ox - bx, my + oy - by
        part = big[y0:y0 + gh, x0:x0 + gw]
        if acc is None:
            acc = part.astype(np.uint16)
        else:
            acc += part
    del big
    acc = _box(_box(acc, lx, 1), ly, 0)
    acc += n // 2
    acc //= n
    return Image.fromarray(acc.astype(np.uint8), "RGB")


# ------------------------------------------------------------------ page previews


def page_images(pl: dict, images: list, out_dir: Path, paper: str = "#f3f0e8") -> list[Path]:
    """One JPG per page (p01.jpg, ...), 1920 px wide: every panel in full colour at the middle of its
    inner move, labelled with its scene number, for a person to check a chapter's crops at a glance."""
    from PIL import Image, ImageDraw, ImageFont
    S = {"panels": pl, "images": [str(p) for p in images], "paper": paper}
    rgb = _paper(S)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(str(FONT), 28)
    out = []
    for n, page in enumerate(pl["pages"], 1):
        canvas = Image.new("RGB", (page["w"], page["h"]), rgb)
        draw = ImageDraw.Draw(canvas)
        for k in page["panels"]:
            pan = pl["panels"][k]
            x, y = pan["x"] - page["x"], pan["y"] - page["y"]
            canvas.paste(_window(S, pan, 0.5), (x, y))
            draw.rectangle([x, y, x + pan["w"] - 1, y + pan["h"] - 1], outline=BORDER_RGB, width=BORDER)
        scale = 1920 / page["w"]
        small = canvas.resize((1920, round(page["h"] * scale)), Image.LANCZOS)
        draw = ImageDraw.Draw(small)
        for k in page["panels"]:                           # labels after the shrink, so they stay legible
            pan = pl["panels"][k]
            lx = round((pan["x"] - page["x"]) * scale) + BORDER + 6
            ly = round((pan["y"] - page["y"]) * scale) + BORDER + 6
            label = f"s{pan.get('id', k + 1):02d}"
            l, tp, r, b = draw.textbbox((lx + 8, ly + 4), label, font=font)
            draw.rectangle([lx, ly, r + 8, b + 6], fill=(20, 20, 20))
            draw.text((lx + 8, ly + 4), label, font=font, fill=(255, 255, 255))
        path = out_dir / f"p{n:02d}.jpg"
        small.save(path, quality=90)
        out.append(path)
    return out
