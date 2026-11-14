"""novel.prompts: the assembled Flow prompt of a v2 picture (fixture book 'tide', invented content)."""

import json
from pathlib import Path

import pytest

from novel import bible as B
from novel import prompts as P
from novel.stages import episode_scenes as S

FIX = Path(__file__).parent / "fixtures" / "gemini" / "bible"
BIBLE = B.load_characters(FIX)
LOCS = B.load_locations(FIX)

HEAD = S.STYLE.format(style="Invented test style")
FOOT = S.FOOTER + (" Each person holds only what is written for them under IN THE PICTURE; hands described as "
                   "empty are empty. Never add a prop that is not listed.")
QUAY = ("PLACE: Harbour Quay. A long stone quay curving around a small grey harbour. Rowing boats tied to iron "
        "rings along the edge, stacks of lobster pots, coils of rope, a row of tall iron lamp posts, a squat bell "
        "tower at the far end and low whitewashed houses with slate roofs behind. Time: dawn. Light: pale light "
        "from the left. Weather: clear.")
QUAY_STAGING = ("STAGING (the same in every picture of this moment): Maren at the quay edge on the left, Tobias "
                "walking in from the right, Harl by his boat")
LOFT = ("PLACE: Net Loft. A long attic room above the boat shed. Bare wooden beams, nets hung from hooks along "
        "both walls, a workbench under a round window, wooden floats in baskets, a narrow ladder in the floor and "
        "one oil lamp on a chain above the bench. Time: dusk. Light: oil lamp light. Weather: heavy rain.")
CONT = ("CONTINUITY: same place, same furniture, same light as the previous picture of this moment; "
        "only the camera moves.")


def cast(key, var="base", pose="standing", hands="empty, at the sides", mood="calm", wearing=None):
    return {"key": key, "var": var, "pose": pose, "hands": hands, "mood": mood, "wearing": wearing}


def scene(i, group, shot, cast_, action="Maren looks over the water.", others=""):
    return {"id": i, "para": 1, "group": group, "shot": shot, "cast": cast_, "others": others,
            "action": action, "focus": [0.5, 0.5], "characters": [c["key"] for c in cast_], "scene": action}


DOC = {
    "version": 2, "chapter": 1,
    "groups": {
        "g01": {"loc": "harbour_quay", "time": "dawn", "light": "pale light from the left", "weather": "clear",
                "present": ["maren_vale", "tobias_reed", "old_harl"],
                "staging": "Maren at the quay edge on the left, Tobias walking in from the right, Harl by his boat"},
        "g02": {"loc": "net_loft", "time": "dusk", "light": "oil lamp light", "weather": "heavy rain",
                "present": ["maren_vale"], "staging": "Maren at the workbench"},
        "g03": {"loc": "net_loft", "time": "dusk", "light": "oil lamp light", "weather": "still",
                "present": ["maren_vale"], "staging": "Maren at the workbench"},
    },
    "scenes": [
        scene(1, "g01", "wide", [cast("maren_vale", pose="standing at the quay edge",
                                      hands="empty, resting on the stone wall", mood="calm")]),
        scene(2, "g01", "medium", [cast("maren_vale", pose="turning", hands="empty, at her sides", mood="wary"),
                                   cast("tobias_reed", pose="walking in", hands="holding nothing, arms swinging",
                                        mood="cheerful")]),
        scene(3, "g01", "over-the-shoulder", [cast("maren_vale", pose="facing Harl", hands="empty", mood="calm"),
                                              cast("old_harl", pose="sitting by his boat",
                                                   hands="both hands on his knees", mood="grave")],
                                             others="two gulls on a post"),
        scene(4, "g02", "detail", [cast("maren_vale", pose="bent over the bench", hands="one hand on a float",
                                        mood="focused")]),
        scene(5, "g01", "establishing", [cast("tobias_reed", pose="sitting on a lobster pot",
                                              hands="empty, folded", mood="bored")]),
    ],
}


def doc_of(*scenes):
    return {**DOC, "scenes": list(scenes)}


def build(i, doc=DOC, chapter=1):
    s = next(x for x in doc["scenes"] if x["id"] == i)
    return P.flow_prompt(s, doc, chapter, BIBLE, LOCS, {}, P.positions(doc)[i])


def test_golden_first_picture_of_a_group():
    want = "\n\n".join([
        HEAD,
        "CAST (attached Flow characters, keep their look exactly): Maren Vale.",
        QUAY + "\n" + QUAY_STAGING,
        "IN THE PICTURE:\n- Maren Vale: standing at the quay edge; hands: empty, resting on the stone wall; "
        "mood: calm.",
        "SHOT: wide, camera far back, full-length figures, surroundings clearly visible.",
        "SCENE: Maren looks over the water.",
        FOOT]) + "\n"
    assert build(1) == want


def test_golden_later_picture_of_the_same_group_has_continuity_and_identical_place():
    first, later = build(1), build(2)
    want = "\n\n".join([
        HEAD,
        "CAST (attached Flow characters, keep their look exactly): Maren Vale, Tobias Reed.",
        QUAY + "\n" + QUAY_STAGING + "\n" + CONT,
        "IN THE PICTURE:\n- Maren Vale: turning; hands: empty, at her sides; mood: wary.\n"
        "- Tobias Reed: walking in; hands: holding nothing, arms swinging; mood: cheerful.",
        "SHOT: " + P.SHOT_TEXT["medium"] + ".",
        "SCENE: Maren looks over the water.",
        FOOT]) + "\n"
    assert later == want
    pick = lambda t: [l for l in t.splitlines() if l.startswith(("PLACE:", "STAGING"))]
    assert pick(first) == pick(later) and len(pick(first)) == 2
    assert [l.encode() for l in pick(first)] == [l.encode() for l in pick(later)]
    assert "CONTINUITY" not in first


def test_golden_text_only_person_and_others():
    want = "\n\n".join([
        HEAD,
        "CAST (attached Flow characters, keep their look exactly): Maren Vale.",
        QUAY + "\n" + QUAY_STAGING + "\n" + CONT,
        "IN THE PICTURE:\n- Maren Vale: facing Harl; hands: empty; mood: calm.\n"
        "- Old Harl: sitting by his boat; hands: both hands on his knees; mood: grave.\n"
        "ALSO IN THE PICTURE: Old Harl: stooped old man, white beard, weathered face, a faded green sweater; "
        "two gulls on a post",
        "SHOT: " + P.SHOT_TEXT["over-the-shoulder"] + ".",
        "SCENE: Maren looks over the water.",
        FOOT]) + "\n"
    assert build(3) == want


def test_golden_wearing_and_variant_with_its_own_flow_name():
    d = {**DOC, "chapter": 2, "scenes": [
        scene(1, "g01", "low-angle", [cast("maren_vale", var="storm_coat", pose="bracing", hands="gripping nothing",
                                           mood="stern", wearing="a hood pulled up against the rain"),
                                      cast("tobias_reed", pose="shouting", hands="cupped round his mouth",
                                           mood="urgent")])]}
    want = "\n\n".join([
        HEAD,
        "CAST (attached Flow characters, keep their look exactly): Maren Vale - Storm Coat, Tobias Reed.",
        QUAY + "\n" + QUAY_STAGING,
        "IN THE PICTURE:\n- Maren Vale: bracing; hands: gripping nothing; mood: stern.\n"
        "- Tobias Reed: shouting; hands: cupped round his mouth; mood: urgent.\n"
        "WEARING (this picture only): Maren Vale: a hood pulled up against the rain",
        "SHOT: " + P.SHOT_TEXT["low-angle"] + ".",
        "SCENE: Maren looks over the water.",
        FOOT]) + "\n"
    assert build(1, d, chapter=2) == want
    # an attached person is never described, even with a variant
    assert "yellow oilskin" not in want and "tall woman" not in want


def test_header_mark_is_first_and_scene_is_one_line():
    s = scene(1, "g01", "wide", [cast("maren_vale")], action="Line one\nline two\r\n  line three SCENE: again")
    t = P.flow_prompt(s, doc_of(s), 1, BIBLE, LOCS, {}, 0)
    assert t.startswith(P.HEADER_MARK) and P.HEADER_MARK == S.HEADER_MARK
    assert t.count("SCENE:") == 1
    line = t.split("SCENE:")[1].split("\n")[0]
    assert line.strip() == "Line one line two line three scene: again"
    assert P.scene_sentence(t) == line.strip()


def test_weather_omitted_when_still_or_empty():
    for w in ("still", "Still", "", None):
        d = json.loads(json.dumps(DOC))
        d["groups"]["g01"]["weather"] = w
        assert "Weather" not in build(1, d)
    assert "Weather: clear." in build(1)


def test_resumed_group_gets_continuity():
    d = doc_of(DOC["scenes"][0], DOC["scenes"][3], DOC["scenes"][4])     # g01, g02, g01 again
    assert P.positions(d) == {1: 0, 4: 0, 5: 1}
    assert "CONTINUITY" not in build(4, d)
    assert CONT in build(5, d)


def test_each_shot_name_has_its_instruction():
    assert tuple(P.SHOT_TEXT) == S.SHOTS_V2 and len(P.SHOT_TEXT) == 7
    for shot, text in P.SHOT_TEXT.items():
        s = scene(1, "g01", shot, [cast("maren_vale")])
        t = P.flow_prompt(s, doc_of(s), 1, BIBLE, LOCS, {}, 0)
        assert f"SHOT: {text}." in t
    bad = scene(1, "g01", "close-up", [cast("maren_vale")])
    with pytest.raises(P.PromptError):
        P.flow_prompt(bad, doc_of(bad), 1, BIBLE, LOCS, {}, 0)


def test_unknown_place_or_variant_chapter_is_an_error():
    s = scene(1, "g01", "wide", [cast("maren_vale", var="storm_coat")])      # the storm coat covers chapter 2 only
    with pytest.raises(P.PromptError):
        P.flow_prompt(s, doc_of(s), 1, BIBLE, LOCS, {}, 0)
    d = json.loads(json.dumps(DOC))
    d["groups"]["g01"]["loc"] = "nowhere"
    with pytest.raises(P.PromptError):
        build(1, d)


def test_reference_plan_lists_not_made_and_needs_new():
    bible = json.loads(json.dumps(BIBLE))
    bible["characters"]["tobias_reed"]["needs_new_reference"] = True
    d = {**DOC, "scenes": [
        scene(1, "g01", "wide", [cast("maren_vale", var="storm_coat"), cast("tobias_reed"), cast("old_harl")]),
        scene(2, "g01", "wide", [cast("maren_vale", var="storm_coat")])]}
    r = P.reference_plan(d, 2, bible)
    assert r["needed"] == [("maren_vale", "storm_coat"), ("tobias_reed", "base")]     # Old Harl has no Flow name
    assert r["not_made"] == [("maren_vale", "storm_coat")]
    assert r["needs_new_reference"] == [("tobias_reed", "base")]
    assert P.reference_plan(DOC, 1, BIBLE)["not_made"] == []


def test_adopt_regex_matches_the_expression_in_flow_adopt():
    """flow/adopt.mjs reads the SCENE sentence with this exact expression; scene_sentence() is its Python twin."""
    src = (Path(__file__).parent.parent / "flow" / "adopt.mjs").read_text(encoding="utf-8")
    assert "fs.readFileSync(shot.prompt, 'utf8').split('SCENE:')[1]?.split('\\n')[0]" in src
    t = build(2)
    assert P.scene_sentence(t) == "Maren looks over the water."
