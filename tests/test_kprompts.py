"""novel.kprompts: the text for the open image model (fixture book 'tide', invented content)."""

import json
from pathlib import Path

import pytest

from novel import bible as B
from novel import kprompts as K
from novel.prompts import PromptError
from novel.stages import episode_scenes as S

FIX = Path(__file__).parent / "fixtures" / "gemini" / "bible"
BIBLE = B.load_characters(FIX)
LOCS = B.load_locations(FIX)

STYLE = "16:9 illustration. Invented test style."
MAREN = "tall woman in her thirties, short dark hair, grey eyes, a long blue wool coat"
TOBIAS = "lean young man, curly red hair, freckles, a patched brown jacket"
HARL = "stooped old man, white beard, weathered face, a faded green sweater"
QUAY_BLOCK = ("A long stone quay curving around a small grey harbour. Rowing boats tied to iron rings along the edge, "
              "stacks of lobster pots, coils of rope, a row of tall iron lamp posts, a squat bell tower at the far "
              "end and low whitewashed houses with slate roofs behind.")
QUAY_SETTING = ("Outdoors, at dawn. Open sky above with the first pale light at the horizon; the sun is not up high; "
                "no lamps lit unless listed.")
LOFT_BLOCK = ("A long attic room above the boat shed. Bare wooden beams, nets hung from hooks along both walls, a "
              "workbench under a round window, wooden floats in baskets, a narrow ladder in the floor and one oil "
              "lamp on a chain above the bench.")
CLOSING_PEOPLE = ("Exactly one of each named person. No text, speech bubbles, captions or borders. Hands are drawn "
                  "simply and correctly, five fingers each. Any page, book, letter or sign is blank, with no writing.")
CLOSING_NOBODY = ("Nobody in the picture. No text, speech bubbles, captions or borders. "
                  "Any page, book, letter or sign is blank, with no writing.")
FLOW_FOOTER_WORDS = ("STAGING", "CONTINUITY", "FRAMING", "SAFE AREA", "WIDE shot", "Keep every attached character")


def cast(key, var="base", position="centre", pose="standing", hands="empty, at the sides", mood="calm", wearing=None):
    return {"key": key, "var": var, "position": position, "pose": pose, "hands": hands, "mood": mood,
            "wearing": wearing}


def scene(i, group, shot, cast_, action="Maren looks over the water.", others=""):
    return {"id": i, "para": 1, "group": group, "shot": shot, "cast": cast_, "others": others,
            "action": action, "focus": [0.5, 0.5], "characters": [c["key"] for c in cast_], "scene": action}


DOC = {
    "version": 2, "chapter": 1,
    "groups": {
        "g01": {"loc": "harbour_quay", "time": "dawn", "where": "outdoor", "light": "pale light from the left",
                "weather": "clear", "present": ["maren_vale", "tobias_reed", "old_harl"],
                "staging": "Maren at the quay edge on the left, Tobias walking in from the right, Harl by his boat"},
        "g02": {"loc": "net_loft", "time": "dusk", "where": "indoor", "light": "oil lamp light",
                "weather": "heavy rain", "present": ["maren_vale"], "staging": "Maren at the workbench"},
    },
    "scenes": [
        scene(1, "g01", "wide", [cast("maren_vale", position="left, on the quay edge", pose="standing",
                                      hands="empty, resting on the stone wall")]),
        scene(2, "g01", "medium",
              [cast("maren_vale", position="left foreground, by the rail", pose="turning",
                    hands="empty, at her sides", mood="wary"),
               cast("tobias_reed", position="right, a step behind", pose="walking in",
                    hands="holding nothing, arms swinging", mood="cheerful", wearing="a borrowed grey scarf")],
              action="Maren turns toward Tobias as he walks along the quay."),
        scene(3, "g01", "over-the-shoulder",
              [cast("maren_vale", position="left", pose="facing Harl", hands="empty"),
               cast("old_harl", position="right", pose="sitting by his boat", hands="both hands on his knees",
                    mood="grave")], others="two gulls on a post"),
        scene(4, "g02", "detail", [cast("maren_vale", position="centre", pose="bent over the bench",
                                        hands="one hand on a float", mood="focused")]),
        scene(5, "g01", "establishing", []),
    ],
}


def build(i, doc=DOC, chapter=1, **kw):
    s = next(x for x in doc["scenes"] if x["id"] == i)
    return K.klein_prompt(s, doc, chapter, BIBLE, LOCS, **kw)


# ------------------------------------------------------------------ the picture prompt

def test_golden_two_people_with_references():
    want = "\n\n".join([
        STYLE,
        "<ref_image_0> is Maren Vale. <ref_image_1> is Tobias Reed. <ref_image_2> is the place.",
        f"PLACE: Harbour Quay. {QUAY_SETTING} Keep the place of <ref_image_2>: the same walls, ground, furniture and "
        "light.",
        f"- Maren Vale (<ref_image_0>): {MAREN}. left foreground, by the rail; turning; mood: wary.\n"
        f"- Tobias Reed (<ref_image_1>): {TOBIAS}. right, a step behind; walking in; hands: holding nothing, arms "
        "swinging; mood: cheerful. Wearing in this picture: a borrowed grey scarf.",
        "SHOT: medium shot from the chest up, the person large in the frame, hands out of the frame unless they hold "
        "something.",
        "SCENE: Maren turns toward Tobias as he walks along the quay.",
        CLOSING_PEOPLE]) + "\n"
    r = build(2)
    assert r["text"] == want
    assert r["refs"] == [{"role": "character", "key": "maren_vale", "var": "base", "display": "Maren Vale"},
                         {"role": "character", "key": "tobias_reed", "var": "base", "display": "Tobias Reed"},
                         {"role": "place", "group": "g01"}]
    assert r["seed"] == K.seed_for(1, "s02")
    assert len(r["text"]) < 1300


def test_each_character_has_exactly_one_reference():
    r = build(2)
    chars = [x for x in r["refs"] if x["role"] == "character"]
    assert [(x["key"], x["var"]) for x in chars] == [("maren_vale", "base"), ("tobias_reed", "base")]
    assert r["refs"][-1] == {"role": "place", "group": "g01"} and sum(x["role"] == "place" for x in r["refs"]) == 1
    # one sentence per reference, one "(<ref_image_n>)" per person line; nobody is named twice
    assert r["text"].count("<ref_image_0> is") == 1 and r["text"].count("(<ref_image_0>)") == 1
    assert r["text"].count("<ref_image_1> is") == 1 and r["text"].count("(<ref_image_1>)") == 1
    assert r["text"].count("Maren Vale") == 2 and r["text"].count("Tobias Reed") == 2    # sentence + line each


def test_a_person_listed_twice_with_the_same_variant_gets_one_reference():
    s = scene(1, "g01", "medium", [cast("maren_vale", position="left"), cast("maren_vale", position="right")])
    r = K.klein_prompt(s, DOC, 1, BIBLE, LOCS)
    assert [x["role"] for x in r["refs"]] == ["character", "place"]


def test_text_only_person_gets_no_reference_and_is_described():
    r = build(3)
    assert [x.get("key") or x["role"] for x in r["refs"]] == ["maren_vale", "place"]
    t = r["text"]
    assert "<ref_image_0> is Maren Vale. <ref_image_1> is the place." in t
    assert f"- Old Harl: {HARL}. right; sitting by his boat; hands: both hands on his knees; mood: grave." in t
    assert "Old Harl (" not in t and "<ref_image_2>" not in t
    assert "Keep the place of <ref_image_1>:" in t                         # numbering follows the real references
    assert "Background: two gulls on a post, small and blurred." in t


def test_empty_cast():
    r = build(5)
    assert r["refs"] == [{"role": "place", "group": "g01"}]
    want = "\n\n".join([
        STYLE,
        "<ref_image_0> is the place.",
        f"PLACE: Harbour Quay. {QUAY_SETTING} Keep the place of <ref_image_0>: the same walls, ground, furniture and "
        "light.",
        "SHOT: establishing view of the whole place, no person in focus.",
        "SCENE: Maren looks over the water.",
        CLOSING_NOBODY]) + "\n"
    assert r["text"] == want
    assert "\n- " not in r["text"]


def test_closing_lines():
    assert K.CLOSING == CLOSING_PEOPLE and K.CLOSING_EMPTY == CLOSING_NOBODY
    for i in (1, 2, 3, 4):
        assert build(i)["text"].endswith("\n\n" + CLOSING_PEOPLE + "\n")
    assert build(5)["text"].endswith("\n\n" + CLOSING_NOBODY + "\n")
    assert "five fingers" not in build(5)["text"]                      # nobody in the picture: no hands line
    for i in range(1, 6):
        assert build(i)["text"].count("Any page, book, letter or sign is blank, with no writing.") == 1
    assert "blank" not in K.plate_prompt("g01", DOC, 1, BIBLE, LOCS)["text"]


def test_hands_piece_is_left_out_when_empty_and_kept_otherwise():
    def line(hands):
        s = scene(1, "g01", "medium", [cast("maren_vale", position="left", pose="standing", hands=hands, mood="calm")])
        return next(l for l in K.klein_prompt(s, DOC, 1, BIBLE, LOCS)["text"].splitlines() if l.startswith("- "))
    assert line("empty, at her sides").endswith(f"{MAREN}. left; standing; mood: calm.")
    assert "hands" not in line("empty, at her sides")
    for text in ("empty", "Empty-handed", "  EMPTY, relaxed. ", ""):
        assert "hands" not in line(text), text
    assert line("holding one card").endswith(f"{MAREN}. left; standing; hands: holding one card; mood: calm.")
    assert "hands: holding nothing" in line("holding nothing")        # only a leading "empty" drops the piece


def test_detail_with_a_cast_is_a_medium_close_shot_and_keeps_the_references():
    r = build(4)                                                       # scene 4: detail, Maren in the cast
    assert "\n\nSHOT: " + K.DETAIL_WITH_PERSON + ".\n\n" in r["text"]
    assert K.DETAIL_WITH_PERSON == ("medium close shot from the chest up: the person holds the object in front of their "
                                    "chest, face and object both clearly in the frame")
    assert "close view of the object alone" not in r["text"] and "no hands" not in r["text"]
    assert [x["role"] for x in r["refs"]] == ["character", "place"]
    assert r["refs"][0]["key"] == "maren_vale"
    assert "<ref_image_0> is Maren Vale. <ref_image_1> is the place." in r["text"]
    assert "(<ref_image_0>)" in r["text"] and "hands: one hand on a float" in r["text"]


def test_detail_with_an_empty_cast_is_the_object_alone():
    s = scene(1, "g01", "detail", [], action="A brass key lies on the quay stones.")
    r = K.klein_prompt(s, DOC, 1, BIBLE, LOCS)
    shot = "close view of the object alone, filling most of the frame, no people and no hands"
    assert f"\n\nSHOT: {shot}.\n\n" in r["text"]
    assert K.DETAIL_WITH_PERSON not in r["text"]
    assert r["refs"] == [{"role": "place", "group": "g01"}] and r["text"].endswith(CLOSING_NOBODY + "\n")


def test_plate_off_has_no_place_reference_and_carries_the_block():
    r = build(2, plate=False)
    assert [x["role"] for x in r["refs"]] == ["character", "character"]
    t = r["text"]
    assert "<ref_image_2>" not in t and "the place." not in t and "Keep the place" not in t
    assert f"PLACE: Harbour Quay. {QUAY_SETTING} {QUAY_BLOCK}\n" in t
    none = build(5, plate=False)
    assert none["refs"] == [] and "<ref_image" not in none["text"]
    assert none["text"].split("\n\n")[1].startswith("PLACE: Harbour Quay.")      # no reference paragraph at all


@pytest.mark.parametrize("i", [1, 2, 3, 4, 5])
def test_never_staging_or_the_flow_footer(i):
    assert DOC["groups"][DOC["scenes"][i - 1]["group"]]["staging"]                  # the group does have one
    t = build(i)["text"]
    for word in FLOW_FOOTER_WORDS:
        assert word not in t
    assert "Maren at the" not in t and "walking in from the right" not in t         # no staging text either
    t2 = build(i, plate=False)["text"]
    for word in FLOW_FOOTER_WORDS:
        assert word not in t2


def test_pictures_of_one_group_share_the_place_paragraph_and_the_look_prefix():
    paras = {i: build(i)["text"].split("\n\n") for i in (1, 2, 5)}
    place = {i: next(p for p in ps if p.startswith("PLACE:")) for i, ps in paras.items()}
    # identical word for word, except the number of the plate picture (the last reference: 1, 2 and 0 here)
    norm = {i: p.replace(f"<ref_image_{len(build(i)['refs']) - 1}>", "<ref_image_N>") for i, p in place.items()}
    assert len(set(norm.values())) == 1 and "<ref_image_N>:" in norm[1]
    assert [p.encode() for p in norm.values()] == [norm[1].encode()] * 3
    assert len(set(place.values())) == 3

    def prefix(i):
        line = next(l for l in build(i)["text"].splitlines() if l.startswith("- Maren Vale"))
        return line.split(": ")[0] + ": " + MAREN + "."
    # the same person: the same "- name (<ref_image_n>): look." start, whatever the scene
    one = next(l for l in build(1)["text"].splitlines() if l.startswith("- Maren Vale"))
    two = next(l for l in build(2)["text"].splitlines() if l.startswith("- Maren Vale"))
    assert one.startswith(f"- Maren Vale (<ref_image_0>): {MAREN}.")
    assert two.startswith(f"- Maren Vale (<ref_image_0>): {MAREN}.")
    assert prefix(1) == prefix(2)


def test_variant_look_and_wearing_and_empty_pieces():
    s = scene(1, "g01", "low-angle", [cast("maren_vale", var="storm_coat", position="", pose="",
                                           hands="gripping nothing", mood="stern",
                                           wearing="a hood pulled up.")])
    r = K.klein_prompt(s, DOC, 2, BIBLE, LOCS)
    line = next(l for l in r["text"].splitlines() if l.startswith("- "))
    assert line == ("- Maren Vale (<ref_image_0>): tall woman in her thirties, short dark hair, grey eyes, a heavy yellow "
                    "oilskin coat. hands: gripping nothing; mood: stern. Wearing in this picture: a hood pulled up.")
    assert ";;" not in r["text"] and "; ;" not in r["text"]
    assert r["refs"][0]["var"] == "storm_coat"


def test_placeholder_others_adds_no_background_line():
    for word in ("empty", "None.", "no one", "n/a", ""):
        s = scene(3, "g01", "medium", [cast("maren_vale")], others=word)
        assert "Background" not in K.klein_prompt(s, DOC, 1, BIBLE, LOCS)["text"]


def test_scene_is_one_line_and_falls_back_to_scene_text():
    s = scene(1, "g01", "wide", [cast("maren_vale")], action="Line one\nline two  SCENE: again")
    t = K.klein_prompt(s, DOC, 1, BIBLE, LOCS)["text"]
    assert "SCENE: Line one line two scene: again\n" in t and t.count("SCENE:") == 1
    s = dict(scene(1, "g01", "wide", [cast("maren_vale")]), action="", scene="Fallback text.")
    assert "SCENE: Fallback text.\n" in K.klein_prompt(s, DOC, 1, BIBLE, LOCS)["text"]


def test_text_ends_with_one_newline_and_has_no_double_blank_lines():
    for i in range(1, 6):
        t = build(i)["text"]
        assert t.endswith(".\n") and not t.endswith("\n\n") and "\n\n\n" not in t


# ------------------------------------------------------------------ labels

def test_changing_ref_label_changes_every_mention(monkeypatch):
    monkeypatch.setattr(K, "REF_LABEL", "pic#{n}")
    t = build(2)["text"]
    assert "pic#0 is Maren Vale. pic#1 is Tobias Reed. pic#2 is the place." in t        # 0-based, never capitalised
    assert "Maren Vale (pic#0)" in t and "Tobias Reed (pic#1)" in t
    assert "Keep the place of pic#2:" in t
    assert "image" not in t.lower()
    assert "image" not in build(3)["text"].lower() and "image" not in build(5)["text"].lower()


# ------------------------------------------------------------------ seeds

def test_default_label_is_the_comfyui_token_and_matches_kcomfy():
    from novel import kcomfy
    assert K.REF_LABEL == "<ref_image_{n}>"
    assert [K.REF_LABEL.format(n=i) for i in range(3)] == [kcomfy.ref_token(i) for i in range(3)]


def test_seed_for_is_stable_and_differs_between_scenes():
    assert K.seed_for(24, "s01") == 959279139
    assert K.seed_for(24, "g01") == 570588047
    assert K.seed_for(24, "s01") == K.seed_for(24, "s01")
    assert K.seed_for(24, "s01") != K.seed_for(24, "s02") != K.seed_for(25, "s02")
    assert all(0 <= K.seed_for(c, f"s{n:02d}") <= 0x7FFFFFFF for c in (1, 24) for n in range(1, 60))
    assert build(1)["seed"] != build(2)["seed"] and build(2, chapter=1)["seed"] == build(2, chapter=1)["seed"]
    assert build(1)["seed"] == K.seed_for(1, "s01")


# ------------------------------------------------------------------ place plates and the plan

def test_golden_plate_prompt():
    want = "\n\n".join([
        STYLE,
        f"PLACE: Harbour Quay. {QUAY_BLOCK} {QUAY_SETTING} Light: pale light from the left. Weather: clear.",
        "Wide view of the whole place, empty: nobody in the picture. No text, captions or borders."]) + "\n"
    r = K.plate_prompt("g01", DOC, 1, BIBLE, LOCS)
    assert r == {"text": want, "seed": K.seed_for(1, "g01")}
    for word in FLOW_FOOTER_WORDS:
        assert word not in r["text"]


def test_plate_light_default_and_weather_rule():
    d = json.loads(json.dumps(DOC))
    d["groups"]["g02"].update(light="", weather="still")
    t = K.plate_prompt("g02", d, 1, BIBLE, LOCS)["text"]
    assert "Light: warm lamp light." in t and "Weather" not in t
    assert "Indoors, at dusk." in t and LOFT_BLOCK in t
    d["groups"]["g02"]["weather"] = "heavy rain"
    assert "Light: warm lamp light. Weather: heavy rain." in K.plate_prompt("g02", d, 1, BIBLE, LOCS)["text"]


def test_plan_lists_each_plate_once_in_first_use_order():
    d = {**DOC, "scenes": [DOC["scenes"][3], DOC["scenes"][0], DOC["scenes"][1], DOC["scenes"][3]]}    # g02 g01 g01 g02
    p = K.plan(d, 1, BIBLE, LOCS)
    assert [x["group"] for x in p["plates"]] == ["g02", "g01"]
    assert [x["seed"] for x in p["plates"]] == [K.seed_for(1, "g02"), K.seed_for(1, "g01")]
    assert p["plates"][1]["text"] == K.plate_prompt("g01", d, 1, BIBLE, LOCS)["text"]
    assert [(x["id"], x["n"], x["group"]) for x in p["shots"]] == [
        ("c0001-s04", 4, "g02"), ("c0001-s01", 1, "g01"), ("c0001-s02", 2, "g01"), ("c0001-s04", 4, "g02")]
    for sh in p["shots"]:
        want = build(sh["n"], d)
        assert (sh["text"], sh["refs"], sh["seed"]) == (want["text"], want["refs"], want["seed"])
    assert set(p) == {"plates", "shots"} and set(p["shots"][0]) == {"id", "n", "group", "text", "refs", "seed"}


def test_plan_ids_use_the_chapter_number():
    d = {**DOC, "scenes": [DOC["scenes"][0]]}
    assert K.plan(d, 24, BIBLE, LOCS)["shots"][0]["id"] == "c0024-s01"


# ------------------------------------------------------------------ errors and the table

def test_each_shot_name_has_its_instruction():
    assert tuple(K.KSHOT_TEXT) == S.SHOTS_V2 and len(K.KSHOT_TEXT) == 7
    assert K.KSHOT_TEXT == {
        "wide": "wide view of the place, people small and full length",
        "establishing": "establishing view of the whole place, no person in focus",
        "medium": "medium shot from the chest up, the person large in the frame, hands out of the frame unless they "
                  "hold something",
        "over-the-shoulder": "over-the-shoulder shot, both people from the chest up, one seen from behind",
        "low-angle": "low camera looking up, from the knees up",
        "high-angle": "high camera looking down, from the knees up",
        "detail": "close view of the object alone, filling most of the frame, no people and no hands",
    }
    for shot, text in K.KSHOT_TEXT.items():
        # Over-the-shoulder needs two people: with one, the model drew a second copy of them from behind.
        # Detail is the object alone: its cast is empty (with a cast it becomes DETAIL_WITH_PERSON, tested apart).
        people = {"over-the-shoulder": [cast("maren_vale"), cast("tobias_reed")],
                  "detail": []}.get(shot, [cast("maren_vale")])
        s = scene(1, "g01", shot, people)
        assert f"\n\nSHOT: {text}.\n\n" in K.klein_prompt(s, DOC, 1, BIBLE, LOCS)["text"]
    alone = scene(1, "g01", "over-the-shoulder", [cast("maren_vale")])
    assert f"\n\nSHOT: {K.KSHOT_TEXT['medium']}.\n\n" in K.klein_prompt(alone, DOC, 1, BIBLE, LOCS)["text"]


def test_unknown_shot_group_or_location_is_an_error():
    bad = scene(1, "g01", "close-up", [cast("maren_vale")])
    with pytest.raises(PromptError, match="shot 'close-up'"):
        K.klein_prompt(bad, DOC, 1, BIBLE, LOCS)
    lost = scene(1, "g99", "wide", [cast("maren_vale")])
    with pytest.raises(PromptError, match="group 'g99'"):
        K.klein_prompt(lost, DOC, 1, BIBLE, LOCS)
    with pytest.raises(PromptError, match="group 'g99'"):
        K.plate_prompt("g99", DOC, 1, BIBLE, LOCS)
    d = json.loads(json.dumps(DOC))
    d["groups"]["g01"]["loc"] = "nowhere"
    with pytest.raises(PromptError, match="place 'nowhere'"):
        build(1, d)
    with pytest.raises(PromptError, match="place 'nowhere'"):
        K.plate_prompt("g01", d, 1, BIBLE, LOCS)
    with pytest.raises(PromptError):
        K.plan(d, 1, BIBLE, LOCS)


def test_variant_outside_its_chapters_is_an_error():
    s = scene(1, "g01", "wide", [cast("maren_vale", var="storm_coat")])         # the storm coat covers chapter 2 only
    with pytest.raises(PromptError):
        K.klein_prompt(s, DOC, 1, BIBLE, LOCS)
