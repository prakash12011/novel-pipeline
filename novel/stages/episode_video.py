"""Build, render and join an episode's chapter videos with HyperFrames.

One HyperFrames project per chapter, generated from video/chapter-template/:
one sub-composition per image, a title card over the first image while the
chapter heading is read, the narration, and a fade to black. A chapter is the
restart unit: a failed render costs one chapter, and finished chapters are
joined with a plain stream copy because every one starts and ends on black.

When each image appears comes from the measured timing map: image k cuts in,
with a crossfade centred on the moment, when the narrator starts the first
paragraph of its window (episode_scenes.chapter_windows).

Usage:
    python -m abpipe.episode_video build  --episode 1 --chapter 1
    python -m abpipe.episode_video render --episode 1 --chapter 1
    python -m abpipe.episode_video join   --episode 1
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from . import episode as E
from .episode_scenes import chapter_paras, chapter_windows, load_roster, scenes_path
from .manifest import Manifest
from .package import _ffmetadata

TEMPLATE = Path(__file__).resolve().parent.parent / "video" / "chapter-template"
GSAP = Path(shutil.which("hyperframes") or ".").resolve().parent / \
    "node_modules/hyperframes/dist/vendor/npm/gsap@3.15.0/dist/gsap.min.js"
LEAD = 1.5          # black, then the first image fades up, before the narration starts
TAIL = 2.0          # the last image holds, then fades to black, after it ends
XFADE = 0.8         # crossfade between images, centred on the paragraph boundary
FPS = 24


def series_label() -> str:
    return E.series_label()


def logo_signature(logo: dict | None) -> dict | None:
    """None when the logo is off. Else the settings plus the file's size and mtime, so a changed
    setting or a replaced picture counts as a change (stored in the chapter's render report)."""
    if not logo or not logo.get("enabled"):
        return None
    from novel.core import settings
    f = settings.asset_path(logo.get("file") or "assets/logo.png")
    if not f.exists():
        raise SystemExit(f"video.logo is enabled but the logo file does not exist: {f}")
    st = f.stat()
    return {"file": str(f), "size": st.st_size, "mtime": st.st_mtime_ns,
            "position": logo.get("position", "top-left"), "margin": int(logo.get("margin", 26)),
            "opacity": float(logo.get("opacity", 1.0)), "width": logo.get("width")}


def logo_layer(logo: dict | None, size: tuple[int, int]):
    """(rgb, alpha, (x, y)) of the logo ready to paste onto a frame of `size`, or None when off."""
    sig = logo_signature(logo)
    if not sig:
        return None
    from PIL import Image
    im = Image.open(sig["file"]).convert("RGBA")
    if sig["width"]:
        im = im.resize((int(sig["width"]), max(1, round(im.height * int(sig["width"]) / im.width))), Image.LANCZOS)
    alpha = im.getchannel("A")
    if sig["opacity"] < 1.0:
        alpha = alpha.point(lambda v: round(v * sig["opacity"]))
    pos, m = sig["position"], sig["margin"]
    if pos not in ("top-left", "top-right", "bottom-left", "bottom-right"):
        raise SystemExit(f"video.logo.position '{pos}': use top-left, top-right, bottom-left or bottom-right")
    x = m if pos.endswith("left") else size[0] - im.width - m
    y = m if pos.startswith("top") else size[1] - im.height - m
    return im.convert("RGB"), alpha, (x, y)


def paste_logo(img, layer) -> None:
    if layer:
        rgb, alpha, xy = layer
        img.paste(rgb, xy, alpha)


DEFAULT_TITLE_FONT = "assets/fonts/PlayfairDisplay.ttf"


def title_font_setting(cfg: dict) -> str:
    """video.title_font as written in the settings (stored in the render report: a change makes the chapter
    video stale). Reports from before this setting was read have no entry and mean the default."""
    return str(((cfg or {}).get("video") or {}).get("title_font") or DEFAULT_TITLE_FONT)


def title_font_file(cfg: dict) -> Path:
    from novel.core import settings
    f = settings.asset_path(title_font_setting(cfg))
    if not f.is_file():
        raise SystemExit(f"video.title_font: the font file does not exist: {f}")
    return f


def card_fonts(cfg: dict) -> tuple[Path, Path]:
    """(regular, bold) font files for the disclaimer and subscribe cards, from settings."""
    from novel.core import settings
    v = cfg["video"]
    return settings.asset_path(v["disclaimer_font"]), settings.asset_path(v["disclaimer_font_bold"])
# Camera moves, cycled by scene number. Pans run at 1.12x so 40 px of travel
# never shows an edge (1920 x 0.12 / 2 = 115 px of margin each side).
MOVES = [
    ("{scale: 1.04, x: 0, y: 0}", "{scale: 1.16, x: 0, y: 0}"),     # push in
    ("{scale: 1.12, x: 40, y: 0}", "{scale: 1.12, x: -40, y: 0}"),  # pan left
    ("{scale: 1.16, x: 0, y: 0}", "{scale: 1.04, x: 0, y: 0}"),     # pull out
    ("{scale: 1.12, x: -40, y: 0}", "{scale: 1.12, x: 40, y: 0}"),  # pan right
]


# Flow stamps a small ✦ near the bottom-right corner of every image: on the
# 2752x1536 downloads its top edge sits 92% of the way down. Taking 10% off the
# bottom removes it, and 5% off each side keeps the frame 16:9. Scene prompts
# ask Flow to keep those edges empty (episode_scenes.FOOTER). The downloads
# are never changed; only the copies inside the video project are cropped.
CROP_BOTTOM, CROP_SIDE = 0.10, 0.05
MARK = Path(__file__).resolve().parents[2] / "assets" / "watermark-template.png"


def find_mark(image: Path) -> tuple[float, float, float]:
    """Where Flow's mark is, as (x, y) of its TOP-LEFT in image fractions, and the
    match score (normalised cross-correlation, 1 = identical).

    Searched in the bottom-right corner only, by FFT correlation against a
    template cut from a real download.
    """
    from PIL import Image
    img = np.asarray(Image.open(image).convert("L"), dtype=np.float64)
    tpl = np.asarray(Image.open(MARK).convert("L"), dtype=np.float64)
    H, W = img.shape
    y0, x0 = int(H * 0.70), int(W * 0.75)
    reg = img[y0:, x0:]
    th, tw = tpl.shape
    t0 = tpl - tpl.mean()
    shape = (reg.shape[0] + th, reg.shape[1] + tw)
    corr = np.fft.irfft2(np.fft.rfft2(reg, shape) * np.conj(np.fft.rfft2(t0, shape)), shape)
    corr = corr[: reg.shape[0] - th + 1, : reg.shape[1] - tw + 1]
    # Window sums from integral images, for each window's own brightness spread.
    ii = np.pad(reg, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    ii2 = np.pad(reg ** 2, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    win = lambda a: a[th:, tw:] - a[:-th, tw:] - a[th:, :-tw] + a[:-th, :-tw]
    n = th * tw
    var = np.maximum(win(ii2) - win(ii) ** 2 / n, 1e-6)
    ncc = corr / (np.sqrt(var) * np.sqrt((t0 ** 2).sum()))
    y, x = np.unravel_index(ncc.argmax(), ncc.shape)
    return (x0 + x) / W, (y0 + y) / H, float(ncc.max())


def painted_marks(image: Path, threshold: float = 0.7) -> list[tuple[float, float, float, float]]:
    """Sparkles that survive the crop: (x, y, score, size) for each, in image fractions.

    The image model sometimes PAINTS a copy of Flow's mark into the picture,
    larger and higher up (chapter 1 scene 8: a 95 px ✦ above the real 54 px
    one). Cropping cannot remove it, so cleaned images are searched for the
    template at several sizes across their bottom-right 40%.
    """
    from PIL import Image
    img = Image.open(image).convert("L")
    tpl = Image.open(MARK).convert("L")
    found = []
    for size in (1.0, 1.3, 1.7, 2.2):
        t = tpl.resize((round(tpl.width * size), round(tpl.height * size)), Image.LANCZOS)
        a = np.asarray(img, dtype=np.float64)
        H, W = a.shape
        y0, x0 = int(H * 0.6), int(W * 0.6)
        reg, tp = a[y0:, x0:], np.asarray(t, dtype=np.float64)
        th, tw = tp.shape
        t0 = tp - tp.mean()
        shape = (reg.shape[0] + th, reg.shape[1] + tw)
        corr = np.fft.irfft2(np.fft.rfft2(reg, shape) * np.conj(np.fft.rfft2(t0, shape)), shape)
        corr = corr[: reg.shape[0] - th + 1, : reg.shape[1] - tw + 1]
        ii = np.pad(reg, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        ii2 = np.pad(reg ** 2, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        win = lambda m: m[th:, tw:] - m[:-th, tw:] - m[th:, :-tw] + m[:-th, :-tw]
        var = np.maximum(win(ii2) - win(ii) ** 2 / (th * tw), 1e-6)
        ncc = corr / (np.sqrt(var) * np.sqrt((t0 ** 2).sum()))
        y, x = np.unravel_index(ncc.argmax(), ncc.shape)
        if ncc.max() >= threshold:
            found.append(((x0 + x) / W, (y0 + y) / H, round(float(ncc.max()), 2), size))
    return found


def clean_image(src: Path, dest: Path, scale: str | None = None) -> None:
    """Write src without Flow's mark: found, then cropped (and optionally scaled).

    The mark is the same size in pixels on every download, so on a half-size
    1376x768 image it sits higher (84% down, against 92% on a 2752x1536 one).
    The crop is therefore taken from where the mark actually is: its top edge
    plus a 1% margin, never less than CROP_BOTTOM, and half as much off each
    side to stay 16:9.
    """
    mx, my, score = find_mark(src)
    bottom = CROP_BOTTOM
    if score > 0.6:
        bottom = max(CROP_BOTTOM, 1 - my + 0.01)
    else:
        print(f"  note: no mark found in {src.name} (best match {score:.2f}) - check it by eye")
    side = bottom / 2
    vf = f"crop=iw*{1 - 2 * side:.5f}:ih*{1 - bottom:.5f}:iw*{side:.5f}:0"
    if scale:
        vf += f",scale={scale}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    p = run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf", vf, "-q:v", "2", dest])
    if p.returncode:
        raise SystemExit(f"could not prepare {src}: {p.stderr[-300:]}")
    # The image model sometimes PAINTS a sparkle into the picture, which no crop
    # removes. Say so, but keep the image: the user chose to render with what exists.
    painted = painted_marks(dest)
    if painted:
        x, y, score, size = painted[0]
        print(f"  warning: {src.parent.name}/{src.name}: a sparkle may be painted into the picture "
              f"at {x:.0%} across, {y:.0%} down (match {score}) - check it by eye")


def clean_chapter(ep_root: Path, n: int) -> list[Path]:
    """Clean, full-size copies of every downloaded image, in images/cNNNN/clean/.

    Only images that are new or newer than their clean copy are redone.
    """
    img_dir = ep_root / "images" / f"c{n:04d}"
    done = []
    for src in sorted(img_dir.glob("s*-2k.jpg")):
        dest = img_dir / "clean" / src.name.replace("-2k", "")
        if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
            clean_image(src, dest)
        done.append(dest)
    return done


def hf() -> str:
    exe = shutil.which("hyperframes")
    if not exe:
        sys.exit("hyperframes CLI not found - npm i -g hyperframes")
    return exe


def run(cmd: list, cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-show_entries",
               "format=duration:stream=codec_type,width,height,r_frame_rate",
               "-of", "json", path]).stdout
    return json.loads(out)


def fill(text: str, values: dict) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    left = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if left:
        raise ValueError(f"template placeholders left unfilled: {sorted(set(left))}")
    return text


def chapter_title(ep_root: Path, n: int) -> str:
    man = Manifest.load(ep_root / "manifest.json")
    title = next(c.title for c in man.chapters if c.idx == n)
    return title.split(":", 1)[1].strip().strip('"') if ":" in title else title


def plan_timeline(starts: list[float], audio: float) -> list[tuple[float, float, float]]:
    """(host start, host duration, fade) per image, in video seconds.

    starts[k] is when image k's window begins in the narration (k >= 1); image
    0 is on screen from the first frame. Neighbouring hosts overlap by XFADE so
    the incoming image can fade in over the outgoing one.
    """
    end = LEAD + audio + TAIL
    cuts = [0.0] + [LEAD + s for s in starts[1:]]
    out = []
    for k, cut in enumerate(cuts):
        begin = 0.0 if k == 0 else cut - XFADE / 2
        stop = end if k == len(cuts) - 1 else cuts[k + 1] + XFADE / 2
        if stop - begin < 2 * XFADE + 1:
            raise ValueError(f"image {k + 1} would be on screen for only {stop - begin:.1f}s")
        out.append((round(begin, 3), round(stop - begin, 3), LEAD if k == 0 else XFADE))
    return out


def narration(ep_root: Path, n: int) -> dict:
    """What both renderers need: scenes, image downloads, the mp3, its length, and
    `starts` - when the narrator begins each image's window - plus `para_starts`
    (paragraph number -> first chunk's start), all in narration seconds."""
    name = f"{n:05d}"
    E.check_lock(ep_root, n)
    doc = json.loads(scenes_path(ep_root, n).read_text(encoding="utf-8"))
    scenes = doc["scenes"]
    img_dir = ep_root / "images" / f"c{n:04d}"
    images = [img_dir / f"s{s['id']:02d}-2k.jpg" for s in scenes]
    missing = [p.name for p in images if not p.exists()]
    if missing:
        raise SystemExit(f"chapter {n}: {len(missing)} images missing ({missing[:4]})")
    mp3 = ep_root / "audio" / f"{name}.mp3"
    tmap = json.loads((ep_root / "timing" / f"{name}.json").read_text(encoding="utf-8"))
    audio = float(probe(mp3)["format"]["duration"])
    if abs(audio - tmap["total_seconds"]) > 0.25:
        raise SystemExit(f"chapter {n}: mp3 is {audio:.2f}s but the timing map says "
                         f"{tmap['total_seconds']:.2f}s - they are not the same narration")
    first_chunk, span = {}, {}
    for c in tmap["chunks"]:
        first_chunk.setdefault(c["para"], c["start_sec"])
        a, b = span.get(c["para"], (c["start_sec"], c["end_sec"]))
        span[c["para"]] = (min(a, c["start_sec"]), max(b, c["end_sec"]))
    paras = chapter_paras(ep_root, n)
    wins = chapter_windows(paras, doc.get("pacing"))
    # A window that starts mid-paragraph starts that far into the paragraph's
    # narration: speech runs at an even pace within a paragraph (one TTS chunk).
    starts = [span[a][0] + (span[a][1] - span[a][0]) * sa / max(1, len(paras[a]))
              for a, sa, _, _ in wins]
    return {"scenes": scenes, "images": images, "mp3": mp3, "audio": audio,
            "starts": starts, "para_starts": first_chunk}


def build(ep_root: Path, n: int, out_dir: Path) -> dict:
    """Write the chapter's HyperFrames project into out_dir. Returns its timing."""
    nar = narration(ep_root, n)
    scenes, images, mp3, audio, starts = (nar[k] for k in ("scenes", "images", "mp3", "audio", "starts"))
    slots = plan_timeline(starts, audio)

    # Replace only what this generates. Removing the folder itself fails on
    # Windows while a preview or check's Chrome still has it open.
    for sub in ("compositions", "assets"):
        if (out_dir / sub).exists():
            shutil.rmtree(out_dir / sub)
    (out_dir / "compositions").mkdir(parents=True)
    (out_dir / "assets").mkdir()
    shutil.copy2(GSAP, out_dir / "assets" / "gsap.min.js")
    shutil.copy2(mp3, out_dir / "assets" / "vo.mp3")
    for f in ("hyperframes.json",):
        shutil.copy2(TEMPLATE / f, out_dir / f)

    scene_tpl = (TEMPLATE / "compositions" / "scene.html").read_text(encoding="utf-8")
    hosts = []
    for k, (s, img, (start, dur, fade)) in enumerate(zip(scenes, images, slots)):
        sid = f"s{k + 1:02d}"
        # Downscale once here: decoding a 2752 px JPEG on every frame is slow,
        # and 2400 px still leaves room for a 1.16x push-in at 1920.
        clean_image(img, out_dir / "assets" / f"{sid}.jpg", scale="2400:-2")
        fx, fy = s.get("focus", [0.5, 0.5])
        frm, to = MOVES[k % len(MOVES)]
        (out_dir / "compositions" / f"{sid}.html").write_text(fill(scene_tpl, {
            "ID": sid, "DURATION": dur, "FADE": fade, "FROM": frm, "TO": to,
            "FOCUS_X": round(fx * 100, 1), "FOCUS_Y": round(fy * 100, 1),
            "ALT": html.escape(s["scene"][:160], quote=True),
        }), encoding="utf-8")
        hosts.append(_host(sid, start, dur, track=1 + k % 2, z=k + 1))

    # The title stays up while the chapter heading is read, at least 4 s.
    title_dur = round(max(4.0, min(10.0, LEAD + starts[1] if len(starts) > 1 else 6.0)), 3)
    first_in_episode = n == E.info(ep_root)["start"]
    series = (f'<p id="title-series">{html.escape(series_label())} · Episode '
              f'{E.info(ep_root)["episode"]}</p>' if first_in_episode else "")
    (out_dir / "compositions" / "title.html").write_text(fill(
        (TEMPLATE / "compositions" / "title.html").read_text(encoding="utf-8"),
        {"DURATION": title_dur, "OUT_AT": round(title_dur - 1.1, 3), "CHAPTER": n,
         "NAME": html.escape(chapter_title(ep_root, n)), "SERIES_HTML": series}), encoding="utf-8")
    hosts.append(_host("title", 0, title_dur, track=3, z=100))

    total = round(LEAD + audio + TAIL, 3)
    fade_dur = TAIL - 0.2
    (out_dir / "compositions" / "fade.html").write_text(fill(
        (TEMPLATE / "compositions" / "fade.html").read_text(encoding="utf-8"),
        {"DURATION": fade_dur + 0.2, "FADE": fade_dur}), encoding="utf-8")
    hosts.append(_host("fade", round(total - fade_dur - 0.2, 3), fade_dur + 0.2, track=4, z=101))

    (out_dir / "index.html").write_text(fill(
        (TEMPLATE / "index.html").read_text(encoding="utf-8"),
        {"DURATION": total, "FPS": FPS, "LEAD": LEAD, "AUDIO": round(audio, 3),
         "HOSTS": "\n".join(hosts)}), encoding="utf-8")
    timing = {"chapter": n, "duration": total, "audio": round(audio, 3), "lead": LEAD, "title": title_dur,
              "tail": TAIL, "fps": FPS, "images": [{"id": f"s{k + 1:02d}", "start": a, "dur": d}
                                                   for k, (a, d, _) in enumerate(slots)]}
    (out_dir / "timeline.json").write_text(json.dumps(timing, indent=1), encoding="utf-8")
    return timing


def _host(sid: str, start: float, dur: float, track: int, z: int) -> str:
    return (f'      <div id="{sid}" data-composition-id="{sid}" '
            f'data-composition-src="compositions/{sid}.html" data-start="{start}" '
            f'data-duration="{dur}" data-track-index="{track}" data-track-kind="graphics" '
            f'data-width="1920" data-height="1080" style="z-index: {z}"></div>')


def check(proj: Path) -> list[str]:
    """Errors from `hyperframes check`; warnings are printed, not fatal."""
    p = run([hf(), "check", "--json"], cwd=proj, timeout=900)
    try:
        d = json.loads(p.stdout[p.stdout.index("{"):])
    except ValueError:
        return [f"check produced no JSON: {(p.stdout + p.stderr)[-800:]}"]
    errors = []
    for k, v in d.items():
        if isinstance(v, dict):
            for f in v.get("findings", []):
                line = f"{k}/{f.get('code')}: {f.get('message', '')}"
                if f.get("severity") == "error":
                    errors.append(line)
                else:
                    print(f"  warning {line}")
    return errors


def frame_stats(video: Path, lead: float, tail: float,
                skip: list[tuple[float, float]] = ()) -> dict:
    """Stream the video once, small and gray: jerky motion and blank frames.

    jerk: the worst frame-to-frame change relative to its neighbours (a camera
    that steps instead of glides scores high; under 2 looks smooth). It is
    only meaningful during a camera move, so `skip` takes out the crossfades
    and title fades: a crossfade's first frame jumps by design, and scored 2.1
    on the first test render. blank: frames that are nearly black outside the
    deliberate lead and tail - a crossfade that dips to black shows up here.
    """
    W, H = 192, 108
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(video), "-vf",
                             f"scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                            stdout=subprocess.PIPE)
    size, prev, diffs, means = W * H, None, [], []
    while True:
        buf = proc.stdout.read(size * 240)
        if not buf:
            break
        frames = np.frombuffer(buf[: len(buf) // size * size], np.uint8).reshape(-1, H, W).astype(np.float32)
        means += frames.mean(axis=(1, 2)).tolist()
        if prev is not None:
            frames = np.concatenate([prev[None], frames])
        diffs += np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2)).tolist()
        prev = frames[-1]
    proc.wait()
    d = np.array(diffs)
    fps = len(means) / max(1e-6, float(probe(video)["format"]["duration"]))
    local = np.convolve(d, np.ones(9) / 9, mode="same")
    ratio = d / (local + 0.05)
    # A jump must also be big enough to see. Chapter 1's worst "jerks" (2.2) were all H.264
    # keyframes, where the picture shifts by 0.37 of 255 levels: encoder noise, not motion.
    ratio[d < 1.0] = 0
    keep = np.ones(len(d), bool)
    keep[:4], keep[-4:] = False, False
    for a, b in skip:
        keep[max(0, int(a * fps) - 5): int(b * fps) + 5] = False
    jerk = float(ratio[keep].max()) if keep.any() else 0.0
    body = np.array(means[int((lead + 0.5) * fps): len(means) - int((tail + 0.5) * fps)])
    return {"frames": len(means), "jerk": round(jerk, 2),
            "blank_frames": int((body < 4).sum()), "darkest_body_frame": round(float(body.min()), 1)}


# ------------------------------------------------------------------ commands


def fades(timing: dict) -> list[tuple[float, float]]:
    """Every moment something fades rather than moves: crossfades, title, lead, tail."""
    out = [(0, timing["lead"] + 0.2), (timing["duration"] - timing["tail"], timing["duration"])]
    out += [(i["start"], i["start"] + XFADE) for i in timing["images"][1:]]
    out += [(0, timing.get("title", 10.0) + 0.2)]
    return out


def proj_dir(ep_root: Path, n: int) -> Path:
    return ep_root / "video" / f"c{n:04d}" / "hf"


def cmd_clean(args) -> None:
    files = clean_chapter(E.root(args.episode), args.chapter)
    print(f"{len(files)} clean images in {files[0].parent if files else '-'}")


def cmd_build(args) -> None:
    ep_root = E.root(args.episode)
    proj = proj_dir(ep_root, args.chapter)
    t = build(ep_root, args.chapter, proj)
    print(f"built {proj}: {len(t['images'])} images, {t['duration'] / 60:.1f} min")
    errors = check(proj)
    print("check " + ("passed" if not errors else "FAILED:\n  " + "\n  ".join(errors)))
    sys.exit(1 if errors else 0)


def cmd_render(args) -> None:
    ep_root = E.root(args.episode)
    proj = proj_dir(ep_root, args.chapter)
    timing = json.loads((proj / "timeline.json").read_text(encoding="utf-8"))
    out = ep_root / "video" / f"c{args.chapter:04d}.mp4"
    cmd = [hf(), "render", "-o", out.resolve(), "--fps", FPS, "--crf", args.crf,
           "--workers", args.workers, "--quiet"]
    if args.end:
        cmd += ["--end", args.end]
        out = out.with_name(out.stem + f"-first{args.end}s.mp4")
        cmd[3] = out.resolve()
    t0 = time.time()
    p = run(cmd, cwd=proj)
    took = time.time() - t0
    if p.returncode or not out.exists():
        sys.exit(f"render failed:\n{(p.stdout + p.stderr)[-3000:]}")
    info = probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    secs = float(info["format"]["duration"])
    want = float(args.end) if args.end else timing["duration"]
    stats = frame_stats(out, timing["lead"], 0 if args.end else timing["tail"], fades(timing))
    report = {"file": out.name, "width": v["width"], "height": v["height"], "seconds": round(secs, 3),
              "expected_seconds": want, "render_seconds": round(took, 1),
              "render_fps": round(stats["frames"] / took, 2), "mb": round(out.stat().st_size / 1e6, 1),
              "has_audio": any(s["codec_type"] == "audio" for s in info["streams"]),
              "crf": args.crf, "workers": args.workers, **stats}
    report["pass"] = (v["width"], v["height"]) == (1920, 1080) and abs(secs - want) <= 1 / FPS + 0.05 \
        and report["has_audio"] and stats["jerk"] < 2 and stats["blank_frames"] == 0
    out.with_suffix(".render.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for k, val in report.items():
        print(f"  {k:20} {val}")
    sys.exit(0 if report["pass"] else 1)


def cmd_join(args) -> None:
    ep_root = E.root(args.episode)
    info = E.info(ep_root)
    man = Manifest.load(ep_root / "manifest.json")
    parts = [(c, ep_root / "video" / f"c{c.idx:04d}.mp4") for c in man.chapters]
    missing = [c.idx for c, p in parts if not p.exists()]
    if missing:
        sys.exit(f"chapters not rendered yet: {missing}")

    class Part:          # what package._ffmetadata reads: rendered length and title
        def __init__(self, title, seconds):
            self.title, self.seconds = title, seconds
    marks = [Part(c.title, float(probe(p)["format"]["duration"])) for c, p in parts]
    work = ep_root / "video"
    (work / "concat.txt").write_text("\n".join(f"file '{p.resolve().as_posix()}'" for _, p in parts),
                                     encoding="utf-8")
    (work / "chapters.txt").write_text(_ffmetadata(marks, f"{series_label()} - Episode {info['episode']}"),
                                       encoding="utf-8")
    out = ep_root / f"E{info['episode']:03d}.mp4"
    p = run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", work / "concat.txt",
             "-i", work / "chapters.txt", "-map", "0", "-map_metadata", "1", "-c", "copy",
             "-movflags", "+faststart", out])
    if p.returncode:
        sys.exit(f"join failed: {p.stderr[-800:]}")
    total = float(probe(out)["format"]["duration"])
    print(f"{out}: {total / 3600:.2f} h, {out.stat().st_size / 1e9:.2f} GB, "
          f"{len(marks)} chapter markers (sum of parts {sum(m.seconds for m in marks) / 3600:.2f} h)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("clean", help="mark-free copies of a chapter's images, in images/cNNNN/clean/")
    b = sub.add_parser("build")
    r = sub.add_parser("render")
    for p in (c, b, r):
        p.add_argument("--episode", type=int, required=True)
        p.add_argument("--chapter", type=int, required=True)
    r.add_argument("--crf", default="20", help="H.264 quality; 18 better/larger, 23 smaller")
    r.add_argument("--workers", default="2")
    r.add_argument("--end", default=None, help="render only the first N seconds, to measure speed")
    j = sub.add_parser("join")
    j.add_argument("--episode", type=int, required=True)
    c.set_defaults(fn=cmd_clean)
    b.set_defaults(fn=cmd_build)
    r.set_defaults(fn=cmd_render)
    j.set_defaults(fn=cmd_join)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
