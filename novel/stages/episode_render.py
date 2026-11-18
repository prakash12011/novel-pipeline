"""Render a chapter video with ffmpeg alone: no browser, no HyperFrames project.

Each image stays up for its narration window (when the narrator starts reading
it, from the timing map, as in episode_video) with ONE continuous camera move,
cycled by image: push in toward the scene's focus, pan right, pull out, pan
left. Moves run at a fixed speed, so a longer window travels further (zoom is
capped at 1.20x). Images change with a 0.6 s dissolve. The first image fades
up from black, the title card sits over it while the chapter heading is read,
and the last image fades to black.

Frames are drawn in Python on all cores and piped to ffmpeg for H.264 + AAC.
Every frame is a sub-pixel affine transform, and each view's source is
pre-scaled (Lanczos) so the transform only ever shrinks by up to ~12%: moves
glide instead of stepping a pixel at a time, and fine line art doesn't shimmer.

A second style, "panels" (video.style, or --style; see episode_panels), lays the pictures out as comic panels
the camera slides between. Everything around the picture (title card, fades, logo, subtitles) is shared.

Usage:
    novel render --chapter 1 --ep 1
    novel render --chapter 1 --ep 1 --end 60    # first minute, to check the look
    novel render --chapter 1 --ep 1 --style panels --end 60   # try the other style (c0001-first60s-panels.mp4)
(`py -m novel.stages.episode_render --episode 1 --chapter 1 [--book B]` does the same without the checks.)
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import subprocess
import sys
import time
from collections import OrderedDict, deque
from pathlib import Path

from . import episode as E
from .episode_video import (chapter_title, clean_chapter, frame_stats, logo_layer, logo_signature,
                            narration, paste_logo, probe, series_label, title_font_file, title_font_setting)

W, H, FPS = 1920, 1080, 24


def CL_mode(ep_root: Path, n: int) -> str:
    """images.watermark for this chapter: crop (default), remove or none."""
    from .episode_clean import mode_for
    return mode_for(ep_root, n)
LEAD = 1.5            # black, while the first image fades up, before the narration starts
TAIL = 2.0            # after the narration: the last image holds, then fades to black
XF_IMAGE = 0.6        # dissolve when the image changes
XF_VIEW = 0.0         # unused: one view per image (kept for older plans' shape)
FONT = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "PlayfairDisplay.ttf"

# One move per image (user, 2026-11-10: a second framing inside the window read
# as a hard cut to a zoom from a random place). Speeds are 20% above the last
# two-framing version (wide push 0.08 in 6.43 s; pan 0.06 of the width in 6.43 s).
ZOOM_RATE = 0.015     # zoom change per second (1.00 -> 1.15 over 10 s)
PAN_RATE = 0.0112     # centre travel per second, in image widths
ZMAX = 1.20           # never zoom further than this, however long the window
MOVES = ["push", "pan_right", "pull", "pan_left"]


# ------------------------------------------------------------------ plan


def plan(starts: list[float], audio: float, focus: list[tuple[float, float]]) -> dict:
    """Every view of the chapter, in video seconds.

    starts[k] is when image k's window begins being read (k >= 1 is a cut);
    image 0 is up from the first frame.
    """
    total = LEAD + audio + TAIL
    cuts = [0.0] + [LEAD + s for s in starts[1:]] + [total]
    views = []
    for k in range(len(cuts) - 1):
        span = cuts[k + 1] - cuts[k]
        if span < 2.0:
            raise SystemExit(f"image {k + 1} would be on screen for only {span:.1f}s")
        move = MOVES[k % len(MOVES)]
        fx, fy = focus[k]
        run = span + XF_IMAGE                          # the move continues through both dissolves
        if move in ("push", "pull"):
            z = (1.0, min(ZMAX, 1.0 + ZOOM_RATE * run))
            c = ((0.5, 0.5), (fx, fy))
            if move == "pull":
                z, c = z[::-1], c[::-1]
        else:
            travel = min(PAN_RATE * run, 1 - 1 / ZMAX - 0.01)
            zz = max(1.04, 1 / (1 - travel) + 0.01)    # just enough zoom to have room to pan
            half = (1 / zz) / 2                        # centre must stay in [half, 1 - half]
            x0 = min(max(fx - travel / 2, half), 1 - half - travel)
            y = min(max(fy, half), 1 - half)
            c = ((x0, y), (x0 + travel, y))
            if move == "pan_left":
                c = c[::-1]
            z = (zz, zz)
        views.append({"img": k, "framing": move, "t0": cuts[k], "t1": cuts[k + 1], "z": z, "c": c})
    # Each boundary's dissolve, centred on it: views[i] dissolves into views[i + 1].
    for a, b in zip(views, views[1:]):
        a["xf_out"] = b["xf_in"] = XF_IMAGE if a["img"] != b["img"] else XF_VIEW
    views[0]["xf_in"] = views[-1]["xf_out"] = 0.0
    return {"total": total, "views": views}


# ------------------------------------------------------------------ drawing (worker processes)

_S: dict = {}          # per-worker state: plan, image paths, title layers, source cache


def _init(state: dict) -> None:
    _S.update(state)
    _S["cache"] = OrderedDict()
    _S["title_layers"] = _title_layers(state["title"]) if state["title"] and state["title"].get("show", True) \
        else None


def _source(k: int, zmax: float):
    """Image k scaled so that at zoom zmax it maps 1:1 onto the frame (cached)."""
    from PIL import Image
    cache = _S["cache"]                                # LRU, ~15 MB an entry

    def get(key, make):
        if key in cache:
            cache.move_to_end(key)
        else:
            cache[key] = make()
            while len(cache) > 7:
                cache.popitem(last=False)
        return cache[key]

    def scaled():
        full = get((k, "full"), lambda: Image.open(_S["images"][k]).convert("RGB"))
        f = max(W / full.width, H / full.height) * zmax
        return full.resize((round(full.width * f), round(full.height * f)), Image.LANCZOS)
    return get((k, round(zmax, 3)), scaled)


def _view_frame(v: dict, t: float):
    from PIL import Image
    zmax = max(v["z"])
    src = _source(v["img"], zmax)
    a, b = v["t0"] - v["xf_in"] / 2, v["t1"] + v["xf_out"] / 2
    p = min(1.0, max(0.0, (t - a) / (b - a)))
    z = v["z"][0] + (v["z"][1] - v["z"][0]) * p
    (cx0, cy0), (cx1, cy1) = v["c"]
    cx, cy = cx0 + (cx1 - cx0) * p, cy0 + (cy1 - cy0) * p
    s = max(W / src.width, H / src.height) * z          # output px per source px: z / zmax, <= 1
    w, h = W / s, H / s
    x0 = min(max(cx * src.width - w / 2, 0.0), src.width - w)
    y0 = min(max(cy * src.height - h / 2, 0.0), src.height - h)
    return src.transform((W, H), Image.AFFINE, (1 / s, 0, x0 + 0.5 / s - 0.5, 0, 1 / s, y0 + 0.5 / s - 0.5),
                         resample=Image.BILINEAR)


def _frame(i: int) -> bytes:
    from PIL import Image
    t = i / FPS
    total = _S["total"]
    if _S.get("style") == "panels":
        from . import episode_panels as P              # lazily: episode_panels imports this module
        frame = P.frame(_S, t)
    else:
        frame = _kenburns_frame(t)
    if _S["title_layers"] and t < _S["title"]["dur"]:
        frame = _title_over(frame, t)
    fade = min(1.0, t / LEAD, (total - t) / (TAIL - 0.2))
    if fade < 1.0:
        frame = Image.blend(Image.new("RGB", (W, H)), frame, max(0.0, fade))
    paste_logo(frame, _S.get("logo"))                  # on top of everything, fades included (as the old overlay)
    return frame.tobytes()


def _kenburns_frame(t: float):
    from PIL import Image
    views = _S["views"]
    lo, hi = 0, len(views) - 1                         # the view whose nominal span holds t
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if views[mid]["t0"] <= t:
            lo = mid
        else:
            hi = mid - 1
    v = views[lo]
    frame = _view_frame(v, t)
    # Inside a dissolve: blend with the neighbour on that side.
    if lo + 1 < len(views) and t > v["t1"] - v["xf_out"] / 2:
        nxt = views[lo + 1]
        alpha = (t - (v["t1"] - v["xf_out"] / 2)) / v["xf_out"]
        frame = Image.blend(frame, _view_frame(nxt, t), min(1.0, alpha))
    elif lo > 0 and t < v["t0"] + v["xf_in"] / 2:
        prv = views[lo - 1]
        alpha = (t - (v["t0"] - v["xf_in"] / 2)) / v["xf_in"]
        frame = Image.blend(_view_frame(prv, t), frame, max(0.0, alpha))
    return frame


def _frames(batch: list[int]) -> bytes:
    return b"".join(_frame(i) for i in batch)


# ------------------------------------------------------------------ title card


def _title_layers(title: dict):
    """(band, text): RGBA layers matching the HyperFrames title card."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    import numpy as np
    top = int(H * 0.4)
    f = np.clip((np.arange(H) - top) / (H - top), 0, None)   # 0 at the band's top, 1 at the bottom
    a = np.where(f < 0.5, 0.8 * f / 0.5, 0.8 + 0.06 * (f - 0.5) / 0.5) * (np.arange(H) >= top)
    rgba = np.zeros((H, W, 4), np.uint8)
    rgba[..., 3] = np.round(255 * a)[:, None]
    band = Image.fromarray(rgba, "RGBA")

    font_file = title.get("font") or FONT

    def font(size, weight="Regular"):
        f = ImageFont.truetype(str(font_file), size)
        try:
            f.set_variation_by_name(weight)             # a variable font (the default Playfair) picks its weight
        except (OSError, AttributeError, ValueError):   # a plain font file has one weight
            pass
        return f

    name_f, chap_f, ser_f = font(96, "Bold"), font(42), font(30)
    # Wrap the name at 1500 px.
    words, lines = title["name"].split(), [""]
    for wd in words:
        trial = (lines[-1] + " " + wd).strip()
        if name_f.getlength(trial) > 1500 and lines[-1]:
            lines.append(wd)
        else:
            lines[-1] = trial
    rows = []                                          # (text, font, colour, spacing px), top to bottom
    if title["series"]:
        rows.append((title["series"].upper(), ser_f, (244, 232, 204), 0.22 * 30))
    if title.get("layout") != "episode":               # the old card: "Chapter N" above the chapter name
        rows.append((f"Chapter {title['chapter']}", chap_f, (244, 232, 204), 0))
    rows += [(ln, name_f, (255, 255, 255), 0) for ln in lines]
    heights = [round(f.size * (1.1 if f is name_f else 1.3)) for _, f, _, _ in rows]
    # 14 px between rows (CSS gap), none between wrapped lines of the name.
    gaps = [0 if i == len(rows) - 1 or (rows[i][1] is name_f and rows[i + 1][1] is name_f) else 14
            for i in range(len(rows))]
    y = int(H * 0.84) - sum(heights) - sum(gaps)
    text = Image.new("RGBA", (W, H))
    shadow = Image.new("RGBA", (W, H))
    dt, ds = ImageDraw.Draw(text), ImageDraw.Draw(shadow)
    for (s, f, col, sp), hgt, gap in zip(rows, heights, gaps):
        width = f.getlength(s) + sp * (len(s) - 1)
        x = (W - width) / 2
        for ch in (s if sp else [s]):
            dt.text((x, y + hgt / 2), ch, font=f, fill=col + (255,), anchor="lm")
            if f is name_f:
                ds.text((x, y + 4 + hgt / 2), ch, font=f, fill=(0, 0, 0, 217), anchor="lm")
            x += f.getlength(ch) + sp if sp else 0
        y += hgt + gap
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    return band, Image.alpha_composite(shadow, text)


def _title_over(frame, t: float):
    from PIL import Image, ImageChops
    band, text = _S["title_layers"]
    out_at = _S["title"]["dur"] - 1.1
    ease = lambda x: 1 - (1 - x) ** 2                  # power2.out
    clamp = lambda x: min(1.0, max(0.0, x))
    out = 1 - clamp((t - out_at) / 1.0) ** 2           # power1.in fade-out
    band_a = clamp(t / 1.0) * out
    text_p = clamp((t - 0.5) / 1.2)
    text_a = ease(text_p) * out
    dy = round(24 * (1 - ease(text_p)))
    base = frame.convert("RGBA")
    for layer, a, shift in ((band, band_a, 0), (text, text_a, dy)):
        if a <= 0:
            continue
        if shift:
            layer = ImageChops.offset(layer, 0, shift)
        if a < 1:
            r, g, b, al = layer.split()
            layer = Image.merge("RGBA", (r, g, b, al.point(lambda v: round(v * a))))
        base = Image.alpha_composite(base, layer)
    return base.convert("RGB")


# ------------------------------------------------------------------ command


def chapter_images(ep_root: Path, n: int, scenes: list, hd: bool = True, mode: str | None = None) -> list[Path]:
    """One picture per scene, for images.watermark `mode` (default: the setting).

    crop (today's way): the 1920x1080 chaiNNer copy (images/cNNNN/hd/, made by episode_hd) when it is there and
    not older than the download, else the mark-free copy from clean_chapter.
    remove: the same from the clean/ copies of episode_clean (full frame where the mark was removed, cropped where
    removal failed its check; either way 16:9, made here when missing or out of date); an hd copy counts only when
    it is newer than the clean copy and was made in this mode.
    none: the downloads as they are, with the mark in them (or hd copies made from them)."""
    from . import episode_clean as CL
    mode = mode or CL.mode_for(ep_root, n)
    img_dir = ep_root / "images" / f"c{n:04d}"
    base = {p.name: p for p in CL.clean_chapter(ep_root, n)["paths"]} if mode == "remove" else {}
    hd_ok = hd and CL.hd_mode(img_dir) == mode          # hd copies made in another mode are not ours
    found, need_clean = {}, False
    for s in scenes:
        name = f"s{s['id']:02d}.jpg"
        made, src = img_dir / "hd" / name, img_dir / f"s{s['id']:02d}-2k.jpg"
        ref = base.get(name, src)                       # what an hd copy was made from: it must not be older
        if hd_ok and made.exists() and (not ref.exists() or made.stat().st_mtime >= ref.stat().st_mtime):
            found[name] = made
        else:
            need_clean = True
    if mode == "crop":
        if need_clean:
            CL.discard_remove_copies(ep_root, n)        # clean copies made by `remove` are not today's crop
        clean = {p.name: p for p in clean_chapter(ep_root, n)} if need_clean else {}   # redone only if newer
    elif mode == "remove":
        clean = base
    else:
        clean = {f"s{s['id']:02d}.jpg": img_dir / f"s{s['id']:02d}-2k.jpg" for s in scenes}
    out = [found.get(f"s{s['id']:02d}.jpg") or clean[f"s{s['id']:02d}.jpg"] for s in scenes]
    if need_clean:
        what = {"crop": "cleaned downloads", "remove": "clean copies", "none": "raw downloads (mark left in)"}[mode]
        print(f"  chapter {n}: {len(found)} hd pictures, {len(out) - len(found)} {what} (no hd copy yet)")
    return out


def _plan_title(ep_root: Path, n: int, style: str = "kenburns"):
    nar = narration(ep_root, n)
    if style == "panels":
        from . import episode_panels as P
        pl = P.plan(nar["starts"], nar["audio"], nar["scenes"])
    else:
        focus = [tuple(s.get("focus", [0.5, 0.5])) for s in nar["scenes"]]
        pl = plan(nar["starts"], nar["audio"], focus)
    info = E.info(ep_root)
    from .episode_scenes import chapter_paras
    from .heading import body_start
    # Older chapters open with a spoken heading (paragraph 0); new ones start with the story.
    heading_end = nar["para_starts"].get(1, 3.0) if body_start(chapter_paras(ep_root, n)) else 0.0
    cfg = E.cfg(info["episode"], n)
    # The title card shows only the episode number, over the first chapter of the episode; other chapters
    # (and extras such as the intro hook) have none. The panels style letters every sentence as a caption,
    # so its card is short: gone soon after the story starts, and the first caption with it.
    show = n == info["start"] and not info.get("extra")
    dur = max(3.0, LEAD + heading_end) if style == "panels" else min(10.0, max(4.0, LEAD + heading_end + 2.5))
    title = {"dur": round(dur, 3) if show else 0.0, "show": show, "chapter": n,
             "font": str(title_font_file(cfg)), "font_setting": title_font_setting(cfg),
             "name": f"Episode {info['episode']}", "series": "", "layout": "episode"}
    return nar, pl, title


def panel_cues(ep_root: Path, n: int, pl: dict, title: dict, images: list | None = None) -> list[dict]:
    """The panels style's captions: English subtitle lines cut to fit the panel being narrated, lettered
    inside it (panel_captions decides what and when, panel_lettering how it looks, episode_panels where in
    the panel: away from the busy part of the picture when `images` are given)."""
    from . import episode_captions as C
    from . import panel_captions as PC
    from . import panel_lettering as PL
    from . import episode_panels as P
    C.ensure(ep_root, n)
    cues = PC.cues(C.timed_lines(ep_root, n), pl, PL.measure, lead=LEAD, title_end=title["dur"])
    for c in cues:                                     # they stay on their panel, stacked: none fades out
        c["fade_out"] = False
    return P.place_captions(pl, cues, images)


def _fades(pl, title, ass: Path | None, style: str = "kenburns", cues: list | None = None):
    """Windows where a frame change is by design, not a camera jerk: title and end fades, image
    dissolves (panels: camera slides), and a subtitle box (or panel caption) appearing or disappearing."""
    fades = [(0, title["dur"] + 0.2), (pl["total"] - TAIL, pl["total"])]
    if style == "panels":
        from . import episode_panels as P
        fades += P.slide_windows(pl)
        if cues:
            from . import panel_lettering as PL
            for c in cues:
                fades += [(c["t0"], c["t0"] + PL.POP)]
    else:
        fades += [(v["t1"] - v["xf_out"] / 2, v["t1"] + v["xf_out"] / 2) for v in pl["views"][:-1]]
    if ass and ass.exists():
        import re
        def secs(t):
            h, m, x = t.split(":")
            return int(h) * 3600 + int(m) * 60 + float(x)
        for a, b in re.findall(r"Dialogue: 0,([\d:.]+),([\d:.]+)", ass.read_text(encoding="utf-8")):
            fades += [(secs(a), secs(a)), (secs(b), secs(b))]
    return fades


def recheck(ep: int, n: int) -> dict:
    """Run the checks again on an existing c000N.mp4 (no rendering) and update its report."""
    ep_root = E.root(ep).resolve()
    out = ep_root / "video" / f"c{n:04d}.mp4"
    rep = out.with_suffix(".render.json")
    report = json.loads(rep.read_text(encoding="utf-8"))
    style = report.get("style", "kenburns")
    nar, pl, title = _plan_title(ep_root, n, style)
    total = pl["total"]
    pi = probe(out)
    vs = next(s for s in pi["streams"] if s["codec_type"] == "video")
    secs = float(pi["format"]["duration"])
    cues = panel_cues(ep_root, n, pl, title) if style == "panels" and report.get("captions") else None
    stats = frame_stats(out, LEAD, TAIL, _fades(pl, title, out.with_suffix(".en.ass") if report.get("captions") else None,
                                                style, cues))
    report.update(stats)
    report["pass"] = (vs["width"], vs["height"]) == (W, H) and abs(secs - total) <= 1 / FPS + 0.05         and report["has_audio"] and stats["jerk"] < 2 and stats["blank_frames"] == 0
    rep.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def render(ep: int, n: int, crf: str, preset: str, workers: int, end: float | None,
           root: Path | None = None, captions: bool = True, hd: bool = True, style: str | None = None) -> dict:
    """Render chapter n. style: None = the setting (video.style); another style is a try, and its file
    gets `-<style>` in its name (c0015-panels.mp4) so it never replaces the real chapter video."""
    ep_root = (root or E.root(ep)).resolve()
    setting = E.video_style(E.cfg(ep, n))
    if style is None:
        style = setting
    elif style not in E.VIDEO_STYLES:
        raise SystemExit(f"style '{style}': use {' or '.join(E.VIDEO_STYLES)}")
    sub = ""
    if captions and style != "panels":                 # English subtitles, burned in by libass
        from . import episode_captions as C
        C.ensure(ep_root, n)
        ass = ep_root / "video" / f"c{n:04d}.en.ass"
        ass.parent.mkdir(parents=True, exist_ok=True)
        C.write_ass(ep_root, n, ass)
        # absolute, escaped path plus the bundled fonts folder: nothing depends on ffmpeg's working directory
        sub = "," + C.chapter_ass_filter(ep_root, n, ass)
    nar, pl, title = _plan_title(ep_root, n, style)
    is_extra = E.info(ep_root).get("extra")             # an extra (the intro hook): its own pictures, no Flow mark
    wm = "none" if is_extra else CL_mode(ep_root, n)
    images = chapter_images(ep_root, n, nar["scenes"], hd, wm)
    cues = panel_cues(ep_root, n, pl, title, images) if captions and style == "panels" else []

    total = end if end else pl["total"]
    nframes = round(total * FPS)
    tag = f"-{style}" if style != setting and not is_extra else ""   # a try of the other style: never the real name
    out = ep_root / "video" / f"c{n:04d}{f'-first{int(end)}s' if end else ''}{tag}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(nar["mp3"]),
           "-filter_complex", f"[0:v]scale=out_color_matrix=bt709:out_range=tv,format=yuv420p{sub}[v];"
                              f"[1:a]adelay={int(LEAD * 1000)}:all=1,apad[a]",
           "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-preset", preset, "-crf", crf,
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    logo_cfg = E.cfg(ep, n)["video"]["logo"]           # chapter layer: a chapter can switch it off
    logo = logo_layer(logo_cfg, (W, H))
    state = {"style": style, "total": pl["total"], "images": [str(p.resolve()) for p in images], "title": title,
             "logo": logo}
    if style == "panels":
        pc = E.cfg(ep, n)["video"].get("panels") or {}
        state.update({"panels": pl, "paper": pc.get("paper") or "#f3f0e8",
                      "dim": float(pc["dim"]) if pc.get("dim") is not None else 0.45, "cues": cues})
    else:
        state["views"] = pl["views"]
    batch = 6
    t0 = time.time()
    with mp.Pool(workers, initializer=_init, initargs=(state,)) as pool:
        pending: deque = deque()
        nxt, done = 0, 0
        while done < nframes:
            # At most 2 batches per worker in flight: ~6 MB a frame, so ~450 MB.
            while nxt < nframes and len(pending) < 2 * workers:
                pending.append(pool.apply_async(_frames, (list(range(nxt, min(nframes, nxt + batch))),)))
                nxt += batch
            data = pending.popleft().get()
            ff.stdin.write(data)
            done += len(data) // (W * H * 3)
            if done % (FPS * 30) < batch:
                el = time.time() - t0
                print(f"  {done / FPS:6.0f}s of {total:.0f}s  {done / el:5.1f} fps  "
                      f"eta {(nframes - done) / max(done / el, 1e-6) / 60:4.1f} min", flush=True)
    ff.stdin.close()
    if ff.wait():
        sys.exit(f"ffmpeg failed (exit {ff.returncode})")
    took = time.time() - t0

    fades = _fades(pl, title, ass if sub else None, style, cues)
    pi = probe(out)
    vs = next(s for s in pi["streams"] if s["codec_type"] == "video")
    secs = float(pi["format"]["duration"])
    stats = frame_stats(out, LEAD, 0 if end else TAIL, fades)
    views = pl["panels"] if style == "panels" else pl["views"]
    extra = {"panels": len(pl["panels"]), "pages": len(pl["pages"])} if style == "panels" else {}
    report = {"file": out.name, "renderer": "ffmpeg", "width": vs["width"], "height": vs["height"],
              "seconds": round(secs, 3), "expected_seconds": round(total, 3),
              "render_seconds": round(took, 1), "render_fps": round(nframes / took, 2),
              "mb": round(out.stat().st_size / 1e6, 1),
              "has_audio": any(s["codec_type"] == "audio" for s in pi["streams"]),
              "style": style, "images": len(images), "views": len(views),
              "view_seconds": round(sum(v["t1"] - v["t0"] for v in views) / len(views), 2),
              **extra, "captions": bool(sub or cues), "logo": logo_signature(logo_cfg), "title_font": title["font_setting"], "watermark": wm, "crf": crf, "preset": preset, "workers": workers, **stats}
    report["pass"] = (vs["width"], vs["height"]) == (W, H) and abs(secs - total) <= 1 / FPS + 0.05 \
        and report["has_audio"] and stats["jerk"] < 2 and stats["blank_frames"] == 0
    out.with_suffix(".render.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def pages(ep: int, n: int, hd: bool = True, root: Path | None = None) -> list[Path]:
    """The panels layout of chapter n as still pictures, video/cNNNN-pages/pNN.jpg (no video, no ffmpeg)."""
    from . import episode_panels as P
    ep_root = (root or E.root(ep)).resolve()
    nar = narration(ep_root, n)
    images = chapter_images(ep_root, n, nar["scenes"], hd, CL_mode(ep_root, n))
    pl = P.plan(nar["starts"], nar["audio"], nar["scenes"])
    paper = (E.cfg(ep, n)["video"].get("panels") or {}).get("paper") or "#f3f0e8"
    return P.page_images(pl, [str(p) for p in images], ep_root / "video" / f"c{n:04d}-pages", paper)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--episode", type=int, required=True)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--book", help="default: the current book")
    ap.add_argument("--crf", default="20", help="H.264 quality; 18 better/larger, 23 smaller")
    ap.add_argument("--preset", default="fast", help="x264 preset; medium is ~1.5x slower, slightly smaller")
    ap.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 2))
    ap.add_argument("--end", type=float, default=None, help="render only the first N seconds")
    ap.add_argument("--root", type=Path, default=None,
                    help="episode folder to render from instead of workspace/episodes/E00N")
    ap.add_argument("--recheck", action="store_true", help="re-run the checks on the existing video, no rendering")
    ap.add_argument("--no-captions", action="store_true", help="leave out the English subtitles")
    ap.add_argument("--no-hd", action="store_true", help="use the cleaned downloads, not the chaiNNer hd pictures")
    ap.add_argument("--style", choices=E.VIDEO_STYLES, default=None,
                    help="draw in this style whatever video.style says (the file gets -<style> in its name)")
    a = ap.parse_args()
    from novel.core import context
    context.activate(context.resolve(a.book)[0])        # point E.root at this book's episodes
    if a.recheck:
        report = recheck(a.episode, a.chapter)
        print(f"  jerk {report['jerk']}  blank {report['blank_frames']}  pass {report['pass']}")
        sys.exit(0 if report["pass"] else 1)
    report = render(a.episode, a.chapter, a.crf, a.preset, a.workers, a.end, a.root, not a.no_captions, not a.no_hd,
                    a.style)
    for k, v in report.items():
        print(f"  {k:18} {v}")
    sys.exit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
