import json
from pathlib import Path

import pytest

from novel import bible as B

FIX = Path(__file__).parent / "fixtures" / "bible" / "old_characters.json"


def old():
    return json.loads(FIX.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- convert

def test_convert_mappings_and_place():
    chars, locs = B.convert_v1(old(), ["grey_tower"])
    assert chars["version"] == 2 and chars["style"] == "Test style"
    assert set(chars["characters"]) == {"mira_vale", "tom_reed", "old_gil"}
    assert chars["content_flags"] == {"3": "keep it clean"}
    m = chars["characters"]["mira_vale"]
    assert m["look"] == m["legacy"]["appearance"] and "lantern" in m["look"]
    assert m["props"] == [] and m["first_chapter"] == 4 and m["tier"] == 1 and m["minor"] is False
    assert m["legacy"]["outfits"] == {"25-26 inn": "blue dress"}
    assert m["legacy"]["flow_name"] == "Mira Vale"
    base = m["variants"]["base"]
    assert base == {"flow_name": "Mira Vale", "chapters": "4-", "look": None, "reason": "base",
                    "flow_made": True, "ref_version": 1}
    assert m["invented"] == ""
    # text-only character; unknown field kept under legacy; invented kept
    t = chars["characters"]["tom_reed"]
    assert t["variants"]["base"]["flow_name"] is None and t["variants"]["base"]["flow_made"] is False
    assert t["legacy"]["added_episode"] == 3 and t["invented"] == "x" and t["first_chapter"] == 56
    # unsorted chapters: smallest wins
    assert chars["characters"]["old_gil"]["first_chapter"] == 2
    assert chars["characters"]["old_gil"]["variants"]["base"]["chapters"] == "2-"
    g = locs["locations"]["grey_tower"]
    assert g["name"] == "Grey Tower" and g["block"].startswith("a tall stone tower")
    assert g["kind"] is None and g["flow_name"] == "Grey Tower" and g["flow_made"] is True
    assert locs["version"] == 1


def test_convert_empty_chapters_character():
    chars, _ = B.convert_v1(old(), [])
    assert chars["characters"]["grey_tower"]["first_chapter"] is None
    assert chars["characters"]["grey_tower"]["variants"]["base"]["chapters"] == "1-"


# ---------------------------------------------------------------- ranges / resolve

@pytest.mark.parametrize("rng,ch,exp", [
    ("4-", 3, False), ("4-", 4, True), ("4-", 999, True), ("22-24,34", 23, True), ("22-24,34", 34, True),
    ("22-24,34", 25, False), ("7", 7, True), ("7", 8, False), ("1-3, 9-", 12, True),
])
def test_chapters_cover(rng, ch, exp):
    assert B.chapters_cover(rng, ch) is exp


@pytest.mark.parametrize("bad", ["", "abc", "5-3", "1,,2", "-4"])
def test_bad_ranges(bad):
    with pytest.raises(B.BibleError):
        B.parse_ranges(bad)


def test_resolve_and_errors():
    chars, _ = B.convert_v1(old(), ["grey_tower"])
    chars["characters"]["mira_vale"]["variants"]["inn"] = {
        "flow_name": "Mira Vale - inn", "label": "inn", "chapters": "25-26", "reason": "outfit",
        "look": "blue-dress look", "flow_made": False, "ref_version": 2}
    r = B.resolve(chars, "mira_vale", "base", 5)
    assert r["flow_name"] == "Mira Vale" and not r["text_only"]
    assert B.resolve(chars, "mira_vale", "inn", 25)["look"] == "blue-dress look"
    assert B.resolve(chars, "tom_reed", "base", 56)["text_only"] is True
    with pytest.raises(B.BibleError, match="mira_vale.*'inn'.*25-26"):
        B.resolve(chars, "mira_vale", "inn", 30)
    with pytest.raises(B.BibleError, match="mira_vale.*'ghost'"):
        B.resolve(chars, "mira_vale", "ghost", 5)
    with pytest.raises(B.BibleError, match="nobody"):
        B.resolve(chars, "nobody", "base", 5)


# ---------------------------------------------------------------- prop detector
# Decision: a weapon or bag worn on the body ("a sword at his hip") IS a prop (it would be baked into the
# reference). Clothing and jewellery are not.

POSITIVE = [
    "holding a pen", "carries a lantern", "carrying two suitcases", "clutching a letter",
    "with an orange purse and tarot cards", "with a medium-sized orange purse", "a sword at his hip",
    "gripping a cane", "wields an axe", "a bag slung over her shoulder", "a rifle strapped to his back",
    "a pipe in his hand", "with a folded fan",
]
NEGATIVE = [
    "a long coat", "silver earrings", "wearing a gold necklace and a straw hat", "short black hair, green eyes",
    "with bags under her eyes", "a leather jacket and brown pants", "with a gunmetal grey scarf",
    "with a bookish expression", "wearing a ring and a ribbon",
]


@pytest.mark.parametrize("t", POSITIVE)
def test_props_positive(t):
    assert B.find_props(t), t


@pytest.mark.parametrize("t", NEGATIVE)
def test_props_negative(t):
    assert not B.find_props(t), (t, B.find_props(t))


def test_prop_report():
    chars, _ = B.convert_v1(old(), ["grey_tower"])
    rep = B.prop_report(chars)
    assert [r["key"] for r in rep] == ["mira_vale"]
    assert rep[0]["source"] == "legacy.appearance" and rep[0]["flow_made_variants"] == ["base"]
    assert any("holding" in p for p in rep[0]["phrases"])
    # a ref_version 2 base is judged on its (clean) look
    m = chars["characters"]["mira_vale"]
    m["look"] = "tall woman with red hair"
    m["variants"]["base"]["ref_version"] = 2
    assert B.prop_report(chars) == []
    # a variant with its own prop-carrying look is flagged by variant
    m["variants"]["x"] = {"flow_name": None, "chapters": "9", "look": "man carrying a bag", "flow_made": False}
    rep = B.prop_report(chars)
    assert rep[0]["var"] == "x" and rep[0]["flow_made_variants"] == []


# ---------------------------------------------------------------- apply_lines

def state():
    chars, locs = B.convert_v1(old(), ["grey_tower"])
    return {"characters": chars, "locations": locs, "names": {"Mira": "मीरा"}}


BLOCK = " ".join(["word"] * 50)


def C(**kw):
    d = {"type": "character", "key": "ned_cole", "display": "Ned Cole", "hi": "नेड कोल", "tier": 1, "minor": False,
         "role": "A smith.", "first_chapter": 62, "look": "broad man with a grey beard", "props": ["hammer"]}
    d.update(kw)
    return d


def V(**kw):
    d = {"type": "variant", "key": "mira_vale", "var": "inn_dress", "label": "inn dress", "chapters": "62-63",
         "reason": "outfit", "look": "tall woman in a blue dress"}
    d.update(kw)
    return d


def L(**kw):
    d = {"type": "location", "key": "smithy", "name": "The Smithy", "kind": "interior", "chapters": "61-63",
         "block": BLOCK, "default_light": "forge fire"}
    d.update(kw)
    return d


def run(lines, **kw):
    s = state()
    before = json.dumps(s, ensure_ascii=False)
    errs, warns = B.apply_lines(s, lines, 61, 70, **kw)
    return s, before, errs, warns


def test_apply_ok_all_types():
    s, _, errs, warns = run([C(), C(key="zed_fox", display="Zed Fox", tier=3, hi="ज़ेड फॉक्स", props=[]),
                             V(), V(key="zed_fox", var="old", label="old", chapters="65"), L(),
                             {"type": "name", "en": "Ned", "hi": "नेड"},
                             {"type": "flag", "chapters": "63-64", "note": "gore"}])
    assert errs == [] and warns == []
    ned = s["characters"]["characters"]["ned_cole"]
    assert ned["variants"]["base"] == {"flow_name": "Ned Cole", "chapters": "62-", "look": None, "reason": "base",
                                       "flow_made": False, "ref_version": 2}
    assert s["characters"]["characters"]["zed_fox"]["variants"]["base"]["flow_name"] is None
    assert s["characters"]["characters"]["zed_fox"]["variants"]["old"]["flow_name"] is None
    v = s["characters"]["characters"]["mira_vale"]["variants"]["inn_dress"]
    assert v["flow_name"] == "Mira Vale - inn dress" and v["ref_version"] == 2 and v["flow_made"] is False
    assert s["locations"]["locations"]["smithy"]["flow_made"] is False
    assert s["names"]["Ned"] == "नेड"
    assert s["characters"]["content_flags"]["63"] == "gore" and s["characters"]["content_flags"]["64"] == "gore"


def test_variant_on_character_defined_in_same_batch():
    s, _, errs, _ = run([C(), V(key="ned_cole", var="young", label="young", reason="age")])
    assert errs == [] and "young" in s["characters"]["characters"]["ned_cole"]["variants"]


ERROR_CASES = [
    ("unknown type", {"type": "wat"}, "unknown type"),
    ("missing field", {"type": "name", "en": "A"}, "missing hi"),
    ("key not snake", C(key="Ned-Cole"), "snake_case"),
    ("var not snake", V(var="Inn Dress"), "snake_case"),
    ("existing char", C(key="mira_vale"), "already exists"),
    ("near dup", C(key="mira_valee"), "near-duplicate"),
    ("existing loc", L(key="grey_tower"), "already exists"),
    ("unknown variant char", V(key="nobody"), "unknown character"),
    ("var exists", V(var="base"), "already exists"),
    ("bad reason", V(reason="fashion"), "reason"),
    ("bad chapters", V(chapters="six"), "cannot parse"),
    ("held item in look", C(look="man holding a hammer"), "held item"),
    ("own prop in look", C(look="man with a grey beard and a ring", props=["ring"]), "own prop 'ring'"),
    ("variant look prop", V(look="woman carrying a bag"), "held item"),
    ("short block", L(block="too short"), "needs 40-120"),
    ("long block", L(block=" ".join(["w"] * 121)), "needs 40-120"),
    ("hi latin", C(hi="Ned Cole"), "Devanagari"),
    ("hi mixed", C(hi="नेड Cole"), "Devanagari"),
    ("revise off", {"type": "revise", "key": "mira_vale", "look": "tall woman", "props": []}, "not allowed"),
    ("bad kind", L(kind="outdoor"), "kind"),
    ("bad tier", C(tier=4), "tier"),
]


@pytest.mark.parametrize("name,line,frag", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
def test_apply_errors(name, line, frag):
    s, before, errs, _ = run([line])
    assert any(frag in e for e in errs), errs
    assert json.dumps(s, ensure_ascii=False) == before


def test_near_duplicate_threshold():
    keys = ["tarot_lady"]
    assert B.near_duplicate("tarot_woman", keys) is None
    assert B.near_duplicate("tarotlady", keys) == "tarot_lady"
    assert B.near_duplicate("tarot_ladyy", keys) == "tarot_lady"
    assert B.near_duplicate("lumian_lee", ["aurore_lee"]) is None


def test_nothing_written_when_any_line_errors():
    s, before, errs, _ = run([C(), L(), {"type": "wat"}])
    assert errs and json.dumps(s, ensure_ascii=False) == before


def test_unknown_revise_key_and_bad_json():
    _, _, errs, _ = run([{"type": "revise", "key": "nobody", "look": "x", "props": []}], allow_revise=True)
    assert any("unknown character" in e for e in errs)
    _, _, errs, _ = run(["{not json"])
    assert any("not valid JSON" in e for e in errs)


def test_json_line_strings_accepted():
    s, _, errs, _ = run([json.dumps(C(), ensure_ascii=False)])
    assert errs == [] and "ned_cole" in s["characters"]["characters"]


def test_warnings():
    s, _, errs, warns = run([
        C(first_chapter=40), V(chapters="30-31"), V(var="b", chapters="68-75"),
        {"type": "name", "en": "Mira", "hi": "मिरा"}, {"type": "flag", "chapters": "5", "note": "n"}])
    assert errs == []
    assert len(warns) == 5, warns
    assert any("first_chapter 40" in w for w in warns)
    assert any("already maps to" in w for w in warns)
    assert s["names"]["Mira"] == "मीरा"      # the existing one is kept


def test_revise_keeps_legacy_and_flags_new_reference():
    s, _, errs, _ = run([{"type": "revise", "key": "mira_vale", "look": "tall woman with red hair",
                          "props": ["brass lantern"]}], allow_revise=True)
    assert errs == []
    m = s["characters"]["characters"]["mira_vale"]
    assert m["look"] == "tall woman with red hair" and m["props"] == ["brass lantern"]
    assert "lantern" in m["legacy"]["revisions"][0]["look"]
    assert m["needs_new_reference"] is True       # base Flow Character was made from text with a held item
    # a clean base made from clean text is not flagged
    s, _, errs, _ = run([{"type": "revise", "key": "old_gil", "look": "elderly man", "props": []}],
                        allow_revise=True)
    assert "needs_new_reference" not in s["characters"]["characters"]["old_gil"]


# ---------------------------------------------------------------- save / history / unicode

def test_save_history_and_devanagari(tmp_path):
    d = tmp_path / "bible"
    chars, locs = B.convert_v1(old(), ["grey_tower"])
    assert B.save_characters(d, chars) is None            # first save: nothing to copy
    assert B.save_characters(d, chars) is not None
    B.save_characters(d, chars)
    snaps = sorted((d / "history").glob("characters-*.json"))
    assert len(snaps) == 2
    raw = (d / "characters.json").read_text(encoding="utf-8")
    assert "मीरा वेल" in raw and "\\u09" not in raw
    assert B.load_characters(d)["characters"]["mira_vale"]["hi"] == "मीरा वेल"
    assert list(B.load_characters(d)["characters"]) == list(chars["characters"])   # key order kept
    assert not list(d.glob("*.tmp"))
    B.save_names(d, {"Mira": "मीरा"})
    assert "मीरा" in (d / "names.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------- prompts (golden text)

def test_prompt_golden():
    assert B.portrait_prompt("Anime style", "tall woman") == (
        "Anime style. Character reference: tall woman. Head and shoulders, facing the viewer, neutral "
        "expression, hands not visible, plain warm-gray background, even soft light. No props, no text, no logo.")
    assert B.body_prompt("Anime style") == (
        "Anime style. Full body, standing straight, facing the viewer, arms relaxed at the sides, hands open and "
        "empty, holding nothing, no bag, no weapon, same face, hair and outfit. Plain warm-gray background, "
        "even soft light. No text, no logo.")
