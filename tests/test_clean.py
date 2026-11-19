"""images.watermark and `novel clean`: removing Flow's Gemini mark, checking the result, cropping what failed.

GENERATED pictures only (a dark textured 1376x768 JPEG with the real 56 px template blended in where Flow puts it),
a temporary NOVEL_HOME, no network. The package call is injected (a fake that copies files and can fail for one
file); ONE integration test runs the real npm package when node and flow/node_modules are there.

The check (episode_clean.judge/measure/verdict) was tuned on 77 real before/after pairs judged by eye
(1376x768: 47 pictures, 2752x1536: 31): confusion counts, "bad" = mark left, ghost or visible outline:
    1376x768  bad 44 rejected / 0 accepted;  clean 2 accepted / 1 rejected
    2752x1536 bad  6 rejected / 0 accepted;  clean 24 accepted / 1 rejected
"""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from novel.commands import clean_cmd
from novel.core import context, paths
from novel.stages import episode as E
from novel.stages import episode_clean as CL
from novel.stages import episode_video as V
from novel.stages.manifest import Chapter, Manifest

BOOK = "demo"
W, H = 1376, 768
MARK_XY = (1249, 642)                      # where Flow's 56 px star sits on a 1376x768 download


# ------------------------------------------------------------------ generated pictures


def background(w=W, h=H, seed=1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    base = 40 + 25 * np.sin(xx / 97.0) * np.cos(yy / 71.0) + 10 * (xx / w)
    return np.stack([base * 1.1, base * 0.9, base * 0.7], axis=2) + rng.normal(0, 2.5, (h, w, 3))


def star_alpha() -> np.ndarray:
    tpl = np.asarray(Image.open(V.MARK).convert("L"), dtype=float)
    return np.clip((tpl - 32) / (105 - 32), 0, 1) * 0.33


def save(arr: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(path, quality=92)
    return path


def with_mark(arr: np.ndarray, xy=MARK_XY) -> np.ndarray:
    a = star_alpha()[..., None]
    x, y = xy
    out = arr.copy()
    out[y:y + 56, x:x + 56] = out[y:y + 56, x:x + 56] * (1 - a) + 255 * a
    return out


def star_mask() -> np.ndarray:
    return np.asarray(Image.open(V.MARK).convert("L"), dtype=float) > CL.JUDGE["star_level"]


def with_ghost(arr: np.ndarray, delta: float, xy=MARK_XY, rim: bool = False) -> np.ndarray:
    """A star-shaped patch lighter/darker by `delta` (rim=True: only the band around the star)."""
    star = star_mask()
    m = CL._dilate(star, 3) & ~star if rim else star
    x, y = xy
    out = arr.copy()
    out[y:y + 56, x:x + 56][m] += delta
    return out


# ------------------------------------------------------------------ the check on synthetic boxes


def test_verdict_on_clean_light_dark_and_rim_boxes(tmp_path):
    bg = background()
    cases = {
        "clean": (bg, None),
        "mark still there": (with_mark(bg), CL.STILL),
        "light star-shaped patch": (with_ghost(bg, +12), CL.STILL),
        "dark ghost": (with_ghost(bg, -15), CL.GHOST),
        "bright rim": (with_ghost(bg, +14, rim=True), CL.GHOST),
        "dark halo": (with_ghost(bg, -14, rim=True), CL.GHOST),
        "a hair of noise": (with_ghost(bg, -1.0), None),
    }
    for name, (arr, want) in cases.items():
        got = CL.verdict(CL.measure(save(arr, tmp_path / "p.jpg"), MARK_XY))
        assert got == want, (name, CL.measure(tmp_path / "p.jpg", MARK_XY))


def test_measure_is_sign_insensitive(tmp_path):
    bg = background()
    light = CL.measure(save(with_ghost(bg, +15), tmp_path / "l.jpg"), MARK_XY)
    dark = CL.measure(save(with_ghost(bg, -15), tmp_path / "d.jpg"), MARK_XY)
    assert light["inner"] > 10 and dark["inner"] < -10 and light["shape"] > 0.5 and dark["shape"] < -0.5
    assert CL.verdict(light) and CL.verdict(dark)               # both fail, whichever way they point


def test_mark_box_finds_the_mark_and_a_bare_download_measures_clean(tmp_path):
    bg = background()
    p = save(with_mark(bg), tmp_path / "m.jpg")
    box, score = CL.mark_box(p)
    assert abs(box[0] - MARK_XY[0]) <= 2 and abs(box[1] - MARK_XY[1]) <= 2 and score > 0.9
    assert CL.verdict(CL.measure(p, box)) == CL.STILL                  # a download with the star on it fails the check
    q = save(bg, tmp_path / "n.jpg")
    assert CL.verdict(CL.measure(q, CL.mark_box(q)[0])) is None        # no star: nothing to remove


# ------------------------------------------------------------------ an episode to clean


@pytest.fixture
def world(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    old = (E.EPISODES, context._ACTIVE_BOOK)
    context.activate(BOOK)
    set_mode("crop")
    root = E.root(1)
    root.mkdir(parents=True, exist_ok=True)
    Manifest(path=root / "manifest.json", chapters=[Chapter(idx=1, title="Chapter 1: Test", source="0001_x.txt")]).save()
    (root / "episode.json").write_text(json.dumps({"episode": 1, "start": 1, "end": 1}), encoding="utf-8")
    yield {"root": root, "tmp": tmp_path}
    E.EPISODES, context._ACTIVE_BOOK = old


def set_mode(mode):
    (paths.book_dir(BOOK) / "book.yaml").parent.mkdir(parents=True, exist_ok=True)
    (paths.book_dir(BOOK) / "book.yaml").write_text(
        yaml.safe_dump({"book": {"title": "Demo", "author": "A"}, "images": {"watermark": mode},
                        "video": {"style": "kenburns", "hd": {"enabled": False}, "disclaimer": {"seconds": 1.0}}}), encoding="utf-8")


def make_chapter(root: Path, n: int = 1) -> dict:
    """Five downloads: s01 clean after the tool, s02 the tool leaves the mark, s03 leaves a dark ghost, s04 makes the
    tool fail, s05 has no mark at all. Returns what the fake tool does per picture."""
    img = root / "images" / f"c{n:04d}"
    bg = background()
    marked = with_mark(bg)
    fates = {}
    for i, (arr, fate) in enumerate([(marked, bg), (marked, marked), (marked, with_ghost(bg, -15)), (marked, "fail"),
                                     (bg, None)], 1):
        save(arr, img / f"s{i:02d}-2k.jpg")
        fates[f"s{i:02d}-2k.jpg"] = fate
    return fates


class FakeTool:
    """Stands in for the npm package: writes what `fates` says for each picture it is given."""

    def __init__(self, tmp: Path, fates: dict):
        self.tmp, self.fates, self.calls = tmp, fates, []

    def __call__(self, pictures: dict, out_dir: Path) -> dict:
        self.calls.append(sorted(pictures))
        res = {}
        for name, raw in pictures.items():
            fate = self.fates[name]
            if isinstance(fate, str) and fate == "fail":
                res[name] = {"applied": False, "skip": None, "error": "boom: could not decode"}
                continue
            if fate is None:
                shutil.copyfile(raw, out_dir / name)
            else:
                save(fate, out_dir / name)
            res[name] = {"applied": True, "skip": None, "error": None}
        return res


def size_of(p: Path):
    return Image.open(p).size


# ------------------------------------------------------------------ cleaning a chapter


def test_clean_chapter_labels_fallback_and_clean_json(world):
    root = world["root"]
    tool = FakeTool(world["tmp"], make_chapter(root))
    r = CL.clean_chapter(root, 1, remover=tool, log=lambda s: None)
    got = {k: v["result"] for k, v in r["pictures"].items()}
    assert got == {"s01.jpg": CL.REMOVED, "s02.jpg": CL.STILL, "s03.jpg": CL.GHOST, "s04.jpg": CL.ERROR,
                   "s05.jpg": CL.NO_MARK}
    out = root / "images" / "c0001" / "clean"
    assert size_of(out / "s01.jpg") == (W, H) and size_of(out / "s05.jpg") == (W, H)       # full frame
    assert (out / "s05.jpg").read_bytes() == (root / "images" / "c0001" / "s05-2k.jpg").read_bytes()
    for k in ("s02.jpg", "s03.jpg", "s04.jpg"):                                            # cropped instead: same shape, smaller
        w, h = size_of(out / k)
        assert (w, h) != (W, H) and abs(w / h - W / H) < 0.01 and h < H
    assert tool.calls == [["s01-2k.jpg", "s02-2k.jpg", "s03-2k.jpg", "s04-2k.jpg"]]        # the picture without a star is never sent
    doc = json.loads((out / "clean.json").read_text(encoding="utf-8"))
    assert doc["mode"] == "remove" and doc["tool"]["name"] == "@pilio/gemini-watermark-remover" and doc["tool"]["version"]
    assert doc["pictures"]["s04.jpg"]["error"] == "boom: could not decode" and doc["pictures"]["s04.jpg"]["cropped"] is True
    assert doc["pictures"]["s01.jpg"]["cropped"] is False and doc["pictures"]["s01.jpg"]["measure"]["inner"] is not None
    assert (out / "rejected" / "s02.jpg").is_file() and (out / "rejected" / "s03.jpg").is_file()
    s = CL.summary(r["pictures"])
    assert CL.summary_line(s) == "removed 1, cropped instead 3 (ghost 1, still marked 1, error 1), no mark 1"


def test_the_fallback_is_exactly_todays_crop(world):
    """A rejected picture's clean copy is byte-for-byte what the old crop step makes."""
    root = world["root"]
    make_chapter(root)
    shutil.copytree(root / "images", world["tmp"] / "old-images")
    CL.clean_chapter(root, 1, remover=FakeTool(world["tmp"], make_chapter(root)), log=lambda s: None)
    old_root = world["tmp"] / "old-root"
    (old_root / "images").parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(world["tmp"] / "old-images"), str(old_root / "images"))
    old = {p.name: p for p in V.clean_chapter(old_root, 1)}
    new = root / "images" / "c0001" / "clean"
    for k in ("s02.jpg", "s03.jpg", "s04.jpg"):
        assert (new / k).read_bytes() == old[k].read_bytes(), k


def test_incremental_cleaning_and_force(world):
    root = world["root"]
    fates = make_chapter(root)
    tool = FakeTool(world["tmp"], fates)
    CL.clean_chapter(root, 1, remover=tool, log=lambda s: None)
    r = CL.clean_chapter(root, 1, remover=tool, log=lambda s: None)
    assert r["done"] == [] and len(tool.calls) == 1                    # everything already clean: the tool is not called
    raw = root / "images" / "c0001" / "s01-2k.jpg"
    later = raw.stat().st_mtime + 10
    import os
    os.utime(raw, (later, later))                                      # a new download of s01
    r = CL.clean_chapter(root, 1, remover=tool, log=lambda s: None)
    assert r["done"] == ["s01.jpg"] and tool.calls[-1] == ["s01-2k.jpg"]
    r = CL.clean_chapter(root, 1, force=True, remover=tool, log=lambda s: None)
    assert len(r["done"]) == 5 and len(tool.calls) == 3


def test_clean_copies_from_crop_mode_are_not_taken_as_clean(world):
    """clean/ filled by the old crop step is a guarded chapter; with the guard lifted it has no clean.json entry, so
    remove mode redoes every picture."""
    root = world["root"]
    fates = make_chapter(root)
    V.clean_chapter(root, 1)
    tool = FakeTool(world["tmp"], fates)
    with pytest.raises(CL.Refused, match="clean/ pictures made by the old crop method"):
        CL.clean_chapter(root, 1, remover=tool, log=lambda s: None)
    assert tool.calls == []
    r = CL.clean_chapter(root, 1, remover=tool, log=lambda s: None, allow_old=True)
    assert len(r["done"]) == 5 and size_of(root / "images" / "c0001" / "clean" / "s01.jpg") == (W, H)


def test_an_output_of_the_wrong_size_counts_as_an_error(world):
    root = world["root"]
    make_chapter(root)

    def bad_tool(pictures, out_dir):
        for name in pictures:
            Image.new("RGB", (100, 50)).save(out_dir / name)
        return {name: {"applied": True, "skip": None, "error": None} for name in pictures}
    r = CL.clean_chapter(root, 1, remover=bad_tool, log=lambda s: None)
    assert {v["result"] for k, v in r["pictures"].items() if k != "s05.jpg"} == {CL.ERROR}


# ------------------------------------------------------------------ the three modes in the renderer


def scenes(n=5):
    return [{"id": i} for i in range(1, n + 1)]


def test_render_picks_the_copy_for_each_mode(world, monkeypatch):
    from novel.stages import episode_render as R
    root = world["root"]
    fates = make_chapter(root)
    monkeypatch.setattr(CL, "run_remover", FakeTool(world["tmp"], fates))
    img = root / "images" / "c0001"

    crop = R.chapter_images(root, 1, scenes())                          # default setting: crop
    assert [p.parent.name for p in crop] == ["clean"] * 5 and not (img / "clean" / "clean.json").exists()
    assert all(size_of(p)[1] < H for p in crop)                         # every one cropped
    crop_bytes = {p.name: p.read_bytes() for p in crop}

    set_mode("remove")                                                  # the crop copies are old pictures: left alone
    assert [p.read_bytes() for p in R.chapter_images(root, 1, scenes())] == list(crop_bytes.values())
    assert not (img / "clean" / "clean.json").exists()
    shutil.rmtree(img / "clean")                                        # a chapter with nothing made yet
    rem = R.chapter_images(root, 1, scenes())
    assert [size_of(p) == (W, H) for p in rem] == [True, False, False, False, True]    # full frame where removed / no mark
    assert (img / "clean" / "clean.json").exists()

    set_mode("none")
    assert R.chapter_images(root, 1, scenes()) == [img / f"s{i:02d}-2k.jpg" for i in range(1, 6)]

    set_mode("crop")                                                    # back: today's crop, byte for byte
    back = R.chapter_images(root, 1, scenes())
    assert not (img / "clean" / "clean.json").exists()                  # the remove copies were discarded
    assert {p.name: p.read_bytes() for p in back} == crop_bytes
    set_mode("remove")
    assert CL.mode_for(root, 1) == "crop"                               # and the crop copies are old again: guarded


def test_default_is_crop_and_the_old_path_is_untouched(world):
    """No images.watermark set: the chapter's pictures are the old step's output, and episode_video is unchanged."""
    from novel.stages import episode_render as R
    assert CL.watermark_mode({}) == "crop" and CL.watermark_mode({"images": {}}) == "crop"
    root = world["root"]
    make_chapter(root)
    (paths.book_dir(BOOK) / "book.yaml").write_text(yaml.safe_dump({"book": {"title": "Demo", "author": "A"}}),
                                                    encoding="utf-8")
    got = R.chapter_images(root, 1, scenes())
    again = V.clean_chapter(root, 1)
    assert [p.read_bytes() for p in got] == [p.read_bytes() for p in again]
    assert subprocess.run(["git", "diff", "--quiet", "--", str(Path(V.__file__))], capture_output=True,
                          cwd=str(CL.REPO)).returncode in (0, 128)       # no edit to episode_video.py (128: no git here)


def test_bad_mode_is_refused():
    with pytest.raises(SystemExit, match="use crop, remove or none"):
        CL.watermark_mode({"images": {"watermark": "inpaint"}})


# ------------------------------------------------------------------ the hd step


def test_hd_step_starts_from_the_clean_copies_and_crops_nothing(world, monkeypatch, tmp_path):
    from novel.stages import episode_hd as HD
    root = world["root"]
    make_chapter(root)
    set_mode("remove")
    monkeypatch.setattr(CL, "run_remover", FakeTool(tmp_path, make_chapter(root)))
    exe, model = tmp_path / "c.exe", tmp_path / "m.param"
    exe.write_bytes(b"x")
    model.write_bytes(b"x")
    model.with_suffix(".bin").write_bytes(b"x")
    monkeypatch.setattr(HD, "CHAINNER", exe)
    monkeypatch.setattr(HD, "MODEL", model)
    seen = []

    def fake_chain(chain, work, expected=0):
        nodes = {n["data"]["schemaId"]: n["data"]["inputData"] for n in chain["content"]["nodes"]}
        src, dst = Path(nodes["chainner:image:load_images"]["0"] if "0" in nodes["chainner:image:load_images"]
                        else nodes["chainner:image:load_images"][0]), None
        crop = nodes["chainner:image:crop"]
        seen.append({"inputs": sorted(p.name for p in src.glob("*.jpg")), "sizes": sorted({size_of(p) for p in src.glob("*.jpg")}),
                     "crop": [crop.get("4", crop.get(4)), crop.get("5", crop.get(5)), crop.get("6", crop.get(6))]})
        out = Path(nodes["chainner:image:save"].get("1", nodes["chainner:image:save"].get(1)))
        out.mkdir(parents=True, exist_ok=True)
        for p in src.glob("*.jpg"):
            Image.new("RGB", (HD.W, HD.H), (20, 20, 20)).save(out / p.name, quality=90)
    monkeypatch.setattr(HD, "run_chain", fake_chain)
    HD.make_hd(root, [1])
    # one chaiNNer run per picture size: the full-frame ones, and the cropped ones; none of them cropped again
    assert all(s["crop"] == [0, 0, 0] for s in seen)
    names = sorted(n for s in seen for n in s["inputs"])
    assert len(names) == 5 and all(n.startswith("c0001-s0") for n in names)
    assert sorted(size for s in seen for size in s["sizes"])[-1] == (W, H)
    assert (root / "images" / "c0001" / "hd" / "hd.json").exists()
    assert CL.hd_mode(root / "images" / "c0001") == "remove"


def test_hd_copies_made_in_another_mode_are_not_used(world):
    from novel.stages import episode_render as R
    root = world["root"]
    make_chapter(root)
    img = root / "images" / "c0001"
    (img / "hd").mkdir()
    for i in range(1, 6):
        Image.new("RGB", (1920, 1080)).save(img / "hd" / f"s{i:02d}.jpg")
    got = R.chapter_images(root, 1, scenes())                           # crop and no marker: ours
    assert all(p.parent.name == "hd" for p in got)
    CL.mark_hd(img, "remove")                                           # made in remove mode: not for crop
    assert not list((img / "hd").glob("s*.jpg"))
    for i in range(1, 6):
        Image.new("RGB", (1920, 1080)).save(img / "hd" / f"s{i:02d}.jpg")
    got = R.chapter_images(root, 1, scenes())                           # crop: hd copies of another mode are not used
    assert all(p.parent.name == "clean" for p in got)


# ------------------------------------------------------------------ staleness


def test_a_video_is_stale_after_the_setting_changes(world):
    """A chapter video made the old way stays current when the setting becomes `remove` (that chapter is guarded);
    once the chapter has been cleaned by remove on purpose, the mode is part of the render report."""
    from novel.stages import episode_build as B
    from novel.stages.episode_video import logo_signature, title_font_setting
    root = world["root"]
    make_chapter(root)
    cfg = E.cfg(1, 1)
    video = root / "video"
    video.mkdir()
    mp4 = video / "c0001.mp4"
    mp4.write_bytes(b"x")
    report = {"captions": True, "pass": True, "logo": logo_signature(cfg["video"]["logo"]),
              "title_font": title_font_setting(cfg)}
    rep = mp4.with_suffix(".render.json")
    rep.write_text(json.dumps(report), encoding="utf-8")
    assert B.why_render(root, 1) is None                                # an old report (no field) is a crop render
    set_mode("remove")
    assert B.why_render(root, 1) is None                                # guarded chapter: stays crop, nothing goes stale
    clean = root / "images" / "c0001" / "clean"
    clean.mkdir(parents=True)
    (clean / "clean.json").write_text(json.dumps({"mode": "remove", "pictures": {}}), encoding="utf-8")   # cleaned on purpose
    assert B.why_render(root, 1) == "watermark setting changed"
    rep.write_text(json.dumps({**report, "watermark": "remove"}), encoding="utf-8")
    assert B.why_render(root, 1) is None
    pic = clean / "s01.jpg"
    pic.write_bytes(b"x")
    import os
    later = mp4.stat().st_mtime + 10
    os.utime(pic, (later, later))
    assert B.why_render(root, 1) == "clean picture s01.jpg changed"
    set_mode("crop")
    assert B.why_render(root, 1) == "watermark setting changed"


def test_missing_inputs_names_the_tool_in_remove_mode_only(world, monkeypatch):
    from novel.stages import episode_build as B
    root = world["root"]
    monkeypatch.setattr(CL, "tool_missing", lambda: "the npm package sharp not installed: run `npm ci` in flow")
    assert "clean-tool" not in {m["kind"] for m in B.missing_inputs(root, 1, 1)}
    set_mode("remove")
    item = next(m for m in B.missing_inputs(root, 1, 1) if m["kind"] == "clean-tool")
    assert "npm ci" in item["fix"] and "images.watermark: crop" in item["fix"]


# ------------------------------------------------------------------ novel clean


def run_cmd(argv, capsys):
    rc = clean_cmd.main(["--book", BOOK, "--ep", "1", *argv])
    return rc, capsys.readouterr().out


def test_novel_clean_prints_per_chapter_and_exits_0_when_pictures_were_cropped(world, monkeypatch, capsys):
    root = world["root"]
    monkeypatch.setattr(CL, "run_remover", FakeTool(world["tmp"], make_chapter(root)))
    monkeypatch.setattr(CL, "tool_missing", lambda: None)
    rc, out = run_cmd([], capsys)
    assert rc == 0, out
    assert "chapter 1: 5 pictures; made 5, already clean 0" in out
    assert "removed 1, cropped instead 3 (ghost 1, still marked 1, error 1), no mark 1" in out
    assert "images.watermark is 'crop'" in out                           # a note: the copies are not used yet
    rc, out = run_cmd([], capsys)                                        # second run: nothing to do
    assert rc == 0 and "made 0, already clean 5" in out
    rc, out = run_cmd(["--force"], capsys)
    assert "made 5, already clean 0" in out


def test_novel_clean_sheet_and_open(world, monkeypatch, capsys):
    root = world["root"]
    monkeypatch.setattr(CL, "run_remover", FakeTool(world["tmp"], make_chapter(root)))
    monkeypatch.setattr(CL, "tool_missing", lambda: None)
    opened = []
    monkeypatch.setattr(clean_cmd.webbrowser, "open", lambda url: opened.append(url))
    rc, out = run_cmd(["--sheet"], capsys)
    sheet = root / "images" / "c0001" / "clean" / "sheet.html"
    assert rc == 0 and f"sheet: {sheet}" in out and sheet.is_file() and opened == []
    page = sheet.read_text(encoding="utf-8")
    assert "removed 1, cropped instead 3 (ghost 1, still marked 1, error 1), no mark 1" in page
    names = [page.index(f">{n} ") for n in ("s02.jpg", "s03.jpg", "s04.jpg", "s01.jpg", "s05.jpg")]
    assert names == sorted(names)                                        # rejected first, then removed, then no mark
    assert page.count("data:image/jpeg;base64,") >= 9 and "boom: could not decode" in page
    rc, out = run_cmd(["--open"], capsys)
    assert rc == 0 and opened == [sheet.as_uri()]


def test_novel_clean_exit_codes(world, monkeypatch, capsys):
    root = world["root"]
    monkeypatch.setattr(CL, "run_remover", FakeTool(world["tmp"], make_chapter(root)))
    monkeypatch.setattr(CL, "tool_missing", lambda: None)
    rc, out = run_cmd(["--chapters", "9"], capsys)
    assert rc == 1 and "chapters [9] are not in episode 1" in out
    shutil.rmtree(root / "images")
    rc, out = run_cmd([], capsys)
    assert rc == 1 and "no downloaded pictures" in out
    monkeypatch.setattr(CL, "tool_missing", lambda: "the npm package sharp not installed: run `npm ci` in flow")
    rc, out = run_cmd([], capsys)
    assert rc == 1 and "cannot remove the mark" in out and "npm ci" in out


def test_novel_clean_does_not_depend_on_the_working_directory(world, monkeypatch, capsys, tmp_path):
    root = world["root"]
    monkeypatch.setattr(CL, "run_remover", FakeTool(world["tmp"], make_chapter(root)))
    monkeypatch.setattr(CL, "tool_missing", lambda: None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    rc, out = run_cmd(["--sheet"], capsys)
    assert rc == 0 and (root / "images" / "c0001" / "clean" / "sheet.html").is_file()
    assert list(elsewhere.iterdir()) == []                               # nothing written next to the working directory
    assert CL.GWR.is_absolute() and CL.FLOW.is_absolute()


# ------------------------------------------------------------------ the guards: old pictures are never touched


def refused(root, n=1, **kw):
    return CL.refusal(root, n, **kw)


def test_guard_published_episode(world):
    root = world["root"]
    make_chapter(root)
    assert refused(root) is None
    (root / "episode.yaml").write_text("published: true\n", encoding="utf-8")
    assert "published: true" in refused(root)
    assert "published: true" in refused(root, force=True)                # never lifted
    with pytest.raises(CL.Refused, match="chapter 1: refused, the episode is marked published.*left as they are"):
        CL.clean_chapter(root, 1, remover=FakeTool(world["tmp"], {}), allow_old=True)
    assert not (root / "images" / "c0001" / "clean").exists()


def test_guard_archive_folder(world, tmp_path):
    arch = tmp_path / "_archive" / "v1-old-flow" / "E003"
    shutil.copytree(world["root"], arch)
    make_chapter(arch)
    assert "_archive" in refused(arch) and "_archive" in refused(arch, force=True)
    with pytest.raises(CL.Refused, match="_archive"):
        CL.clean_chapter(arch, 1, remover=FakeTool(tmp_path, {}), allow_old=True)


def test_guard_old_pictures_and_videos(world):
    root = world["root"]
    make_chapter(root)
    img = root / "images" / "c0001"
    assert refused(root) is None
    # a chapter video rendered the old way
    (root / "video").mkdir()
    mp4 = root / "video" / "c0001.mp4"
    mp4.write_bytes(b"x")
    assert "rendered from cropped pictures" in refused(root)
    mp4.with_suffix(".render.json").write_text(json.dumps({"watermark": "remove"}), encoding="utf-8")
    assert refused(root) is None                                         # a remove-mode video is fine
    mp4.unlink()
    # clean/ made by the old crop step
    V.clean_chapter(root, 1)
    assert "clean/ pictures made by the old crop method" in refused(root)
    shutil.rmtree(img / "clean")
    # hd/ made by the old crop step (no marker)
    (img / "hd").mkdir()
    Image.new("RGB", (1920, 1080)).save(img / "hd" / "s01.jpg")
    assert "hd/ pictures made by the old crop method" in refused(root)
    # --force lifts exactly this case
    assert refused(root, force=True) is None
    # a chapter already cleaned by remove has a record and is not refused, old hd copies or not
    (img / "clean").mkdir()
    (img / "clean" / "clean.json").write_text(json.dumps({"mode": "remove", "pictures": {}}), encoding="utf-8")
    assert refused(root) is None


def test_novel_clean_refuses_old_chapters_unless_forced_with_chapters(world, monkeypatch, capsys):
    root = world["root"]
    fates = make_chapter(root)
    V.clean_chapter(root, 1)
    old = {p.name: p.read_bytes() for p in (root / "images" / "c0001" / "clean").glob("*.jpg")}
    tool = FakeTool(world["tmp"], fates)
    monkeypatch.setattr(CL, "run_remover", tool)
    monkeypatch.setattr(CL, "tool_missing", lambda: None)
    rc, out = run_cmd([], capsys)
    assert rc == 1 and "chapter 1: refused, it already has clean/ pictures made by the old crop method" in out
    assert "The old pictures are left as they are." in out and tool.calls == []
    rc, out = run_cmd(["--force"], capsys)                               # --force alone does not lift it
    assert rc == 1 and "only together with an explicit --chapters" in out and tool.calls == []
    assert {p.name: p.read_bytes() for p in (root / "images" / "c0001" / "clean").glob("*.jpg")} == old
    rc, out = run_cmd(["--chapters", "1", "--force"], capsys)
    assert rc == 0 and "made 5" in out and len(tool.calls) == 1
    (root / "episode.yaml").write_text("published: true\n", encoding="utf-8")
    rc, out = run_cmd(["--chapters", "1", "--force"], capsys)            # published: never, even forced
    assert rc == 1 and "marked published: true" in out and len(tool.calls) == 1


def test_build_and_render_stay_on_the_old_method_for_guarded_chapters(world, monkeypatch, capsys):
    """images.watermark: remove does not apply to a published episode or a chapter with old pictures: the renderer
    uses today's crop and never calls the tool."""
    from novel.stages import episode_render as R
    root = world["root"]
    make_chapter(root)
    tool = FakeTool(world["tmp"], {})
    monkeypatch.setattr(CL, "run_remover", tool)
    set_mode("remove")
    (root / "episode.yaml").write_text("published: true\n", encoding="utf-8")
    CL._said.clear()
    got = R.chapter_images(root, 1, scenes())
    assert tool.calls == [] and all(size_of(p)[1] < H for p in got)
    assert "not applied: the episode is marked published: true" in capsys.readouterr().out
    assert not (root / "images" / "c0001" / "clean" / "clean.json").exists()


# ------------------------------------------------------------------ the real package (skipped without it)


needs_tool = pytest.mark.skipif(CL.tool_missing() is not None, reason=f"the npm package is not installed: {CL.tool_missing()}")


@needs_tool
def test_real_package_on_a_plain_picture_and_a_synthetic_mark(world, tmp_path):
    """The real remover, run the way novel runs it (node flow/node_modules/.../gwr.mjs, never npx): a plain 1376x768
    picture is read and written back unchanged in size with `applied: false`, a picture with a synthetic mark is
    recognised; novel's own check then decides what to keep."""
    bg = background()
    plain = save(bg, tmp_path / "in" / "s01-2k.jpg")
    marked = save(with_mark(bg), tmp_path / "in" / "s02-2k.jpg")
    out = tmp_path / "out"
    res = CL.run_remover({plain.name: plain, marked.name: marked}, out)
    assert res[plain.name] == {"applied": False, "skip": "no-watermark-detected", "error": None}
    assert res[marked.name]["applied"] is True and res[marked.name]["error"] is None
    for name in (plain.name, marked.name):
        assert Image.open(out / name).size == (W, H) and Image.open(out / name).format == "JPEG"
    # the picture the tool did not touch still differs from its source only by the q95 re-encode
    a, b = np.asarray(Image.open(plain), float), np.asarray(Image.open(out / plain.name), float)
    assert np.abs(a - b).mean() < 2.0
    # one unreadable file must not stop the rest
    junk = tmp_path / "in" / "s03-2k.jpg"
    junk.write_bytes(b"not a jpeg")
    res = CL.run_remover({plain.name: plain, junk.name: junk}, tmp_path / "out2")
    assert res[plain.name]["error"] is None and res[junk.name]["error"]
    assert (tmp_path / "out2" / plain.name).is_file() and not (tmp_path / "out2" / junk.name).exists()


@needs_tool
def test_real_package_end_to_end_keeps_only_clean_results(world):
    root = world["root"]
    img = root / "images" / "c0001"
    bg = background()
    save(with_mark(bg), img / "s01-2k.jpg")
    save(bg, img / "s02-2k.jpg")
    r = CL.clean_chapter(root, 1, log=lambda s: None)
    out = img / "clean"
    assert r["pictures"]["s02.jpg"]["result"] == CL.NO_MARK
    assert r["pictures"]["s01.jpg"]["result"] in (CL.REMOVED, CL.GHOST, CL.STILL, CL.ERROR)
    for name, e in r["pictures"].items():
        size = size_of(out / name)
        assert (size == (W, H)) == (not e["cropped"])
        if not e["cropped"] and e["result"] == CL.REMOVED:
            assert CL.verdict(CL.measure(out / name, tuple(e["box"]))) is None      # nothing ghostly is ever kept


# ------------------------------------------------------------------ a real render from a mixed chapter


def test_a_chapter_of_removed_and_cropped_pictures_renders(world, monkeypatch, capsys):
    """Removed pictures are full frame, rejected ones are cropped: both 16:9-ish, and the renderer takes each as it is."""
    pytest.importorskip("PIL")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    import test_render as TR
    from novel.commands import render_cmd
    root = world["root"]
    assets = TR.make_assets(world["tmp"])
    TR.make_hindi(root, 1)
    TR.make_scenes(root, 1)
    TR.make_audio(root, 1, assets)
    TR.make_captions(root, 1)
    img = root / "images" / "c0001"
    bg, marked = background(), with_mark(background())
    fates = {}
    for i, fate in enumerate([bg, marked, with_ghost(bg, -15)], 1):          # removed, still marked, ghost
        save(marked, img / f"s{i:02d}-2k.jpg")
        fates[f"s{i:02d}-2k.jpg"] = fate
    monkeypatch.setattr(CL, "run_remover", FakeTool(world["tmp"], fates))
    set_mode("remove")
    rc = render_cmd.main(["--book", BOOK, "--ep", "1", "--chapter", "1", "--end", "6"])
    out = capsys.readouterr().out
    assert rc == 0, out
    rep = json.loads((root / "video" / "c0001-first6s.render.json").read_text(encoding="utf-8"))
    assert rep["pass"] is True and rep["watermark"] == "remove" and rep["images"] == 3
    sizes = [size_of(root / "images" / "c0001" / "clean" / f"s{i:02d}.jpg") for i in (1, 2, 3)]
    assert sizes[0] == (W, H) and sizes[1][1] < H and sizes[2][1] < H           # mixed, and it rendered
    doc = json.loads((root / "images" / "c0001" / "clean" / "clean.json").read_text(encoding="utf-8"))
    assert [doc["pictures"][f"s0{i}.jpg"]["result"] for i in (1, 2, 3)] == [CL.REMOVED, CL.STILL, CL.GHOST]
