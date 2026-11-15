"""Every named character involved in a group is listed in `present`; everyone drawn has a `position`
(fixture book 'tide', invented text)."""

import json
from pathlib import Path

import pytest

from novel import bible as B
from novel import prompts as PR
from novel import qa as Q
from novel.gemini import contract as C
from novel.gemini import fix as F
from novel.gemini import prompt as P
from novel.stages import episode_scenes as S
from test_import import (bible_reply, block, cast, chapter_lines, counts, ep, group, image, run, say, setup_book)

FIX = Path(__file__).parent / "fixtures" / "gemini" / "bible"
STATE = B.load_state(FIX)


def ctx():
    return C.ChapterCtx(1, 5, STATE, 4, S.BANNED)


def check_image(**kw):
    im = image(1, "g01", **kw)
    return C.check_image(im, ctx())


@pytest.fixture
def book(tmp_path, monkeypatch, capsys):
    bd = setup_book(tmp_path, monkeypatch)
    assert run(monkeypatch, capsys, bible_reply())[0] == 0
    return bd


def lines_for(bd, edit):
    lines = chapter_lines(counts(bd)[1], 1)
    edit(lines)
    return lines


# ---------------------------------------------------------------- the rules text

@pytest.mark.parametrize("mode", ["scenes", "full"])
def test_the_prompt_rules_ask_for_present_position_and_fewer_people(mode):
    text = P.build_prompt(mode, channel="C", title="T", first=1, last=2, paced=7, chars_per_second=15.3,
                          max_characters=4, window_counts={1: 5, 2: 6} if mode == "scenes" else None)
    assert 'EVERY named character who is in this place during the group' in text
    assert "required\n  and not empty, unless the group truly has nobody (an empty street)" in text
    assert "a cast member who is not listed in \"present\" makes the whole block be rejected" in text
    assert "Fewer people is better: show who the moment is about" in text
    assert 'gets an empty cast [] and an\n  "establishing" or "detail" shot' in text
    assert '"position":"<where in the frame and the place>"' in text
    assert "Two people in one picture\n  never share a position" in text
    assert '"position":"left middle distance, behind the stall"' in text         # the example line


def test_the_api_images_rules_carry_the_same_text():
    r = P.build_rules("images", channel="C", title="T", first=1, last=2, paced=7, chars_per_second=15.3,
                      max_characters=4, chapter=1, window_count=5)
    assert "Fewer people is better" in r and "never share a position" in r


def test_the_example_block_passes_the_contract():
    items, errs, _ = C.parse_jsonl(P.example_block().split("```jsonl")[1].split("```")[0].strip().split("\n"))
    assert not errs
    im = next(i.obj for i in items if i.obj["type"] == "image")
    norm, errs, _ = C.check_image(im, C.ChapterCtx(1, 1, {"characters": {"characters": {
        "maren": {"display": "Maren", "tier": 1, "variants": {"base": {"chapters": ""}}},
        "tobias": {"display": "Tobias", "tier": 1, "variants": {"base": {"chapters": ""}}}}},
        "locations": {"locations": {}}, "names": {}}, 4, S.BANNED))
    assert not [e for e in errs if "position" in e[0]]
    assert all(c["position"] for c in im["cast"])


# ---------------------------------------------------------------- position

def test_position_is_stored_normalised():
    norm, errs, _ = check_image()
    assert not errs and norm["cast"][0]["position"] == "left foreground, by the rail"


@pytest.mark.parametrize("bad", [None, "", "   ", 5])
def test_position_is_required(bad):
    c = cast("maren_vale")
    if bad is None:
        del c["position"]
    else:
        c["position"] = bad
    norm, errs, _ = check_image(cast_=[c])
    assert norm is None and any(f.endswith(".position") and "missing or empty" in m for f, m in errs)


def test_two_people_never_share_a_position_even_in_different_words_or_case():
    a, b = cast("maren_vale", position="Left foreground."), cast("tobias_reed", position="left foreground")
    norm, errs, _ = check_image(cast_=[a, b])
    assert norm is None and any(f == "cast" and "share the position" in m for f, m in errs)
    b["position"] = "right foreground"
    assert check_image(cast_=[a, b])[0] is not None


def test_position_must_be_english():
    norm, errs, _ = check_image(cast_=[cast("maren_vale", position="बाएं आगे")])
    assert norm is None and any(f.endswith(".position") and "Devanagari" in m for f, m in errs)


def test_import_rejects_a_missing_or_shared_position_and_says_what_to_fix(book, monkeypatch, capsys):
    def edit(L):
        L[1]["cast"] = [cast("maren_vale", position="centre"), cast("tobias_reed", position="Centre")]
        del L[2]["cast"][0]["position"]
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines_for(book, edit)))
    assert code == 1
    assert "share the position 'Centre'" in out and "position): missing or empty" in out
    assert "FIX:" in say(out) and "position" in say(out)
    assert not S.scenes_path(ep(book), 1).exists()


def test_a_scene_file_without_position_still_loads_and_assembles():
    doc = {"version": 2, "chapter": 1,
           "groups": {"g01": {"loc": "harbour_quay", "time": "dawn", "where": "outdoor", "light": "pale light",
                              "weather": "", "present": ["maren_vale"], "staging": "Maren at the quay"}},
           "scenes": [{"id": 1, "para": 1, "group": "g01", "shot": "wide", "others": "", "action": "Maren waits.",
                       "focus": [0.5, 0.5], "characters": ["maren_vale"], "scene": "Maren waits.",
                       "cast": [{"key": "maren_vale", "var": "base", "pose": "standing", "hands": "empty",
                                 "mood": "calm", "wearing": None}]}]}
    text = PR.flow_prompt(doc["scenes"][0], doc, 1, STATE["characters"], STATE["locations"], {}, 0)
    assert "- Maren Vale: standing; hands: empty; mood: calm." in text


def test_the_flow_prompt_says_the_position_first():
    doc = {"version": 2, "chapter": 1,
           "groups": {"g01": {"loc": "harbour_quay", "time": "dawn", "where": "outdoor", "light": "pale light",
                              "weather": "", "present": ["maren_vale", "tobias_reed"], "staging": "at the quay"}},
           "scenes": [{"id": 1, "para": 1, "group": "g01", "shot": "wide", "others": "", "action": "They talk.",
                       "focus": [0.5, 0.5], "characters": [], "scene": "They talk.",
                       "cast": [cast("maren_vale"), cast("tobias_reed")]}]}
    text = PR.flow_prompt(doc["scenes"][0], doc, 1, STATE["characters"], STATE["locations"], {}, 0)
    assert ("- Maren Vale: left foreground, by the rail; standing; hands: empty, at the sides; mood: calm.\n"
            "- Tobias Reed: right middle distance, on the quay; standing; hands: empty, at the sides; mood: calm.") in text


# ---------------------------------------------------------------- present

def test_a_cast_member_outside_present_is_an_error_at_import(book, monkeypatch, capsys):
    def edit(L):
        L[0]["present"] = ["maren_vale"]                          # tobias_reed is drawn in image 2
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines_for(book, edit)))
    assert code == 1, out
    assert 'tobias_reed is in the cast but not in group g01\'s "present"' in out
    assert "image 2 cast" in say(out) and 'FIX: ' in say(out)
    assert not S.scenes_path(ep(book), 1).exists()                # nothing is written for a rejected block


def test_present_listed_first_makes_the_same_block_pass(book, monkeypatch, capsys):
    def edit(L):
        L[0]["present"] = ["maren_vale", "tobias_reed"]
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines_for(book, edit)))
    assert code == 0, out


def test_a_group_with_nobody_has_an_empty_present_and_empty_casts(book, monkeypatch, capsys):
    def edit(L):
        for l in L:
            if l["type"] == "group" and l["group"] == "g02":
                l["present"] = []
            elif l["type"] == "image" and l["group"] == "g02":
                l["cast"] = []
                l["shot"] = "establishing"
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines_for(book, edit)))
    assert code == 0, out
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0002", chapter_lines(counts(book)[2], 2)))
    assert code == 0, out


def test_an_empty_present_with_a_cast_is_the_same_error(book, monkeypatch, capsys):
    def edit(L):
        L[0]["present"] = []
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines_for(book, edit)))
    assert code == 1 and 'is in the cast but not in group g01\'s "present"' in out


def test_a_patch_with_a_cast_member_outside_present_is_rejected(book, monkeypatch, capsys):
    assert run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(counts(book)[1], 1)))[0] == 0
    im = image(1, "g01", cast_=[cast("old_harl")])                  # old_harl is not in g01's present
    code, out = run(monkeypatch, capsys, block("PATCH", "c0001", [im]))
    assert code == 1 and 'old_harl is in the cast but not in group g01\'s "present"' in out


# ---------------------------------------------------------------- QA

def qa_run(capsys, *args):
    from novel.cli import main
    code = main(["qa", "--book", "tide", "--ep", "1", "--only-code", *args])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_qa_warns_about_a_character_involved_but_never_drawn(book, monkeypatch, capsys):
    def edit(L):
        L[0]["present"] = ["maren_vale", "tobias_reed", "old_harl"]
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines_for(book, edit)))
    assert code == 0 and 'old_harl is listed in "present" but appears in none of the group\'s images' in out
    code, out = qa_run(capsys, "--chapters", "1")
    assert code == 0, out
    doc = json.loads((ep(book) / "qa" / "00001.json").read_text(encoding="utf-8"))
    hit = [w for w in doc["warnings"] if w["kind"] == "present_unused"]
    assert hit and hit[0]["ref"] == "group g01" and "involved but never drawn" in hit[0]["text"]


def test_qa_keeps_the_named_in_the_action_but_not_in_the_cast_warning(book, monkeypatch, capsys):
    def edit(L):
        L[1]["action"] = ("Maren Vale stands at the quay while Old Harl mends a net by his boat and the gulls circle "
                          "above the harbour wall in the pale morning light.")
    assert run(monkeypatch, capsys, block("IMAGES", "c0001", lines_for(book, edit)))[0] == 0
    code, out = qa_run(capsys, "--chapters", "1")
    doc = json.loads((ep(book) / "qa" / "00001.json").read_text(encoding="utf-8"))
    assert any(w["kind"] == "people" and "is named in the action" in w["text"] for w in doc["warnings"])


def test_the_review_prompt_says_everyone_acting_must_be_in_the_cast_and_shows_positions(book, monkeypatch, capsys):
    assert run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(counts(book)[1], 1)))[0] == 0
    run_ = Q.Run("tide", 1)
    r = Q.code_check(run_, 1)
    r.mode = "scenes"
    system, user = Q.build_review_prompt(run_, r, "English text")
    assert "Everyone the narration of that window shows acting or speaking must be in the cast" in system
    assert "position: left foreground, by the rail" in user


def test_the_fix_message_asks_for_position_too(book, monkeypatch, capsys):
    assert run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(counts(book)[1], 1)))[0] == 0
    p = S.scenes_path(ep(book), 1)
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["scenes"][2]["cast"][0]["var"] = "storm_coat"
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    assert qa_run(capsys, "--chapters", "1")[0] == 1
    fx = F.build(ep(book), 1)
    assert "`var`, `position`, `pose`" in fx.text and '"position":"' in fx.text
