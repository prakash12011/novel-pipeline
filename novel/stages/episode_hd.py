"""Full-HD copies of an episode's Flow images with chaiNNer: mark cropped off, upscaled, 1920x1080.
(images.watermark: remove starts from the clean/ copies of episode_clean instead and crops nothing here;
none starts from the downloads as they are. Each chapter's hd/hd.json records the mode the copies were made in.)

Flow's ✦ mark sits at the bottom right. Each image is cropped above it (same rule as
episode_video.clean_image: the mark's top edge plus 1%, at least 10% off the bottom,
and the sides trimmed to stay 16:9), then upscaled 2x by an anime model on the GPU
(NCNN/Vulkan, so it runs on any card), then resized to exactly 1920x1080.
Images already 1080 px tall after the crop (2K downloads) skip the model.

Nothing here calls an AI service: chaiNNer runs locally from the command line.

    py -X utf8 -m novel.stages.episode_hd --episode 1 --chapters 13 --limit 5
    py -X utf8 -m novel.stages.episode_hd --episode 1 --chapters 5-20
    # -> workspace/episodes/E001/images/cNNNN/hd/sNN.jpg  (redone only when the source is newer)

Settings: CHAINNER (default D:/chaiNNer/app/chaiNNer.exe), CHAINNER_MODEL (an NCNN .param,
its .bin beside it; default realesr-animevideov3-x2).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import episode as E
from .episode_video import CROP_BOTTOM, find_mark

def _setting(key: str, default: str) -> str:
    """settings.chainner.* from the pipeline home, when set; never fails at import."""
    try:
        from novel.core import settings
        return str(settings.get(key, default=None) or default)
    except (Exception, SystemExit):
        return default


CHAINNER = Path(os.environ.get("CHAINNER") or _setting("chainner.exe", "D:/chaiNNer/app/chaiNNer.exe"))
MODEL = Path(os.environ.get("CHAINNER_MODEL") or _setting("chainner.model", "D:/chaiNNer/models/resrgan/models/realesr-animevideov3-x2.param"))
W, H = 1920, 1080
QUALITY = 95
JPEG_444 = 0x111111          # cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444: no colour blur on the line art


# ---------- the chain ----------
# Every input needs a value, hidden ones too (the GUI fills them in; the CLI does not).
# These are chaiNNer 0.26's own defaults for Save Image; toggles are 0/1.
SAVE_DEFAULTS = {2: None, 3: None, 4: "png", 15: "u8", 14: 0, 5: 95, 11: 2167057, 12: 0, 16: "u8", 18: 5,
                 6: "BC1_UNORM_SRGB", 7: 0, 9: 0, 8: 0, 10: 0, 13: 0, 17: "4:2:0", 1000: 0}

def _node(schema: str, inputs: dict, x: int, kind: str = "regularNode") -> dict:
    nid = str(uuid.uuid4())
    return {"id": nid, "type": kind, "position": {"x": x, "y": 0},
            "data": {"id": nid, "schemaId": schema, "inputData": inputs}}


def _edge(src: dict, out: int, dst: dict, inp: int) -> dict:
    return {"id": str(uuid.uuid4()), "source": src["id"], "target": dst["id"],
            "sourceHandle": f"{src['id']}-{out}", "targetHandle": f"{dst['id']}-{inp}",
            "type": "main", "animated": False, "data": {}}


def build_chain(src_dir: Path, out_dir: Path, crop: dict, upscale: bool) -> dict:
    """Load Images (folder) -> Crop (edges) -> [NCNN Upscale] -> Resize 1920x1080 -> Save JPG."""
    load = _node("chainner:image:load_images",
                 {0: str(src_dir), 1: 0, 2: 0, 3: "**/*", 4: 0, 5: 10, 6: 1}, 0, "generator")
    cut = _node("chainner:image:crop",
                {1: 1, 2: 0, 3: 0, 4: crop["left"], 5: crop["bottom"], 6: crop["right"], 7: 100, 8: 100}, 300)
    size = _node("chainner:image:resize", {1: 1, 2: 100.0, 3: W, 4: H, 5: -1, 6: 0}, 900)
    save = _node("chainner:image:save",
                 {**SAVE_DEFAULTS, 1: str(out_dir), 4: "jpg", 5: QUALITY, 11: JPEG_444}, 1200)
    nodes = [load, cut, size, save]
    edges = [_edge(load, 0, cut, 0), _edge(load, 3, save, 3), _edge(size, 0, save, 0)]
    if upscale:
        model = _node("chainner:ncnn:load_model", {0: str(MODEL), 1: str(MODEL.with_suffix(".bin"))}, 300)
        up = _node("chainner:ncnn:upscale_image", {2: 0, 3: 256, 4: 0}, 600)
        nodes += [model, up]
        edges += [_edge(cut, 0, up, 1), _edge(model, 0, up, 0), _edge(up, 0, size, 0)]
    else:
        edges.append(_edge(cut, 0, size, 0))
    content = {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 1}}
    # chaiNNer warns ("tampered with") when the checksum is missing or wrong; it is md5 of the content JSON.
    checksum = hashlib.md5(json.dumps(content, separators=(",", ":")).encode()).hexdigest()
    return {"version": "0.26.1", "content": content, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "checksum": checksum}


def _stop_backends() -> None:
    """chaiNNer's Python backend outlives each run (about 100 MB apiece): stop this install's leftovers."""
    marker = str(CHAINNER.parent).replace("/", "\\").lower()
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
                    f"$_.CommandLine.ToLower().Contains('{marker}') -and $_.CommandLine -like '*server.py*' }} | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"], capture_output=True)


STALL_SECONDS = 240   # chaiNNer sometimes hangs right after it starts (0 CPU, nothing written): no new picture this long = killed
DONE_SECONDS = 20     # all expected pictures written and nothing new this long: done, kill the process that will not exit
TRIES = 3


def run_chain(chain: dict, work: Path, expected: int = 0) -> None:
    path = work / "chain.chn"
    path.write_text(json.dumps(chain, separators=(",", ":")), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}   # set inside VS Code; breaks Electron
    out_dir, log = work / "out", work / "chainner.log"
    for attempt in range(1, TRIES + 1):
        with open(log, "w", encoding="utf-8", errors="replace") as lf:
            p = subprocess.Popen([str(CHAINNER), "run", str(path)], env=env, stdout=lf, stderr=subprocess.STDOUT)
            last, since = -1, time.time()
            try:
                while p.poll() is None:
                    time.sleep(5)
                    n = len(list(out_dir.glob("*.jpg"))) if out_dir.exists() else 0
                    if n != last:
                        last, since = n, time.time()
                    elif expected and n >= expected and time.time() - since > DONE_SECONDS:
                        p.kill()                        # every picture is written; chaiNNer sometimes never exits
                        p.wait()
                        return
                    elif time.time() - since > STALL_SECONDS:
                        p.kill()
                        break
            finally:
                _stop_backends()
        if p.returncode == 0:
            return
        if time.time() - since > STALL_SECONDS and attempt < TRIES:
            print(f"  chaiNNer stalled (no new picture in {STALL_SECONDS} s): killed, trying again ({attempt}/{TRIES})")
            continue
        tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-25:])
        raise SystemExit(f"chaiNNer failed (exit {p.returncode}); log: {log}\n{tail}")


# ---------- which images, and how much to cut ----------

def size_of(path: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def crop_for(images: list[Path], w: int, h: int) -> dict:
    """Pixels to cut for every image of one size: above the highest mark found, sides keep 16:9."""
    bottom = CROP_BOTTOM
    for img in images:
        mx, my, score = find_mark(img)
        if score > 0.6:
            bottom = max(bottom, 1 - my + 0.01)
    b = round(h * bottom)
    side = round(b * w / h / 2)
    return {"left": side, "right": side, "bottom": b, "width": w - 2 * side, "height": h - b}


# ---------- a second trim, for a sparkle that is part of the picture ----------
# The image model sometimes PAINTS a big copy of Flow's mark into the artwork, above the real one
# (chapter 2 scene 15: a 95 px star at 83% down). The first crop only clears the real mark, so such
# an image is searched, at full size, for a star in the bottom-right, and when one is found the
# crop is raised above it (and the sides trimmed to stay 16:9, which also drops any black bars).
STAR_SCORE = 0.75                 # true cases scored 0.77-0.89, everything else stayed under 0.75
STAR_SIZES = (1.0, 1.3, 1.7, 2.2, 2.8)
STAR_MARGIN = 0.012               # of the image height, left clear above the star
STAR_MIN_Y = 0.70                 # a match higher than this is artwork, not a corner star (cutting there would lose a third)


def find_painted_star(src: Path, real: tuple[float, float]) -> tuple[float, float, float, float] | None:
    """(score, x, y, size) of a star in the picture other than Flow's real mark at `real`, or None.

    x, y are the star's top-left corner as fractions of the image; size is a multiple of the
    56 px template. The real mark's own neighbourhood is blanked out before looking for the best match.
    """
    import numpy as np
    from PIL import Image
    from .episode_video import MARK
    img = np.asarray(Image.open(src).convert("L"), dtype=np.float64)
    H_, W_ = img.shape
    y0, x0 = int(H_ * 0.55), int(W_ * 0.65)
    reg = img[y0:, x0:]
    tpl = Image.open(MARK).convert("L")
    best = None
    for s in STAR_SIZES:
        tp = np.asarray(tpl.resize((round(tpl.width * s), round(tpl.height * s)), Image.LANCZOS), dtype=np.float64)
        th, tw = tp.shape
        if th >= reg.shape[0] or tw >= reg.shape[1]:
            continue
        t0 = tp - tp.mean()
        shape = (reg.shape[0] + th, reg.shape[1] + tw)
        corr = np.fft.irfft2(np.fft.rfft2(reg, shape) * np.conj(np.fft.rfft2(t0, shape)), shape)
        corr = corr[: reg.shape[0] - th + 1, : reg.shape[1] - tw + 1]
        ii = np.pad(reg, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        ii2 = np.pad(reg ** 2, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        win = lambda m: m[th:, tw:] - m[:-th, tw:] - m[th:, :-tw] + m[:-th, :-tw]
        var = np.maximum(win(ii2) - win(ii) ** 2 / (th * tw), 1e-6)
        ncc = corr / (np.sqrt(var) * np.sqrt((t0 ** 2).sum()))
        ys = (y0 + np.arange(ncc.shape[0])) / H_
        xs = (x0 + np.arange(ncc.shape[1])) / W_
        near_real = (np.abs(ys - real[1]) < 0.04)[:, None] & (np.abs(xs - real[0]) < 0.03)[None, :]
        ncc = np.where(near_real, 0.0, ncc)
        y, x = np.unravel_index(ncc.argmax(), ncc.shape)
        if best is None or ncc.max() > best[0]:
            best = (float(ncc.max()), float(xs[x]), float(ys[y]), s)
    return best if best and best[0] >= STAR_SCORE and best[2] >= STAR_MIN_Y else None


def trim_above(crop: dict, w: int, h: int, star_y: float) -> dict:
    """A tighter crop: ends just above a star at `star_y` (fraction of the image), 16:9, centred."""
    new_h = min(crop["height"], round(h * (star_y - STAR_MARGIN)))
    side = round((w - new_h * 16 / 9) / 2)
    return {"left": side, "right": side, "bottom": h - new_h, "width": w - 2 * side, "height": new_h}


def todo(ep_root: Path, chapters: list[int], force: bool, mode: "str | dict" = "crop") -> list[tuple[Path, Path]]:
    """(source, hd copy) pairs still to make. The source is the download (crop: it is cropped here; none: used
    as it is) or, in remove mode, the clean/ copy of episode_clean (already free of the mark: not cropped here)."""
    pairs = []
    for n in chapters:
        img_dir = ep_root / "images" / f"c{n:04d}"
        if (mode.get(n, "crop") if isinstance(mode, dict) else mode) == "remove":
            sources = sorted((img_dir / "clean").glob("s[0-9]*.jpg"))
        else:
            sources = sorted(img_dir.glob("s*-2k.jpg"))
        for src in sources:
            dest = img_dir / "hd" / src.name.replace("-2k", "")
            if force or not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
                pairs.append((src, dest))
    return pairs


def chapters_arg(text: str) -> list[int]:
    out = []
    for part in text.split(","):
        a, _, b = part.partition("-")
        out += range(int(a), int(b or a) + 1)
    return out


def hd_mode(cfg: dict) -> str:
    """video.hd.enabled as 'auto', 'true' or 'false' (YAML true/false arrive as booleans)."""
    v = ((cfg.get("video") or {}).get("hd") or {}).get("enabled", "auto")
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v).strip().lower()
    if s in ("auto", "true", "false"):
        return s
    raise SystemExit(f"video.hd.enabled '{v}': use auto, true or false")


def hd_missing() -> str | None:
    """None when chaiNNer and its model are installed, else what is missing (with the way to set it)."""
    if not CHAINNER.is_file():
        return f"chaiNNer not found at {CHAINNER} (set CHAINNER or `novel settings init --chainner-exe <path> --force`)"
    if not MODEL.is_file() or not MODEL.with_suffix(".bin").is_file():
        return f"chaiNNer model not found: {MODEL} and its .bin (set CHAINNER_MODEL)"
    return None


def hd_plan(cfg: dict, *, no_hd: bool = False, needs_tool: bool = True) -> dict:
    """The one test for "is HD available", used by `novel render` and `novel build`.

    Returns {"mode", "run", "use_pictures", "note"}:
      run           make the hd copies now (needs the tool)       - build only
      use_pictures  let the renderer use hd copies already made   - never when --no-hd or enabled: false
      note          one informational line to print once, or ""
    enabled: auto = run only when the tool exists (note when it does not); true = the tool is required
    (SystemExit when it is missing and needs_tool); false = never.
    """
    mode = hd_mode(cfg)
    use = mode != "false" and not no_hd
    out = {"mode": mode, "run": False, "use_pictures": use, "note": ""}
    if no_hd or mode == "false" or not needs_tool:
        return out
    why = hd_missing()
    if why is None:
        out["run"] = True
    elif mode == "true":
        raise SystemExit(f"video.hd.enabled is true but {why}")
    else:
        out["note"] = f"hd: {why}; using the cleaned downloads (video.hd.enabled: auto)"
    return out


def make_hd(ep_root: Path, chapters: list[int], *, limit: int | None = None, force: bool = False,
            marks_only: bool = False) -> int:
    """Make the hd copies of the chapters' images. Returns how many were made."""
    if not CHAINNER.exists():
        raise SystemExit(f"chaiNNer not found at {CHAINNER} (set CHAINNER)")
    if not MODEL.exists() or not MODEL.with_suffix(".bin").exists():
        raise SystemExit(f"model not found: {MODEL} and its .bin (set CHAINNER_MODEL)")

    from . import episode_clean as CL
    modes = {n: CL.mode_for(ep_root, n) for n in chapters}      # per chapter: a chapter `remove` must not touch stays crop
    for n in chapters:
        if modes[n] == "remove":
            CL.clean_chapter(ep_root, n)              # the hd step starts from the cleaned pictures
        CL.mark_hd(ep_root / "images" / f"c{n:04d}", modes[n])   # copies made in another mode go; this mode is recorded
    pairs = todo(ep_root, chapters, force or marks_only, modes)[: limit]
    if not pairs:
        print("nothing to do: every image already has an up-to-date hd copy")
        return 0

    def mode_of(src: Path) -> str:
        return modes[int((src.parent.parent if src.parent.name == "clean" else src.parent).name[1:])]
    by_size: dict[tuple, list[tuple[Path, Path]]] = {}
    for src, dest in pairs:
        by_size.setdefault((*size_of(src), mode_of(src)), []).append((src, dest))

    t0 = time.time()
    done = 0
    CHUNK = 50          # one chaiNNer run per 50 images: a failure loses at most 50, and finished ones are kept
    batches, trimmed = [], []
    for (w, h, mode), all_items in by_size.items():
        if mode == "crop":
            base = crop_for([s for s, _ in all_items], w, h)   # one crop for the whole size group
        else:                                                  # remove: the clean copies are already clean; none: left as they are
            base = {"left": 0, "right": 0, "bottom": 0, "width": w, "height": h}
        by_crop: dict[tuple, list] = {}
        for src, dest in all_items:                            # second trim for any image with a star left in it
            star = None
            if mode != "none":          # none (Qwen pictures carry no mark): no mark search, no crop, only the upscale
                mx, my, score = find_mark(src)
                star = find_painted_star(src, (mx, my)) if score > 0.6 else find_painted_star(src, (0.95, 0.91))
            crop = base
            if star:
                crop = trim_above(base, w, h, star[2])
                trimmed.append((src, star))
                print(f"  second trim: {src.parent.name}/{src.name} has a star at {star[1]:.0%} across, "
                      f"{star[2]:.0%} down (match {star[0]:.2f}): cut to {crop['width']}x{crop['height']}")
            by_crop.setdefault((crop["left"], crop["bottom"]), (crop, []))[1].append((src, dest))
        for crop, items in by_crop.values():
            for k in range(0, len(items), CHUNK):
                batches.append(((w, h), items[k:k + CHUNK], crop))
    if marks_only:
        if not trimmed:
            print("no image has a star left in it")
            return 0
        keep = {s for s, _ in trimmed}
        batches = [(sz, [(s, d) for s, d in items if s in keep], crop) for sz, items, crop in batches]
        batches = [b for b in batches if b[1]]
    for (w, h), items, crop in batches:
        upscale = crop["height"] < H
        print(f"{len(items)} images {w}x{h}: " + (f"cut {crop['bottom']} px bottom, {crop['left']} px each side " if crop["bottom"] else "not cropped ")
              + f"-> {crop['width']}x{crop['height']}" + (" -> 2x model" if upscale else "") + f" -> {W}x{H}")
        with tempfile.TemporaryDirectory(prefix="hd-") as tmp:
            work = Path(tmp)
            src_dir, out_dir = work / "in", work / "out"
            src_dir.mkdir()
            names = {}
            for src, dest in items:
                chap = src.parent.parent.name if src.parent.name == "clean" else src.parent.name
                key = f"{chap}-{src.stem}"                      # c0013-s07-2k (or c0013-s07): unique across chapters
                shutil.copy2(src, src_dir / f"{key}.jpg")
                names[key] = dest
            started = time.time()
            run_chain(build_chain(src_dir, out_dir, crop, upscale), work, expected=len(items))
            for key, dest in names.items():
                made = out_dir / f"{key}.jpg"
                if not made.exists():
                    log = (work / "chainner.log").read_text(encoding="utf-8")[-2000:]
                    raise SystemExit(f"chaiNNer reported success but did not write {made.name}\n{log}")
                if size_of(made) != (W, H):
                    raise SystemExit(f"{made.name} came out {size_of(made)}, not {W}x{H}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(made, dest)
                done += 1
            print(f"  {len(items)} done in {time.time() - started:.0f} s ({(time.time() - started) / len(items):.1f} s each)")
    print(f"\n{done} hd images in {time.time() - t0:.0f} s")
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--episode", type=int, required=True)
    ap.add_argument("--chapters", required=True, help="e.g. 13 or 5-20 or 3,7")
    ap.add_argument("--book", help="default: the current book")
    ap.add_argument("--limit", type=int, help="only the first N images (a test)")
    ap.add_argument("--force", action="store_true", help="redo images that already have an hd copy")
    ap.add_argument("--marks-only", action="store_true",
                    help="look for a sparkle painted into the picture and redo only those images, with a second trim")
    args = ap.parse_args()
    from novel.core import context
    context.activate(context.resolve(args.book)[0])
    make_hd(E.root(args.episode), chapters_arg(args.chapters), limit=args.limit, force=args.force,
            marks_only=args.marks_only)


if __name__ == "__main__":
    main()
