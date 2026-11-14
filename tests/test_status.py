"""novel status: one fixture episode per situation, asserting the data, the verdict, the exit code and the exact
missing lines. Invented content, temp NOVEL_HOME, no network; ffprobe, why_render and Gemini keys are injected."""

import hashlib
import json
import os

import pytest

from novel import status as ST
from novel.commands import status_cmd
from novel.stages.manifest import Chapter, Manifest

BOOK = "bk"
START = 21
GOOD_JPEG = b"\xff\xd8" + b"0" * 60000 + b"\xff\xd9"
CUT_JPEG = b"\xff\xd8" + b"0" * 60000            # no end marker: a download that stopped
TINY_JPEG = b"\xff\xd8" + b"0" * 100 + b"\xff\xd9"
SCENE_TEXT = "A quiet lamp-lit room at night, a man at a table with a letter in his hands, rain on the window glass."
HINDI = "\n\n".join(["Chapter title"] + [f"Paragraph {i}. " + "word " * 40 for i in range(4)])
SECONDS: dict = {}
PROMPT_TAIL = "(then paste into Gemini and run: novel import)"


@pytest.fixture(autouse=True)
def world(tmp_path, monkeypatch):
    SECONDS.clear()
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    (home / "settings.yaml").write_text(f"data_dir: {data.as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("NOVEL_HOME", str(home))
    ep_root = data / BOOK / "episodes" / "E001"
    ep_root.mkdir(parents=True)
    return {"home": home, "data": data, "root": ep_root}


def deps(**kw):
    base_ = dict(why_render=lambda root, n: None, media_seconds=lambda p: SECONDS.get(p.name, 60.0),
                 gemini_keys=lambda: ["key"], captions_needed=lambda ep, n: True, cards_state=lambda: "ready",
                 chrome_lock_files=lambda: [])
    base_.update(kw)
    return ST.Deps(**base_)


# ------------------------------------------------------------------ fixture builders

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def put(root, rel, content):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return p


def base(root, count=3):
    chs = [Chapter(idx=START + i, title=f"Chapter {START + i}", source=f"{START + i:04d}_t.txt")
           for i in range(count)]
    Manifest(path=root / "manifest.json", chapters=chs).save()
    put(root, "episode.json", {"episode": 1, "start": START, "end": START + count - 1})
    put(root, "episode.yaml", "published: false\n")
    for c in chs:
        put(root, f"chapters/{c.source}", "english")
    put(root, "bible/characters.json", {"style": "s", "characters": {
        "mira": {"display": "Mira", "variants": {"base": {"flow_name": "Mira", "flow_made": True},
                                                  "v2": {"flow_name": "Mira v2", "flow_made": False}}},
        "tarot": {"display": "Tarot", "variants": {"base": {"flow_name": "Tarot Lady", "flow_made": False}}}}})
    return [c.idx for c in chs]


def hindi(root, chs):
    for n in chs:
        put(root, f"hindi/{n:05d}.txt", HINDI)


def lock(root, chs):
    f = root / "hindi.lock.json"
    doc = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    for n in chs:
        doc[f"{n:05d}.txt"] = sha(root / "hindi" / f"{n:05d}.txt")
    put(root, "hindi.lock.json", doc)


def scenes(root, chs, version=1, cast=None):
    """A scene file that passes check_chapter (4 windows: one per body paragraph)."""
    from novel.stages import episode_scenes as S
    for n in chs:
        wins = S.chapter_windows(S.chapter_paras(root, n), None)
        doc = {"version": version, "chapter": n, "hindi_sha256": sha(root / "hindi" / f"{n:05d}.txt"), "scenes": []}
        if version == 2:
            doc["groups"] = {"g1": {}}
        for k, (a, _, b, _) in enumerate(wins, 1):
            s = {"id": k, "para": a, "shot": "wide", "scene": SCENE_TEXT, "focus": [0.5, 0.5],
                 "characters": [m["key"] for m in cast or []]}
            if version == 2:
                s["group"], s["cast"] = "g1", cast or []
            doc["scenes"].append(s)
        put(root, f"scenes/{n:05d}.json", doc)


def images(root, n, k, *, cut=()):
    for i in range(1, k + 1):
        put(root, f"images/c{n:04d}/s{i:02d}-2k.jpg", CUT_JPEG if i in cut else GOOD_JPEG)


def plan(root, n, k):
    f = root / "flow" / "plan.json"
    doc = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"shots": []}
    doc["shots"] += [{"id": f"c{n:04d}-s{i:02d}"} for i in range(1, k + 1)]
    put(root, "flow/plan.json", doc)


def audio(root, chs, seconds=60.0):
    man = Manifest.load(root / "manifest.json")
    for c in man.chapters:
        if c.idx in chs:
            c.status, c.hindi, c.audio, c.timing, c.seconds = "done", f"{c.idx:05d}.txt", f"{c.idx:05d}.mp3", f"{c.idx:05d}.json", seconds
            put(root, f"audio/{c.idx:05d}.mp3", b"mp3")
            put(root, f"timing/{c.idx:05d}.json", {"total_seconds": seconds, "chunks": []})
            SECONDS[f"{c.idx:05d}.mp3"] = seconds
    man.save()


def journal(root, *events):
    put(root, "flow/journal.jsonl", "\n".join(json.dumps({"id": i, "state": s}) for i, s in events) + "\n")


def ready(world, count=3):
    """Everything a build needs, in the migrated style (v1 scenes, old-format captions)."""
    root = world["root"]
    chs = base(root, count)
    hindi(root, chs)
    lock(root, chs)
    scenes(root, chs)
    for n in chs:
        images(root, n, 4)
        put(root, f"captions/{n:05d}.json", {"lines": []})
    audio(root, chs)
    return chs


def collect(world, chapters=None, d=None):
    return ST.collect(world["root"], BOOK, 1, d or deps(), chapters=chapters, state=(BOOK, 1))


def lines(res):
    return [m["line"] for m in res["missing"]]


# ------------------------------------------------------------------ the situations

def test_brand_new_episode_only_english_text(world):
    base(world["root"])
    res = collect(world)
    assert [r["text"] for r in res["chapters"]] == [True] * 3
    assert [r["hindi"] for r in res["chapters"]] == [False] * 3
    assert res["verdict"] == "NOT READY" and res["verdict_text"] == "NOT READY (1 blocker)"
    assert res["exit_code"] == 1
    assert lines(res) == [f"chapters 21-23: no Hindi yet -> novel prompt --chapters 21-23 --copy   {PROMPT_TAIL}"]


def test_hindi_imported_not_locked(world):
    root = world["root"]
    hindi(root, base(root))
    res = collect(world)
    assert [r["lock"] for r in res["chapters"]] == ["-"] * 3
    assert lines(res) == ["chapters 21-23: not approved (Hindi not locked or prompts not written) -> "
                          "novel approve --chapters 21-23"]
    assert res["exit_code"] == 1


def test_locked_without_scenes_names_prompt_first(world):
    root = world["root"]
    chs = base(root)
    hindi(root, chs)
    lock(root, chs)
    res = collect(world)
    assert [r["scenes_state"] for r in res["chapters"]] == ["-"] * 3
    assert lines(res) == [
        f"chapters 21-23: no scenes yet -> novel prompt --chapters 21-23 --copy   {PROMPT_TAIL}",
        "chapters 21-23: no narration -> novel run   (or: novel tts push --chapters 21-23)"]
    assert res["verdict_text"] == "NOT READY (2 blockers)"


def test_v2_scenes_with_flow_character_not_made(world):
    root = world["root"]
    chs = base(root)
    hindi(root, chs)
    lock(root, chs)
    scenes(root, chs[:2], version=2, cast=[{"key": "mira", "var": "base"}, {"key": "tarot", "var": "base"}])
    scenes(root, chs[2:], version=2, cast=[{"key": "mira", "var": "v2"}])
    res = collect(world)
    r = res["chapters"]
    assert (r[0]["scenes_state"], r[0]["scenes"]) == ("ok", 4)
    assert (r[0]["chars_made"], r[0]["chars_needed"]) == (1, 2)
    assert (r[2]["chars_made"], r[2]["chars_needed"]) == (0, 1)
    got = lines(res)
    assert 'chapters 21-22: Flow Character "Tarot Lady" not created (needed by chapters 21-22) -> ' \
           'novel characters make --keys tarot' in got
    assert 'chapter 23: Flow Character "Mira v2" not created (needed by chapters 23) -> ' \
           'novel characters make --keys mira' in got


def test_images_partly_done_invalid_file_and_journal(world):
    root = world["root"]
    chs = base(root)
    hindi(root, chs)
    lock(root, chs)
    scenes(root, chs[:1])                          # chapter 21: 4 scenes
    plan(root, 21, 4)
    images(root, 21, 2, cut=(2,))                  # s01 good, s02 cut off, s03 and s04 absent
    journal(root, ("c0021-s03", "sending"), ("c0021-s04", "refused"),
            ("c0021-s02", "sending"), ("c0021-s02", "saved"))
    res = collect(world, chapters=[21])
    r = res["chapters"][0]
    assert (r["images_ok"], r["images_expected"]) == (1, 4)
    assert r["images_invalid"] == ["c0021-s02"]
    assert r["needs_check"] == ["c0021-s03"] and r["refused"] == ["c0021-s04"]
    got = lines(res)
    assert "chapter 21: 1 of 4 images (1 invalid file: s02) -> novel images --chapters 21" in got
    assert any(x.startswith("chapter 21: 1 image need a manual look in Flow (c0021-s03) -> novel images --resend c0021-s03")
               for x in got)
    assert any(x.startswith("chapter 21: Flow refused 1 prompt (c0021-s04)") for x in got)
    text = ST.format_text(res)
    assert "chk1" in text and "ref1" in text and "1/4" in text


def test_image_prompts_not_written(world):
    root = world["root"]
    chs = base(root)
    hindi(root, chs)
    lock(root, chs)
    scenes(root, chs[:1])
    got = lines(collect(world, chapters=[21]))
    assert "chapter 21: image prompts and Flow plan not written -> novel approve --chapters 21" in got
    assert "chapter 21: 0 of 4 images -> novel images --chapters 21" in got


def test_narration_running(world):
    root = world["root"]
    chs = base(root)
    hindi(root, chs)
    lock(root, chs)
    put(root, "tts-state.json", {"chapters": chs, "pushed": "20261110T174906", "pulled": False})
    res = collect(world)
    assert [r["audio"] for r in res["chapters"]] == ["RUNNING"] * 3
    run = [m for m in res["missing"] if m["stage"] == "audio"]
    assert len(run) == 1 and run[0]["kind"] == "waiting" and run[0]["command"] == "novel jobs"
    assert run[0]["text"].startswith("chapters 21-23: narration RUNNING on Kaggle since ")
    assert not any("no narration" in x for x in lines(res))


def test_ready_to_build(world):
    ready(world)
    res = collect(world)
    assert [r["audio"] for r in res["chapters"]] == ["ok"] * 3
    assert [r["images_ok"] for r in res["chapters"]] == [4] * 3
    assert res["verdict"] == "READY TO BUILD" and res["verdict_text"] == "READY TO BUILD" and res["exit_code"] == 0
    assert lines(res)[0] == "chapters 21-23: video not rendered -> novel build"


def test_audio_that_does_not_match_its_timing(world):
    ready(world)
    SECONDS["00022.mp3"] = 75.0
    res = collect(world)
    assert res["chapters"][1]["audio"] == "BAD"
    assert "chapter 22: narration is not usable (mp3 is 75.0s, timing says 60.0s) -> novel tts push --chapters 22" in lines(res)
    assert res["exit_code"] == 1


def test_chapter_video_stale_after_a_setting_change(world):
    root = world["root"]
    for n in ready(world):
        put(root, f"video/c{n:04d}.mp4", b"v")
    d = deps(why_render=lambda r, n: "logo settings changed" if n == 22 else None)
    res = collect(world, d=d)
    assert [r["video"] for r in res["chapters"]] == ["ok", "stale: logo settings changed", "ok"]
    assert res["verdict"] == "READY TO BUILD"
    assert "chapter 22: video is out of date (logo settings changed) -> novel build" in lines(res)


def joined(world):
    root = world["root"]
    chs = ready(world)
    for n in chs:
        p = put(root, f"video/c{n:04d}.mp4", b"v")
        os.utime(p, (1000, 1000))
    put(root, "E001.mp4", b"final")
    put(root, "E001.timestamps.txt", "0:00 a")
    return chs


def test_joined_and_ready_to_publish(world):
    root = world["root"]
    joined(world)
    put(root, "publish/publish.json", {"valid": True, "title": "t"})
    put(root, "publish/thumb-a.jpg", b"x")
    res = collect(world)
    assert res["info"]["joined"]["current"] is True and res["info"]["timestamps"] == ["E001.timestamps.txt"]
    assert res["verdict"] == "READY TO PUBLISH" and res["exit_code"] == 0 and res["missing"] == []


def test_joined_but_no_publish_text_or_thumbnails(world):
    joined(world)
    res = collect(world)
    assert lines(res) == ["publish text missing -> novel meta", "thumbnails missing -> novel thumb"]
    assert res["verdict_text"] == "NOT READY (2 blockers)"
    assert [m["kind"] for m in res["missing"]] == ["later", "later"]


def test_invalid_publish_text(world):
    root = world["root"]
    joined(world)
    put(root, "publish/publish.json", {"valid": False, "errors": ["title too long", "x"]})
    put(root, "publish/thumb-a.jpg", b"x")
    assert lines(collect(world)) == ["publish text invalid (2 errors) -> novel meta"]


def test_joined_video_older_than_a_chapter_video(world):
    root = world["root"]
    joined(world)
    os.utime(root / "E001.mp4", (500, 500))
    res = collect(world)
    assert res["info"]["joined"]["current"] is False
    assert any(x.startswith("episode video is out of date: ") and x.endswith("-> novel build") for x in lines(res))


def test_published_episode_is_never_not_ready(world):
    root = world["root"]
    base(root)
    put(root, "episode.yaml", "published: true\npublished_url: https://youtu.be/abcdefghijk\n")
    res = collect(world)
    assert res["verdict"] == "PUBLISHED" and res["exit_code"] == 0 and res["missing"] == []
    assert res["info"]["published_url"] == "https://youtu.be/abcdefghijk"
    assert "https://youtu.be/abcdefghijk" in ST.format_text(res)


def test_chapters_subset(world):
    root = world["root"]
    ready(world)
    (root / "audio" / "00023.mp3").unlink()
    assert collect(world)["exit_code"] == 1
    sub = collect(world, chapters=[21, 22])
    assert [r["chapter"] for r in sub["chapters"]] == [21, 22]
    assert sub["verdict_text"] == "READY TO BUILD for chapters 21-22" and sub["exit_code"] == 0
    assert "info" not in sub
    assert collect(world, chapters=[23])["verdict_text"] == "NOT READY (1 blocker) for chapters 23"


def test_lock_that_no_longer_matches(world):
    root = world["root"]
    ready(world)
    put(root, "hindi/00022.txt", HINDI + "\n\nan edit after the lock")
    res = collect(world)
    assert res["chapters"][1]["lock"] == "CHANGED"
    assert "chapter 22: Hindi changed after it was locked -> novel approve --chapters 22   " \
           "(or restore the file; audio, scenes and captions made from the old text go stale)" in lines(res)
    assert "CHANGED" in ST.format_text(res)


def test_range_grouping_in_missing_list(world):
    root = world["root"]
    chs = base(root, 7)
    hindi(root, chs)
    lock(root, chs)
    audio(root, chs)
    got = lines(collect(world))
    # nothing has scenes: one run of seven chapters
    assert f"chapters 21-27: no scenes yet -> novel prompt --chapters 21-27 --copy   {PROMPT_TAIL}" in got
    # scenes for 24 only: novel prompt takes one range, so the two runs are two commands
    scenes(root, [24])
    got = lines(collect(world))
    assert f"chapters 21-23: no scenes yet -> novel prompt --chapters 21-23 --copy   {PROMPT_TAIL}" in got
    assert f"chapters 25-27: no scenes yet -> novel prompt --chapters 25-27 --copy   {PROMPT_TAIL}" in got
    # a command that takes lists gets one line, with a comma list
    for n in (24, 26, 27):
        (root / "audio" / f"{n:05d}.mp3").unlink()
        (root / "timing" / f"{n:05d}.json").unlink()
    got = lines(collect(world))
    assert "chapters 24, 26-27: no narration -> novel run   (or: novel tts push --chapters 24,26-27)" in got
    assert ST.ranges([21, 22, 23, 25]) == "21-23, 25" and ST.run_arg([21, 22, 23, 25]) == "21-23,25"


def test_migrated_style_episode_reads_complete(world):
    """v1 scenes, old-format captions, old QA files, no `version` anywhere."""
    root = world["root"]
    chs = ready(world)
    for n in chs:
        put(root, f"qa/{n:05d}.json", {"chapter": n, "source": "draft", "words": 5})
        put(root, f"qa/{n:05d}.attach.json", {"x": 1})
    res = collect(world)
    assert [r["qa"] for r in res["chapters"]] == ["old"] * 3
    assert [r["chars_needed"] for r in res["chapters"]] == [None] * 3
    assert [r["capt"] for r in res["chapters"]] == ["ok"] * 3
    assert res["verdict"] == "READY TO BUILD"
    text = ST.format_text(res)
    assert "qa old = checked by an older version" in text and "chars = Flow Characters made/needed" in text
    assert "chk" not in text and "ref" not in text.split("qa old")[0]      # nothing to explain for pictures


def test_legend_only_explains_values_that_occur(world):
    root = world["root"]
    chs = ready(world)
    for n in chs:
        put(root, f"qa/{n:05d}.json", {"version": 2, "chapter": n, "errors": [], "warnings": []})
    text = ST.format_text(collect(world))
    legend = [l for l in text.splitlines() if l.startswith("  chars = ")]
    assert legend == ["  chars = Flow Characters made/needed"]
    assert "qa old" not in text and "chkN" not in text and "refN" not in text


def test_qa_states(world):
    root = world["root"]
    ready(world)
    put(root, "qa/00021.json", {"version": 2, "chapter": 21, "errors": ["a", "b"], "warnings": [], "forced": False})
    put(root, "qa/00022.json", {"version": 2, "chapter": 22, "errors": [], "warnings": []})
    put(root, "qa/00022.review.json", {"verdict": "FIX", "reviewer": "claude",
                                       "findings": [{"severity": "error", "kind": "k", "ref": "r", "text": "t"}]})
    put(root, "qa/00023.json", {"version": 2, "chapter": 23, "errors": ["a"], "warnings": [], "forced": True})
    res = collect(world)
    assert [r["qa"] for r in res["chapters"]] == ["FIX", "FIX", "forced"]
    got = lines(res)
    assert "chapter 21: QA says FIX (2 errors) -> novel qa --fix-prompt 21" in got
    assert "chapter 22: QA says FIX (1 error) -> novel qa --fix-prompt 22" in got
    put(root, "qa/00021.json", {"version": 2, "chapter": 21, "errors": [], "warnings": []})
    assert ST.qa_state(root, 21) == ("ok", 0)
    assert ST.qa_state(root, 99) == ("-", 0)


def test_bad_scene_file_and_captions_without_key(world):
    root = world["root"]
    ready(world)
    doc = json.loads((root / "scenes" / "00021.json").read_text(encoding="utf-8"))
    doc["scenes"][0]["shot"] = "nonsense"
    put(root, "scenes/00021.json", doc)
    (root / "captions" / "00023.json").unlink()
    res = collect(world, d=deps(gemini_keys=lambda: []))
    assert res["chapters"][0]["scenes_state"] == "BAD"
    got = lines(res)
    assert any(x.startswith("chapter 21: scenes are not usable (scene 1: shot 'nonsense'") for x in got)
    assert "chapter 23: captions missing and no Gemini key to make them -> novel captions --chapters 23   " \
           "(needs GEMINI_API_KEY in secrets.env)" in got
    with_key = collect(world)
    assert not any("captions" in x for x in lines(with_key))
    assert any("render makes them itself" in n for n in with_key["notes"])


def test_background_jobs_gemini_progress_and_chrome(world, tmp_path):
    root = world["root"]
    ready(world)
    put(root, "jobs/tts.json", {"name": "tts", "pid": 1, "cmd": "x", "started": "2026-11-13T10:00:00+00:00",
                                "state": "running", "log": "jobs/tts.log"})
    put(root, "jobs/images.json", {"name": "images", "state": "crashed", "started": "2026-11-13T09:00:00+00:00",
                                   "finished": "2026-11-13T09:30:00+00:00", "log": "jobs/images.log"})
    put(root, "jobs/captions.json", {"name": "captions", "state": "done"})
    put(root, "jobs/old.json", {"name": "x", "state": "failed", "finished": "2026-01-01T00:00:00+00:00"})
    b = world["data"] / BOOK / "gemini" / "B021-030"
    put(b, "batch.json", {"batch": "B021-030", "mode": "scenes", "chapters": list(range(21, 31))})
    put(b, "progress.json", {"imported": [21, 22], "weird": {"shape": True}})
    other = world["data"] / BOOK / "gemini" / "B001-010"
    put(other, "batch.json", {"batch": "B001-010", "chapters": list(range(1, 11))})
    put(other, "progress.json", {"x": 1})
    lockdir = tmp_path / "locks"
    put(lockdir, "chrome-cdp-9222.lock", {"pid": 4, "book": "other", "episode": 2, "started": "2026-11-13T10:00:00+00:00"})
    d = deps(chrome_lock_files=lambda: sorted(lockdir.glob("*.lock")),
             now=lambda: ST.datetime(2026, 11, 13, 12, 0, tzinfo=ST.timezone.utc))
    res = collect(world, d=d)
    assert [(j["name"], j["state"]) for j in res["jobs"]] == [("images", "crashed"), ("tts", "running")]
    assert [x["batch"] for x in res["gemini"]] == ["B021-030"]
    assert res["chrome"][0].startswith("Chrome is in use by other E002 since ")
    assert res["verdict"] == "READY TO BUILD"                       # none of it is a blocker
    text = ST.format_text(res)
    assert "job tts: running" in text and "gemini batch B021-030" in text and "Chrome is in use by" in text


def test_text_fits_110_columns_and_is_ascii(world):
    root = world["root"]
    chs = base(root, 10)
    hindi(root, chs)
    lock(root, chs)
    text = ST.format_text(collect(world))
    assert max(len(x) for x in text.splitlines()) <= 110
    text.encode("ascii")


# ------------------------------------------------------------------ the command: --json, --all, exit codes

@pytest.fixture
def cmd(world, monkeypatch):
    monkeypatch.setattr(ST, "default_deps", lambda book: deps())
    return world


def test_json_matches_text(cmd, capsys):
    ready(cmd)
    assert status_cmd.main(["--book", BOOK, "--ep", "1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "READY TO BUILD" and data["exit_code"] == 0
    assert [r["chapter"] for r in data["chapters"]] == [21, 22, 23]
    assert data["missing"][0]["command"].startswith("novel build") and data["missing"][0]["chapters"] == [21, 22, 23]
    assert status_cmd.main(["--book", BOOK, "--ep", "1"]) == 0
    text = capsys.readouterr().out
    assert f"VERDICT: {data['verdict_text']}" in text
    assert data["missing"][0]["line"] in text


def test_command_exit_code_and_chapters_option(cmd, capsys):
    ready(cmd)
    (cmd["root"] / "audio" / "00023.mp3").unlink()
    assert status_cmd.main(["--book", BOOK, "--ep", "1"]) == 1
    assert "NOT READY" in capsys.readouterr().out
    assert status_cmd.main(["--book", BOOK, "--ep", "1", "--chapters", "21-22"]) == 0
    assert "READY TO BUILD for chapters 21-22" in capsys.readouterr().out
    assert status_cmd.main(["--book", BOOK, "--ep", "1", "--chapters", "99"]) == 2
    assert status_cmd.main(["--book", BOOK, "--ep", "7"]) == 2


def test_all_episodes_newest_first(cmd, capsys):
    ready(cmd)
    eps = cmd["data"] / BOOK / "episodes"
    e2 = eps / "E002"
    put(e2, "episode.json", {"episode": 2, "start": 31, "end": 40})
    put(e2, "manifest.json", {"version": 1, "chapters": []})
    e3 = eps / "E003"
    base(e3)
    put(e3, "episode.yaml", "published: true\n")
    (eps / "E900").mkdir()
    rows = ST.collect_all(BOOK, deps(), state=(BOOK, 1))
    assert [r["episode"] for r in rows] == [3, 2, 1]
    assert rows[0]["verdict"] == "PUBLISHED" and rows[0]["next"] == "-"
    assert rows[2]["verdict"] == "READY TO BUILD" and rows[2]["next"] == "novel build"
    assert status_cmd.main(["--book", BOOK, "--all"]) == 0
    out = capsys.readouterr().out
    assert out.index("E003") < out.index("E002") < out.index("E001")
    assert status_cmd.main(["--book", BOOK, "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["episodes"][2]["episode"] == 1


def test_commands_name_book_and_episode_when_not_current(world):
    ready(world)
    res = ST.collect(world["root"], BOOK, 1, deps(), state=("other", 5))
    assert res["missing"][0]["command"] == "novel build --book bk --ep 1"


def test_jpeg_rule_matches_run_mjs(tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(GOOD_JPEG)
    assert ST.jpeg_ok(f)
    for bad in (CUT_JPEG, TINY_JPEG, b"\x00\x00" + b"0" * 60000 + b"\xff\xd9"):
        f.write_bytes(bad)
        assert not ST.jpeg_ok(f)
    assert not ST.jpeg_ok(tmp_path / "missing.jpg")


def test_journal_follows_run_mjs_maybe_sent(tmp_path):
    journal(tmp_path, ("a", "sending"), ("b", "sending"), ("b", "saved"), ("c", "refused"), ("c", "resend-approved"),
            ("d", "wall"), ("e", "sent"), ("e", "error-before-send"), ("f", "error-after-send"))
    assert ST.journal_states(tmp_path) == {"a": "needs_check", "f": "needs_check"}
    journal(tmp_path, ("g", "refused"))
    assert ST.journal_states(tmp_path) == {"g": "refused"}


def test_v2_qa_with_old_hashes_is_stale_and_names_the_fix(world):
    root = world["root"]
    ready(world)
    h, s = sha(root / "hindi" / "00021.txt"), sha(root / "scenes" / "00021.json")
    put(root, "qa/00021.json", {"version": 2, "chapter": 21, "errors": [], "warnings": [], "forced": False,
                                "hindi_sha256": h, "scenes_sha256": s})
    put(root, "qa/00021.review.json", {"version": 2, "chapter": 21, "verdict": "FIX", "reviewer": "claude",
                                       "findings": [{"severity": "error", "kind": "k", "ref": "r", "text": "t"}],
                                       "hindi_sha256": h, "scenes_sha256": s})
    assert ST.qa_state(root, 21) == ("FIX", 1)                      # hashes match: it counts
    # the Hindi changes: neither file counts any more
    put(root, "hindi/00021.txt", (root / "hindi" / "00021.txt").read_text(encoding="utf-8") + "\nOne more line.\n")
    assert ST.qa_state(root, 21) == ("stale", 0)
    res = collect(world)
    assert res["chapters"][0]["qa"] == "stale"
    assert "chapter 21: QA is out of date (the Hindi or the scenes changed after it ran) -> novel qa --chapters 21" \
        in lines(res)
    assert not [l for l in lines(res) if "QA says FIX" in l]
    # a stale review alone (code file current) is stale too; a changed scene file does the same
    put(root, "hindi/00021.txt", HINDI)
    h = sha(root / "hindi" / "00021.txt")
    put(root, "qa/00021.json", {"version": 2, "chapter": 21, "errors": [], "warnings": [], "hindi_sha256": h,
                                "scenes_sha256": s})
    put(root, "scenes/00021.json", (root / "scenes" / "00021.json").read_text(encoding="utf-8") + "\n")
    assert ST.qa_state(root, 21) == ("stale", 0)


def test_gemini_progress_line_is_readable_not_raw_json():
    prog = {"batch": "B001-004", "bible": {"done": True, "blocks": {"B001-004": {"sha": "x" * 64}}},
            "chapters": {"1": {"status": "done", "images": "7 of 7"}, "2": {"status": "done", "images": "6 of 6"},
                         "3": {"status": "partial", "images": "3 of 5"}, "4": {"status": "waiting", "images": "0 of 6"}},
            "updated": "2026-11-14T04:21:29+00:00", "mode": "scenes"}
    assert ST.progress_text(prog) == "bible done; done 1-2; partial 3 (3 of 5); waiting 4"
    assert ST.progress_text({"bible": {"done": False}, "chapters": {"1": {"status": "waiting"}}}) == \
        "bible waiting; waiting 1"
    assert ST.progress_text({"imported": [21, 22], "weird": {"shape": True}}) == '{"imported":[21,22],"weird":{"shape":true}}'
    assert ST.progress_text(None) == "unreadable"


def test_ready_to_publish_prints_the_next_command(world):
    import time
    root = world["root"]
    chs = ready(world)
    for n in chs:
        put(root, f"video/c{n:04d}.mp4", b"v")
    time.sleep(0.02)
    put(root, "E001.mp4", b"final")
    put(root, "publish/publish.json", {"valid": True})
    put(root, "publish/thumb-a.jpg", b"jpg")
    res = collect(world)
    assert res["verdict"] == "READY TO PUBLISH" and res["missing"] == []
    text = ST.format_text(res)
    assert text.rstrip().endswith("VERDICT: READY TO PUBLISH\n\nNext: upload it, then: novel published --url <URL>")
