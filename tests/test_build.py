"""novel build: render + join on a two-chapter fixture episode, current/forced renders, HD settings,
--when-ready waiting. Fixture builders come from test_render.py (invented content, temp NOVEL_HOME,
real ffmpeg, no network)."""

import json
import shutil
import subprocess

import pytest

from novel.core import context, paths
from novel.stages import episode as E

from test_render import BOOK, duration, ff, make_assets, make_episode, make_images, set_hd, write_yaml

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")

import test_render  # noqa: E402  (the module-scoped `world` fixture is reused below)

world = test_render.world


@pytest.fixture
def no_tool(monkeypatch, tmp_path):
    """No chaiNNer on this PC, whatever the real machine has."""
    from novel.stages import episode_hd
    monkeypatch.setattr(episode_hd, "CHAINNER", tmp_path / "no-such-chainner.exe")
    return episode_hd


@pytest.fixture
def fakes(world, monkeypatch, tmp_path):
    """Stand-ins for render, join and make_hd so the orchestration runs in milliseconds.
    Returns the recorded calls: {"render": [...], "join": [...], "hd": [...]}."""
    from novel.stages import episode_build as B, episode_hd
    calls = {"render": [], "join": [], "hd": []}
    clip = tmp_path / "joined.mp4"
    ff("-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=24", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
       "-t", "1", "-c:v", "libx264", "-c:a", "aac", clip)

    def render(ep, n, crf, preset, workers, end, root, captions, hd):
        calls["render"].append({"chapter": n, "captions": captions, "hd": hd})
        return {"pass": True, "seconds": 15.5, "mb": 0.1, "file": f"c{n:04d}.mp4"}

    def join(ep, chapters, watermark=None):
        calls["join"].append(chapters)
        return clip
    monkeypatch.setattr(B, "render", render)
    monkeypatch.setattr(B, "join", join)
    monkeypatch.setattr(episode_hd, "make_hd", lambda ep_root, chapters, **kw: calls["hd"].append(list(chapters)) or 0)
    return calls


# ------------------------------------------------------------------ the real thing


def test_build_two_chapters_then_current_then_forced(world, capsys, monkeypatch, no_tool):
    from novel.commands import build_cmd
    set_hd(False)
    world["elsewhere"].mkdir(exist_ok=True)
    monkeypatch.chdir(world["elsewhere"])                  # nothing may depend on the working directory
    root = E.root(1)
    shutil.rmtree(root / "video", ignore_errors=True)

    assert build_cmd.main(["--book", BOOK, "--ep", "1"]) == 0
    out = capsys.readouterr().out
    assert "0 chapters current, 2 to render" in out
    for n in (1, 2):
        assert (root / "video" / f"c{n:04d}.mp4").exists()
        rep = json.loads((root / "video" / f"c{n:04d}.render.json").read_text(encoding="utf-8"))
        assert rep["pass"] is True and rep["captions"] is True
        assert abs(rep["seconds"] - (1.5 + world["assets"]["seconds"] + 2.0)) < 0.2
    final = root / "E001.mp4"
    assert final.exists() and str(final) in out
    assert (root / "E001.timestamps.txt").exists() and (root / "E001.timestamps.json").exists()
    stamps = json.loads((root / "E001.timestamps.json").read_text(encoding="utf-8"))
    assert [c["chapter"] for c in stamps["chapters"]] == [1, 2]
    assert "0:00 " in (root / "E001.timestamps.txt").read_text(encoding="utf-8")
    assert abs(duration(final) - stamps["duration"]) < 0.1
    assert "next: novel meta" in out and "chapter 1: rendered (no video yet), pass" in out
    assert "E001.timestamps.txt" in out and "E001.timestamps.json" in out and "duration:" in out

    # second run: both current, nothing rendered, the join is redone
    before = {n: (root / "video" / f"c{n:04d}.mp4").stat().st_mtime_ns for n in (1, 2)}
    assert build_cmd.main(["--book", BOOK, "--ep", "1"]) == 0
    out = capsys.readouterr().out
    assert "2 chapters current, 0 to render" in out and "rendered (" not in out
    assert before == {n: (root / "video" / f"c{n:04d}.mp4").stat().st_mtime_ns for n in (1, 2)}

    # --force re-renders; --chapters builds a partial episode with the partial name
    assert build_cmd.main(["--book", BOOK, "--ep", "1", "--chapters", "1", "--force"]) == 0
    out = capsys.readouterr().out
    assert "chapter 1: rendered (forced)" in out
    assert (root / "video" / "c0001.mp4").stat().st_mtime_ns > before[1]
    assert (root / "video" / "c0002.mp4").stat().st_mtime_ns == before[2]
    assert (root / "E001-c1.mp4").exists() and (root / "E001-c1.timestamps.txt").exists()


# ------------------------------------------------------------------ HD


def test_hd_auto_without_the_tool_prints_one_line_and_continues(world, capsys, fakes, no_tool):
    from novel.commands import build_cmd
    set_hd("auto")
    make_episode(4, [21, 22], world["assets"])
    assert build_cmd.main(["--book", BOOK, "--ep", "4"]) == 0
    out = capsys.readouterr().out
    assert len([l for l in out.splitlines() if l.startswith("hd:")]) == 1
    assert fakes["hd"] == []
    assert [c["chapter"] for c in fakes["render"]] == [21, 22] and fakes["join"] == [None]
    assert all(c["hd"] is True for c in fakes["render"])      # existing hd pictures would still be used


def test_hd_true_without_the_tool_is_an_error(world, fakes, no_tool):
    from novel.commands import build_cmd
    set_hd(True)
    make_episode(4, [21, 22], world["assets"])
    with pytest.raises(SystemExit, match="video.hd.enabled is true but chaiNNer not found"):
        build_cmd.main(["--book", BOOK, "--ep", "4"])
    assert fakes["render"] == [] and fakes["hd"] == []


def test_hd_false_never_calls_the_tool(world, capsys, fakes, monkeypatch, tmp_path):
    from novel.commands import build_cmd
    from novel.stages import episode_hd
    exe, model = tmp_path / "chainner.exe", tmp_path / "m.param"          # an installed tool: still not used
    for f in (exe, model, model.with_suffix(".bin")):
        f.write_bytes(b"x")
    monkeypatch.setattr(episode_hd, "CHAINNER", exe)
    monkeypatch.setattr(episode_hd, "MODEL", model)
    set_hd(False)
    make_episode(4, [21, 22], world["assets"])
    assert build_cmd.main(["--book", BOOK, "--ep", "4"]) == 0
    assert fakes["hd"] == [] and not [l for l in capsys.readouterr().out.splitlines() if l.lower().startswith("hd")]
    assert all(c["hd"] is False for c in fakes["render"])
    # enabled: auto with the tool present runs it once, for the chapters about to be rendered; --no-hd skips it
    set_hd("auto")
    assert build_cmd.main(["--book", BOOK, "--ep", "4", "--no-hd"]) == 0
    assert fakes["hd"] == []
    assert build_cmd.main(["--book", BOOK, "--ep", "4", "--chapters", "22"]) == 0
    assert fakes["hd"] == [[22]]
    assert fakes["join"][-1] == "22"


# ------------------------------------------------------------------ --when-ready


def test_when_ready_waits_for_an_image_then_proceeds(world, capsys, fakes, no_tool):
    from novel.commands import build_cmd
    set_hd(False)
    root = make_episode(5, [31, 32], world["assets"])
    (root / "images" / "c0031" / "s02-2k.jpg").unlink()
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:                                  # the image arrives during the second wait
            make_images(root, 31, world["assets"], only=2)
    clock = lambda: 1000.0                                   # time stands still: the slot wait is the full gap

    # without --when-ready: stops and lists it
    assert build_cmd.main(["--book", BOOK, "--ep", "5"]) == 1
    out = capsys.readouterr().out
    assert "images missing" in out and "fix: novel images --ep 5 --chapters 31" in out and fakes["render"] == []

    rc = build_cmd.main(["--book", BOOK, "--ep", "5", "--when-ready", "--per-hour", "2"],
                        sleep=sleep, clock=clock, interval=5)
    out = capsys.readouterr().out
    assert rc == 0, out
    waits = [l for l in out.splitlines() if "waiting for inputs" in l]
    assert len(waits) == 2 and "c0031 images" in waits[0]            # a line at every check
    assert slept[:2] == [5, 5]
    assert [c["chapter"] for c in fakes["render"]] == [31, 32]
    assert slept[2:] == [1800.0]                                      # --per-hour 2: a slot every 30 minutes
    assert "next slot in 30 min" in out
    assert (root / "pace.log").read_text(encoding="utf-8").count("waiting for inputs") == 2


def test_when_ready_gives_up_and_ctrl_c_exits_cleanly(world, capsys, fakes, no_tool):
    from novel.commands import build_cmd
    set_hd(False)
    root = make_episode(5, [31, 32], world["assets"])
    (root / "images" / "c0032" / "s03-2k.jpg").unlink()
    rc = build_cmd.main(["--book", BOOK, "--ep", "5", "--when-ready"], sleep=lambda s: None, clock=lambda: 0.0,
                        interval=10, max_wait=30)
    out = capsys.readouterr().out
    assert rc == 1 and "giving up on chapters [32]" in out
    assert [c["chapter"] for c in fakes["render"]] == [31] and fakes["join"] == []      # 31 rendered, no join

    def stop(_):
        raise KeyboardInterrupt
    assert build_cmd.main(["--book", BOOK, "--ep", "5", "--when-ready"], sleep=stop, clock=lambda: 0.0) == 130
    assert "interrupted" in capsys.readouterr().out
