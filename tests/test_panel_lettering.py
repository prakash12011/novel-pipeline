"""Captions lettered inside the active comic panel: the look (panel_lettering), where a caption sits
(episode_panels.place_captions, focus_in_panel), how `frame` draws it, and the switch (episode_build.captions_on).

GENERATED inputs only: made-up cue/scene dicts and small PIL pictures written into tmp_path. No ffmpeg, no network.
"""

import random

import numpy as np
import pytest
from PIL import Image

from novel.stages import episode as E
from novel.stages import episode_build as B
from novel.stages import episode_panels as P
from novel.stages import episode_render as R
from novel.stages import panel_lettering as PL

EPS = 1e-9


def cue(lines=("HELLO WORLD",), k=0, t0=1.0, t1=3.0, **kw):
    return {"k": k, "t0": t0, "t1": t1, "lines": list(lines), "text": " ".join(lines), **kw}


def scene(i, chars=0, shot="medium", focus=(0.5, 0.45), **kw):
    return {"id": i, "para": i, "characters": [f"c{j}" for j in range(chars)], "shot": shot,
            "focus": list(focus), "scene": "", **kw}


def timing(n, seed=0, lo=2.5, hi=9.0):
    rng = random.Random(seed)
    starts, t = [], 0.0
    for _ in range(n):
        starts.append(t)
        t += rng.uniform(lo, hi)
    return starts, t


# ------------------------------------------------------------------ clean / measure / tilt_for


@pytest.mark.parametrize("raw,want", [
    ("Hello, world.", "HELLO, WORLD"),          # capitals, no full stop at the end, a comma inside stays
    ("  wait...  ", "WAIT"),
    ("What?!", "WHAT"),
    ("so;", "SO"),
    ("ends:", "ENDS"),
    ("Dr. Who", "DR. WHO"),                     # only the end is stripped
    ("plain", "PLAIN"),
    ("", ""),
    ("   ", ""),
    (".", ""),
])
def test_clean(raw, want):
    assert PL.clean(raw) == want


def test_measure_is_text_width_plus_box():
    base = PL.font().getlength("HELLO")
    assert PL.measure("HELLO") == pytest.approx(base + 2 * (PL.PAD + PL.BORDER))
    assert PL.measure("hello") == PL.measure("HELLO") == PL.measure("hello.")   # measured as lettered
    assert PL.measure("") == pytest.approx(2 * (PL.PAD + PL.BORDER))
    assert PL.measure("HELLO WORLD") > PL.measure("HELLO") > PL.measure("HI")


def test_tilt_for_is_fixed_small_and_per_caption():
    c = cue(k=2, t0=4.5)
    assert PL.tilt_for(c) == PL.tilt_for(dict(c)) == PL.tilt_for(cue(("OTHER TEXT",), k=2, t0=4.5))
    tilts = [PL.tilt_for(cue(k=k, t0=t0)) for k in range(6) for t0 in (0.0, 1.5, 7.25, 40.0)]
    assert all(abs(t) <= PL.TILT for t in tilts)
    assert len({round(t, 6) for t in tilts}) > 15                    # not all alike
    assert min(tilts) < 0 < max(tilts)                               # both ways


# ------------------------------------------------------------------ box_image


def alpha_at(im, x, y):
    return im.getpixel((x, y))[3]


def test_box_image_size_and_pivot_untilted():
    lines = ["A LONG LINE OF TEXT", "SHORT"]
    im, (px, py), h = PL.box_image(lines)
    widths = [round(PL.font().getlength(PL.clean(l))) + 2 * (PL.PAD + PL.BORDER) for l in lines]
    assert im.mode == "RGBA"
    assert h == PL.STEP * (len(lines) - 1) + PL.BOX_H + PL.SHADOW
    assert im.size == (max(widths) + PL.SHADOW, h)
    assert (px, py) == (0.0, h / 2)
    one, _, h1 = PL.box_image(["A LONG LINE OF TEXT"])
    assert h1 == PL.BOX_H + PL.SHADOW and one.size == (im.width, h1)


def test_box_image_draws_border_paper_shadow_and_text():
    im, _, h = PL.box_image(["HELLO WORLD"])
    w = im.width
    mid = PL.BOX_H // 2
    assert im.getpixel((0, mid)) == PL.INK                           # the border, left
    assert im.getpixel((PL.BORDER + 1, mid)) == PL.PAPER             # paper between border and text
    assert im.getpixel((w - 1, mid)) == PL.INK                       # shadow, right
    assert im.getpixel((w - 1, h - 1)) == PL.INK                     # shadow, down (right corner)
    assert alpha_at(im, w - 1, 0) == 0                               # shadow starts SHADOW below the top
    assert alpha_at(im, 0, h - 1) == 0                               # ... and SHADOW right of the left edge
    inside = np.asarray(im)[PL.BORDER + 2:PL.BOX_H - PL.BORDER - 2, PL.BORDER + 2:w - PL.SHADOW - PL.BORDER - 2]
    black = ((inside[..., :3].max(axis=2) < 30) & (inside[..., 3] == 255)).sum()
    white = ((inside[..., :3].min(axis=2) > 225) & (inside[..., 3] == 255)).sum()
    assert black > 300 and white > black                             # letters on paper
    assert (np.asarray(im)[..., 3] == 255).sum() + (np.asarray(im)[..., 3] == 0).sum() == im.width * im.height


def test_box_image_each_line_hugs_its_text_and_stacks_down():
    lines = ["A LONG LINE OF TEXT", "HI"]
    im, _, h = PL.box_image(lines)
    w2 = round(PL.font().getlength("HI")) + 2 * (PL.PAD + PL.BORDER)
    y2 = PL.STEP + PL.BOX_H // 2
    assert im.getpixel((w2 - 1, y2)) == PL.INK                       # second box's right border
    assert alpha_at(im, w2 + PL.SHADOW + 4, y2) == 0                 # nothing right of its shadow
    gap = PL.BOX_H + PL.SHADOW + 4                                   # between the boxes, below the first shadow
    assert gap < PL.STEP
    assert alpha_at(im, PL.SHADOW + 10, gap) == 0
    assert alpha_at(im, 5, PL.STEP + 2) == 255                       # second box's top border


def test_box_image_ignores_empty_lines():
    ref, _, h = PL.box_image(["HELLO"])
    got, _, h2 = PL.box_image(["", "hello.", "  ", "."])
    assert h == h2 and got.tobytes() == ref.tobytes()


def test_box_image_tilt_keeps_left_middle_as_pivot_and_turns_the_right_way():
    lines = ["A LONG LINE OF TEXT HERE"]
    flat, _, h = PL.box_image(lines)
    for tilt in (1.6, -1.6):
        im, (px, py), h2 = PL.box_image(lines, tilt)
        assert h2 == h and im.mode == "RGBA"
        assert im.width >= flat.width and im.height > flat.height    # expanded to hold the turn
        # The pivot is on the caption's left edge (the black border), mid height.
        assert alpha_at(im, round(px) + 2, round(py)) == 255
        assert np.asarray(im)[round(py), round(px) + 2, :3].max() < 30
        # Positive tilt turns counter-clockwise: the right end rises (smaller y); negative: it falls.
        a = np.asarray(im)[..., 3]
        col = np.nonzero(a[:, a.shape[1] - 12])[0]
        assert len(col)
        assert (col.mean() < py) if tilt > 0 else (col.mean() > py)
    assert PL.box_image(lines, 5e-4)[1:] == ((0.0, h / 2), h)        # a negligible tilt is no tilt


# ------------------------------------------------------------------ offset


def test_offset_top_and_bottom():
    assert PL.offset(cue(), 1012) == (PL.INSET, PL.INSET)
    assert PL.offset(cue(at="top"), 500) == (PL.INSET, PL.INSET)
    x, y = PL.offset(cue(at="bottom"), 1012)
    assert x == PL.INSET and y == 1012 - PL.INSET - PL.SHADOW - PL.BOX_H
    two = cue(("ONE", "TWO"), at="bottom")
    x2, y2 = PL.offset(two, 1012)
    assert y2 == y - PL.STEP                                         # one more line: the top moves up a step
    _, _, h = PL.box_image(two["lines"])
    assert y2 + h + PL.INSET == 1012                                 # last box and shadow end INSET above the bottom


# ------------------------------------------------------------------ pop


def test_pop_in_grows_from_85_percent_and_transparent():
    c = cue(t0=10.0, t1=20.0)
    assert PL.pop(c, 10.0) == (PL.POP_FROM, 0.0)
    assert PL.pop(c, 5.0) == (PL.POP_FROM, 0.0)                      # before the start: clamped
    assert PL.pop(c, 10.0 + PL.POP) == (1.0, 1.0)
    assert PL.pop(c, 15.0) == (1.0, 1.0)
    prev = (PL.POP_FROM, 0.0)
    for i in range(1, 10):
        s, a = PL.pop(c, 10.0 + PL.POP * i / 10)
        assert PL.POP_FROM < s < 1.0 and 0.0 < a < 1.0
        assert s > prev[0] and a > prev[1]                           # rising
        assert s == pytest.approx(PL.POP_FROM + (1 - PL.POP_FROM) * a)   # the same ease drives both
        prev = (s, a)
    assert PL.pop(c, 10.0 + PL.POP / 2)[1] > 0.5                     # ease-out: fast at first


def test_pop_fade_out_at_the_end_unless_swapped():
    c = cue(t0=10.0, t1=20.0)                                        # fade_out absent: it fades
    assert PL.pop(c, 20.0 - PL.FADE_OUT) == (1.0, 1.0)
    assert PL.pop(c, 20.0 - PL.FADE_OUT / 2)[1] == pytest.approx(0.5)
    assert PL.pop(c, 20.0)[1] == 0.0 and PL.pop(c, 21.0)[1] == 0.0
    assert PL.pop(c, 20.0 - PL.FADE_OUT / 2)[0] == 1.0               # fading does not shrink it
    swap = cue(t0=10.0, t1=20.0, fade_out=False)
    assert PL.pop(swap, 20.0 - PL.FADE_OUT / 2) == (1.0, 1.0) and PL.pop(swap, 19.999) == (1.0, 1.0)
    assert PL.pop(dict(c, fade_out=True), 20.0 - PL.FADE_OUT / 2)[1] == pytest.approx(0.5)


# ------------------------------------------------------------------ paste


def grey(w=700, h=300, v=128):
    return Image.new("RGB", (w, h), (v, v, v))


def diff_bbox(a, b):
    d = np.abs(np.asarray(a).astype(int) - np.asarray(b).astype(int)).max(axis=2) > 0
    ys, xs = np.nonzero(d)
    return (xs.min(), ys.min(), xs.max(), ys.max()) if len(xs) else None


def test_paste_nothing_before_the_start_and_after_the_end():
    c = cue(t0=2.0, t1=4.0)
    ref = grey()
    for t in (0.0, 2.0, 4.0, 9.0):
        canvas = grey()
        PL.paste(canvas, 40, 40, c, t)
        assert canvas.tobytes() == ref.tobytes(), t


def test_paste_draws_the_box_with_its_left_middle_at_the_spot():
    c = cue(("HELLO WORLD",), t0=2.0, t1=4.0)
    _, _, h = PL.box_image(c["lines"])
    for k in (0, 1, 2, 3):                                           # different tilts
        canvas = grey()
        PL.paste(canvas, 100, 60, dict(c, k=k), 3.0)
        x0, y0, x1, y1 = diff_bbox(canvas, grey())
        assert abs(x0 - 100) <= 4                                    # the left edge stays at x
        assert tuple(np.asarray(canvas)[60 + h // 2, 102]) == (0, 0, 0)    # its border, at the middle
        w = PL.box_image(c["lines"])[0].width
        assert w - 8 <= x1 - x0 + 1 <= w + 8
        assert 60 - 12 <= y0 and y1 <= 60 + h + 12


def test_paste_at_full_opacity_shows_white_paper_and_black_ink():
    canvas = grey()
    PL.paste(canvas, 100, 60, cue(t0=2.0, t1=4.0), 3.0)
    a = np.asarray(canvas)[60:60 + PL.BOX_H, 100:300]
    assert ((a == 255).all(axis=2)).sum() > 1000 and ((a == 0).all(axis=2)).sum() > 300


def test_paste_fade_out_blends_with_the_canvas():
    c = cue(t0=2.0, t1=4.0)
    canvas = Image.new("RGB", (700, 300), (0, 0, 0))
    PL.paste(canvas, 100, 60, c, 4.0 - PL.FADE_OUT / 2)
    v = np.asarray(canvas)[60 + PL.BOX_H // 2, 100 + PL.BORDER + 2]  # paper, half transparent over black
    assert 124 <= v[0] <= 132 and v[0] == v[1] == v[2]
    canvas2 = Image.new("RGB", (700, 300), (0, 0, 0))
    PL.paste(canvas2, 100, 60, dict(c, fade_out=False), 4.0 - PL.FADE_OUT / 2)
    assert tuple(np.asarray(canvas2)[60 + PL.BOX_H // 2, 100 + PL.BORDER + 2]) == (255, 255, 255)


def test_paste_pop_in_is_smaller_and_see_through_then_full():
    c = cue(t0=2.0, t1=4.0)
    early, full = grey(), grey()
    PL.paste(early, 100, 60, c, 2.0 + PL.POP / 3)
    PL.paste(full, 100, 60, c, 3.0)
    e, f = diff_bbox(early, grey()), diff_bbox(full, grey())
    assert e is not None
    assert (e[2] - e[0]) < (f[2] - f[0]) and abs(e[0] - f[0]) <= 3   # smaller, but still growing from the left edge
    mid = np.asarray(early)[60 + PL.BOX_H // 2, 100 + PL.BORDER + 2].astype(int)
    assert 128 < mid[0] < 255                                        # paper only partly there


def test_paste_uses_the_cache_hook_with_a_stable_key():
    c = cue(("HELLO", "WORLD"), k=3, t0=2.0, t1=4.0)
    made, store = [], {}

    def cached(key, make):
        if key not in store:
            made.append(key)
            store[key] = make()
        return store[key]
    a, b, plain = grey(), grey(), grey()
    PL.paste(a, 50, 50, c, 3.0, cached)
    PL.paste(b, 50, 50, c, 3.5, cached)
    PL.paste(plain, 50, 50, c, 3.0)
    assert made == [("caption", 3, 2.0, ("HELLO", "WORLD"))]          # drawn once
    assert a.tobytes() == b.tobytes() == plain.tobytes()             # the cache changes nothing


def test_paste_partly_off_the_canvas_is_fine():
    canvas = grey(200, 100)
    PL.paste(canvas, -50, -20, cue(t0=0.0, t1=9.0), 1.0)
    PL.paste(canvas, 150, 80, cue(t0=0.0, t1=9.0), 1.0)
    assert canvas.size == (200, 100)


# ------------------------------------------------------------------ focus_in_panel


def pan_(z=(1.0, 1.0), c=((0.5, 0.5), (0.5, 0.5)), focus=(0.3, 0.6), t0=0.0, t1=10.0, w=1600, h=900):
    return {"w": w, "h": h, "focus": list(focus),
            "move": {"kind": "x", "t0": t0, "t1": t1, "z": list(z), "c": [list(c[0]), list(c[1])]}}


def test_focus_in_panel_unzoomed_is_the_focus_itself():
    assert P.focus_in_panel(pan_(), 5.0) == pytest.approx((0.3, 0.6))        # 16:9 panel, whole picture shown


def test_focus_in_panel_zoomed_window():
    u, v = P.focus_in_panel(pan_(z=(2.0, 2.0)), 5.0)                         # window is half the picture, centred
    assert (u, v) == pytest.approx(((0.3 - 0.25) / 0.5, (0.6 - 0.25) / 0.5))
    u, v = P.focus_in_panel(pan_(focus=(0.5, 0.5), z=(2.0, 2.0)), 5.0)
    assert (u, v) == pytest.approx((0.5, 0.5))                               # the centre stays the centre


def test_focus_in_panel_follows_the_move_and_clamps_time():
    p = pan_(z=(1.0, 2.0), c=((0.5, 0.5), (0.5, 0.5)))
    at0, mid, end = (P.focus_in_panel(p, t) for t in (0.0, 5.0, 10.0))
    assert at0 == pytest.approx((0.3, 0.6))
    assert mid == pytest.approx(((0.3 - (0.5 - 1 / 3)) / (2 / 3), (0.6 - (0.5 - 1 / 3)) / (2 / 3)))   # z = 1.5
    assert end == pytest.approx((0.1, 0.7))
    assert P.focus_in_panel(p, -9.0) == pytest.approx(at0) and P.focus_in_panel(p, 99.0) == pytest.approx(end)
    pan = pan_(c=((0.4, 0.5), (0.6, 0.5)), focus=(0.5, 0.5))                  # window sliding right: the subject drifts left
    assert P.focus_in_panel(pan, 0.0)[0] > P.focus_in_panel(pan, 10.0)[0]


def test_focus_in_panel_degenerate_move_uses_its_start():
    p = pan_(z=(1.0, 2.0), t0=5.0, t1=5.0)
    assert P.focus_in_panel(p, 7.0) == pytest.approx((0.3, 0.6))


# ------------------------------------------------------------------ place_captions


def pl_of(*pans):
    return {"panels": [dict(p, k=i) for i, p in enumerate(pans)]}


def test_place_captions_without_pictures_uses_the_focus_rule():
    cs = [cue(("HELLO",), k=0), cue(("HELLO",), k=1), cue(("HELLO",), k=2), cue(("HELLO",), k=3)]
    pl = pl_of(pan_(focus=(0.05, 0.05)),       # under a top-left caption: bottom
               pan_(focus=(0.9, 0.9)),         # far from it: top
               pan_(focus=(0.5, 0.5)),         # middle: top
               pan_(focus=(0.05, 0.9)))        # left, but low: top
    out = P.place_captions(pl, cs)
    assert out is cs
    assert [c["at"] for c in cs] == ["bottom", "top", "top", "top"]


def test_place_captions_focus_rule_follows_the_caption_size():
    pl = pl_of(pan_(focus=(0.30, 0.10), w=1600, h=900))
    short, long_ = cue(("HI",), k=0), cue(("A QUITE LONG CAPTION LINE HERE",), k=0)
    P.place_captions(pl, [short])              # one at a time: a panel's captions stack and share one place
    P.place_captions(pl, [long_])
    assert short["at"] == "top"                # the focus is to the right of the short box (and its margin)
    assert long_["at"] == "bottom"             # the long box reaches over it


def noise(path, w=640, h=360, seed=0):
    rng = np.random.default_rng(seed)
    Image.fromarray(rng.integers(0, 256, (h, w, 3), dtype=np.uint8)).save(path)


def picture(path, busy_top=False, busy_bottom=False, w=640, h=360, checker=False):
    """Plain grey picture; the top-left (and/or bottom-left) 40% x 40% busy: noise or a checkerboard."""
    a = np.full((h, w, 3), 140, dtype=np.uint8)
    rng = np.random.default_rng(1)

    def busy(ys, xs):
        if checker:
            yy, xx = np.mgrid[ys, xs]
            a[ys, xs] = (((yy // 4 + xx // 4) % 2) * 255).astype(np.uint8)[..., None]
        else:
            a[ys, xs] = rng.integers(0, 256, a[ys, xs].shape, dtype=np.uint8)
    if busy_top:
        busy(slice(0, int(h * 0.4)), slice(0, int(w * 0.4)))
    if busy_bottom:
        busy(slice(int(h * 0.6), h), slice(0, int(w * 0.4)))
    Image.fromarray(a).save(path)
    return str(path)


@pytest.mark.parametrize("checker", [False, True])
@pytest.mark.parametrize("lines", [("HELLO WORLD",), ("HELLO WORLD", "AND MORE")])
def test_place_captions_busy_top_plain_bottom_goes_to_the_bottom(tmp_path, checker, lines):
    img = picture(tmp_path / "a.png", busy_top=True, checker=checker)
    cs = [cue(lines, k=0)]
    P.place_captions(pl_of(pan_(focus=(0.9, 0.9))), cs, [img])
    assert cs[0]["at"] == "bottom"


@pytest.mark.parametrize("checker", [False, True])
def test_place_captions_plain_top_busy_bottom_stays_on_top(tmp_path, checker):
    img = picture(tmp_path / "a.png", busy_bottom=True, checker=checker)
    cs = [cue(k=0)]
    P.place_captions(pl_of(pan_(focus=(0.9, 0.9))), cs, [img])
    assert cs[0]["at"] == "top"


def test_place_captions_equally_plain_or_equally_busy_stays_on_top(tmp_path):
    plain = picture(tmp_path / "plain.png")
    both = picture(tmp_path / "both.png", busy_top=True, busy_bottom=True)
    cs = [cue(k=0), cue(k=1)]
    P.place_captions(pl_of(pan_(), pan_()), cs, [plain, both])
    assert [c["at"] for c in cs] == ["top", "top"]                   # the comic convention unless clearly calmer


def test_place_captions_with_pictures_ignores_the_focus_rule(tmp_path):
    plain = picture(tmp_path / "plain.png")
    cs = [cue(("HELLO",), k=0)]
    pl = pl_of(pan_(focus=(0.05, 0.05)))                             # the focus rule alone would say bottom
    assert P.place_captions(pl, [dict(cs[0])])[0]["at"] == "bottom"
    assert P.place_captions(pl, cs, [plain])[0]["at"] == "top"


def test_place_captions_each_cue_is_judged_on_its_own_panels_picture(tmp_path):
    busy_top = picture(tmp_path / "a.png", busy_top=True)
    busy_bottom = picture(tmp_path / "b.png", busy_bottom=True)
    cs = [cue(k=1), cue(k=0), cue(k=1, t0=5.0, t1=6.0)]
    P.place_captions(pl_of(pan_(), pan_()), cs, [busy_top, busy_bottom])
    assert [c["at"] for c in cs] == ["top", "bottom", "top"]


def test_place_captions_accepts_paths_and_reads_a_picture_once(tmp_path, monkeypatch):
    img = tmp_path / "a.png"
    picture(img, busy_top=True)
    seen = []
    real = Image.open
    monkeypatch.setattr(Image, "open", lambda p, *a, **k: (seen.append(str(p)), real(p, *a, **k))[1])
    cs = [cue(k=0), cue(k=0, t0=4.0, t1=5.0)]
    P.place_captions(pl_of(pan_()), cs, [img])                       # a Path, not a str
    assert [c["at"] for c in cs] == ["bottom", "bottom"]
    assert len(seen) == 1


def test_place_captions_empty_and_in_place():
    assert P.place_captions(pl_of(pan_()), []) == []
    cs = [cue(k=0, at="bottom")]
    P.place_captions(pl_of(pan_(focus=(0.9, 0.9))), cs)
    assert cs[0]["at"] == "top"                                      # decided afresh each time


# ------------------------------------------------------------------ frame: the caption inside the panel


@pytest.fixture(scope="module")
def chapter(tmp_path_factory):
    d = tmp_path_factory.mktemp("lettered")
    paths = []
    for k, c in enumerate([(220, 40, 40), (40, 60, 220), (40, 180, 70)]):
        p = d / f"s{k + 1}.png"
        Image.new("RGB", (640, 360), c).save(p)
        paths.append(str(p))
    scenes = [scene(1, 0, "wide"), scene(2, 1), scene(3, 2)]
    starts, audio = timing(3, seed=5, lo=4.0, hi=6.0)
    pl = P.plan(starts, audio, scenes)
    return pl, paths


def state(chapter, cues=None):
    pl, paths = chapter
    S = {"panels": pl, "images": paths, "paper": "#f3f0e8", "dim": 0.45}
    if cues is not None:
        S["cues"] = cues
    return S


def screen_origin(pl, k=0):
    """Top-left of panel k on screen while it is the active one at rest."""
    pan = pl["panels"][k]
    return pan["x"] - (pan["cam"][0] - R.W // 2), pan["y"] - (pan["cam"][1] - R.H // 2)


def changed(a, b):
    return np.abs(np.asarray(a).astype(int) - np.asarray(b).astype(int)).max(axis=2) > 0


def test_frame_letters_the_caption_in_the_top_left_of_the_active_panel(chapter):
    pl, _ = chapter
    c = cue(("MOON LIGHT",), k=0, t0=0.5, t1=2.5, at="top", fade_out=False)
    t = 1.0                                                          # well after the pop, in panel 0's hold
    assert P._transition(pl, t) == (None, None) and P._active(pl, t) == 0
    ox, oy = screen_origin(pl)
    with_ = P.frame(state(chapter, [c]), t)
    without = P.frame(state(chapter), t)
    assert with_.size == (R.W, R.H) and with_.mode == "RGB"
    diff = changed(with_, without)
    assert diff.any()
    ys, xs = np.nonzero(diff)
    bw = PL.measure("MOON LIGHT") + PL.SHADOW
    # Everything that changed lies in the INSET area at the top left (allowing for the tilt of a few px).
    assert xs.min() >= ox + PL.INSET - 3 and xs.max() <= ox + PL.INSET + bw + 3
    assert ys.min() >= oy + PL.INSET - 14 and ys.max() <= oy + PL.INSET + PL.BOX_H + PL.SHADOW + 14
    area = np.asarray(with_)[int(oy + PL.INSET):int(oy + PL.INSET + PL.BOX_H), int(ox + PL.INSET):int(ox + PL.INSET + bw)]
    assert (area.min(axis=2) >= 240).sum() > 1000                    # near-white box
    # Text: black pixels in the inside of the box, clear of the border (tilt moves it at most 2 px here).
    x0, y0 = int(ox + PL.INSET + PL.BORDER + PL.PAD), int(oy + PL.INSET + PL.BORDER + 8)
    text = np.asarray(with_)[y0:y0 + PL.BOX_H - 2 * (PL.BORDER + 8), x0:x0 + 60]
    assert (text.max(axis=2) <= 30).sum() > 100                      # near-black letters
    plain = np.asarray(without)[y0:y0 + PL.BOX_H - 2 * (PL.BORDER + 8), x0:x0 + 60]
    assert (plain.max(axis=2) <= 30).sum() == 0                      # and the picture has none there


def test_frame_caption_at_the_bottom_left(chapter):
    pl, _ = chapter
    c = cue(("MOON LIGHT",), k=0, t0=0.5, t1=2.5, at="bottom", fade_out=False)
    ox, oy = screen_origin(pl)
    pan = pl["panels"][0]
    diff = changed(P.frame(state(chapter, [c]), 1.0), P.frame(state(chapter), 1.0))
    ys, xs = np.nonzero(diff)
    top = oy + pan["h"] - PL.INSET - PL.SHADOW - PL.BOX_H
    assert ys.min() >= top - 14 and ys.max() <= oy + pan["h"] - PL.INSET + 14
    assert xs.min() >= ox + PL.INSET - 3
    assert not diff[int(oy):int(oy + PL.INSET + PL.BOX_H + 30)].any()        # nothing at the top


def test_frame_outside_any_cue_is_identical(chapter):
    pl, _ = chapter
    c = cue(("MOON LIGHT",), k=0, t0=1.0, t1=2.0)
    for t in (0.3, 0.99, 2.0, 2.4):                                  # before, just before, at t1 (exclusive), after
        a = np.asarray(P.frame(state(chapter, [c]), t))
        b = np.asarray(P.frame(state(chapter), t))
        assert np.array_equal(a, b), t
    assert not np.array_equal(np.asarray(P.frame(state(chapter, [c]), 1.5)), np.asarray(P.frame(state(chapter), 1.5)))


def test_frame_caption_pops_in_then_is_full(chapter):
    pl, _ = chapter
    c = cue(("MOON LIGHT",), k=0, t0=0.5, t1=2.5, fade_out=False)
    S = state(chapter, [c])
    first = changed(P.frame(S, 0.5), P.frame(state(chapter), 0.5))
    assert not first.any()                                           # the first instant: transparent
    # How strongly the frame changes, not how many pixels: half-way through the pop-in the caption is smaller
    # but see-through, so it can touch about as many pixels as the solid one while changing them less.
    def strength(t):
        a = np.asarray(P.frame(S, t)).astype(int)
        b = np.asarray(P.frame(state(chapter), t)).astype(int)
        return int(np.abs(a - b).sum())
    mid, full = strength(0.5 + PL.POP / 2), strength(0.5 + PL.POP + 0.05)
    assert full > mid > 0


def test_frame_caption_for_another_panel_is_not_drawn_on_this_one(chapter):
    pl, _ = chapter
    c = cue(("MOON LIGHT",), k=1, t0=0.5, t1=2.5)                    # panel 1's caption while panel 0 is narrated
    assert np.array_equal(np.asarray(P.frame(state(chapter, [c]), 1.0)), np.asarray(P.frame(state(chapter), 1.0)))


def test_frame_caption_is_drawn_inside_its_panel_when_it_is_active(chapter):
    pl, _ = chapter
    p1 = pl["panels"][1]
    t = (p1["t0"] + p1["t1"]) / 2
    c = cue(("MOON LIGHT",), k=1, t0=t - 1.0, t1=t + 1.0, fade_out=False)
    ox, oy = p1["x"] - (p1["cam"][0] - R.W // 2), p1["y"] - (p1["cam"][1] - R.H // 2)
    diff = changed(P.frame(state(chapter, [c]), t), P.frame(state(chapter), t))
    ys, xs = np.nonzero(diff)
    assert len(xs)
    assert xs.min() >= ox + PL.INSET - 3 and ys.min() >= oy + PL.INSET - 14


def test_frame_empty_or_missing_cues_draw_nothing(chapter):
    a = np.asarray(P.frame(state(chapter, []), 1.0))
    b = np.asarray(P.frame(state(chapter), 1.0))
    assert np.array_equal(a, b)


# ------------------------------------------------------------------ captions_on


@pytest.fixture
def fake_cfg(monkeypatch):
    calls = []

    def install(cfg):
        def fake(ep=None, ch=None):
            calls.append((ep, ch))
            return cfg
        monkeypatch.setattr(E, "cfg", fake)
        return calls
    return install


@pytest.mark.parametrize("video,style,want", [
    # the panels style has its own switch, video.panels.captions, on by default
    ({"style": "panels"}, None, True),
    ({"style": "panels", "panels": {}}, None, True),
    ({"style": "panels", "panels": None}, None, True),
    ({"style": "panels", "panels": {"captions": True}}, None, True),
    ({"style": "panels", "panels": {"captions": False}}, None, False),
    # ... and ignores the subtitle switch
    ({"style": "panels", "captions": {"enabled": False}}, None, True),
    ({"style": "panels", "captions": {"enabled": True}, "panels": {"captions": False}}, None, False),
    # the other style: video.captions.enabled, on by default
    ({}, None, True),
    ({"style": "kenburns"}, None, True),
    ({"style": "kenburns", "captions": {}}, None, True),
    ({"style": "kenburns", "captions": None}, None, True),
    ({"style": "kenburns", "captions": {"enabled": True}}, None, True),
    ({"style": "kenburns", "captions": {"enabled": False}}, None, False),
    ({"captions": {"enabled": False}}, None, False),
    ({"style": "kenburns", "panels": {"captions": False}}, None, True),      # not its switch
    # an explicit style wins over the setting
    ({"style": "kenburns", "panels": {"captions": False}}, "panels", False),
    ({"style": "kenburns", "captions": {"enabled": False}}, "panels", True),
    ({"style": "panels", "panels": {"captions": False}}, "kenburns", True),
    ({"style": "panels", "captions": {"enabled": False}}, "kenburns", False),
])
def test_captions_on(fake_cfg, video, style, want):
    calls = fake_cfg({"video": video})
    assert B.captions_on(1, 3, style) is want
    assert calls and all(c == (1, 3) for c in calls)                 # asks for this chapter's settings


def test_captions_on_without_a_video_section_is_on(fake_cfg):
    fake_cfg({})
    assert B.captions_on(2, 1) is True and B.captions_on(2, 1, "panels") is True
    fake_cfg({"video": None})
    assert B.captions_on(2, 1) is True and B.captions_on(2, 1, "panels") is True


def test_captions_on_unknown_style_setting_is_an_error(fake_cfg):
    fake_cfg({"video": {"style": "slideshow"}})
    with pytest.raises(SystemExit):
        B.captions_on(1, 1)
    assert B.captions_on(1, 1, "kenburns") is True                   # an explicit style needs no setting
