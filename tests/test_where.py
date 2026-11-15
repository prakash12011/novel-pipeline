"""Group lines say where they are (indoor / outdoor) and when (time): the contract, the import warnings, the QA
checks, the prompt text and the review prompt (fixture book 'tide', invented text)."""

import json
import sys
from pathlib import Path

import pytest

from novel import bible as B
from novel import qa as Q
from novel.gemini import contract as C
from novel.gemini import prompt as P
from novel.stages import episode_scenes as S
from test_import import (bible_reply, block, chapter_lines, counts, ep, group, image, run, say, setup_book)

FIX = Path(__file__).parent / "fixtures" / "gemini" / "bible"
STATE = B.load_state(FIX)


def ctx(count=5):
    return C.ChapterCtx(1, count, STATE, 4, S.BANNED)


def check(**kw):
    g = group(**{k: v for k, v in kw.items() if k != "drop"})
    for k in kw.get("drop", ()):
        g.pop(k, None)
    return C.check_group(g, ctx())


# ---------------------------------------------------------------- the contract

def test_where_is_stored_normalised():
    norm, errs, warns = check(where=" Outdoor ")
    assert not errs and not warns and norm["where"] == "outdoor"


def test_a_group_without_where_takes_the_locations_kind_with_a_warning():
    norm, errs, warns = check(drop=["where"])                         # harbour_quay is exterior
    assert not errs and norm["where"] == "outdoor"
    assert len(warns) == 1 and "where is missing: using 'outdoor'" in warns[0] and "exterior" in warns[0]
    norm, errs, warns = check(loc="net_loft", drop=["where"])
    assert norm["where"] == "indoor"


def test_a_group_without_where_is_an_error_only_when_the_kind_does_not_say():
    state = json.loads(json.dumps(STATE))
    state["locations"]["locations"]["harbour_quay"]["kind"] = "mixed"
    c = C.ChapterCtx(1, 5, state, 4, S.BANNED)
    g = group()
    g.pop("where")
    norm, errs, _ = C.check_group(g, c)
    assert norm is None and any(f == "where" and "does not say" in m for f, m in errs)
    g["where"] = "outdoor"
    assert C.check_group(g, c)[0]["where"] == "outdoor"             # said explicitly: fine


@pytest.mark.parametrize("bad", ["inside", "indoors", "both", 3, ["indoor"]])
def test_where_must_be_indoor_or_outdoor(bad):
    norm, errs, _ = check(where=bad)
    assert norm is None and any(f == "where" and "is not one of indoor|outdoor" in m for f, m in errs)


def test_time_is_still_required():
    norm, errs, _ = check(time="midnight")
    assert norm is None and any(f == "time" for f, _ in errs)
    norm, errs, _ = check(drop=["time"])
    assert norm is None and any(f == "time" for f, _ in errs)


def chapter(groups_, images_):
    return C.chapter_warnings({g["group"]: g for g in groups_}, {i["n"]: i for i in images_}, ctx())


def two_groups(a, b):
    ga = C.check_group(group("g01", **a), ctx())[0]
    gb = C.check_group(group("g02", **b), ctx())[0]
    ims = [C.check_line(image(1, "g01"), ctx())[1], C.check_line(image(2, "g02"), ctx())[1]]
    return [ga, gb], ims


def test_where_against_the_locations_kind_is_a_warning_not_an_error():
    norm, errs, warns = check(where="indoor")                          # a yard is not a room, but it may be
    assert norm is not None and not errs
    gs, ims = two_groups({"where": "indoor"}, {"where": "outdoor"})
    got = chapter(gs, ims)
    assert any("group g01: where is 'indoor' but the location 'harbour_quay' has kind 'exterior'" in w for w in got)
    assert not any("group g02: where is" in w for w in got)


@pytest.mark.parametrize("fields,word", [
    ({"time": "night", "light": "bright sunlight from the left"}, "sunlight"),
    ({"time": "night", "light": "a golden afternoon sun"}, "afternoon"),
    ({"time": "day", "light": "cold moonlight on the stones"}, "moonlight"),
    ({"time": "day", "light": "starry sky glow"}, "starry"),
])
def test_light_that_contradicts_the_time_is_a_warning(fields, word):
    gs, ims = two_groups(fields, {"where": "outdoor"})
    got = chapter(gs, ims)
    assert any("group g01: the light mentions" in w and f"'{word}'" in w for w in got), got


def test_moon_or_stars_indoors_without_a_window_is_a_warning_and_with_one_is_not():
    bad = {"loc": "net_loft", "where": "indoor", "time": "night", "light": "silver moonlight on the floor",
           "staging": "Maren by the bench"}
    ok = dict(bad, light="silver moonlight through the round window", staging="Maren by the bench")
    ok2 = dict(bad, staging="Maren stands in the open doorway by the bench")
    for fields, expect in ((bad, True), (ok, False), (ok2, False)):
        gs, ims = two_groups(fields, {"where": "outdoor"})
        got = chapter(gs, ims)
        assert any("indoors, and no window or opening" in w for w in got) is expect, (fields, got)


def test_lamplight_as_the_only_light_outdoors_by_day_is_a_warning():
    gs, ims = two_groups({"where": "outdoor", "time": "day", "light": "warm lantern light"}, {"where": "outdoor"})
    assert any("the only light outdoors by day is 'lantern'" in w for w in chapter(gs, ims))
    gs, ims = two_groups({"where": "outdoor", "time": "day", "light": "overcast sky and one lantern"},
                         {"where": "outdoor"})
    assert not any("the only light outdoors" in w for w in chapter(gs, ims))
    gs, ims = two_groups({"where": "outdoor", "time": "night", "light": "warm lantern light"}, {"where": "outdoor"})
    assert not any("the only light outdoors" in w for w in chapter(gs, ims))


def test_a_negated_word_is_not_a_contradiction():
    gs, ims = two_groups({"time": "night", "light": "no sunlight, only cold blue dark"}, {"where": "outdoor"})
    assert not any("the light mentions" in w for w in chapter(gs, ims))


def test_a_move_through_the_door_gets_a_note():
    gs, ims = two_groups({"loc": "net_loft", "where": "indoor", "time": "dusk"},
                         {"loc": "net_loft", "where": "outdoor", "time": "dusk"})
    assert any(w.startswith("note: group g02 follows group g01 in the same place (net_loft) at the same time (dusk) "
                            "but moves from indoor to outdoor") for w in chapter(gs, ims))
    gs, ims = two_groups({"loc": "net_loft", "where": "indoor", "time": "dusk"},
                         {"loc": "net_loft", "where": "outdoor", "time": "night"})
    assert not any(w.startswith("note: group g02") for w in chapter(gs, ims))


# ---------------------------------------------------------------- the importer and the scene file

@pytest.fixture
def book(tmp_path, monkeypatch, capsys):
    bd = setup_book(tmp_path, monkeypatch)
    assert run(monkeypatch, capsys, bible_reply())[0] == 0
    return bd


def test_import_stores_where_in_the_scene_file_and_warns_for_a_line_without_it(book, monkeypatch, capsys):
    c = counts(book)
    lines = chapter_lines(c[1], 1)
    for l in lines:
        if l["type"] == "group" and l["group"] == "g02":
            l.pop("where")
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines))
    assert code == 0, out
    assert "(group g02): where is missing: using 'outdoor' from the location's kind" in out
    doc = json.loads(S.scenes_path(ep(book), 1).read_text(encoding="utf-8"))
    assert [g["where"] for g in doc["groups"].values()] == ["outdoor", "outdoor"]
    assert all("time" in g for g in doc["groups"].values())


def test_import_rejects_a_bad_where_and_says_what_to_fix(book, monkeypatch, capsys):
    c = counts(book)
    lines = chapter_lines(c[1], 1)
    lines[0]["where"] = "inside"
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines))
    assert code == 1 and "is not one of indoor|outdoor" in out
    assert "FIX:" in say(out) and "where" in say(out)


def test_import_warns_about_where_against_kind_and_about_the_light(book, monkeypatch, capsys):
    c = counts(book)
    lines = chapter_lines(c[1], 1)
    lines[0].update(where="indoor", time="night", light="bright sunlight from the left")
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines))
    assert code == 0, out
    assert "group g01: where is 'indoor' but the location 'harbour_quay' has kind 'exterior'" in out
    assert "group g01: the light mentions 'sunlight' but the time is night" in out


# ---------------------------------------------------------------- QA

def write_scenes(bd, n, edit):
    p = S.scenes_path(ep(bd), n)
    doc = json.loads(p.read_text(encoding="utf-8"))
    edit(doc)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def qa_run(capsys, *args):
    from novel.cli import main
    code = main(["qa", "--book", "tide", "--ep", "1", "--only-code", *args])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_qa_code_checks_report_the_setting_problems_as_warnings(book, monkeypatch, capsys):
    c = counts(book)
    assert run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(c[1], 1)))[0] == 0
    write_scenes(book, 1, lambda d: d["groups"]["g01"].update(time="night", light="bright sunlight from the left"))
    code, out = qa_run(capsys, "--chapters", "1")
    assert code == 0, out
    doc = json.loads((ep(book) / "qa" / "00001.json").read_text(encoding="utf-8"))
    setting = [w for w in doc["warnings"] if w["kind"] == "setting"]
    assert setting and "the light mentions 'sunlight' but the time is night" in setting[0]["text"]
    assert setting[0]["ref"] == "group g01" and not doc["errors"]


def test_qa_notes_a_scene_file_without_where_and_still_passes(book, monkeypatch, capsys):
    c = counts(book)
    assert run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(c[1], 1)))[0] == 0
    write_scenes(book, 1, lambda d: [g.pop("where") for g in d["groups"].values()])
    code, out = qa_run(capsys, "--chapters", "1")
    assert code == 0, out
    doc = json.loads((ep(book) / "qa" / "00001.json").read_text(encoding="utf-8"))
    assert any(w["kind"] == "setting" and "has no `where`" in w["text"] for w in doc["warnings"])


def test_the_review_prompt_shows_where_and_forbids_contradicting_it(book, monkeypatch, capsys):
    c = counts(book)
    assert run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(c[1], 1)))[0] == 0
    run_ = Q.Run("tide", 1)
    r = Q.code_check(run_, 1)
    r.mode = "scenes"
    system, user = Q.build_review_prompt(run_, r, "English text")
    assert 'must also not contradict its group\'s indoor/outdoor ("where") and time of day' in system
    assert "place Harbour Quay; outdoor; time dawn; light" in user


# ---------------------------------------------------------------- the prompt text and the contact sheet

@pytest.mark.parametrize("mode", ["scenes", "full"])
def test_the_prompt_rules_and_example_say_where(mode):
    args = dict(channel="C", title="T", first=1, last=2, paced=7, chars_per_second=15.3, max_characters=4,
                window_counts={1: 5, 2: 6} if mode == "scenes" else None)
    text = P.build_prompt(mode, **args)
    assert '"time":"dawn|day|dusk|night","where":"indoor|outdoor"' in text
    assert '"where" is "indoor" when the camera stands inside' in text and "NOT from the location's kind" in text
    assert '"time": "dusk", "where": "outdoor"' in text or '"time":"dusk","where":"outdoor"' in text
    assert "no sunlight at night" in text


def test_the_example_block_is_valid_for_the_contract():
    items, errs, _ = C.parse_jsonl(P.example_block().split("```jsonl")[1].split("```")[0].strip().split("\n"))
    assert not errs
    g = next(i.obj for i in items if i.obj["type"] == "group")
    assert g["where"] in C.WHERES and g["time"] in C.TIMES
