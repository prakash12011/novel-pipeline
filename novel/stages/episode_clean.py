"""Remove Flow's Gemini mark from downloaded pictures instead of cropping it off (images.watermark: remove).

Flow stamps a small four-pointed star near the bottom-right corner of every download (episode_video.py explains
where). `crop` (today's way, episode_video.clean_image) cuts it off and loses about a tenth of the picture.
`remove` runs the npm package @pilio/gemini-watermark-remover (flow/node_modules, pinned in flow/package.json:
reverse alpha blending, local, no network) and then CHECKS what it left, because on Flow's 1376x768 downloads
the package usually misses the mark's real position and leaves the mark or a dark star-shaped ghost:

    download -> package -> judge(): the mark's box against a ring around it -> clean?  keep it          removed
                                                                          \\-> not clean?  CROP it instead   ghost / still-marked / error
                                                                                (episode_video.clean_image, exactly today's crop)

So images/cNNNN/clean/ never holds a visible mark or ghost. It holds a mix of full-frame pictures (removed)
and cropped ones, both 16:9, which is all the renderer assumes; clean/clean.json says which is which.
The tool's rejected outputs are kept in clean/rejected/ so that `novel clean --sheet` can show them.

    novel clean --ep 2 --chapters 11-12 --sheet --open
"""

from __future__ import annotations

import base64
import html
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import numpy as np

from . import episode as E
from . import episode_video as V

REPO = Path(__file__).resolve().parents[2]
FLOW = REPO / "flow"
GWR = FLOW / "node_modules" / "@pilio" / "gemini-watermark-remover" / "bin" / "gwr.mjs"
SHARP = FLOW / "node_modules" / "sharp" / "package.json"
MODES = ("crop", "remove", "none")
DEFAULT_MODE = "crop"

# Labels in clean.json. "removed" and "no-mark" pictures are full frame; the other three were cropped instead.
REMOVED, NO_MARK = "removed", "no-mark"
GHOST, STILL, ERROR = "ghost", "still-marked", "error"
CROPPED = (GHOST, STILL, ERROR)

Remover = Callable[[dict, Path], dict]      # ({name: raw path}, out_dir) -> {name: {"applied", "skip", "error"}}


def watermark_mode(cfg: dict) -> str:
    """images.watermark: crop (default), remove or none."""
    v = (cfg.get("images") or {}).get("watermark", DEFAULT_MODE)
    s = str(v).strip().lower()
    if s not in MODES:
        raise SystemExit(f"images.watermark '{v}': use crop, remove or none")
    return s


def setting_for(ep_root: Path, n: int | None = None) -> str:
    """images.watermark as set for an episode (and chapter) folder."""
    return watermark_mode(E.cfg(E.info(ep_root)["episode"], n))


class Refused(Exception):
    """The mark removal will not touch this chapter: the message says why."""


NOTE = "The old pictures are left as they are."


def _remove_record(ep_root: Path, n: int) -> bool:
    return load_report(ep_root, n).get("mode") == "remove"


def refusal(ep_root: Path, n: int, force: bool = False) -> str | None:
    """Why `remove` must not touch chapter n, or None. Old pictures are never cleaned again by accident:

    (a) the episode is marked `published: true` in its episode.yaml;             never lifted
    (b) the episode lives under an `_archive` folder;                              never lifted
    (c) the chapter already has pictures or a video made the old (crop) way: a rendered chapter video whose
        report does not say `watermark: remove`, clean/ copies without a remove record, or hd/ copies made in crop
        mode. Lifted by `force` (`novel clean --chapters N --force`). A chapter cleaned by `remove` before has a
        record and is not refused."""
    if "_archive" in {p.lower() for p in Path(ep_root).resolve().parts}:
        return "it is under an _archive folder"
    try:
        import yaml
        doc = yaml.safe_load((ep_root / "episode.yaml").read_text(encoding="utf-8")) if (ep_root / "episode.yaml").exists() else {}
    except (OSError, ValueError):
        doc = {}
    if isinstance(doc, dict) and doc.get("published") is True:
        return "the episode is marked published: true in its episode.yaml"
    if force or _remove_record(ep_root, n):
        return None
    img = ep_root / "images" / f"c{n:04d}"
    mp4 = ep_root / "video" / f"c{n:04d}.mp4"
    if mp4.exists():
        try:
            made_by = json.loads(mp4.with_suffix(".render.json").read_text(encoding="utf-8")).get("watermark", "crop")
        except (OSError, ValueError):
            made_by = "crop"
        if made_by != "remove":
            return f"its chapter video video/{mp4.name} was rendered from cropped pictures"
    if list((img / "clean").glob("s[0-9]*.jpg")):
        return "it already has clean/ pictures made by the old crop method"
    if list((img / "hd").glob("s*.jpg")) and hd_mode(img) == "crop":
        return "it already has hd/ pictures made by the old crop method"
    return None


_said: set = set()


def mode_for(ep_root: Path, n: int | None = None) -> str:
    """The mode this chapter is actually built in: images.watermark, except that a chapter `remove` must not
    touch (refusal()) stays on `crop` (one line says so, once). Everything that builds pictures asks this."""
    mode = setting_for(ep_root, n)
    if mode == "remove" and n is not None and (why := refusal(ep_root, n)):
        if (str(ep_root), n) not in _said:
            _said.add((str(ep_root), n))
            print(f"  chapter {n}: images.watermark is remove but not applied: {why}. {NOTE} Using the crop method.")
        return "crop"
    return mode


# ------------------------------------------------------------------ the check

# Tuned on 77 real before/after pairs judged by eye (report of 2026-11-15; the confusion counts are in
# tests/test_clean.py's docstring and `novel clean --help`): the star is the 56 px template's bright part; the
# ring is the band from 4 to 14 px outside the template's box. All numbers are 0-255 grey levels or
# correlations. Sign-insensitive on purpose: a mark left behind is LIGHTER than the ring, a ghost is DARKER,
# a bright rim is lighter again; every one of them fails.
JUDGE = {
    "star_level": 70,         # template pixels brighter than this are the star (the template is dark with a light star)
    "ring_in": 4,             # ring: this many px outside the box ...
    "ring_out": 14,           # ... to this many
    "rim_width": 3,           # rim: this many px around the star
    "inner_light": 5.0,       # star interior lighter than the ring by this much  -> still-marked
    "shape_light": 0.35,      # ... or shaped like the star (correlation) and lighter than the ring by shape_gain
    "shape_gain": 1.5,
    "inner_dark": -3.5,       # star interior darker than the ring by this much AND shaped like it (correlation <= -shape_dark) -> ghost
    "shape_dark": 0.25,
    "rim_dark": -4.0,         # the star's rim darker than the ring by this much  -> ghost (a dark halo)
    "rim_light": 6.0,         # ... or lighter by this much                       -> ghost (a bright rim)
}


def _template():
    from PIL import Image
    return np.asarray(Image.open(V.MARK).convert("L"), dtype=np.float64)


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    h, w = mask.shape
    p = np.pad(mask, r)
    out = np.zeros_like(mask)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy * dy + dx * dx <= r * r:
                out |= p[r + dy:r + dy + h, r + dx:r + dx + w]
    return out


def measure(after: Path, box: tuple[int, int]) -> dict:
    """The mark's box at (x, y) in picture `after`, against the ring around it. Grey levels.

    inner: mean of the star pixels minus the mean of the ring (positive: mark still there, negative: dark hole)
    rim:   mean of the band around the star minus the ring (dark halo or bright rim)
    shape: correlation of the box with the star shape (+1: a star-shaped light patch, -1: a dark one)
    """
    from PIL import Image
    tpl = _template()
    star = tpl > JUDGE["star_level"]
    rim = _dilate(star, JUDGE["rim_width"]) & ~star
    L = np.asarray(Image.open(after).convert("L"), dtype=np.float64)
    H, W = L.shape
    th, tw = tpl.shape
    x, y = box
    x, y = min(max(x, 0), W - tw), min(max(y, 0), H - th)
    patch = L[y:y + th, x:x + tw]
    a, b = JUDGE["ring_in"], JUDGE["ring_out"]
    y0, y1, x0, x1 = max(0, y - b), min(H, y + th + b), max(0, x - b), min(W, x + tw + b)
    reg = L[y0:y1, x0:x1]
    keep = np.ones(reg.shape, bool)
    keep[max(0, y - a - y0):y + th + a - y0, max(0, x - a - x0):x + tw + a - x0] = False
    ring = reg[keep]
    base = ring.mean() if ring.size else float(patch.mean())
    p0, s0 = patch - patch.mean(), star.astype(np.float64) - star.mean()
    shape = float((p0 * s0).sum() / (np.sqrt((p0 ** 2).sum() * (s0 ** 2).sum()) + 1e-9))
    return {"inner": round(float(patch[star].mean() - base), 2), "rim": round(float(patch[rim].mean() - base), 2),
            "shape": round(shape, 2)}


def verdict(m: dict) -> str | None:
    """None when the box looks clean, else STILL (mark, or a star-shaped light patch, left) or GHOST (a dark hole,
    a dark halo or a bright rim). Near-black pictures can keep an outline of 2 or 3 grey levels that passes."""
    j = JUDGE
    if m["inner"] >= j["inner_light"] or (m["shape"] >= j["shape_light"] and m["inner"] >= j["shape_gain"]):
        return STILL
    if (m["inner"] <= j["inner_dark"] and m["shape"] <= -j["shape_dark"]) or m["rim"] <= j["rim_dark"] \
            or m["rim"] >= j["rim_light"]:
        return GHOST
    return None


def mark_box(raw: Path) -> tuple[tuple[int, int], float]:
    """((x, y) of the mark's top-left in pixels, find_mark score): where the template matches best in the download's
    bottom-right corner. A low score does not mean there is no mark (three of E002 chapter 11's pictures scored 0.5
    with a plain star on them); whether one is there is decided by measure() on that spot."""
    from PIL import Image
    mx, my, score = V.find_mark(raw)
    with Image.open(raw) as im:
        w, h = im.size
    return (round(mx * w), round(my * h)), score


# ------------------------------------------------------------------ the tool


def package_version() -> str:
    try:
        return json.loads((GWR.parents[1] / "package.json").read_text(encoding="utf-8")).get("version", "?")
    except (OSError, ValueError):
        return "?"


def tool_missing() -> str | None:
    """None when node and both npm packages are installed, else what is missing and the way to fix it."""
    if shutil.which("node") is None:
        return "node is not on PATH (install Node.js 20 or newer)"
    gone = [name for name, f in (("@pilio/gemini-watermark-remover", GWR), ("sharp", SHARP)) if not f.is_file()]
    if gone:
        return f"the npm package{'s' if len(gone) > 1 else ''} {' and '.join(gone)} not installed: run `npm ci` in {FLOW}"
    return None


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(["node", str(GWR), "remove", *args, "--json"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, cwd=str(FLOW))


def _meta(entry: dict) -> dict:
    m = entry.get("meta") or {}
    return {"applied": bool(m.get("applied")), "skip": m.get("skipReason"), "error": None}


def run_remover(pictures: dict, out_dir: Path) -> dict:
    """Run the package over {name: raw picture} and write out_dir/name. Returns {name: {"applied", "skip", "error"}}.

    One folder run (Node starts once: about 2 s a 1376x768 picture, 4 s a 2752x1536 one). The tool stops at the
    first picture it cannot read; then every picture is run on its own so one bad file costs one result."""
    missing = tool_missing()
    if missing:
        raise SystemExit(f"cannot remove the mark: {missing}")
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {}
    with tempfile.TemporaryDirectory(prefix="gwr-") as tmp:
        src = Path(tmp) / "in"
        src.mkdir()
        for name, raw in pictures.items():
            shutil.copyfile(raw, src / name)             # the tool takes every file of a folder: give it only ours
        outs = Path(tmp) / "out"
        p = _run([str(src), "--out-dir", str(outs), "--overwrite"], timeout=3600)
        if p.returncode == 0:
            try:
                rows = json.loads(p.stdout)
                rows = rows if isinstance(rows, list) else [rows]
                for r in rows:
                    result[Path(r["input"]).name] = _meta(r)
            except (ValueError, KeyError, TypeError):
                result = {}
        if len(result) != len(pictures):                 # the folder run failed (or printed something unreadable)
            result = {}
            shutil.rmtree(outs, ignore_errors=True)
            for name in pictures:
                q = _run([str(src / name), "--output", str(outs / name), "--overwrite"], timeout=600)
                if q.returncode == 0:
                    try:
                        result[name] = _meta(json.loads(q.stdout))
                        continue
                    except (ValueError, KeyError, TypeError):
                        pass
                result[name] = {"applied": False, "skip": None,
                                "error": (q.stderr or q.stdout or f"exit {q.returncode}").strip()[-300:]}
        for name, r in result.items():
            if not r["error"] and (outs / name).is_file():
                shutil.copyfile(outs / name, out_dir / name)
            elif not r["error"]:
                r["error"] = "the tool wrote no output file"
    return result


# ------------------------------------------------------------------ one chapter


def clean_dir(ep_root: Path, n: int) -> Path:
    return ep_root / "images" / f"c{n:04d}" / "clean"


def report_file(ep_root: Path, n: int) -> Path:
    return clean_dir(ep_root, n) / "clean.json"


def load_report(ep_root: Path, n: int) -> dict:
    f = report_file(ep_root, n)
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def raw_pictures(ep_root: Path, n: int) -> list[Path]:
    return sorted((ep_root / "images" / f"c{n:04d}").glob("s*-2k.jpg"))


def _clean_name(raw: Path) -> str:
    return raw.name.replace("-2k", "")


def _size(path: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def _valid(path: Path, size: tuple[int, int]) -> bool:
    """The tool's output exists, is a readable JPEG and has the download's pixel size."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            im.load()
            return im.format == "JPEG" and im.size == size
    except (OSError, ValueError, SyntaxError):
        return False


def _current(entry: dict, raw: Path, dest: Path) -> bool:
    return bool(entry) and dest.is_file() and dest.stat().st_mtime >= raw.stat().st_mtime \
        and entry.get("source_mtime") == raw.stat().st_mtime


def clean_chapter(ep_root: Path, n: int, force: bool = False, remover: Remover | None = None,
                  log: Callable[[str], None] = print, allow_old: bool = False) -> dict:
    """Clean copies of chapter n's downloads in images/cNNNN/clean/ (sNN.jpg) and clean/clean.json.

    Only pictures whose clean copy is missing, older than the download, or not made by this step are done
    (`force`: all). Returns {"mode", "tool", "chapter", "pictures": {name: entry}, "done": [names this call made],
    "paths": [clean copies in order]}. An entry is {"source", "source_mtime", "result": REMOVED | NO_MARK | GHOST |
    STILL | ERROR, "cropped": bool, "size", "box", "measure", "painted", "error"}. Raises Refused for a chapter that
    must not be touched (refusal(); `allow_old` lifts only the old-pictures case)."""
    why = refusal(ep_root, n, force=allow_old)
    if why:
        raise Refused(f"chapter {n}: refused, {why}. {NOTE}")
    remover = remover or run_remover
    out = clean_dir(ep_root, n)
    prior = load_report(ep_root, n)
    entries = dict(prior.get("pictures") or {}) if prior.get("mode") == "remove" else {}
    todo = []
    for raw in raw_pictures(ep_root, n):
        name = _clean_name(raw)
        if force or not _current(entries.get(name), raw, out / name):
            todo.append(raw)
    done = []
    if todo:
        out.mkdir(parents=True, exist_ok=True)
        (out / "rejected").mkdir(exist_ok=True)
        boxes = {raw.name: mark_box(raw) for raw in todo}
        # a download with nothing star-shaped at the mark's spot has no mark to remove: not sent to the tool
        bare = {raw.name for raw in todo if verdict(measure(raw, boxes[raw.name][0])) is None}
        wanted = {raw.name: raw for raw in todo if raw.name not in bare}
        with tempfile.TemporaryDirectory(prefix="clean-") as tmp:
            tool = remover(wanted, Path(tmp)) if wanted else {}
            for raw in todo:
                name = _clean_name(raw)
                dest = out / name
                box, found = boxes[raw.name]
                size = _size(raw)
                entry = {"source": raw.name, "source_mtime": raw.stat().st_mtime, "size": list(size),
                         "box": list(box), "mark_score": round(found, 2), "measure": None,
                         "painted": False, "error": None}
                (out / "rejected" / name).unlink(missing_ok=True)
                if raw.name in bare:                              # nothing to remove: the download as it is
                    shutil.copyfile(raw, dest)
                    entry.update(result=NO_MARK, cropped=False)
                else:
                    r = tool.get(raw.name) or {"applied": False, "skip": None, "error": "the tool returned no result"}
                    made = Path(tmp) / raw.name
                    label = None
                    if r["error"]:
                        label, entry["error"] = ERROR, r["error"]
                    elif not made.is_file() or not _valid(made, size):
                        label, entry["error"] = ERROR, "the tool's output is missing, not a JPEG, or a different size"
                    else:
                        entry["measure"] = measure(made, box)
                        label = verdict(entry["measure"])
                        if not r["applied"]:
                            entry["tool"] = r["skip"] or "not applied"
                    if label is None:
                        shutil.copyfile(made, dest)
                        entry.update(result=REMOVED, cropped=False)
                        painted = V.painted_marks(dest)
                        entry["painted"] = bool(painted)
                        if painted:
                            x, y, score, _ = painted[0]
                            log(f"  warning: {raw.parent.name}/{raw.name}: a sparkle may be painted into the picture "
                                f"at {x:.0%} across, {y:.0%} down (match {score}) - check it by eye")
                    else:
                        if made.is_file():
                            shutil.copyfile(made, out / "rejected" / name)
                        V.clean_image(raw, dest)                  # today's crop, exactly
                        entry.update(result=label, cropped=True)
                entries[name] = entry
                done.append(name)
    names = [_clean_name(p) for p in raw_pictures(ep_root, n)]
    entries = {k: entries[k] for k in names if k in entries}
    doc = {"version": 1, "mode": "remove", "tool": {"name": "@pilio/gemini-watermark-remover", "version": package_version()},
           "judge": JUDGE, "made": time.strftime("%Y-%m-%dT%H:%M:%S"), "pictures": entries}
    if todo or not report_file(ep_root, n).exists():
        if names:
            out.mkdir(parents=True, exist_ok=True)
            report_file(ep_root, n).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    doc["done"], doc["chapter"] = done, n
    doc["paths"] = [out / k for k in entries]
    return doc


def discard_remove_copies(ep_root: Path, n: int) -> int:
    """In crop mode: delete the clean copies that `remove` mode made (they are listed in clean.json) and clean.json
    itself, so the old crop step starts from nothing and gives exactly today's pictures. The sheet and rejected/
    stay (the sheet is self-contained). Returns how many pictures went."""
    rep = load_report(ep_root, n)
    if rep.get("mode") != "remove":
        return 0
    out, gone = clean_dir(ep_root, n), 0
    for name in list((rep.get("pictures") or {})):
        gone += (out / name).is_file()
        (out / name).unlink(missing_ok=True)
    report_file(ep_root, n).unlink(missing_ok=True)
    return gone


def summary(entries: dict) -> dict:
    """{"total", "removed", "no_mark", "cropped", "ghost", "still", "error"} for a clean.json's pictures."""
    c = {k: sum(1 for e in entries.values() if e.get("result") == k) for k in (REMOVED, NO_MARK, GHOST, STILL, ERROR)}
    return {"total": len(entries), "removed": c[REMOVED], "no_mark": c[NO_MARK], "ghost": c[GHOST],
            "still": c[STILL], "error": c[ERROR], "cropped": c[GHOST] + c[STILL] + c[ERROR]}


def summary_line(s: dict) -> str:
    """removed 41, cropped instead 20 (ghost 14, still marked 6), no mark 0"""
    why = ", ".join(f"{label} {s[k]}" for label, k in (("ghost", "ghost"), ("still marked", "still"), ("error", "error"))
                    if s[k])
    return f"removed {s['removed']}, cropped instead {s['cropped']}" + (f" ({why})" if why else "") + f", no mark {s['no_mark']}"


# ------------------------------------------------------------------ what the renderer and the HD step use


def source_pictures(ep_root: Path, n: int, mode: str, scenes_ids: list[int]) -> list[Path]:
    """One picture per scene id, for `mode`. crop: today's crop step (episode_video.clean_chapter); remove: this
    step's clean/ copies (made now if missing or out of date); none: the downloads as they are."""
    img_dir = ep_root / "images" / f"c{n:04d}"
    if mode == "none":
        return [img_dir / f"s{i:02d}-2k.jpg" for i in scenes_ids]
    if mode == "remove":
        got = {p.name: p for p in clean_chapter(ep_root, n)["paths"]}
        return [got[f"s{i:02d}.jpg"] for i in scenes_ids]
    discard_remove_copies(ep_root, n)
    got = {p.name: p for p in V.clean_chapter(ep_root, n)}
    return [got[f"s{i:02d}.jpg"] for i in scenes_ids]


# ------------------------------------------------------------------ the sheet


def _jpeg_uri(im, quality: int = 85) -> str:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _corner(path: Path, box: list, size: tuple[int, int]):
    """The mark's neighbourhood in a picture of the download's size, enlarged 2x."""
    from PIL import Image
    w, h = size
    x, y = box
    pad = 72
    x0, y0 = min(max(x - pad, 0), w - 200), min(max(y - pad, 0), h - 200)
    with Image.open(path) as im:
        c = im.convert("RGB").crop((x0, y0, x0 + 200, y0 + 200)).resize((400, 400), Image.LANCZOS)
    return c


SHEET_CSS = """body{font:14px system-ui;background:#111;color:#ddd;margin:16px}
h1{font-size:20px} .sum{font-size:16px;color:#f0d070;margin:6px 0 18px}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(830px,1fr));gap:14px}
figure{margin:0;background:#1c1c1c;border-radius:6px;padding:8px}
.pair{display:flex;gap:6px} .pair div{flex:1} img{width:100%;display:block;image-rendering:auto}
figcaption{padding:6px 2px} b.ok{color:#8d8} b.bad{color:#f88} b.mid{color:#9bd} small{color:#999}"""


def write_sheet(ep_root: Path, n: int) -> Path:
    """images/cNNNN/clean/sheet.html: before / after crops of every picture's corner, rejected ones first."""
    rep = load_report(ep_root, n)
    entries = rep.get("pictures") or {}
    if not entries:
        raise SystemExit(f"chapter {n}: no clean.json yet; run novel clean first")
    out = clean_dir(ep_root, n)
    img_dir = ep_root / "images" / f"c{n:04d}"
    order = {GHOST: 0, STILL: 0, ERROR: 0, REMOVED: 1, NO_MARK: 2}
    figs = []
    for name, e in sorted(entries.items(), key=lambda kv: (order.get(kv[1]["result"], 3), kv[0])):
        raw = img_dir / e["source"]
        if not raw.is_file():
            continue
        size = tuple(e["size"])
        rejected = out / "rejected" / name
        after = rejected if e["result"] in CROPPED else out / name
        before = _corner(raw, e["box"], size)
        shown = _corner(after, e["box"], size) if after.is_file() and _size(after) == size else None
        cls = "bad" if e["result"] in CROPPED else "ok" if e["result"] == REMOVED else "mid"
        m = e.get("measure") or {}
        what = {REMOVED: "removed: the clean copy is the tool's output",
                NO_MARK: "nothing star-shaped at the mark's spot in the download: copied as it is",
                GHOST: "rejected, CROPPED instead: a ghost or rim is left", STILL: "rejected, CROPPED instead: the mark is still there",
                ERROR: f"rejected, CROPPED instead: {html.escape(str(e.get('error') or ''))}"}[e["result"]]
        figs.append(
            f'<figure><div class="pair"><div><img src="{_jpeg_uri(before)}" alt="before"><small>before</small></div>'
            + (f'<div><img src="{_jpeg_uri(shown)}" alt="after"><small>after (the tool\'s output)</small></div>' if shown else "")
            + f'</div><figcaption><b class="{cls}">{html.escape(name)} {html.escape(e["result"])}</b> {what}'
            f'<br><small>{size[0]}x{size[1]}'
            + (f' · inner {m["inner"]:+.1f} rim {m["rim"]:+.1f} shape {m["shape"]:+.2f}' if m else "") + "</small>"
            f'</figcaption></figure>')
    s = summary(entries)
    page = (f'<!doctype html><meta charset="utf-8"><title>Chapter {n} mark removal</title><style>{SHEET_CSS}</style>'
            f'<h1>Chapter {n}: Gemini mark removal ({rep["tool"]["name"]} {rep["tool"]["version"]})</h1>'
            f'<p class="sum">{summary_line(s)}</p><main>{"".join(figs)}</main>')
    f = out / "sheet.html"
    f.write_text(page, encoding="utf-8")
    return f


# ------------------------------------------------------------------ hd copies remember the mode they were made in


def hd_mode(img_dir: Path) -> str:
    """The images.watermark mode the hd copies in img_dir/hd were made in; copies without a marker are crop's."""
    try:
        return json.loads((img_dir / "hd" / "hd.json").read_text(encoding="utf-8")).get("watermark", "crop")
    except (OSError, ValueError, AttributeError):
        return "crop"


def mark_hd(img_dir: Path, mode: str) -> None:
    """Record `mode` for the hd copies of img_dir. Copies made in another mode are deleted first (they were made
    from different pictures)."""
    hd = img_dir / "hd"
    if hd.is_dir() and hd_mode(img_dir) != mode:
        for f in hd.glob("s*.jpg"):
            f.unlink()
    hd.mkdir(parents=True, exist_ok=True)
    (hd / "hd.json").write_text(json.dumps({"watermark": mode}), encoding="utf-8")
