"""The comic-panels render style (episode_panels): shapes, layout, plan, drawing.

GENERATED inputs only: made-up scene dicts and small solid-colour pictures written with PIL into tmp_path.
No ffmpeg, no network.
"""

import random
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from novel.stages import episode_panels as P
from novel.stages import episode_render as R

EPS = 1e-9


def scene(i, chars=0, shot="medium", focus=(0.5, 0.45), **kw):
    return {"id": i, "para": i, "characters": [f"c{j}" for j in range(chars)], "shot": shot,
            "focus": list(focus), "scene": "", **kw}


def random_scenes(n, seed):
    rng = random.Random(seed)
    shots = ["establishing", "wide", "medium", "close-up", "over-the-shoulder", "low-angle", "high-angle", "detail"]
    return [scene(i + 1, rng.choice([0, 1, 1, 1, 2, 2, 3, 4]), rng.choice(shots),
                  (rng.random(), rng.random())) for i in range(n)]


def timing(n, seed=0, lo=2.5, hi=9.0):
    """starts (narration seconds) and audio length for n windows of random length."""
    rng = random.Random(seed)
    starts, t = [], 0.0
    for _ in range(n):
        starts.append(t)
        t += rng.uniform(lo, hi)
    return starts, t


# ------------------------------------------------------------------ shapes


@pytest.mark.parametrize("chars,shot,want", [
    (1, "medium", "portrait"), (1, "wide", "portrait"), (1, "detail", "portrait"),
    (1, "close-up", "tall"), (1, "low-angle", "tall"), (1, "high-angle", "tall"),
    (2, "medium", "landscape"), (2, "close-up", "landscape"), (2, "over-the-shoulder", "wide"),
    (3, "medium", "wide"), (5, "close-up", "wide"),
    (0, "establishing", "wide"), (0, "wide", "wide"),
    (0, "detail", "square"), (0, "medium", "square"), (0, "close-up", "square"),
])
def test_shape_rules(chars, shot, want):
    assert P.shape_for(scene(1, chars, shot)) == want


def test_shape_override_and_unknown():
    assert P.shape_for(scene(3, 1, "medium", panel="wide")) == "wide"
    with pytest.raises(SystemExit) as e:
        P.shape_for(scene(7, 1, "medium", panel="banner"))
    msg = str(e.value)
    assert "7" in msg and all(s in msg for s in P.SHAPES)


def test_shapes_names_and_alternates():
    assert set(P.SHAPES) == {"wide", "landscape", "square", "portrait", "tall"}
    assert all(P.ALTERNATE[s] != s for s in P.SHAPES)


@pytest.mark.parametrize("scenes", [
    [scene(i, 1, "medium") for i in range(20)],                       # a long run of one character
    [scene(i, 1, "close-up") for i in range(13)],
    [scene(i, 2, "medium") for i in range(9)],
    [scene(i, 3, "wide") for i in range(9)],
    [scene(i, 0, "detail") for i in range(7)],
    random_scenes(45, 1), random_scenes(45, 2), random_scenes(3, 3),
])
def test_shapes_no_two_neighbours_alike(scenes):
    names = P.shapes(scenes)
    assert len(names) == len(scenes)
    assert all(a != b for a, b in zip(names, names[1:]))
    assert names == P.shapes(scenes)                                   # deterministic


def test_shapes_keep_overrides():
    s = [scene(0, 1), scene(1, 1, panel="portrait"), scene(2, 1), scene(3, 1), scene(4, 1, panel="tall"),
         scene(5, 1, "close-up")]
    names = P.shapes(s)
    assert names[1] == "portrait" and names[4] == "tall"                # never swapped: the neighbour is
    assert names == ["tall", "portrait", "tall", "portrait", "tall", "portrait"]


# ------------------------------------------------------------------ layout


def check_layout(names, within_limits=True):
    lay = P.layout(names)
    panels, pages = lay["panels"], lay["pages"]
    assert [p["k"] for p in panels] == list(range(len(names)))
    assert [p["shape"] for p in panels] == lay["shapes"]
    for p in panels:
        assert all(isinstance(p[key], int) for key in ("x", "y", "w", "h", "row", "page"))
        assert p["h"] == P.ROW_H and 0 < p["w"] <= P.MAX_PANEL_W
    rows = {}
    for p in panels:
        rows.setdefault(p["row"], []).append(p)
    last = max(rows)
    for r, ps in rows.items():
        for a, b in zip(ps, ps[1:]):                                   # reading order, one gutter apart
            assert b["x"] == a["x"] + a["w"] + P.GUTTER and b["y"] == a["y"]
        width = sum(p["w"] for p in ps) + P.GUTTER * (len(ps) - 1)
        if r != last:
            assert width == P.ROW_W
        else:
            assert width <= P.ROW_W
        if within_limits and (r != last or width == P.ROW_W):
            for p in ps:
                f = p["w"] / (P.SHAPES[p["shape"]] * P.ROW_H)
                assert P.STRETCH_MIN - 0.002 <= f <= P.STRETCH_MAX + 0.002, (p, f)
    for i, a in enumerate(panels):                                     # no overlaps
        for b in panels[i + 1:]:
            assert (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                    or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])
    for n, pg in enumerate(pages):                                     # panels sit inside their page
        assert pg["panels"] == [p["k"] for p in panels if p["page"] == n]
        assert pg["w"] == P.ROW_W + 2 * P.PAGE_MARGIN
        assert pg["h"] == P.ROWS_PER_PAGE * P.ROW_H + (P.ROWS_PER_PAGE - 1) * P.ROW_GAP + 2 * P.PAGE_MARGIN
        for k in pg["panels"]:
            p = panels[k]
            assert pg["x"] + P.PAGE_MARGIN <= p["x"] and p["x"] + p["w"] <= pg["x"] + pg["w"] - P.PAGE_MARGIN
            assert p["y"] == pg["y"] + P.PAGE_MARGIN + (p["row"] % P.ROWS_PER_PAGE) * (P.ROW_H + P.ROW_GAP)
    for a, b in zip(pages, pages[1:]):                                 # separate sheets, edge to edge
        assert b["x"] >= a["x"] + a["w"]
    assert {p["row"] // P.ROWS_PER_PAGE for p in panels} == set(range(len(pages)))
    return lay


@pytest.mark.parametrize("names", [
    ["wide"], ["portrait"], ["portrait", "tall"], ["wide", "landscape"], ["portrait", "tall", "portrait"],
    ["wide", "tall", "landscape"],
])
def test_layout_small(names):
    check_layout(names)


def test_layout_singles_and_mixed():
    check_layout(P.shapes([scene(i, 1, "medium" if i % 3 else "close-up") for i in range(45)]))
    mixed = ["wide", "tall", "landscape", "portrait", "tall", "portrait", "tall", "portrait", "landscape",
             "square", "portrait", "wide", "landscape", "tall", "portrait", "square", "wide"]
    check_layout(mixed)


@pytest.mark.parametrize("seed", range(6))
def test_layout_random_chapters(seed):
    # Some runs (landscape, tall, landscape) have no row break inside the stretch limits: rows still
    # fill exactly, and go only a little past a limit.
    lay = check_layout(P.shapes(random_scenes(45, seed)), within_limits=False)
    for p in lay["panels"]:
        f = p["w"] / (P.SHAPES[p["shape"]] * P.ROW_H)
        assert 0.7 <= f <= 1.3


@pytest.mark.parametrize("scenes", [
    [scene(i, 1, "medium") for i in range(45)],                 # every picture a lone character
    [scene(i, 1, "medium" if i % 4 else "close-up") for i in range(30)],
    random_scenes(45, 4), random_scenes(45, 5),
])
def test_layout_at_most_two_portraits_per_page(scenes):
    lay = check_layout(P.shapes(scenes), within_limits=False)
    for pg in lay["pages"]:
        on_page = [p for p in lay["panels"] if p["page"] == lay["pages"].index(pg)]
        assert sum(p["shape"] in P.NARROW for p in on_page) <= P.MAX_PORTRAIT_PER_PAGE
    names = lay["shapes"]
    assert all(a != b for a, b in zip(names, names[1:]))


def test_layout_portrait_cap_keeps_fixed_panels():
    scenes = [scene(i, 1, "medium") for i in range(6)]
    scenes[0]["panel"] = "portrait"                             # fixed by the scene: never changed
    lay = P.layout(["portrait"] * 6, fixed=[True, False, False, False, False, False])
    assert lay["shapes"][0] == "portrait"
    for n in range(len(lay["pages"])):                          # the fixed one counts toward the cap
        assert sum(p["shape"] in P.NARROW for p in lay["panels"] if p["page"] == n) <= P.MAX_PORTRAIT_PER_PAGE
    assert [p["shape"] for p in lay["panels"]] == lay["shapes"]


def test_layout_wide_beside_narrow_fills_row():
    # One factor would push the wide panel past MAX_PANEL_W: it stops there and the tall one takes the rest.
    lay = check_layout(["wide", "tall", "landscape", "portrait", "tall"])
    row0 = [p for p in lay["panels"] if p["row"] == 0]
    assert [p["shape"] for p in row0] == ["wide", "tall"]
    assert row0[0]["w"] <= P.MAX_PANEL_W


# ------------------------------------------------------------------ plan


def test_plan_matches_render_timing():
    scenes = random_scenes(12, 4)
    starts, audio = timing(12, 4)
    pl = P.plan(starts, audio, scenes)
    ref = R.plan(starts, audio, [tuple(s["focus"]) for s in scenes])
    assert pl["total"] == pytest.approx(ref["total"])
    assert [t for p in pl["panels"] for t in (p["t0"], p["t1"])] ==         pytest.approx([t for v in ref["views"] for t in (v["t0"], v["t1"])])
    import json, pickle
    json.dumps(pl)
    pickle.dumps(pl)


def test_plan_short_span():
    scenes = [scene(i, 1) for i in range(3)]
    with pytest.raises(SystemExit, match=r"image 2 would be on screen for only 1\.0s"):
        P.plan([0.0, 3.0, 4.0], 10.0, scenes)


def transitions(pl):
    return sorted(pl["slides"] + pl["flips"], key=lambda s: s["t0"])


@pytest.mark.parametrize("seed", range(4))
def test_plan_slides_and_flips(seed):
    n = 30
    scenes = random_scenes(n, seed)
    starts, audio = timing(n, seed, lo=2.0, hi=12.0)
    pl = P.plan(starts, audio, scenes)
    tr = transitions(pl)
    assert [(s["from"], s["to"]) for s in tr] == [(k, k + 1) for k in range(n - 1)]
    assert len(pl["pages"]) >= 3 and len(pl["flips"]) == len(pl["pages"]) - 1
    for s in pl["slides"]:                                             # slides stay inside a page,
        assert pl["panels"][s["from"]]["page"] == pl["panels"][s["to"]]["page"]
    for f in pl["flips"]:                                              # flips exactly at page changes
        assert pl["panels"][f["to"]]["page"] == pl["panels"][f["from"]]["page"] + 1
    assert P.slide_windows(pl) == [(s["t0"], s["t1"]) for s in tr]     # both kinds, in time order
    for s in tr:
        a, b = pl["panels"][s["from"]], pl["panels"][s["to"]]
        assert (s["t0"] + s["t1"]) / 2 == pytest.approx(a["t1"])      # centred on the cut
        dur = s["t1"] - s["t0"]
        assert dur <= max(P.SLIDE_PAGE, P.FLIP_SECONDS) + EPS
        assert dur <= 0.4 * (a["t1"] - a["t0"]) + EPS and dur <= 0.4 * (b["t1"] - b["t0"]) + EPS
        if min(a["t1"] - a["t0"], b["t1"] - b["t0"]) >= 2.5:
            assert dur >= (P.FLIP_SECONDS if a["page"] != b["page"] else P.SLIDE_NEAR) - EPS
    for a, b in zip(tr, tr[1:]):
        assert a["t1"] < b["t0"]                                       # ordered, never overlapping


def inside_page(pl, cx, cy, page):
    pg = pl["pages"][page]
    return (pg["x"] - EPS <= cx - R.W / 2 and cx + R.W / 2 <= pg["x"] + pg["w"] + EPS
            and pg["y"] - EPS <= cy - R.H / 2 and cy + R.H / 2 <= pg["y"] + pg["h"] + EPS)


@pytest.mark.parametrize("seed", range(4))
def test_camera_stays_on_the_page(seed):
    n = 30
    scenes = random_scenes(n, seed)
    starts, audio = timing(n, seed, lo=2.0, hi=12.0)
    pl = P.plan(starts, audio, scenes)
    for p in pl["panels"]:                                             # mid-hold
        cx, cy, slide, _ = P.camera(pl, (p["t0"] + p["t1"]) / 2)
        assert slide is None and [cx, cy] == p["cam"] and isinstance(cx, int) and isinstance(cy, int)
        assert inside_page(pl, cx, cy, p["page"])
        assert cx - R.W / 2 <= p["x"] and p["x"] + p["w"] <= cx + R.W / 2     # active panel wholly on screen
        assert cy - R.H / 2 <= p["y"] and p["y"] + p["h"] <= cy + R.H / 2
        if inside_page(pl, p["cx"], p["cy"], p["page"]):                     # no clamping needed: centred
            assert (cx, cy) == (p["cx"], p["cy"])
    assert any(p["cam"] != [p["cx"], p["cy"]] for p in pl["panels"])   # row ends: off-centre on purpose
    for i in range(int(pl["total"] * 24) + 1):                         # every frame, slides and flips included
        t = i / 24
        cx, cy, slide, _ = P.camera(pl, t)
        kind, tr = P._transition(pl, t)
        if kind == "flip":
            k = tr["from"] if t < (tr["t0"] + tr["t1"]) / 2 else tr["to"]
        else:
            k = slide["from"] if slide else P._active(pl, t)
        assert inside_page(pl, cx, cy, pl["panels"][k]["page"]), t


def window_inside(p, q):
    mv = p["move"]
    z = mv["z"][0] + (mv["z"][1] - mv["z"][0]) * q
    cx = mv["c"][0][0] + (mv["c"][1][0] - mv["c"][0][0]) * q
    cy = mv["c"][0][1] + (mv["c"][1][1] - mv["c"][0][1]) * q
    bw, bh = P._base_window(p["w"] / p["h"], P.PICTURE_ASPECT)
    hw, hh = bw / z / 2, bh / z / 2
    return z >= 1 - EPS and cx - hw >= -EPS and cx + hw <= 1 + EPS and cy - hh >= -EPS and cy + hh <= 1 + EPS


@pytest.mark.parametrize("seed", range(8))
def test_inner_window_stays_inside(seed):
    n = 40
    scenes = random_scenes(n, seed)
    starts, audio = timing(n, seed + 100, lo=2.0, hi=20.0)
    pl = P.plan(starts, audio, scenes)
    kinds = set()
    for k, p in enumerate(pl["panels"]):
        mv = p["move"]
        kinds.add(mv["kind"])
        tr = transitions(pl)
        assert mv["t0"] == (tr[k - 1]["t0"] if k else P.FIRST_HOLD)         # with the slide in; the first after a hold
        assert mv["t1"] == (tr[k]["t1"] if k < n - 1 else pl["total"])
        for q in (0.0, 0.5, 1.0):
            assert window_inside(p, q), (p, q)
        assert mv["z"][0] == 1.0 and 1.0 <= mv["z"][1] <= max(P.ZMAX, P.NARROW_ZMAX)   # opens unzoomed, zooms in only
    assert {"push", "pan_right", "pan_left", "slide_right", "slide_left", "zoom_in"} <= kinds


def test_narrow_slide_sweeps_across():
    # A lone character in a long window: the window sweeps the whole picture, whatever focus says.
    scenes = [scene(1, 1, "medium", (0.5, 0.45)), scene(2, 2, "medium")]
    pl = P.plan([0.0, 9.0], 15.0, scenes)
    p = pl["panels"][0]
    assert p["shape"] == "portrait" and p["move"]["kind"] == "slide_right"
    bw, _ = P._base_window(p["w"] / p["h"], P.PICTURE_ASPECT)
    (x0, _), (x1, _) = p["move"]["c"]
    assert p["move"]["z"] == [1.0, 1.0]
    assert x0 == pytest.approx(bw / 2) and x1 == pytest.approx(1 - bw / 2)
    # A short one: travel limited by MAX_SLIDE_SPEED and centred on focus x.
    pl = P.plan([0.0, 2.5], 8.0, [scene(1, 1, "medium", (0.53, 0.5)), scene(2, 2)])
    p = pl["panels"][0]
    bw, _ = P._base_window(p["w"] / p["h"], P.PICTURE_ASPECT)
    (x0, _), (x1, _) = p["move"]["c"]
    d = p["move"]["t1"] - p["move"]["t0"]
    assert (x1 - x0) * p["w"] / bw == pytest.approx(P.MAX_SLIDE_SPEED * d, rel=1e-6)
    assert (x0 + x1) / 2 == pytest.approx(0.53)
    # Focus near the edge: the same travel, pushed back inside the picture.
    pl = P.plan([0.0, 2.5], 8.0, [scene(1, 1, "medium", (0.95, 0.5)), scene(2, 2)])
    (x0, _), (x1, _) = pl["panels"][0]["move"]["c"]
    assert x1 == pytest.approx(1 - bw / 2) and x0 > bw / 2


# ------------------------------------------------------------------ drawing


COLOURS = [(220, 40, 40), (40, 60, 220), (40, 180, 70), (230, 200, 40), (160, 60, 200), (40, 200, 210),
           (240, 140, 30), (120, 90, 40), (200, 90, 160), (90, 140, 120)]


@pytest.fixture(scope="module")
def chapter(tmp_path_factory):
    d = tmp_path_factory.mktemp("panels")
    paths = []
    for k, c in enumerate(COLOURS):
        im = Image.new("RGB", (640, 360) if k != 3 else (700, 360), c)   # one picture not quite 16:9
        p = d / f"s{k + 1:02d}.jpg"
        im.save(p, quality=95)
        paths.append(str(p))
    scenes = [scene(1, 1), scene(2, 1, "close-up"), scene(3, 2), scene(4, 0, "detail"), scene(5, 3),
              scene(6, 1), scene(7, 2, "over-the-shoulder"), scene(8, 1), scene(9, 0, "wide"), scene(10, 1)]
    starts = [3.5 * k for k in range(len(scenes))]
    pl = P.plan(starts, 3.5 * len(scenes), scenes)
    assert len(pl["pages"]) >= 2 and pl["flips"]
    S = {"panels": pl, "images": paths, "paper": "#f3f0e8", "dim": 0.45}
    return d, pl, S, paths


def px(im, x, y):
    return np.asarray(im)[int(y), int(x)].astype(int)


def test_frame_hold(chapter):
    _, pl, S, _ = chapter
    a, b = pl["panels"][0], pl["panels"][1]
    assert a["row"] == b["row"]
    t = (a["t0"] + a["t1"]) / 2
    im = P.frame(S, t)
    assert im.mode == "RGB" and im.size == (R.W, R.H)
    ox, oy = a["cam"][0] - R.W / 2, a["cam"][1] - R.H / 2
    gx = a["x"] + a["w"] + P.GUTTER // 2 - ox                          # in the gutter between the two
    assert tuple(px(im, gx, R.H / 2)) == (0xf3, 0xf0, 0xe8)
    centre = px(im, a["cx"] - ox, R.H / 2)                             # the active panel: not faded
    assert np.abs(centre - COLOURS[0]).max() <= 8
    inside_b = px(im, b["x"] + b["w"] / 2 - ox, R.H / 2)               # the neighbour: faded toward paper
    want = np.array(COLOURS[1]) * (1 - 0.45) + np.array([0xf3, 0xf0, 0xe8]) * 0.45
    assert np.abs(inside_b - want).max() <= 8
    border = px(im, a["x"] + 1 - ox, R.H / 2)
    assert border.max() < 60


def test_frame_slide_differs(chapter):
    _, pl, S, _ = chapter
    s = pl["slides"][0]
    mid = P.frame(S, (s["t0"] + s["t1"]) / 2)
    before = P.frame(S, s["t0"] - 0.2)
    after = P.frame(S, s["t1"] + 0.2)
    m = np.asarray(mid).astype(int)
    assert np.abs(m - np.asarray(before)).mean() > 5
    assert np.abs(m - np.asarray(after)).mean() > 5
    assert mid.size == (R.W, R.H) and mid.mode == "RGB"


def test_frame_every_slide_and_order_free(chapter):
    _, pl, S, paths = chapter
    for s in pl["slides"]:                                            # row and page changes included
        for u in (0.05, 0.5, 0.95):
            im = P.frame(S, s["t0"] + u * (s["t1"] - s["t0"]))
            assert im.size == (R.W, R.H)
    # Frames do not depend on what was drawn before (workers get them in any order).
    t = pl["slides"][2]["t0"] + 0.3
    fresh = {"panels": pl, "images": paths, "paper": "#f3f0e8", "dim": 0.45}
    assert np.array_equal(np.asarray(P.frame(fresh, t)), np.asarray(P.frame(S, t)))


def test_frame_camera_glides(chapter):
    # Through the start of a slide the picture moves by small, steady steps: no 1 px stepping, no jump.
    _, pl, S, _ = chapter
    s = pl["slides"][0]
    ts = [s["t0"] - 2 / 24 + i / 24 for i in range(6)]
    frames = [np.asarray(P.frame(S, t)).astype(float).mean(axis=2) for t in ts]
    diffs = [np.abs(b - a).mean() for a, b in zip(frames, frames[1:])]
    assert all(d2 >= d1 - 0.5 for d1, d2 in zip(diffs[1:], diffs[2:]))   # speeding up, never a spike back


def test_page_images(chapter, tmp_path):
    _, pl, _, paths = chapter
    out = P.page_images(pl, paths, tmp_path / "pages")
    assert [p.name for p in out] == [f"p{n:02d}.jpg" for n in range(1, len(pl["pages"]) + 1)]
    for p in out:
        with Image.open(p) as im:
            assert im.width == 1920


def test_box_mean_wraps_safely():
    # The running sums wrap around in uint16; window means must still match exact arithmetic.
    rng = np.random.default_rng(5)
    a = rng.integers(0, 1021, size=(40, 300, 3)).astype(np.uint16)       # sums of 4 copies: <= 1020
    for n, axis in ((64, 1), (17, 1), (25, 0), (1, 0)):
        got = P._box(a, n, axis)
        c = np.cumsum(a.astype(np.int64), axis=axis)
        c = np.concatenate([np.zeros_like(np.take(c, [0], axis=axis)), c], axis=axis)
        m = c.shape[axis]
        want = (np.take(c, range(n, m), axis=axis) - np.take(c, range(0, m - n), axis=axis) + n // 2) // n
        assert np.array_equal(got, want if n > 1 else a)


def test_flip_frames(chapter):
    _, pl, S, _ = chapter
    f = pl["flips"][0]
    a, b = pl["panels"][f["from"]], pl["panels"][f["to"]]
    assert a["page"] + 1 == b["page"]
    old = np.asarray(P._view(S, f["t0"], a["cam"], {a["k"]: 0.0}, a["page"])).astype(int)
    new = np.asarray(P._view(S, f["t1"], b["cam"], {b["k"]: 0.0}, b["page"])).astype(int)
    first = np.asarray(P.frame(S, f["t0"])).astype(int)               # the window's ends: the plain screens
    last = np.asarray(P.frame(S, f["t1"])).astype(int)
    assert np.abs(first - old).max() <= 1 and np.abs(last - new).max() <= 1
    near0 = np.asarray(P.frame(S, f["t0"] + 1e-4)).astype(int)
    near1 = np.asarray(P.frame(S, f["t1"] - 1e-4)).astype(int)
    assert np.abs(near0 - old).mean() < 0.5 and np.abs(near1 - new).mean() < 0.5
    t = (f["t0"] + f["t1"]) / 2
    mid = np.asarray(P.frame(S, t)).astype(int)
    before = np.asarray(P.frame(S, f["t0"] - 0.2)).astype(int)
    after = np.asarray(P.frame(S, f["t1"] + 0.2)).astype(int)
    assert np.abs(mid - before).mean() > 5 and np.abs(mid - after).mean() > 5
    # Mid-turn it holds pixels of both screens: the old one at the left, the new one at the right.
    old_t = np.asarray(P._view(S, t, a["cam"], {a["k"]: 0.0}, a["page"])).astype(int)
    new_t = np.asarray(P._view(S, t, b["cam"], {b["k"]: 0.0}, b["page"])).astype(int)
    same_old = (np.abs(mid - old_t).max(axis=2) <= 2).mean(axis=0)
    same_new = (np.abs(mid - new_t).max(axis=2) <= 2).mean(axis=0)
    assert same_old[:100].mean() > 0.9 and same_new[-100:].mean() > 0.9
    # Between them, the back of the sheet: paper-coloured, shaded, a copy of neither screen.
    paper = np.array([0xf3, 0xf0, 0xe8])
    row = mid[R.H // 2]
    back = [x for x in range(R.W) if np.abs(row[x] - paper * (row[x].mean() / paper.mean())).max() <= 6
            and same_old[x] < 0.5 and same_new[x] < 0.5]
    assert len(back) > 200
    assert {P.frame(S, t).size for t in np.linspace(f["t0"], f["t1"], 7)} == {(R.W, R.H)}


def test_frames_never_show_another_page(chapter):
    # Every picture off the first page is magenta: no frame of the first page (holds, slides, up to the
    # first flip) may contain it, even at the end of a row beside the page edge.
    _, pl, S, paths = chapter
    first = set(pl["pages"][0]["panels"])
    hot = [str(Path(paths[0]).with_name(f"m{k}.png")) for k in range(len(paths))]
    for k, m in enumerate(hot):
        Image.new("RGB", (640, 360), COLOURS[k] if k in first else (255, 0, 255)).save(m)
    S2 = {"panels": pl, "images": hot, "paper": "#f3f0e8", "dim": 0.0}
    for t in np.arange(0.0, pl["flips"][0]["t0"], 0.25):
        a = np.asarray(P.frame(S2, float(t))).astype(int)
        assert not ((a[..., 0] > 200) & (a[..., 1] < 60) & (a[..., 2] > 200)).any(), t
