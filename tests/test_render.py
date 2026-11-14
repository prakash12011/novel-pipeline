"""novel render: the input check, a preview render, the subtitle filter, HD settings, working directory.

INVENTED content (a short made-up Hindi chapter with solid-colour pictures and a sine-tone narration),
a temporary NOVEL_HOME, real ffmpeg, no network. A module-scoped fixture builds the book "demo" with
episode E001 = chapters 1 and 2, complete. test_build.py reuses the builders defined here.
"""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from novel.core import context, paths
from novel.stages import episode as E
from novel.stages.manifest import Chapter, Manifest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")

BOOK = "demo"
AUDIO_SECONDS = 12.0
# paragraph 0 is the chapter heading (the title card shows it); three body paragraphs = three scenes.
PARAS = ["अध्याय एक", "सुबह की धूप नदी पर फैल रही थी।", "गाँव धीरे-धीरे जाग रहा था।", "वह चुपचाप आगे बढ़ गया।"]
START = [0.0, 1.0, 4.0, 7.0]          # when the narrator starts each paragraph, in narration seconds
CAPTIONS = ["The morning sun spread over the river.", "The village was slowly waking up.",
            "He walked on without a word."]


def ff(*args):
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True).stdout
    return float(out.strip())


def write_yaml(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, allow_unicode=True), encoding="utf-8")


def make_assets(base: Path) -> dict:
    """Three gradient 640x360 JPEGs (the smallest size the mark search accepts) and a 12 s sine mp3."""
    from PIL import Image
    imgs = []
    for k, (a, b) in enumerate([((230, 120, 60), (250, 220, 120)), ((60, 140, 220), (200, 230, 250)),
                                ((90, 200, 120), (230, 250, 200))]):
        im = Image.new("RGB", (640, 360))
        px = im.load()
        for x in range(640):
            c = tuple(round(a[i] + (b[i] - a[i]) * x / 639) for i in range(3))
            for y in range(360):
                px[x, y] = c
        f = base / f"img{k}.jpg"
        im.save(f, quality=85)
        imgs.append(f)
    mp3 = base / "tone.mp3"
    ff("-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=24000:duration={AUDIO_SECONDS}", "-ac", "1",
       "-c:a", "libmp3lame", "-b:a", "48k", mp3)
    return {"images": imgs, "mp3": mp3, "seconds": duration(mp3)}


def make_manifest(ep_root: Path, ep: int, chapters: list[int]):
    Manifest(path=ep_root / "manifest.json", chapters=[
        Chapter(idx=c, title=f"Chapter {c}: Fixture {c}", source=f"{c:04d}_x.txt") for c in chapters]).save()
    (ep_root / "episode.json").write_text(json.dumps({"episode": ep, "start": chapters[0], "end": chapters[-1]}),
                                          encoding="utf-8")


def make_hindi(ep_root: Path, n: int, lock: bool = True):
    name = f"{n:05d}"
    f = ep_root / "hindi" / f"{name}.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n\n".join(PARAS) + "\n", encoding="utf-8")
    if lock:
        lk = E.lock_path(ep_root)
        data = json.loads(lk.read_text(encoding="utf-8")) if lk.exists() else {}
        data[f"{name}.txt"] = E.sha256(f)
        lk.write_text(json.dumps(data), encoding="utf-8")


def make_scenes(ep_root: Path, n: int):
    name = f"{n:05d}"
    f = ep_root / "scenes" / f"{name}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256((ep_root / "hindi" / f"{name}.txt").read_bytes()).hexdigest()
    f.write_text(json.dumps({"hindi_sha256": sha, "scenes": [
        {"id": i, "para": i, "shot": "wide", "characters": [], "focus": [0.5, 0.5],
         "scene": "A quiet river valley at sunrise with a small village in the distance."} for i in (1, 2, 3)]}),
        encoding="utf-8")


def make_images(ep_root: Path, n: int, assets: dict, only: int | None = None):
    d = ep_root / "images" / f"c{n:04d}"
    d.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(assets["images"], 1):
        if only is None or only == i:
            shutil.copyfile(src, d / f"s{i:02d}-2k.jpg")


def make_audio(ep_root: Path, n: int, assets: dict):
    name = f"{n:05d}"
    (ep_root / "audio").mkdir(parents=True, exist_ok=True)
    (ep_root / "timing").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(assets["mp3"], ep_root / "audio" / f"{name}.mp3")
    ends = START[1:] + [assets["seconds"]]
    (ep_root / "timing" / f"{name}.json").write_text(json.dumps({
        "total_seconds": assets["seconds"],
        "chunks": [{"para": p, "start_sec": s, "end_sec": e} for p, (s, e) in enumerate(zip(START, ends))]}),
        encoding="utf-8")


def make_captions(ep_root: Path, n: int):
    """The old one-step format (no version): used as it is, nothing to translate."""
    f = ep_root / "captions" / f"{n:05d}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    ends = START[1:] + [AUDIO_SECONDS]
    f.write_text(json.dumps({"chapter": n, "lines": [
        {"para": p, "start": START[p], "end": ends[p] - 0.3, "en": CAPTIONS[p - 1]} for p in (1, 2, 3)]}),
        encoding="utf-8")


def make_chapter(ep_root: Path, n: int, assets: dict):
    make_hindi(ep_root, n)
    make_scenes(ep_root, n)
    make_images(ep_root, n, assets)
    make_audio(ep_root, n, assets)
    make_captions(ep_root, n)


def make_episode(ep: int, chapters: list[int], assets: dict, complete: bool = True) -> Path:
    root = E.root(ep)
    root.mkdir(parents=True, exist_ok=True)
    make_manifest(root, ep, chapters)
    if complete:
        for n in chapters:
            make_chapter(root, n, assets)
    return root


def set_hd(mode):
    """video.hd.enabled for the book (and a fast disclaimer card)."""
    write_yaml(paths.book_dir(BOOK) / "book.yaml",
               {"book": {"title": "Demo Tale", "author": "A. Writer"},
                "video": {"hd": {"enabled": mode}, "disclaimer": {"seconds": 1.0}}})


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    base = tmp_path_factory.mktemp("world")
    home, data = base / "home", base / "data"
    home.mkdir()
    data.mkdir()
    mp.setenv("NOVEL_HOME", str(home))
    write_yaml(home / "settings.yaml", {"data_dir": str(data)})
    old = (E.EPISODES, context._ACTIVE_BOOK)
    context.activate(BOOK)
    set_hd(False)
    assets = make_assets(base)
    make_episode(1, [1, 2], assets)
    yield {"base": base, "home": home, "data": data, "assets": assets, "elsewhere": base / "elsewhere"}
    E.EPISODES, context._ACTIVE_BOOK = old
    mp.undo()


# ------------------------------------------------------------------ the input check


def test_input_check_lists_each_missing_kind_with_its_fix(world):
    from novel.stages import episode_build as B
    set_hd(False)
    root = make_episode(3, [7], world["assets"], complete=False)
    kinds = lambda: {m["kind"]: m["fix"] for m in B.missing_inputs(root, 3, 7)}
    got = kinds()
    assert set(got) == {"hindi", "scenes", "audio", "timing"}
    assert got["hindi"].startswith("novel prompt --ep 3 --chapters 7")
    assert got["scenes"].startswith("novel prompt --ep 3 --chapters 7 --mode scenes")
    assert got["audio"].startswith("novel tts push --ep 3 --chapters 7")
    assert got["timing"] == "novel tts pull --ep 3"

    make_hindi(root, 7, lock=False)
    got = kinds()
    assert got["lock"] == "novel approve --ep 3 --chapters 7"
    assert got["captions"].startswith("novel captions --ep 3 --chapters 7")      # no Gemini key to make them
    assert "hindi" not in got

    make_hindi(root, 7)                                                           # locked now
    make_scenes(root, 7)
    got = {m["kind"]: m for m in B.missing_inputs(root, 3, 7)}
    assert "lock" not in got and "scenes" not in got
    assert got["images"]["fix"] == "novel images --ep 3 --chapters 7"
    assert "3 of 3 images missing" in got["images"]["what"] and "s01-2k.jpg" in got["images"]["what"]
    make_images(root, 7, world["assets"], only=2)
    got = {m["kind"]: m for m in B.missing_inputs(root, 3, 7)}
    assert "2 of 3 images missing" in got["images"]["what"] and "s02-2k.jpg" not in got["images"]["what"]

    # a Gemini key in the home's secrets.env means the captions can be made: not missing
    (world["home"] / "secrets.env").write_text("GEMINI_API_KEY=not-a-real-key\n", encoding="utf-8")
    assert "captions" not in {m["kind"] for m in B.missing_inputs(root, 3, 7)}
    assert "captions" not in {m["kind"] for m in B.missing_inputs(root, 3, 7, captions=False)}
    (world["home"] / "secrets.env").unlink()
    assert "captions" not in {m["kind"] for m in B.missing_inputs(root, 3, 7, captions=False)}


def test_render_and_build_stop_with_the_missing_list(world, capsys):
    from novel.commands import build_cmd, render_cmd
    set_hd(False)
    root = make_episode(3, [7], world["assets"], complete=False)
    for f in (root / "audio", root / "timing", root / "scenes", root / "images", root / "hindi", root / "video"):
        shutil.rmtree(f, ignore_errors=True)
    assert render_cmd.main(["--book", BOOK, "--ep", "3", "--chapter", "7", "--no-captions"]) == 1
    out = capsys.readouterr().out
    assert "chapter 7: cannot render yet, 4 things missing" in out
    assert out.count("fix: novel ") == 4 and "nothing was rendered" in out
    assert not (root / "video").exists()
    assert build_cmd.main(["--book", BOOK, "--ep", "3"]) == 1
    out = capsys.readouterr().out
    assert "chapter 7: cannot render yet" in out and out.count("fix: novel ") >= 4
    assert not (root / "video").exists()
    assert render_cmd.main(["--book", BOOK, "--ep", "3", "--chapter", "99"]) == 1
    assert "chapter 99 is not in episode 3" in capsys.readouterr().out


# ------------------------------------------------------------------ rendering


def test_preview_render_from_another_folder(world, capsys, monkeypatch):
    """A 6 s preview: right length, report passes, the subtitle filter carries the bundled fonts folder
    and an absolute path (nothing depends on the working directory)."""
    from novel.commands import render_cmd
    from novel.stages import episode_render
    set_hd(False)
    world["elsewhere"].mkdir(exist_ok=True)
    monkeypatch.chdir(world["elsewhere"])
    seen = []
    real_popen = subprocess.Popen

    def spy(cmd, *a, **k):
        seen.append((list(cmd), k.get("cwd")))
        return real_popen(cmd, *a, **k)
    monkeypatch.setattr(episode_render.subprocess, "Popen", spy)
    target = world["base"] / "out" / "preview.mp4"
    rc = render_cmd.main(["--book", BOOK, "--ep", "1", "--chapter", "1", "--end", "6", "--out", str(target)])
    out = capsys.readouterr().out
    assert rc == 0, out
    root = E.root(1)
    mp4 = root / "video" / "c0001-first6s.mp4"
    assert mp4.exists() and target.exists()
    assert abs(duration(mp4) - 6.0) < 0.1
    report = json.loads(mp4.with_suffix(".render.json").read_text(encoding="utf-8"))
    assert report["pass"] is True and report["captions"] is True and report["has_audio"] is True
    assert (report["width"], report["height"]) == (1920, 1080)
    assert "result: PASS" in out and str(mp4) in out and "duration" in out
    # the render's own ffmpeg: absolute ass path, fonts folder, no working-directory trick
    enc = next(c for c, _ in seen if "-filter_complex" in c)
    graph = enc[enc.index("-filter_complex") + 1]
    fonts = (paths.fonts_dir()).resolve().as_posix().replace(":", "\\:")
    assert f"fontsdir='{fonts}'" in graph
    assert (root / "video").resolve().as_posix().replace(":", "\\:") + "/c0001.en.ass" in graph
    assert [cwd for c, cwd in seen if "-filter_complex" in c] == [None]


def test_subtitle_filter_uses_the_bundled_font(world, tmp_path, monkeypatch):
    from novel.stages import episode_captions as C
    root = E.root(1)
    ass = tmp_path / "with space" / "c0001.en.ass"
    ass.parent.mkdir()
    assert C.write_ass(root, 1, ass) == 2          # line 1 ends 0.2 s after the title card: dropped (< 0.3 s)
    flt = C.chapter_ass_filter(root, 1, ass)
    fonts = paths.fonts_dir().resolve().as_posix()
    assert flt.startswith("ass='") and "fontsdir='" + fonts.replace(":", "\\:") + "'" in flt
    family = C.font_family(paths.fonts_dir() / "NotoSans-SemiBold.ttf")
    assert f"Style: Cap,{family}," in ass.read_text(encoding="utf-8")
    monkeypatch.chdir(tmp_path)                       # run from a folder that holds neither ass nor fonts
    p = subprocess.run(["ffmpeg", "-v", "verbose", "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=1:r=24",
                        "-vf", flt, "-f", "null", "-"], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-800:]
    assert "Noto Sans" in p.stderr or "NotoSans" in p.stderr      # libass picked the font from fontsdir


# ------------------------------------------------------------------ HD settings


def test_hd_plan(monkeypatch, tmp_path):
    from novel.stages import episode_hd as H
    exe, model = tmp_path / "chainner.exe", tmp_path / "m.param"
    monkeypatch.setattr(H, "CHAINNER", tmp_path / "nope.exe")
    monkeypatch.setattr(H, "MODEL", model)
    cfg = lambda v: {"video": {"hd": {"enabled": v}}}
    auto = H.hd_plan(cfg("auto"))
    assert auto["run"] is False and auto["use_pictures"] is True and auto["note"].startswith("hd: chaiNNer not found")
    assert "\n" not in auto["note"]
    assert H.hd_plan(cfg("auto"), no_hd=True) == {"mode": "auto", "run": False, "use_pictures": False, "note": ""}
    off = H.hd_plan(cfg(False))
    assert off == {"mode": "false", "run": False, "use_pictures": False, "note": ""}
    with pytest.raises(SystemExit, match="video.hd.enabled is true but chaiNNer not found"):
        H.hd_plan(cfg(True))
    assert H.hd_plan(cfg(True), needs_tool=False)["run"] is False              # `novel render` never needs the tool
    with pytest.raises(SystemExit, match="use auto, true or false"):
        H.hd_plan(cfg("maybe"))
    exe.write_bytes(b"x")
    monkeypatch.setattr(H, "CHAINNER", exe)
    assert H.hd_plan(cfg("auto"))["run"] is False                              # the model is missing too
    model.write_bytes(b"x")
    model.with_suffix(".bin").write_bytes(b"x")
    ok = H.hd_plan(cfg("auto"))
    assert ok["run"] is True and ok["note"] == "" and H.hd_plan(cfg(True))["run"] is True
