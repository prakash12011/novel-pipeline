"""novel characters make: invented book "tb", fake character script (FLOW_CHAR), the "port answers" check injected.
No real browser, no network, no Flow."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from novel import flowrun as F
from novel.commands import characters_cmd, characters_make
from novel.core import chromelock

from test_images_cmd import UP, build

ROOT = Path(__file__).resolve().parent.parent
FAKE_CHAR = ROOT / "tests" / "fakes" / "fake_char.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

CHARS = {"c0001-s01": ["Mira", "Kael"], "c0002-s01": ["Mira - Cloak", "Vex"], "c0003-s01": ["Orin"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = build(tmp_path, monkeypatch, chars_in=CHARS)
    monkeypatch.setenv("FLOW_CHAR", str(FAKE_CHAR))
    doc = json.loads((e.bible / "characters.json").read_text(encoding="utf-8"))
    doc["characters"]["vex"] = {
        "display": "Vex", "role": "spy", "look": "a thin man in grey", "needs_new_reference": True,
        "legacy": {"appearance": "a thin man holding a dagger"},
        "variants": {"base": {"flow_name": "Vex", "chapters": "1-", "look": None, "flow_made": True, "ref_version": 1}}}
    doc["characters"]["orin"] = {
        "display": "Orin", "role": "monk", "look": "an old monk holding a lantern",
        "variants": {"base": {"flow_name": "Orin", "chapters": "1-", "look": None, "flow_made": False, "ref_version": 2}}}
    (e.bible / "characters.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    e.chars = e.bible / "characters.json"
    e.bflow = e.bible / "flow"
    return e


def doc_of(env):
    return json.loads(env.chars.read_text(encoding="utf-8"))


def make(capsys, *argv, replies=(), interactive=False, answers=UP, inject=None):
    asked, queue = [], list(replies)

    def ask(prompt):
        asked.append(prompt)
        if not queue:
            raise AssertionError(f"unexpected input() call: {prompt!r}")
        return queue.pop(0)
    inj = {"answers": answers}
    inj.update(inject or {})
    code = characters_cmd.make_main(["--book", "tb", "--ep", "1", *argv], inject=inj, ask=ask, interactive=interactive)
    cap = capsys.readouterr()
    return SimpleNamespace(code=code, out=cap.out, err=cap.err, asked=asked)


def calls(env):
    f = env.bflow / "char-calls.log"
    return f.read_text().splitlines() if f.exists() else []


def history(env):
    h = env.bible / "history"
    return sorted(h.glob("characters-*.json")) if h.exists() else []


# ---------------------------------------------------------------- targets and the list

def test_default_targets_come_from_the_plan_and_dry_run_changes_nothing(env, capsys):
    before = env.chars.read_bytes()
    r = make(capsys, "--dry-run")
    assert r.code == 0, r.err
    assert "Create (2):" in r.out and "mira:cloak  'Mira - Cloak'" in r.out and "kael  'Kael'" in r.out
    assert "Recreate under a new name (1" in r.out and "'Vex' -> 'Vex v2'" in r.out
    assert "flagged; waiting for a cleaned-up look from Gemini" in r.out.lower() or \
        "Flagged; waiting for a cleaned-up look from Gemini" in r.out
    assert "orin" in r.out.split("waiting")[1].split("Create")[0]
    assert "Each one costs Flow credits for a portrait and a body image (3 character(s))" in r.out
    assert env.chars.read_bytes() == before and not calls(env) and not history(env)
    assert not chromelock.lock_path().exists()


def test_keys_with_variant(env, capsys):
    r = make(capsys, "--keys", "mira:cloak", "--dry-run")
    assert r.code == 0 and "Create (1):" in r.out and "kael" not in r.out and "Recreate" not in r.out
    r = make(capsys, "--keys", "mira", "--dry-run")                   # already made: skipped with a hint
    assert "already made" in r.out and "--resend mira" in r.out and "Nothing to make" in r.out
    r = make(capsys, "--keys", "nobody", "--dry-run")
    assert r.code == 1 and "not in the bible" in r.err


def test_nothing_to_make(env, capsys):
    doc = doc_of(env)
    for k in ("kael", "orin"):
        doc["characters"][k]["variants"]["base"]["flow_made"] = True
    doc["characters"]["mira"]["variants"]["cloak"]["flow_made"] = True
    doc["characters"]["vex"].pop("needs_new_reference")
    env.chars.write_text(json.dumps(doc), encoding="utf-8")
    r = make(capsys, "--yes")
    assert r.code == 0 and "Nothing to make" in r.out and not calls(env)


# ---------------------------------------------------------------- confirmation, run, report

def test_confirmation_n_changes_nothing_and_yes_flag_skips_the_question(env, capsys):
    before = env.chars.read_bytes()
    r = make(capsys, replies=["n"])
    assert r.code == 1 and r.asked == ["Proceed? [y/N] "] and "Cancelled" in r.out
    assert env.chars.read_bytes() == before and not calls(env) and not history(env)
    assert not chromelock.lock_path().exists()
    r = make(capsys, replies=[""])                                   # Enter = no
    assert r.code == 1 and env.chars.read_bytes() == before
    r = make(capsys, "--yes")
    assert r.code == 0, r.out + r.err
    assert r.asked == []


def test_full_run_marks_made_and_reports_files(env, capsys):
    r = make(capsys, replies=["y"])
    assert r.code == 0, r.out + r.err
    d = doc_of(env)["characters"]
    assert d["kael"]["variants"]["base"]["flow_made"] is True
    assert d["mira"]["variants"]["cloak"]["flow_made"] is True
    assert d["orin"]["variants"]["base"]["flow_made"] is False       # flagged, not cleaned: never touched
    assert "made 3, not made 0" in r.out and "Next: novel images --ep 1" in r.out
    assert "made: kael 'Kael'" in r.out and "kael-portrait.jpg" in r.out and "mira.cloak-body.jpg" in r.out
    assert (env.bflow / "kael-portrait.jpg").exists() and (env.bflow / "mira.cloak-body.jpg").exists()
    assert not any("orin" in c for c in calls(env))
    assert calls(env)[0] == "kael,mira:cloak,vex front=1 resend=-"


def test_report_reflects_made_and_failed(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CHAR_SCRIPT", json.dumps({"kael": ["fail"]}))
    r = make(capsys, "--yes")
    assert r.code == 1, r.out + r.err
    assert "made: mira:cloak" in r.out and "failed or left: kael 'Kael'" in r.out and "made 2, not made 1" in r.out
    assert "Next:" not in r.out
    d = doc_of(env)["characters"]
    assert d["kael"]["variants"]["base"]["flow_made"] is False


def test_resend_passes_through(env, capsys):
    r = make(capsys, "--resend", "mira", "--yes")
    assert r.code == 0, r.out + r.err
    assert calls(env) == ["mira front=1 resend=mira"]


# ---------------------------------------------------------------- the new-reference (v2) flow

def test_new_reference_rename_flow(env, capsys, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=3)
    env.bflow.mkdir(exist_ok=True)
    (env.bflow / "journal.jsonl").write_text(json.dumps({"id": "vex", "state": "done", "at": old.isoformat()}) + "\n")
    monkeypatch.setenv("FAKE_CHAR_SCRIPT", json.dumps({"vex": ["fail", "ok"]}))
    r = make(capsys, "--keys", "vex", "--yes")
    assert r.code == 1, r.out + r.err
    v = doc_of(env)["characters"]["vex"]
    base = v["variants"]["base"]
    assert base["flow_name"] == "Vex v2" and base["ref_version"] == 2 and base["flow_made"] is False
    assert base["legacy"]["flow_name"] == "Vex" and base["legacy"]["ref_version"] == 1
    assert v["needs_new_reference"] is True and base["needs_new_reference"] is True      # not cleared: it failed
    assert len(history(env)) == 1                                       # the rename went through the bible's save
    assert calls(env) == ["vex front=1 resend=vex"]                     # the old "done" belongs to the old reference
    r = make(capsys, "--keys", "vex", "--yes")                           # second try, now plain and successful
    assert r.code == 0, r.out + r.err
    v = doc_of(env)["characters"]["vex"]
    assert v["variants"]["base"]["flow_made"] is True and v["variants"]["base"]["flow_name"] == "Vex v2"
    assert "needs_new_reference" not in v and "needs_new_reference" not in v["variants"]["base"]
    assert v["variants"]["base"]["legacy"]["flow_name"] == "Vex"         # old name still on record
    assert len(history(env)) == 2
    assert calls(env)[-1] == "vex front=1 resend=-"


def test_second_rename_gets_v3(env):
    doc = doc_of(env)
    doc["characters"]["vex"]["variants"]["base"].update(flow_name="Vex v2", ref_version=2)
    assert characters_make.next_name(doc, "Vex v2") == "Vex v3"
    old, new = characters_make.rename_for_new_reference(doc, "vex", "base", datetime.now(timezone.utc))
    b = doc["characters"]["vex"]["variants"]["base"]
    assert (old, new) == ("Vex v2", "Vex v3") and b["ref_version"] == 3 and b["legacy"]["flow_name"] == "Vex v2"
    old, new = characters_make.rename_for_new_reference(doc, "vex", "base", datetime.now(timezone.utc))
    assert new == "Vex v4" and [e["flow_name"] for e in b["legacy"]["earlier"]] == ["Vex v2"]


def test_journal_done_without_a_rename_is_not_resent(env, capsys):
    env.bflow.mkdir(exist_ok=True)
    (env.bflow / "journal.jsonl").write_text(json.dumps({"id": "kael", "state": "done", "at": "2026-01-01T00:00:00+00:00"}) + "\n")
    r = make(capsys, "--keys", "kael", "--yes")
    assert "the journal says done but the bible says not made" in r.out
    assert not calls(env) and r.code == 1


# ---------------------------------------------------------------- gate, chrome, lock (shared with images)

def char_wall(env, at):
    env.bflow.mkdir(exist_ok=True)
    with (env.bflow / "journal.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "kael", "state": "error-before-send", "at": at.isoformat(),
                            "error": "Flow shows a limit or CAPTCHA - stopping"}) + "\n")


def test_gate_refuses_then_acknowledgement_proceeds(env, capsys):
    char_wall(env, datetime.now(timezone.utc) - timedelta(hours=1))
    before = env.chars.read_bytes()
    r = make(capsys, "--yes")
    assert r.code == 1 and "Flow stopped at" in r.err and "--i-checked-flow" in r.err
    assert env.chars.read_bytes() == before and not calls(env)
    r = make(capsys, "--yes", "--i-checked-flow")
    assert r.code == 0, r.out + r.err
    assert len((env.flow / "acks.jsonl").read_text().splitlines()) == 1


def test_gate_also_sees_an_image_stop(env, capsys):
    with env.journal.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "c0001-s01", "state": "wall", "at": datetime.now(timezone.utc).isoformat(),
                            "error": "Flow says: unusual activity"}) + "\n")
    r = make(capsys, "--yes")
    assert r.code == 1 and "unusual activity" in r.err


def test_chrome_not_answering_and_lock_held(env, capsys):
    before = env.chars.read_bytes()
    r = make(capsys, "--yes", answers=lambda u: None)
    assert r.code == 1 and "novel chrome" in r.err
    with chromelock.acquire(cmd="novel images", book="tb", episode=1):
        r = make(capsys, "--yes")
    assert r.code == 1 and "in use by" in r.err and "novel images" in r.err
    assert env.chars.read_bytes() == before and not calls(env)


def test_chrome_prompt_yes_starts_it(env, capsys):
    state = {"up": False, "launched": []}

    def launch(argv):
        state["launched"].append(argv)
        state["up"] = True
    r = make(capsys, "--yes", replies=[""], interactive=True, answers=lambda u: "Chrome/1" if state["up"] else None,
             inject={"do_launch": launch, "sleep": lambda s: None})
    assert r.code == 0, r.out + r.err
    assert len(state["launched"]) == 1 and r.asked[0].startswith("Chrome is not running.")


def test_lock_held_during_the_run(env, capsys, monkeypatch):
    seen = []
    real = F.spawn

    def spy(*a, **k):
        seen.append(chromelock.holder())
        return real(*a, **k)
    monkeypatch.setattr(F, "spawn", spy)
    make(capsys, "--yes", "--keys", "kael")
    assert seen and seen[0]["alive"] and seen[0]["cmd"].startswith("novel characters make")
    assert chromelock.holder() is None


def test_stop_interactive_enter_continues_and_noninteractive_ends(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CHAR_SCRIPT", json.dumps({"kael": ["wall", "ok"]}))
    r = make(capsys, "--yes", "--keys", "kael", replies=[""], interactive=True)
    assert r.code == 0, r.out + r.err
    assert len(r.asked) == 1 and "FLOW STOPPED" in r.out
    assert len((env.flow / "acks.jsonl").read_text().splitlines()) == 1
    assert doc_of(env)["characters"]["kael"]["variants"]["base"]["flow_made"] is True
    assert [c for c in calls(env) if c.endswith("resend=-")] == ["kael front=1 resend=-", "kael front=1 resend=-"]
    # not interactive: ends with 5, and a later run is refused
    d = doc_of(env)
    d["characters"]["kael"]["variants"]["base"]["flow_made"] = False
    env.chars.write_text(json.dumps(d), encoding="utf-8")
    (env.bflow / "journal.jsonl").write_text("")
    (env.flow / "acks.jsonl").unlink()
    monkeypatch.setenv("FAKE_CHAR_SCRIPT", json.dumps({"kael": ["wall"]}))
    (env.bflow / "char-counts.json").unlink()
    r = make(capsys, "--yes", "--keys", "kael")
    assert r.code == 5 and r.asked == [] and "Not interactive" in r.out
    r = make(capsys, "--yes", "--keys", "kael")
    assert r.code == 1 and "Flow stopped at" in r.err


# ---------------------------------------------------------------- the plans follow a rename

CAST = "CAST (attached Flow characters, keep their look exactly): "


def plan_chars(env):
    return {s["id"]: s["chars"] for s in json.loads(env.plan.read_text(encoding="utf-8"))["shots"]}


def test_rename_updates_ungenerated_shots_in_every_episode_plan_and_lists_the_rest(env, capsys, monkeypatch):
    from test_images_cmd import put_done
    plan = json.loads(env.plan.read_text(encoding="utf-8"))
    for s in plan["shots"]:
        if s["id"] == "c0003-s01":
            s["chars"] = ["Vex", "Orin"]
        if s["id"] == "c0004-s01":
            s["chars"] = ["Kael", "Vex", "Vex v2"]
    env.plan.write_text(json.dumps(plan), encoding="utf-8")
    put_done(env, "c0003-s01")                                           # this picture exists already
    p1 = Path(next(s for s in plan["shots"] if s["id"] == "c0002-s01")["prompt"])
    p1.write_text(f"STYLE\n{CAST}Mira - Cloak, Vex.\nPLACE: a hall.\n", encoding="utf-8")
    # a second episode of the same book that also uses the old name
    ep2 = env.ep.parent / "E002"
    (ep2 / "flow").mkdir(parents=True)
    plan2 = {"project": "p", "journal": (ep2 / "flow" / "journal.jsonl").as_posix(), "shots": [
        {"id": "c0006-s01", "chars": ["Vex"], "prompt": (ep2 / "flow" / "x.txt").as_posix(),
         "out": (ep2 / "images" / "c0006" / "s01-2k.jpg").as_posix()}]}
    (ep2 / "flow" / "plan.json").write_text(json.dumps(plan2), encoding="utf-8")
    before_ep1 = env.plan.read_bytes()

    monkeypatch.setenv("FAKE_CHAR_SCRIPT", json.dumps({"vex": ["fail"]}))
    r = make(capsys, "--keys", "vex", "--yes")
    assert "'Vex' -> 'Vex v2'" in r.out and "E001 plan:" in r.out and "E002 plan:" in r.out
    got = plan_chars(env)
    assert got["c0002-s01"] == ["Mira - Cloak", "Vex v2"]
    assert got["c0003-s01"] == ["Vex", "Orin"]                           # picture exists: left alone
    assert got["c0004-s01"] == ["Kael", "Vex v2"]                        # no duplicate
    assert got["c0001-s01"] == ["Mira", "Kael"]
    assert "left alone, the picture already exists (c0003-s01)" in r.out
    assert (env.flow / "plan.prev.json").read_bytes() == before_ep1
    assert f"{CAST}Mira - Cloak, Vex v2.\n" in p1.read_text(encoding="utf-8")
    p2_plan = json.loads((ep2 / "flow" / "plan.json").read_text(encoding="utf-8"))
    assert p2_plan["shots"][0]["chars"] == ["Vex v2"] and (ep2 / "flow" / "plan.prev.json").exists()


def test_update_plans_without_a_rename_touches_nothing(env):
    before = env.plan.read_bytes()
    assert characters_make.update_plans_after_rename("tb", {"Nobody": "Nobody v2"}, out=lambda s: None) == {}
    assert env.plan.read_bytes() == before and not (env.flow / "plan.prev.json").exists()
    res = characters_make.update_plans_after_rename("tb", {"Vex": "Vex v2"}, out=lambda s: None)
    assert res == {"E001": {"updated": ["c0002-s01"], "left": []}}


def test_old_name_on_pictures_already_made_asks_for_nothing(env, capsys):
    from test_images_cmd import put_done
    doc = doc_of(env)
    doc["characters"]["seren"] = {
        "display": "Seren", "role": "guard", "look": "a broad guard",
        "variants": {"base": {"flow_name": "Seren v2", "chapters": "1-", "look": None, "flow_made": True,
                              "ref_version": 2, "legacy": {"flow_name": "Seren"}}}}
    env.chars.write_text(json.dumps(doc), encoding="utf-8")
    plan = json.loads(env.plan.read_text(encoding="utf-8"))
    for s in plan["shots"]:
        if s["id"] == "c0003-s01":
            s["chars"] = ["Seren"]
    env.plan.write_text(json.dumps(plan), encoding="utf-8")
    put_done(env, "c0003-s01")                                    # drawn already: left alone on purpose
    r = make(capsys, "--dry-run")
    assert "'Seren' was renamed (seren)" in r.out and "Nothing to do." in r.out
    assert "the plan still uses the old name" not in r.out and "refresh the plan" not in r.out
    # a picture of the old name that is NOT made yet still needs the plan refreshed
    for s in plan["shots"]:
        if s["id"] == "c0004-s01":
            s["chars"] = ["Seren"]
    env.plan.write_text(json.dumps(plan), encoding="utf-8")
    r = make(capsys, "--dry-run")
    assert "the plan still uses the old name 'Seren' (seren was renamed) in 1 picture not made yet" in r.out
    assert "refresh the plan with novel approve" in r.out
