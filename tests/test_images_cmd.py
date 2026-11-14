"""novel images: invented book "tb", temp NOVEL_HOME and NOVEL_LOCK_DIR, the fake generator (FLOW_GEN), the
"port answers" check injected. No real browser, no network, no Flow."""

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from novel import flowrun as F
from novel.commands import images_cmd
from novel.core import chromelock

ROOT = Path(__file__).resolve().parent.parent
FAKE_GEN = ROOT / "tests" / "fake_gen.mjs"
FAKE_GEN_FRONT = ROOT / "tests" / "fakes" / "fake_gen_front.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
UP = lambda url: "Chrome/130"          # the port answers


def jpeg_bytes(n=60004):
    return b"\xff\xd8" + b"\0" * (n - 4) + b"\xff\xd9"


def build(tmp_path, monkeypatch, chapters=5, per=3, images=None, chars_in=None, gen=FAKE_GEN):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    (home / "settings.yaml").write_text(yaml.safe_dump({
        "data_dir": str(data),
        "chrome": {"cdp_url": "http://127.0.0.1:9444", "exe": r"C:\fake\chrome.exe", "profile": r"D:\fake profile"}}),
        encoding="utf-8")
    book = data / "tb"
    ep = book / "episodes" / "E001"
    flow = ep / "flow"
    flow.mkdir(parents=True)
    (ep / "episode.json").write_text(json.dumps({"episode": 1, "start": 1, "end": chapters}), encoding="utf-8")
    img = {"flow_project": "https://flow.google.com/project/abc", "flow_model": "Nano Banana Pro", "pause": 0,
           "chapter_pause": 0, "rest_every": 7, "rest": 9, "batch": 1, "max_per_run": 200,
           "chapters_per_day": 3, "wall_cooldown_hours": 12}
    img.update(images or {})
    (book / "book.yaml").write_text(yaml.safe_dump({"images": img}), encoding="utf-8")
    shots = []
    for c in range(1, chapters + 1):
        for s in range(1, per + 1):
            sid = f"c{c:04d}-s{s:02d}"
            d = ep / "images" / f"c{c:04d}"
            shots.append({"id": sid, "chars": (chars_in or {}).get(sid, []), "prompt": (flow / f"{sid}.txt").as_posix(),
                          "out": (d / f"s{s:02d}-2k.jpg").as_posix(), "meta": (d / f"s{s:02d}.flow.json").as_posix(),
                          "used": (d / f"s{s:02d}.used.txt").as_posix()})
            d.mkdir(parents=True, exist_ok=True)
    plan = flow / "plan.json"
    plan.write_text(json.dumps({"project": "https://flow.google.com/project/abc", "image_model": "Nano Banana Pro",
                                "header_mark": "Wide", "journal": (flow / "journal.jsonl").as_posix(),
                                "stop_file": (flow / "STOP").as_posix(), "shots": shots}), encoding="utf-8")
    bible = book / "bible"
    bible.mkdir()
    (bible / "characters.json").write_text(json.dumps({"version": 2, "style": "ink", "characters": {
        "mira": {"display": "Mira", "role": "hero", "look": "a tall woman", "variants": {
            "base": {"flow_name": "Mira", "chapters": "1-", "look": None, "flow_made": True, "ref_version": 2},
            "cloak": {"flow_name": "Mira - Cloak", "chapters": "3-", "look": "in a cloak", "flow_made": False,
                      "ref_version": 2}}},
        "kael": {"display": "Kael", "role": "rival", "look": "a lean man", "variants": {
            "base": {"flow_name": "Kael", "chapters": "1-", "look": None, "flow_made": False, "ref_version": 2}}}},
        "content_flags": {}}), encoding="utf-8")
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.setenv("NOVEL_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("FLOW_GEN", str(gen))
    monkeypatch.delenv("FAKE_SCRIPT", raising=False)
    monkeypatch.setenv("FAKE_SCRIPT", "{}")
    return SimpleNamespace(tmp=tmp_path, ep=ep, flow=flow, plan=plan, bible=bible,
                           journal=flow / "journal.jsonl", calls=flow / "calls.log")


@pytest.fixture
def env(tmp_path, monkeypatch):
    return build(tmp_path, monkeypatch)


def run(capsys, *argv, replies=(), interactive=False, answers=UP, inject=None):
    asked = []
    queue = list(replies)

    def ask(prompt):
        asked.append(prompt)
        if not queue:
            raise AssertionError(f"unexpected input() call: {prompt!r}")
        return queue.pop(0)
    inj = {"answers": answers}
    inj.update(inject or {})
    code = images_cmd.main(["--book", "tb", "--ep", "1", *argv], inject=inj, ask=ask, interactive=interactive)
    cap = capsys.readouterr()
    return SimpleNamespace(code=code, out=cap.out, err=cap.err, asked=asked)


def calls(env):
    return env.calls.read_text().split("\n")[:-1] if env.calls.exists() else []


def done_ids(env):
    return sorted(r["id"] for r in F.classify(F.load_plan(env.plan), env.ep) if r["kind"] == "done")


def put_done(env, *ids):
    for r in F.classify(F.load_plan(env.plan), env.ep):
        if r["id"] in ids:
            r["out"].write_bytes(jpeg_bytes())


def wall_event(env, at, error="Flow says: captcha", sid="c0001-s01"):
    with env.journal.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": sid, "state": "wall", "at": at.isoformat(), "error": error}) + "\n")


def now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- choosing chapters

def test_default_is_first_three_chapters_with_missing_pictures(env, capsys):
    put_done(env, *[f"c0001-s{s:02d}" for s in (1, 2, 3)], "c0002-s01")        # chapter 1 done, 2 partly
    r = run(capsys, "--dry-run")
    assert r.code == 0, r.err
    assert "Chapters: 2, 3, 4 " in r.out
    assert "--only c0002,c0003,c0004" in r.out
    r = run(capsys, "--chapters", "5", "--dry-run")
    assert "Chapters: 5 " in r.out and "--only c0005" in r.out


def test_chapters_not_in_plan_refused(env, capsys):
    r = run(capsys, "--chapters", "9")
    assert r.code == 1 and "not in the plan" in r.err and "novel approve --chapters 9" in r.err


def test_everything_done(env, capsys):
    put_done(env, *[r["id"] for r in F.classify(F.load_plan(env.plan), env.ep)])
    r = run(capsys)
    assert r.code == 0 and "all 15 pictures are done" in r.out
    assert not env.calls.exists()


def test_no_plan_means_nothing_approved(env, capsys):
    env.plan.unlink()
    r = run(capsys)
    assert r.code == 1 and "Nothing approved yet" in r.err and "novel approve --chapters 1-5" in r.err


# ---------------------------------------------------------------- the runner's options

def test_max_default_from_settings_and_override(tmp_path, monkeypatch, capsys):
    env = build(tmp_path, monkeypatch, images={"max_per_run": 4})
    r = run(capsys)
    assert r.code == 0, r.out + r.err
    assert len(calls(env)) == 4 and "saved this run: 4" in r.out
    r = run(capsys, "--max", "2")
    assert len(calls(env)) == 6 and "saved this run: 2" in r.out


def test_options_passed_match_settings(tmp_path, monkeypatch, capsys):
    env = build(tmp_path, monkeypatch, images={"batch_pause": 30})
    r = run(capsys, "--dry-run", "--batch", "2")
    assert r.code == 0
    line = next(l for l in r.out.splitlines() if "run.mjs" in l)
    for want in ("--pause 0", "--chapter-pause 0", "--rest-every 7", "--rest 9", "--batch 2", "--batch-pause 30",
                 "--max 200", "--only c0001,c0002,c0003"):
        assert want in line, (want, line)
    assert "FLOW_CDP=http://127.0.0.1:9444" in r.out and "FLOW_MODEL=Nano Banana Pro" in r.out
    eff = {"images": {"pause": 1}}
    argv = images_cmd.runner_argv(Path("p.json"), "c0001", 5, eff, resend="c0001-s01", upgrade=True)
    assert argv[3:] == ["--only", "c0001", "--max", "5", "--pause", "1", "--resend", "c0001-s01", "--upgrade"]
    assert "--batch-pause" not in images_cmd.runner_argv(Path("p.json"), None, 5, {"images": {}})


def test_real_run_saves_pictures_and_counts(env, capsys, monkeypatch):
    monkeypatch.setenv("FLOW_GEN", str(FAKE_GEN_FRONT))
    r = run(capsys, "--chapters", "1-2")
    assert r.code == 0, r.out + r.err
    assert len(done_ids(env)) == 6 and len(calls(env)) == 6
    assert "saved this run: 6" in r.out and "already done before: 0" in r.out
    assert "left in these chapters: 0" in r.out and "left in the episode: 9" in r.out
    assert "Next: novel images again tomorrow" in r.out
    assert "[1/6] c0001-s01" in r.out and "[6/6] c0002-s03" in r.out      # progress headers
    assert "Flow project: https://flow.google.com/project/abc" in r.out and "Keep that Chrome window visible" in r.out


def test_needs_check_and_refused_summary(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_SCRIPT", json.dumps({"c0001-s02": ["after"], "c0001-s03": ["refused"]}))
    r = run(capsys, "--chapters", "1")
    assert r.code == 0, r.out + r.err
    assert "NEEDS CHECK" in r.out and "c0001-s02" in r.out
    assert "adopt.mjs" in r.out and "novel images --resend c0001-s02" in r.out
    assert "REFUSED" in r.out and "novel qa --fix-prompt 1" in r.out
    r = run(capsys, "--chapters", "1")                              # nothing resent
    assert r.code == 0 and "nothing to send in chapters" in r.out
    assert calls(env) == ["c0001-s01", "c0001-s02", "c0001-s03"]


def test_resend_only_that_picture(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_SCRIPT", json.dumps({"c0001-s02": ["after", "ok"]}))
    run(capsys, "--chapters", "1")
    env.calls.unlink()
    r = run(capsys, "--resend", "c0001-s02")
    assert r.code == 0 and calls(env) == ["c0001-s02"]
    assert "c0001-s02" in done_ids(env)[1]


# ---------------------------------------------------------------- the stop gate

def test_wall_inside_cooldown_refuses_and_outside_does_not(env, capsys):
    wall_event(env, now() - timedelta(hours=1))
    r = run(capsys, "--chapters", "1")
    assert r.code == 1 and "Flow stopped at" in r.err and "captcha" in r.err and "--i-checked-flow" in r.err
    assert not env.calls.exists()
    env.journal.write_text("")
    wall_event(env, now() - timedelta(hours=13))
    r = run(capsys, "--chapters", "1")
    assert r.code == 0, r.err


def test_acknowledgement_covers_older_stops_only(env, capsys):
    wall_event(env, now() - timedelta(hours=1))
    r = run(capsys, "--chapters", "1", "--i-checked-flow")
    assert r.code == 0, r.out + r.err
    acks = [json.loads(l) for l in (env.flow / "acks.jsonl").read_text().splitlines()]
    assert len(acks) == 1 and acks[0]["acknowledged"][0]["reason"] == "Flow says: captcha"
    assert F.parse_ts(acks[0]["at"]) is not None
    r = run(capsys, "--chapters", "2")                                # the old stop stays covered
    assert r.code == 0, r.err
    wall_event(env, now() + timedelta(minutes=1), error="Flow says: unusual activity")   # a NEW wall
    r = run(capsys, "--chapters", "3")
    assert r.code == 1 and "unusual activity" in r.err


def test_status_stopped_text_counts_as_a_wall(env, capsys):
    (env.flow / "status.json").write_text(json.dumps({
        "stopped": "c0001-s01: Flow put up a limit, CAPTCHA, sign-in or aspect problem - see above",
        "at": (now() - timedelta(minutes=5)).isoformat()}))
    r = run(capsys)
    assert r.code == 1 and "Flow stopped at" in r.err


def test_user_stop_is_not_a_wall_and_later_run_finishes_without_resending(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_STOP_AFTER", "c0001-s01")
    r = run(capsys, "--chapters", "1")
    assert r.code == 6, r.out + r.err
    assert "Stopped on request" in r.out and calls(env) == ["c0001-s01"]
    assert not (env.flow / "STOP").exists()
    monkeypatch.delenv("FAKE_STOP_AFTER")
    r = run(capsys, "--chapters", "1")                                # the gate does not block a user stop
    assert r.code == 0, r.err
    assert calls(env) == ["c0001-s01", "c0001-s02", "c0001-s03"]


def test_noninteractive_wall_ends_with_5_and_next_run_is_refused(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_SCRIPT", json.dumps({"c0001-s02": ["wall"]}))
    r = run(capsys, "--chapters", "1")
    assert r.code == 5, r.out + r.err
    assert r.asked == [] and "Not interactive" in r.out and "--i-checked-flow" in r.out
    r = run(capsys, "--chapters", "1")
    assert r.code == 1 and "Flow stopped at" in r.err


# ---------------------------------------------------------------- human-assisted stops

def test_interactive_enter_records_ack_and_continues(env, capsys, monkeypatch):
    monkeypatch.setenv("FLOW_GEN", str(FAKE_GEN_FRONT))
    monkeypatch.setenv("FAKE_SCRIPT", json.dumps({"c0001-s02": ["wall", "ok"]}))
    r = run(capsys, "--chapters", "1", replies=[""], interactive=True)
    assert r.code == 0, r.out + r.err
    assert len(r.asked) == 1 and "FLOW STOPPED" in r.out and "\a" in r.out
    assert "Solve it in the Chrome window." in r.out
    assert "When Flow works again, press Enter to continue. Type s then Enter to stop here" in r.asked[0]
    assert len(done_ids(env)) == 3
    acks = (env.flow / "acks.jsonl").read_text().splitlines()
    assert len(acks) == 1
    front = (env.flow / "front.log").read_text().splitlines()
    assert [l for l in front if l.endswith("front=1")] == ["c0001-s01 front=1", "c0001-s02 front=1"]   # start + re-check only


def test_interactive_s_stops_with_the_stop_code(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_SCRIPT", json.dumps({"c0001-s02": ["wall", "ok"]}))
    r = run(capsys, "--chapters", "1", replies=["s"], interactive=True)
    assert r.code == 5 and len(r.asked) == 1
    assert "Stopping here at your request" in r.out
    assert not (env.flow / "acks.jsonl").exists()
    assert len(done_ids(env)) == 1


def test_one_keypress_per_stop_never_automatic(env, capsys, monkeypatch):
    monkeypatch.setenv("FLOW_GEN", str(FAKE_GEN_FRONT))
    monkeypatch.setenv("FAKE_WALL_TEXT", "Flow says: captcha")        # journal events with a timestamp, as gen.mjs writes
    monkeypatch.setenv("FAKE_SCRIPT", json.dumps({"c0001-s02": ["wall", "wall", "ok"]}))
    r = run(capsys, "--chapters", "1", replies=["", ""], interactive=True)
    assert r.code == 0, r.out + r.err
    assert len(r.asked) == 2                                          # two stops, two input() calls
    assert len((env.flow / "acks.jsonl").read_text().splitlines()) == 2
    # with one reply for two stops the loop would call ask() a second time and the test's ask() would fail:
    # nothing ever continues without a new input() call.
    monkeypatch.setenv("FAKE_SCRIPT", json.dumps({"c0002-s01": ["wall", "wall", "ok"]}))
    with pytest.raises(AssertionError, match="unexpected input"):
        run(capsys, "--chapters", "2", replies=[""], interactive=True)


def test_limit_stop_warns_plainly(env, capsys, monkeypatch):
    monkeypatch.setenv("FLOW_GEN", str(FAKE_GEN_FRONT))
    monkeypatch.setenv("FAKE_WALL_TEXT", "Flow says: daily limit")
    monkeypatch.setenv("FAKE_SCRIPT", json.dumps({"c0001-s02": ["wall"]}))
    r = run(capsys, "--chapters", "1", replies=["s"], interactive=True)
    assert r.code == 5 and "usage limit" in r.out and "wait until the limit resets" in r.out
    assert "Press Enter to try anyway" in r.asked[0]


def test_classification_of_stop_reasons():
    for text in ("Flow says: captcha", "Flow says: unusual traffic", "Flow says: unusual activity", "Flow says: not a robot",
                 "Flow says: sign in to continue", "Flow is asking you to sign in",
                 "the tab did not open project abc", "Flow's prompt bar is set to \"Video\" - switch it",
                 "image is 1024x1024, not 16:9 - set Flow's aspect ratio to 16:9", "Flow shows a limit or CAPTCHA - stopping"):
        assert F.classify_stop(text)[0] == "clearable", text
    for text in ("Flow says: out of credits", "Flow says: not enough credits", "Flow says: daily limit",
                 "Flow says: reached your limit", "Flow says: limit reached", "Flow says: quota",
                 "Flow says: too many requests", "Flow says: try again later"):
        assert F.classify_stop(text)[0] == "limit", text


def test_chrome_not_running_offers_to_start_it(env, capsys):
    state = {"up": False, "launched": []}

    def answers(url):
        return "Chrome/130" if state["up"] else None

    def launch(argv):
        state["launched"].append(argv)
        state["up"] = True
    r = run(capsys, "--chapters", "1", replies=[""], interactive=True, answers=answers,
            inject={"do_launch": launch, "sleep": lambda s: None})
    assert r.code == 0, r.out + r.err
    assert r.asked == ["Chrome is not running. Start it now with your signed-in profile? [Y/n] "]
    assert len(state["launched"]) == 1 and "--remote-debugging-port=9444" in state["launched"][0][1]
    assert r"D:\fake profile" in state["launched"][0][2]


def test_chrome_not_running_declined_or_not_interactive(env, capsys):
    launched = []
    r = run(capsys, "--chapters", "1", replies=["n"], interactive=True, answers=lambda u: None,
            inject={"do_launch": launched.append})
    assert r.code == 1 and "novel chrome" in r.err and launched == [] and len(r.asked) == 1
    r = run(capsys, "--chapters", "1", answers=lambda u: None)
    assert r.code == 1 and "novel chrome" in r.err and r.asked == []
    assert not env.calls.exists()


# ---------------------------------------------------------------- references, lock, STOP, dry run

def test_missing_reference_refused_with_keys(tmp_path, monkeypatch, capsys):
    env = build(tmp_path, monkeypatch, chars_in={"c0004-s01": ["Mira", "Kael"], "c0004-s02": ["Mira - Cloak"]})
    r = run(capsys, "--chapters", "4")
    assert r.code == 1
    assert "novel characters make --keys kael,mira:cloak --ep 1" in r.err
    assert not env.calls.exists()
    r = run(capsys, "--chapters", "1")                                # Mira alone is made
    assert r.code == 0


def test_renamed_reference_asks_for_new_plan(tmp_path, monkeypatch, capsys):
    env = build(tmp_path, monkeypatch, chars_in={"c0004-s01": ["Kael"]})
    doc = json.loads((env.bible / "characters.json").read_text())
    v = doc["characters"]["kael"]["variants"]["base"]
    v.update(flow_name="Kael v2", flow_made=True, legacy={"flow_name": "Kael", "ref_version": 1})
    (env.bible / "characters.json").write_text(json.dumps(doc))
    r = run(capsys, "--chapters", "4")
    assert r.code == 1 and "renamed" in r.err and "Kael (now Kael v2)" in r.err and "novel approve" in r.err


def test_lock_is_held_during_the_run_and_released_after(env, capsys, monkeypatch):
    seen = []
    real = F.spawn

    def spy(*a, **k):
        seen.append(chromelock.holder())
        return real(*a, **k)
    monkeypatch.setattr(F, "spawn", spy)
    r = run(capsys, "--chapters", "1")
    assert r.code == 0
    assert seen and seen[0]["alive"] and seen[0]["cmd"].startswith("novel images")
    assert chromelock.holder() is None


def test_held_lock_refuses(env, capsys):
    with chromelock.acquire(cmd="novel characters make", book="tb", episode=1):
        r = run(capsys, "--chapters", "1")
    assert r.code == 1 and "in use by" in r.err and "novel characters make" in r.err
    assert not env.calls.exists()


def test_stale_stop_file_removed_before_run(env, capsys):
    (env.flow / "STOP").write_text("")
    r = run(capsys, "--chapters", "1")
    assert r.code == 0 and "Removed a stale stop file" in r.out
    assert len(done_ids(env)) == 3


def test_dry_run_runs_nothing_and_takes_no_lock(env, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the dry run must not start anything")
    monkeypatch.setattr(F, "spawn", boom)
    monkeypatch.setattr(chromelock, "acquire", boom)
    wall_event(env, now() - timedelta(hours=1))
    r = run(capsys, "--dry-run", "--i-checked-flow")
    assert r.code == 0 and "nothing was run, no lock was taken" in r.out and "would record" in r.out
    assert not (env.flow / "acks.jsonl").exists() and not env.calls.exists()
    assert not chromelock.lock_path().exists()


def test_works_from_another_working_directory(env, capsys, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    r = run(capsys, "--chapters", "1")
    assert r.code == 0, r.out + r.err
    assert len(done_ids(env)) == 3 and not list(elsewhere.iterdir())


# ---------------------------------------------------------------- the Python image rule equals the runner's

def test_image_validity_rule_agrees_with_run_mjs(tmp_path):
    src = (ROOT / "flow" / "run.mjs").read_text(encoding="utf-8")
    fn = re.search(r"function jpegOk\(file\) \{.*?\n\}", src, re.S).group(0)
    samples = {
        "ok.jpg": jpeg_bytes(),
        "exact50000.jpg": jpeg_bytes(50000),
        "just_over.jpg": jpeg_bytes(50001),
        "small.jpg": jpeg_bytes(1000),
        "no_start.jpg": b"\x00\x00" + b"\0" * 60000 + b"\xff\xd9",
        "no_end.jpg": b"\xff\xd8" + b"\0" * 60000,
        "png.png": b"\x89PNG" + b"\0" * 60000,
        "empty.jpg": b"",
    }
    for name, data in samples.items():
        (tmp_path / name).write_bytes(data)
    script = tmp_path / "rule.mjs"
    script.write_text("import fs from 'node:fs';\n" + fn + "\n"
                      "const o = {}; for (const f of process.argv.slice(2)) o[f.split(/[\\\\/]/).pop()] = jpegOk(f);\n"
                      "console.log(JSON.stringify(o));\n", encoding="utf-8")
    out = subprocess.run(["node", str(script), *[str(tmp_path / n) for n in samples], str(tmp_path / "missing.jpg")],
                         capture_output=True, text=True, check=True).stdout
    node_says = json.loads(out)
    for name in [*samples, "missing.jpg"]:
        assert F.jpeg_ok(tmp_path / name) == node_says[name], name
    assert node_says["ok.jpg"] is True and node_says["exact50000.jpg"] is False and node_says["missing.jpg"] is False


# ---------------------------------------------------------------- Ctrl+C, progress

class FakeProc:
    def __init__(self, events):
        self.events, self.pid, self.killed = events, 1, False
        self.stdout = self

    def __iter__(self):
        return self

    def __next__(self):
        if not self.events:
            raise StopIteration
        e = self.events.pop(0)
        if e == "INT":
            raise KeyboardInterrupt
        return e

    def wait(self):
        return 6


def test_first_ctrl_c_makes_the_stop_file_second_ends_the_child(tmp_path, monkeypatch):
    said, out = [], []
    stop = tmp_path / "flow" / "STOP"
    p = FakeProc(["a\n", "INT", "b\n"])
    assert F.pump(p, stop, say=said.append, out=SimpleNamespace(write=out.append, flush=lambda: None, encoding="utf-8")) == 6
    assert stop.exists() and out == ["a\n", "b\n"] and "asked the runner to stop" in said[0]
    monkeypatch.setattr(F, "kill_tree", lambda proc: setattr(proc, "killed", True))
    p = FakeProc(["INT", "INT"])
    with pytest.raises(F.Interrupted):
        F.pump(p, tmp_path / "STOP2", say=said.append, out=SimpleNamespace(write=out.append, flush=lambda: None, encoding="utf-8"))
    assert p.killed and "unknown state" in said[-1]


def test_progress_headers_count_each_picture_once():
    pr = F.Progress(2, 9)
    assert pr("12:00:00 c0001-s03 sent with Mira") == "[3/9] c0001-s03 ..."
    assert pr("12:00:01 c0001-s03 saved x") is None
    assert pr("12:00:02 c0001-s04 sent") == "[4/9] c0001-s04 ..."
    assert pr("saved 1, already done 2, left 3") is None


def test_front_is_set_for_the_first_runner_call_only(env, capsys, monkeypatch):
    monkeypatch.setenv("FLOW_GEN", str(FAKE_GEN_FRONT))
    run(capsys, "--chapters", "1")
    assert (env.flow / "front.log").read_text().splitlines() == [
        "c0001-s01 front=1", "c0001-s02 front=-", "c0001-s03 front=-"]


def test_defaults_yaml_gives_batch_pause_45_to_the_runner():
    from novel.core import settings
    eff = settings._read(settings.defaults_file())
    assert eff["images"]["batch_pause"] == 45
    argv = images_cmd.runner_argv(Path("p.json"), None, 5, eff)
    assert argv[argv.index("--batch-pause") + 1] == "45"


# ---------------------------------------------------------------- the alert when a run pauses for the person

def test_alert_rings_when_the_run_pauses(env, capsys, monkeypatch):
    rang = []
    monkeypatch.setenv("FAKE_SCRIPT", json.dumps({"c0001-s02": ["wall", "ok"]}))
    r = run(capsys, "--chapters", "1", replies=["s"], interactive=True, inject={"beep": lambda: rang.append(1)})
    assert "FLOW STOPPED" in r.out
    assert len(rang) == 1                 # answered at once: one alert, and it is over
