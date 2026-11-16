"""The text an open image model (FLUX.3 klein, multi-reference) receives for a v2 scene file (pure functions).

Sibling of novel.prompts, which builds the Google Flow text. The same scene file feeds both, but an earlier test
with Flow-style prompts failed in specific ways, and every rule below answers one of them:

- A body image AND a face image per character drew the same person twice: ONE reference per character.
- The place was text only, so it was redrawn differently in every picture: each group gets one "place plate"
  picture (`plate_prompt`, made first) that is attached as a reference to every picture of the group.
- The group STAGING line was repeated in every prompt and contradicted single scenes: STAGING is never sent;
  only each person's own position, pose and action.
- A fixed "WIDE shot" footer overrode close shots and long never/no lists confused the model: no Flow footer,
  tight shot texts, short positive wording.
- A person with a reference was never described, so a wrong reference could not be corrected: everyone gets
  the fixed bible `look`, word for word the same in every picture.
- A hand close-up with a person's portrait attached drew a giant extra hand in front of the person: "detail" is an
  object alone with no people; a "detail" shot with a cast becomes a medium close shot of the person holding it
  (DETAIL_WITH_PERSON). Saying what empty hands do pulled hands into the frame: they are not mentioned, and the
  medium shot keeps them out of the frame. Hands that remain get a short "drawn correctly" line, and pages and signs
  are asked to stay blank because they came out covered in scribbles.

Layout of one picture (parts joined by a blank line, ending with one newline):

    16:9 illustration. <bible style>.
    <ref_image_0> is <name>. <ref_image_1> is <name>. <ref_image_2> is the place.   (one sentence per reference)
    PLACE: <name>. <setting sentences> Keep the place of <ref_image_2>: ...        (plate off: the location block)
    - <name> (<ref_image_0>): <look>. <position>; <pose>; hands: <hands>; mood: <mood>.[ Wearing in this picture: ...]
      (the "hands:" piece is left out when the hands are empty: naming them pulls them into the frame)
    - <text-only name>: <look>. ...                                     (no reference, no "(<ref_image_n>)")
    Background: <others>, small and blurred.
    SHOT: <one short instruction>.
    SCENE: <action, one line>
    <closing line>   (with people: one of each, five-finger hands; always: any page, book, letter or sign is blank)

References are named only through REF_LABEL: FLUX.3 klein in ComfyUI addresses the attached images as
`<ref_image_0>`, `<ref_image_1>`, ... (0-based, in the order they are attached), the same token as
`kcomfy.ref_token`. The reference order is the character references in cast order, then the place plate last.
"""

from __future__ import annotations

import zlib

from novel.gemini.contract import no_others
from novel.prompts import PromptError, _cast, _locations, _one, _sentence, setting_line
from novel.stages import episode_scenes as S

# How a reference is named in the text. {n} is its 0-based position in `refs` (= the order the images are attached).
# Every mention goes through this.
REF_LABEL = "<ref_image_{n}>"

# v2 shot names (S.SHOTS_V2) -> one short instruction each. Close framing: this model draws what it is told, so the
# shot text says how much of the person is in the frame (Flow needed the opposite, see prompts.SHOT_TEXT).
KSHOT_TEXT = {
    "wide": "wide view of the place, people small and full length",
    "establishing": "establishing view of the whole place, no person in focus",
    "medium": "medium shot from the chest up, the person large in the frame, hands out of the frame unless they hold "
              "something",
    "over-the-shoulder": "over-the-shoulder shot, both people from the chest up, one seen from behind",
    "low-angle": "low camera looking up, from the knees up",
    "high-angle": "high camera looking down, from the knees up",
    "detail": "close view of the object alone, filling most of the frame, no people and no hands",
}
assert tuple(KSHOT_TEXT) == S.SHOTS_V2

# A "detail" shot that has people in its cast (KSHOT_TEXT["detail"] is for a picture with nobody in it).
DETAIL_WITH_PERSON = ("medium close shot from the chest up: the person holds the object in front of their chest, "
                      "face and object both clearly in the frame")

HEAD = "16:9 illustration."
KEEP_PLACE = "Keep the place of {ref}: the same walls, ground, furniture and light."
NO_WRITING = " Any page, book, letter or sign is blank, with no writing."
CLOSING = ("Exactly one of each named person. No text, speech bubbles, captions or borders."
           " Hands are drawn simply and correctly, five fingers each." + NO_WRITING)
CLOSING_EMPTY = "Nobody in the picture. No text, speech bubbles, captions or borders." + NO_WRITING
PLATE_CLOSING = "Wide view of the whole place, empty: nobody in the picture. No text, captions or borders."


def seed_for(chapter: int, tag: str) -> int:
    """A stable seed per picture: `tag` is `s07` for a scene, the group id for a place plate."""
    return zlib.crc32(f"c{chapter:04d}-{tag}".encode()) & 0x7FFFFFFF


def _ref(n: int) -> str:
    """The text for reference number n (0-based). Never capitalised: the token is an identifier."""
    return REF_LABEL.format(n=n)


def _style(bible: dict) -> str:
    style = _one(bible.get("style"))
    return f"{HEAD} {_sentence(style)}" if style else HEAD


def _setting(scene: dict, doc: dict, locations: dict) -> str:
    """The group's SETTING text without its label and with a capital letter ("Outdoors, at dawn. Open sky ...")."""
    text = setting_line(scene, doc, locations)
    text = text[len("SETTING:"):].strip() if text.startswith("SETTING:") else text
    return text[:1].upper() + text[1:]


def _group_and_loc(scene_id, gid, doc: dict, locations: dict) -> tuple[dict, dict]:
    g = (doc.get("groups") or {}).get(gid)
    if g is None:
        where = f"scene {scene_id}: " if scene_id is not None else ""
        raise PromptError(f"{where}group {gid!r} is not in the file's groups")
    loc = _locations(locations).get(g.get("loc"))
    if loc is None:
        raise PromptError(f"group {gid}: place {g.get('loc')!r} is not in the location registry")
    return g, loc


def _clip(text) -> str:
    """One line, without a trailing full stop or separator (pieces are joined with '; ' and given their own stop)."""
    return _one(text).rstrip(" .;,")


def _person_line(m: dict, number: int | None) -> str:
    c = m["cast"]
    head = m["display"] if number is None else f"{m['display']} ({_ref(number)})"
    look = _one(m["look"])
    pieces = [_clip(c.get("position")), _clip(c.get("pose"))]
    pieces = [p for p in pieces if p]
    hands = _clip(c.get("hands"))
    if hands and not hands.lower().startswith("empty"):   # empty hands are left out: naming them pulls them into view
        pieces.append(f"hands: {hands}")
    if _clip(c.get("mood")):
        pieces.append(f"mood: {_clip(c.get('mood'))}")
    line = f"- {head}:"
    if look:
        line += f" {_sentence(look)}"
    if pieces:
        line += " " + "; ".join(pieces) + "."
    if _clip(c.get("wearing")):
        line += f" Wearing in this picture: {_clip(c.get('wearing'))}."
    return line


def klein_prompt(scene: dict, doc: dict, chapter: int, bible: dict, locations: dict, *, plate: bool = True) -> dict:
    """One picture of a v2 scene file: {"text", "refs", "seed"}.

    refs (the order is the numbering in the text, 0-based): one {"role": "character", "key", "var", "display"} for each
    cast member with a Flow name (each (key, var) once), then, when `plate` is true, {"role": "place", "group"}.
    People without a Flow name get no reference and are described by `look` only."""
    sid = scene.get("id")
    shot = scene.get("shot")
    if shot not in KSHOT_TEXT:
        raise PromptError(f"scene {sid}: shot {shot!r} is not one of {tuple(KSHOT_TEXT)}")
    members = _cast(scene, chapter, bible)
    gid = scene.get("group")
    _, loc = _group_and_loc(sid, gid, doc, locations)

    refs: list[dict] = []
    number: dict[tuple, int] = {}
    for m in members:
        k = (m["key"], m["var"])
        if m["flow_name"] and k not in number:
            refs.append({"role": "character", "key": m["key"], "var": m["var"], "display": m["display"]})
            number[k] = len(refs) - 1
    plate_no = None
    if plate:
        refs.append({"role": "place", "group": gid})
        plate_no = len(refs) - 1

    parts = [_style(bible)]
    if refs:
        sentences = [f"{_ref(i)} is {r['display']}." if r["role"] == "character" else
                     f"{_ref(i)} is the place." for i, r in enumerate(refs)]
        parts.append(" ".join(sentences))

    place = [f"PLACE: {_clip(loc.get('name') or doc['groups'][gid]['loc'])}."]
    setting = _setting(scene, doc, locations)
    if setting:
        place.append(setting)
    if plate:
        place.append(KEEP_PLACE.format(ref=_ref(plate_no)))
    else:
        block = _one(loc.get("block"))
        if block:
            place.append(_sentence(block))
    parts.append(" ".join(place))

    lines = [_person_line(m, number.get((m["key"], m["var"])) if m["flow_name"] else None) for m in members]
    others = _clip(scene.get("others"))
    if others and not no_others(scene.get("others")):
        lines.append(f"Background: {others}, small and blurred.")
    if lines:
        parts.append("\n".join(lines))

    # An over-the-shoulder shot of ONE person made the model invent a second copy of them, seen from behind.
    shot_text = KSHOT_TEXT["medium" if shot == "over-the-shoulder" and len(members) < 2 else shot]
    if shot == "detail" and members:
        # A hand close-up plus an attached portrait made the model draw a giant extra hand in front of the person.
        shot_text = DETAIL_WITH_PERSON
    parts.append(f"SHOT: {shot_text}.")
    parts.append(f"SCENE: {_one(scene.get('action') or scene.get('scene'))}")
    parts.append(CLOSING if members else CLOSING_EMPTY)
    return {"text": "\n\n".join(parts) + "\n", "refs": refs, "seed": seed_for(chapter, f"s{sid:02d}")}


def plate_prompt(gid: str, doc: dict, chapter: int, bible: dict, locations: dict) -> dict:
    """The empty place picture of one group: {"text", "seed"}. Made first, then attached to every picture of the
    group, so the room or street is drawn once."""
    g, loc = _group_and_loc(None, gid, doc, locations)
    place = [f"PLACE: {_clip(loc.get('name') or g['loc'])}."]
    block = _one(loc.get("block"))
    if block:
        place.append(_sentence(block))
    setting = _setting({"group": gid}, doc, locations)
    if setting:
        place.append(setting)
    place.append(f"Light: {_clip(g.get('light') or loc.get('default_light'))}.")
    weather = _clip(g.get("weather"))
    if weather and weather.lower() != "still":
        place.append(f"Weather: {weather}.")
    text = "\n\n".join([_style(bible), " ".join(place), PLATE_CLOSING]) + "\n"
    return {"text": text, "seed": seed_for(chapter, gid)}


def plan(doc: dict, chapter: int, bible: dict, locations: dict) -> dict:
    """Everything one chapter needs, in drawing order (pure; file names are decided elsewhere):

    {"plates": [{"group", "text", "seed"}, ...]   one per group, in the order the scenes first use them
     "shots": [{"id": "c0024-s01", "n": scene id, "group", "text", "refs", "seed"}, ...]}   in scene order"""
    plates, shots, seen = [], [], set()
    for s in doc.get("scenes", []):
        gid = s.get("group")
        if gid not in seen:
            seen.add(gid)
            plates.append({"group": gid, **plate_prompt(gid, doc, chapter, bible, locations)})
        k = klein_prompt(s, doc, chapter, bible, locations)
        shots.append({"id": f"c{chapter:04d}-s{s['id']:02d}", "n": s["id"], "group": gid, **k})
    return {"plates": plates, "shots": shots}
