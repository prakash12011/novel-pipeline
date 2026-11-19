"""One test drives the scenes-only flow, command after command, on an invented two-chapter book ("tide"):

status -> prompt -> import (BIBLE, chapter 1, chapter 2) -> qa --only-code -> approve -> characters make
-> images -> status -> build -> status -> meta -> thumb -> status.

The command handlers are the real ones; the outside world is faked (a fake Flow generator and character script,
real ffmpeg for the audio and the video, no Chrome, Claude or Gemini). After each step the test checks the files
and that the "next" command the step printed is the one the following step runs: a place where one command's
output did not fit the next command's input is a bug to fix where it happens.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from novel import status as ST
from novel.cli import main as novel_main
from novel.commands import characters_cmd, images_cmd, import_cmd
from novel.core import paths
from novel.publish import validate
from novel.stages import episode as E
from novel.stages import episode_translate as T
from novel.stages.manifest import Manifest

from test_import import FIX, bible_reply, block, chapter_lines, counts, ep
from test_images_cmd import UP

HERE = Path(__file__).parent
FAKE_GEN = HERE / "fake_gen.mjs"
FAKE_CHAR = HERE / "fakes" / "fake_char.mjs"
BOOK = "tide"

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                 reason="ffmpeg/ffprobe not installed"),
              pytest.mark.skipif(shutil.which("node") is None, reason="needs node (fake Flow scripts)")]


DESC_TEMPLATE = """Tide Tales ki kahani Hindi mein!
Episode [N] mein Chapter [X] se [Y] tak: [one-line spoiler-light teaser in our own words].

Chapters
00:00 Intro
[mm:ss or h:mm:ss] Chapter [X]
... one line per chapter up to Chapter [Y]

Puri series (playlist): [playlist link]
Previous: Episode [N-1] (Chapter [X-10]-[Y-10])      <- leave this line out for Episode 1
Next: Episode [N+1] (Chapter [Y+1]-[Y+10])

[one short comment question about this episode]

#TideTales #Hindi #Explained"""


def ffmpeg(*args):
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


class Cli:
    """Runs `novel <args>` through the real command table and keeps what it printed."""

    def __init__(self, capsys):
        self.capsys = capsys

    def __call__(self, *args, expect=0):
        code = novel_main(list(args))
        cap = self.capsys.readouterr()
        out = cap.out + cap.err
        if expect is not None:
            assert code == expect, f"novel {' '.join(args)} -> {code}\n{out}"
        return SimpleNamespace(code=code, out=out, flat=" ".join(out.split()))      # flat: status wraps long lines


def next_command(out: str) -> str:
    """The command a step tells the person to run next (the text after 'Next:' or 'next:')."""
    m = re.findall(r"(?im)^\s*(?:next(?: step)?:?\s*)(novel .+?)\s*$", out)
    return m[-1] if m else ""


def make_world(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    (home / "settings.yaml").write_text(yaml.safe_dump({
        "data_dir": str(data),
        "chrome": {"cdp_url": "http://127.0.0.1:9444", "exe": r"C:\fake\chrome.exe", "profile": r"D:\fake profile"}}),
        encoding="utf-8")
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.setenv("NOVEL_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("FLOW_GEN", str(FAKE_GEN))
    monkeypatch.setenv("FLOW_CHAR", str(FAKE_CHAR))
    monkeypatch.setenv("FAKE_SCRIPT", "{}")
    monkeypatch.delenv("FAKE_CHAR_SCRIPT", raising=False)
    for key in ("GEMINI_API_KEY", "GEMINI_API_KEY2", "GEMINI_API_KEY3"):
        monkeypatch.delenv(key, raising=False)
    book = data / BOOK
    shutil.copytree(FIX / "chapters", book / "chapters")
    shutil.copytree(FIX / "bible", book / "bible")
    chars = json.loads((book / "bible" / "characters.json").read_text(encoding="utf-8"))
    chars["characters"]["tobias_reed"]["variants"]["base"]["flow_made"] = False      # one Flow Character to make
    (book / "bible" / "characters.json").write_text(json.dumps(chars, ensure_ascii=False, indent=2), encoding="utf-8")
    (book / "book.yaml").write_text(yaml.safe_dump({
        "book": {"title": "Tide Tales", "author": "A. Writer"},
        "images": {"flow_project": "https://flow.google.com/project/slice", "flow_model": "Nano Banana Pro",
                   "pause": 0, "chapter_pause": 0, "rest_every": 50, "rest": 0, "batch": 1, "max_per_run": 200,
                   "chapters_per_day": 5, "wall_cooldown_hours": 12},
        "video": {"style": "kenburns", "hd": {"enabled": False}, "disclaimer": {"seconds": 1.0}},   # not about the style
        "publish": {"title_template": "Tide Tales Explained in Hindi | Episode [N] (Ch [X]-[Y])",
                    "description_template": DESC_TEMPLATE,
                    "tags": ["tide tales hindi", "tide tales explained in hindi"],
                    "hashtags": ["#TideTales", "#Hindi", "#Explained"],
                    "playlist_url": "https://www.youtube.com/playlist?list=PLinvented"},
    }, allow_unicode=True), encoding="utf-8")
    return home, data, book


def add_audio_and_timing(ep_root: Path) -> dict:
    """A sine tone per chapter and a timing map that matches the Hindi paragraphs (heading + body): the
    narrator reads 30 characters a second (twice the speed of the real voice, to keep the render short)."""
    man = Manifest.load(ep_root / "manifest.json")
    starts_of = {}
    for c in man.chapters:
        name = f"{c.idx:05d}"
        paras = [p for p in (ep_root / "hindi" / f"{name}.txt").read_text(encoding="utf-8").split("\n\n") if p.strip()]
        lengths = [max(1.5 if i == 0 else 4.0, len(p) / 30.0) for i, p in enumerate(paras)]
        starts = [round(sum(lengths[:i]), 3) for i in range(len(paras))]
        total = round(sum(lengths), 3)
        starts_of[c.idx] = (starts, total)
        (ep_root / "audio").mkdir(exist_ok=True)
        (ep_root / "timing").mkdir(exist_ok=True)
        ffmpeg("-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=24000:duration={total}", "-ac", "1",
               "-c:a", "libmp3lame", "-b:a", "48k", ep_root / "audio" / f"{name}.mp3")
        ends = starts[1:] + [total]
        (ep_root / "timing" / f"{name}.json").write_text(json.dumps({
            "total_seconds": total,
            "chunks": [{"para": p, "start_sec": s, "end_sec": e} for p, (s, e) in enumerate(zip(starts, ends))]}),
            encoding="utf-8")
        c.status, c.hindi, c.audio, c.timing, c.seconds = "done", f"{name}.txt", f"{name}.mp3", f"{name}.json", total
    man.save()
    return starts_of


def real_picture(path: Path):
    """The fake generator writes a stub that only passes the size/marker test; the render needs a real picture."""
    from PIL import Image
    import random
    rnd = random.Random(7)
    im = Image.new("RGB", (640, 360))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(640 * 360)])
    im.save(path, quality=90)


def captions_old_format(ep_root: Path, n: int, starts: list[float], total: float):
    ends = starts[1:] + [total]
    (ep_root / "captions").mkdir(exist_ok=True)
    (ep_root / "captions" / f"{n:05d}.json").write_text(json.dumps({"chapter": n, "lines": [
        {"para": p, "start": starts[p], "end": ends[p] - 0.3, "en": f"Line {p} of chapter {n}."}
        for p in range(1, len(starts))]}), encoding="utf-8")


def test_scenes_only_slice(tmp_path, monkeypatch, capsys):
    home, data, book = make_world(tmp_path, monkeypatch)
    novel = Cli(capsys)

    # ---- the episode: chapters extracted, episode made, Hindi locked, narration and timing there already
    r = novel("episode", "add", "--book", BOOK, "--chapters", "1-2")
    root = paths.ep_root(BOOK, 1)
    assert (root / "manifest.json").exists() and (root / "chapters").is_dir()
    shutil.copytree(FIX / "hindi", root / "hindi")
    monkeypatch.setattr(E, "EPISODES", paths.episodes_dir(BOOK))
    T.cmd_lock(SimpleNamespace(episode=1, chapters="1-2"))
    timing = add_audio_and_timing(root)

    # ---- 1. status: nothing but the Hindi, so NOT READY and the first line names `novel prompt`
    r = novel("status", "--book", BOOK, "--ep", "1", expect=1)
    first = re.search(r"Missing, in the order to run it:\n\s+1\. (.+)", r.out).group(1)
    assert "novel prompt --chapters 1-2" in first, r.out
    assert "VERDICT: NOT READY" in r.out

    # ---- 2. prompt: scenes mode (the Hindi is locked), files for Gemini, window counts
    r = novel("prompt", "--book", BOOK, "--chapters", "1-2")
    assert "(scenes mode)" in r.out and "Batch B001-002" in r.out
    batch = book / "gemini" / "B001-002"
    assert (batch / "prompt.txt").is_file() and (batch / "batch.json").is_file()
    windows = counts(book)
    assert windows[1] >= 4 and windows[2] >= 3
    assert f"chapter 1: {windows[1]}" in r.out
    assert "Copy the reply and run: novel import" in r.out        # the next command is the one we use

    # ---- 3. import: the BIBLE reply, then one IMAGES reply per chapter (clipboard faked)
    def imp(text):
        monkeypatch.setattr(import_cmd, "read_clipboard", lambda: text)
        return novel("import", "--book", BOOK)
    r = imp(bible_reply())
    assert "BIBLE B001-002  ok" in r.out and "Say to Gemini: next" in r.out
    r = imp(block("IMAGES", "c0001", chapter_lines(windows[1], 1)))
    assert f"chapter 1 complete: {windows[1]} images" in r.out
    r = imp(block("IMAGES", "c0002", chapter_lines(windows[2], 2)))
    assert f"chapter 2 complete: {windows[2]} images" in r.out
    nxt = re.search(r"Next: (novel .+)$", r.out, re.M).group(1)
    assert nxt == "novel qa --chapters 1-2", r.out         # what import prints is what we run
    for n in (1, 2):
        assert (root / "scenes" / f"{n:05d}.json").is_file()
    assert (root / "hindi.lock.json").is_file() and (book / "bible" / "locations.json").is_file()

    # ---- 4. qa (code checks only)
    r = novel("qa", "--book", BOOK, "--ep", "1", "--chapters", "1-2", "--only-code")
    for n in (1, 2):
        doc = json.loads((root / "qa" / f"{n:05d}.json").read_text(encoding="utf-8"))
        assert doc["version"] == 2 and doc["errors"] == [], doc
    assert next_command(r.out) == "novel approve --chapters 1-2", r.out

    # ---- 5. approve
    r = novel("approve", "--book", BOOK, "--ep", "1", "--chapters", "1-2")
    assert "novel characters make --keys tobias_reed" in r.flat    # approve names the command, status names it too
    assert "Next: novel characters make first" in r.out and "novel images --chapters 1-2" in r.out
    plan = json.loads((root / "flow" / "plan.json").read_text(encoding="utf-8"))
    assert len(plan["shots"]) == 13 and any("Tobias Reed" in s["chars"] for s in plan["shots"])
    r = novel("status", "--book", BOOK, "--ep", "1", expect=1)
    assert "novel characters make --keys tobias_reed" in r.flat and "VERDICT: NOT READY (4 blockers)" in r.out

    # ---- 6. characters make --yes (fake character script; the Chrome check answers "the port answers")
    def with_inject(handler, *args, **kw):
        code = handler(list(args), inject={"answers": UP}, ask=lambda q: pytest.fail(f"asked: {q}"),
                       interactive=False, **kw)
        cap = capsys.readouterr()
        return SimpleNamespace(code=code, out=cap.out + cap.err)
    r = with_inject(characters_cmd.make_main, "--book", BOOK, "--ep", "1", "--yes")
    assert r.code == 0, r.out
    chars = json.loads((book / "bible" / "characters.json").read_text(encoding="utf-8"))
    assert chars["characters"]["tobias_reed"]["variants"]["base"]["flow_made"] is True
    assert (book / "bible" / "flow" / "tobias_reed-portrait.jpg").exists()
    assert "Next: novel images --ep 1" in r.out                   # characters make says what comes next

    # ---- 7. images (fake generator), non-interactive
    r = with_inject(images_cmd.main, "--book", BOOK, "--ep", "1")
    assert r.code == 0, r.out
    assert "saved 13" in r.out and "All pictures are done. Next: novel status" in r.out
    for shot in plan["shots"]:
        assert (root / shot["out"]).stat().st_size > 50000
        real_picture((root / shot["out"]))                           # the stub is not a picture: put a real one in
    assert (root / "flow" / "journal.jsonl").exists()
    for n, (starts, total) in timing.items():
        captions_old_format(root, n, starts, total)                       # no Gemini call: the old one-step format

    # ---- 8. status: everything a build needs
    r = novel("status", "--book", BOOK, "--ep", "1", expect=0)
    assert "VERDICT: READY TO BUILD" in r.out, r.out
    assert "-> novel build" in r.flat and "video not rendered" in r.flat, r.out

    # ---- 9. build (real ffmpeg)
    r = novel("build", "--book", BOOK, "--ep", "1")
    for n in (1, 2):
        assert (root / "video" / f"c{n:04d}.mp4").is_file()
    assert (root / "E001.mp4").is_file() and (root / "E001.timestamps.json").is_file()
    assert next_command(r.out) == "novel meta"                    # build says novel meta ...
    r = novel("status", "--book", BOOK, "--ep", "1", expect=1)
    assert "1. publish text missing -> novel meta" in r.flat       # ... and so does status
    assert "VERDICT: NOT READY (2 blockers)" in r.out and "episode video : E001.mp4" in r.out

    # ---- 10. meta without an AI. Two chapters give two timestamps; YouTube needs three: it must say so.
    teaser = "A lamplighter reads the harbour before the storm."
    r = novel("meta", "--book", BOOK, "--ep", "1", "--ai", "none", "--teaser", teaser, expect=1)
    assert "timestamps: 2 found, YouTube needs at least 3" in r.out
    r = novel("status", "--book", BOOK, "--ep", "1", expect=1)
    assert "publish text invalid (1 error) -> novel meta" in r.flat
    monkeypatch.setattr(validate, "MIN_STAMPS", 2)                # a two-chapter test episode, not a real one
    r = novel("meta", "--book", BOOK, "--ep", "1", "--ai", "none", "--teaser", teaser)
    pub = json.loads((root / "publish" / "publish.json").read_text(encoding="utf-8"))
    assert pub["valid"] is True and teaser in (root / "publish" / "publish.txt").read_text(encoding="utf-8")
    assert "Next: paste publish.txt into Studio, then: novel thumb" in r.out
    r = novel("status", "--book", BOOK, "--ep", "1", expect=1)
    assert "publish text  : valid" in r.out and "VERDICT: NOT READY (1 blocker)" in r.out
    assert "1. thumbnails missing -> novel thumb" in r.flat        # meta said novel thumb, status says it too

    # ---- 11. thumb: bare `novel thumb` has no hooks and no art, and says what to pass; then with a portrait
    r = novel("thumb", "--book", BOOK, "--ep", "1", expect=1)
    assert "publish.json has no thumbnail_hooks" in r.out
    r = novel("thumb", "--book", BOOK, "--ep", "1", "--hook", "THE QUIET|*HARBOUR* WAITS", expect=1)
    assert "No art." in r.out and "tobias_reed" in r.out          # it names the portraits there are
    from PIL import Image
    portrait = book / "bible" / "flow" / "tobias_reed-portrait.jpg"
    assert portrait.read_text(encoding="utf-8") == "fake portrait"   # the fake script's stand-in is not a picture
    Image.new("RGB", (800, 1000), (90, 120, 160)).save(portrait, quality=90)
    r = novel("thumb", "--book", BOOK, "--ep", "1", "--image", "tobias_reed", "--hook", "THE QUIET|*HARBOUR* WAITS")
    thumbs = sorted(p.name for p in (root / "publish").glob("thumb-*.jpg"))
    assert thumbs == ["thumb-a.jpg"] and (root / "publish" / "sheet.jpg").is_file()
    with Image.open(root / "publish" / "thumb-a.jpg") as im:
        assert im.size == (1280, 720)

    # ---- 12. status: done
    r = novel("status", "--book", BOOK, "--ep", "1", expect=0)
    assert "VERDICT: READY TO PUBLISH" in r.out, r.out
    assert "thumbnails    : thumb-a.jpg" in r.out and "publish text  : valid" in r.out
    assert "Next: upload it, then: novel published --url <URL>" in r.flat
    res = ST.collect(root, BOOK, 1, ST.default_deps(BOOK))
    assert res["verdict"] == "READY TO PUBLISH" and res["exit_code"] == 0 and res["missing"] == []
