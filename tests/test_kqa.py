import io
import json
import urllib.error

import pytest
from PIL import Image

from novel import kqa
from novel.stages import kfront

CFG = {"panel": ["a", "b", "c"], "spare": ["d"], "referee": "ref"}
OK = {"pass": True, "issues": []}
BAD = {"pass": False, "issues": [{"kind": "duplicate_person", "where": "left", "fix": "draw Lumian only once"}]}


def answer(v):
    return {"choices": [{"message": {"content": "```json\n" + json.dumps(v) + "\n```"}}]}


def fake(verdicts, seen=None):
    """post() whose answer depends on the model; a verdict may be a list (one per call) or an HTTP code."""
    calls = {}

    def post(base, key, body):
        m = body["model"]
        if seen is not None:
            seen.append(body)
        v = verdicts[m]
        if isinstance(v, list):
            v = v[min(calls.get(m, 0), len(v) - 1)]
        calls[m] = calls.get(m, 0) + 1
        if isinstance(v, int):
            raise urllib.error.HTTPError("u", v, "x", {}, io.BytesIO(b""))
        return answer(v)
    return post


def picture(path):
    Image.new("RGB", (1376, 768), "gray").save(path)
    return path


def test_parse_verdict_needs_a_named_problem_to_fail():
    assert kqa.parse_verdict('ok {"pass": true, "issues": []}') == OK
    assert kqa.parse_verdict(json.dumps({"pass": False, "issues": []}))["pass"] is True
    assert kqa.parse_verdict(json.dumps(BAD))["issues"][0]["kind"] == "duplicate_person"
    with pytest.raises(kqa.QAError):
        kqa.parse_verdict("no json here")
    thought = '<think>maybe {"pass": false}</think>{"pass": true, "issues": []}\n{"pass": true, "issues": []}'
    assert kqa.parse_verdict(thought) == OK
    with pytest.raises(kqa.QAError):
        kqa.parse_verdict("<think>still thinking about {it}")


def test_every_call_is_single_turn_with_a_new_session():
    seen = []
    qa = kqa.QA(CFG, "k", post=fake({"a": OK, "b": OK, "c": OK}, seen))
    qa.judge(b"jpg", "the prompt")
    assert len(seen) == 3
    for body in seen:
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        parts = body["messages"][1]["content"]
        assert parts[0]["text"].startswith("THE PROMPT:\nthe prompt")
        assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_panel_pass_needs_no_referee():
    qa = kqa.QA(CFG, "k", post=fake({"a": OK, "b": OK, "c": OK}))
    j = qa.judge(b"x", "p")
    assert j["pass"] is True and j["referee"] is None


def test_one_panel_fail_goes_to_the_referee_who_can_overrule():
    seen = []
    qa = kqa.QA(CFG, "k", post=fake({"a": OK, "b": BAD, "c": OK, "ref": OK}, seen))
    j = qa.judge(b"x", "p")
    assert j["pass"] is True and j["referee"]["model"] == "ref"
    assert "draw Lumian only once" in seen[-1]["messages"][1]["content"][0]["text"]     # the referee is told


def test_used_up_panel_model_is_replaced_by_a_spare():
    qa = kqa.QA(CFG, "k", post=fake({"a": 429, "b": OK, "c": OK, "d": OK}))
    j = qa.judge(b"x", "p")
    assert [v["model"] for v in j["votes"]] == ["d", "b", "c"]
    assert "a" in qa.out_of_quota
    assert [v["model"] for v in qa.vote(b"x", "p")] == ["b", "c", "d"]


def test_check_redraws_once_and_records(tmp_path):
    pics = {i: picture(tmp_path / f"{i}.jpg") for i in ("s01", "s02", "s03", "s04")}
    # one picture at a time, each finished before the next: s01 passes; c fails s02-s04; the referee fails s02 (its
    # redraw passes), keeps s03, fails s04 (its redraw fails too)
    qa = kqa.QA(CFG, "k", post=fake({"a": OK, "b": OK, "c": [OK, BAD, BAD, BAD], "ref": [BAD, OK, OK, BAD, BAD]}))
    drawn = []

    def redraw(it, fix):
        drawn.append((it["id"], fix))
        picture(it["path"])

    items = [{"id": i, "path": p, "brief": "p"} for i, p in pics.items()]
    rec = kqa.check(items, qa, redraw, tmp_path / "qa.json", say=lambda *_: None, parallel=1)
    assert {i: rec[i]["status"] for i in pics} == {"s01": "pass", "s02": "fixed", "s03": "kept", "s04": "failed"}
    assert [i for i, _ in drawn] == ["s02", "s04"]
    assert all("draw Lumian only once" in f for _, f in drawn)
    assert {i: rec[i]["tries"] for i in pics} == {"s01": 1, "s02": 2, "s03": 1, "s04": 2}
    assert kqa.summary(rec) == "1 failed, 1 fixed, 1 kept, 1 pass; 2 redrawn; NEEDS A LOOK: s04"
    assert "s04    failed" in kqa.status_table(rec)
    saved = json.loads((tmp_path / "qa.json").read_text(encoding="utf-8"))
    assert set(saved) == set(pics)
    # a second run checks nothing again
    again = kqa.check(items, qa, redraw, tmp_path / "qa.json", say=lambda *_: None)
    assert again == saved and len(drawn) == 2


def test_nobody_answers_means_unchecked(tmp_path, monkeypatch):
    qa = kqa.QA(CFG, "k", post=fake({"a": 500, "b": 500, "c": 500}))
    monkeypatch.setattr(kqa.time, "sleep", lambda s: None)
    it = {"id": "s01", "path": picture(tmp_path / "s01.jpg"), "brief": "p"}
    rec = kqa.check([it], qa, lambda *a: pytest.fail("no redraw"), tmp_path / "qa.json", say=lambda *_: None)
    assert rec["s01"]["status"] == "unchecked"


def test_fix_lines_and_the_live_states(tmp_path):
    assert kqa.fix_lines([BAD["issues"][0], BAD["issues"][0]]).count("draw Lumian only once") == 1
    assert kqa.fix_lines([]) == ""
    qa = kqa.QA(CFG, "k", post=fake({"a": OK, "b": OK, "c": OK}))
    live = kqa.Live(qa, tmp_path / "qa.json", lambda *a: None, say=lambda *_: None)
    live.queued(["s01", "s02"])
    live.drawing("s01")
    assert json.loads((tmp_path / "qa.json").read_text())["s01"]["status"] == "drawing"   # readable while it runs
    live.drawn({"id": "s01", "path": picture(tmp_path / "s01.jpg"), "brief": "p"})
    live.set("s02", "draw_failed", error="server gone")
    rec = live.wait()
    assert rec["s01"]["status"] == "pass" and rec["s01"]["tries"] == 1 and rec["s02"]["status"] == "draw_failed"
    assert kqa.summary(rec).endswith("NEEDS A LOOK: s02")


def test_front_seeds_differ_and_prompt_has_the_face():
    assert kfront.seed_of("lumian_lee") != kfront.seed_of("maurice_benet")
    bible = {"style": "", "characters": {
        "x": {"display": "X", "look": "a farmer", "face": "long narrow face", "expression": "a tired squint"},
        "o": {"display": "Owl", "look": "a large owl", "creature": True}}}
    p = kfront.front_prompt(bible, "x")
    assert "FACE: long narrow face." in p and "a tired squint" not in p and "arms at the sides" in p
    assert p.index("HEAD AND FACE") < p.index("POSE")
    assert "not tilted" in p
    bible["characters"]["k"] = {"display": "Knight", "look": "undead knight in black plate, mounted on a skeletal white "
                                "horse", "creature": True}
    k = kfront.front_prompt(bible, "k")
    assert "skeletal white horse" not in k and "no horse, mount" in k and "arms at the sides" in k
    o = kfront.front_prompt(bible, "o")
    assert "FACE:" not in o and "calm natural stance" in o and o.rstrip().endswith("Clean eyes, no white specks.")


def test_apply_faces_rewords_a_look_aloud():
    bible = {"characters": {"ava": {"display": "Ava", "look": "girl with fine downy facial hair"}}}
    said = []
    done = kfront.apply_faces(bible, {"ava": {"face": "round face", "expression": "shy smile", "creature": False,
                                              "look": "girl with soft rosy cheeks"}}, ["ava", "gone"], said.append)
    assert done == ["ava"]
    assert bible["characters"]["ava"]["look"] == "girl with soft rosy cheeks"
    assert any("was: girl with fine downy facial hair" in s for s in said)
