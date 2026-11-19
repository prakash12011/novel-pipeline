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

import re
import zlib

from novel.gemini.contract import no_others
from novel.prompts import PromptError, _cast, _locations, _one, _sentence, setting_line
from novel.stages import episode_scenes as S

# How a reference is named in the text. {n} is its 0-based position in `refs` (= the order the images are attached).
# Every mention goes through this.
REF_LABEL = "<ref_image_{n}>"

# v2 shot names (S.SHOTS_V2) -> one short instruction each. Close framing: this model draws what it is told, so the
# shot text says how much of the person is in the frame (Flow needed the opposite, see prompts.SHOT_TEXT).
# The camera a little closer on the far shots: small, far-away people got distorted faces (chapter 6 s41, a high-angle
# shot Qwen drew full length and small). "wide" said "people small" outright.
KSHOT_TEXT = {
    # full length, but the whole body filling the frame: "about half the picture height" still left faces small (c8 s59);
    # the user wants full figures, not a crop at the knees
    "wide": "wide view with the place around them, the people full length, filling the frame from head to feet, "
            "faces clear",
    "establishing": "establishing view of the whole place, no person in focus",
    "medium": "medium shot from the waist up, the person in front, with the place and the people around them visible "
              "behind",
    "over-the-shoulder": "over-the-shoulder shot, both people from the chest up, one seen from behind",
    "low-angle": "low camera looking up, the people full length, filling the frame from head to feet, faces clear",
    "high-angle": "high camera looking down, the people full length, filling the frame from head to feet, faces clear",
    "detail": "close view of the object alone, filling most of the frame, no people and no hands",
}
assert tuple(KSHOT_TEXT) == S.SHOTS_V2

# A "detail" shot that has people in its cast (KSHOT_TEXT["detail"] is for a picture with nobody in it).
DETAIL_WITH_PERSON = ("medium close shot from the chest up: the person holds the object in front of their chest, "
                      "face and object both clearly in the frame")

HEAD = "16:9 illustration."
KEEP_PLACE = "Keep the place of {ref}: the same walls, ground, furniture and light."
NO_WRITING = " Any page, book, letter or sign is blank, with no writing."
# The closing is plain picture text. Instruction-style sentences ("taking only who they are from the reference",
# "appears only once; every other person has a different face") helped little with two references, so they went;
# what they guarded is now said visually: one scene, five fingers, clean eyes (white specks in the irises, c0001 s28).
CLOSING = ("One single scene, not a reference sheet, no multiple views. No text, speech bubbles, captions or borders."
           " Hands with five fingers. Clean eyes, no white specks." + NO_WRITING)
# A person who has a reference picture: the picture, not the words, decides face and clothes. A text "wearing a linen
# shirt" beat a portrait with a black shirt (2026-11-16, chapter 24), so the clothes clause is dropped from the look.
CLOTHES = re.compile(r",?\s+wearing\b.*$", re.S)     # shortcut: finds only a look written with "wearing", upgrade when a look names clothes otherwise
# Two references in one picture: Qwen drew one sheet twice or put one person's face on the other (Lumian three times,
# Pierre twice, the barman with Lumian's face; c0001 s35-s60, 2026-11-18). So each person is ONE block - where they
# stand, who, which reference, how they look, pose, face - and two or more people are named as different kinds of
# person ("the strapping eighteen-year-old lad" / "the thin, skinny middle-aged village farmer").
NUMBER = {2: "TWO", 3: "THREE", 4: "FOUR"}
# Nobody in the picture: said firmly (a "detail" shot of bare ground got an invented woman with a lamp, chapter 6 s11)
CLOSING_EMPTY = ("Nobody in the picture: no person, no figure, no face and no silhouette anywhere, not even small in the "
                 "distance. No text, speech bubbles, captions or borders." + NO_WRITING)
# The references are front views (one person standing straight, facing the viewer): Qwen copied that pose, so people
# stood side by side facing the camera like a line-up instead of acting the scene (chapter 6 s41).
REF_POSE = ("The reference pictures show each person standing straight and facing the viewer: take only their face, "
            "hair, body and clothes from them, never that stance or that calm face.")
TWO_ACT = ("They are in the middle of the moment: bodies and faces turned toward each other as the scene says, not "
           "standing side by side posing for the camera.")
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


SIDE = re.compile(r"\b(?:far|center|centre|middle)?-?\s*(?:left|right)\b(?:\s+(?:side|edge|of the frame))?", re.I)


def _unname(text: str, members: list[dict], me: dict | None = None) -> str:
    """Another person of the picture by role, never by name: a name in someone else's line (or in the scene) pulled in
    that person's reference again, and Qwen drew them twice (chapter 1 s60: a second Maurice doing Ryan's gesture)."""
    for m in members:
        if m is me or not _kind(m):
            continue
        role = f"the {_kind(m)}"
        names = [m["display"]] + [w for w in m["display"].split()[:1] if len(w) > 2 and sum(
            w in o["display"].split() for o in members) == 1]   # also the first name, when only this person has it
        for n in names:
            text = re.sub(rf"\b{re.escape(n)}\b(?:'s)?", lambda x: role + ("'s" if x.group(0).endswith("'s") else ""),
                          text)
    return text


ARTICLES = {"the", "a", "an"}
LYING = re.compile(r"\b(?:lying|lies|prone|fallen|sprawled|on the (?:ground|floor|mud)|on (?:its|his|her) back)\b", re.I)


def _offstage(text: str, bible: dict, members: list[dict]) -> str:
    """A bible person named in the scene but not in the picture becomes "someone out of view": the name alone made
    Qwen draw them as an extra person (s54: "Lumian Lee waits" in a picture of Ryan and Maurice)."""
    cast = {m["key"] for m in members}
    chars = bible.get("characters") or {}
    firsts = [c["display"].split()[0] for c in chars.values() if c.get("display")]
    for key, c in chars.items():
        name = c.get("display") or ""
        if key in cast or not name:
            continue
        first = name.split()[0]
        # never the first word "The" of "The Owl": every sentence starting with "The" became "Someone out of view ..."
        # (~15% of the prompts of EP1-EP2 had it)
        alias = len(first) > 2 and firsts.count(first) == 1 and first.lower() not in ARTICLES
        for n in [name] + ([first] if alias else []):
            text = re.sub(rf"\b{re.escape(n)}\b(?:'s)?", "someone out of view", text)
    return text


def _back_person(scene: dict, members: list[dict]) -> dict | None:
    """In a two-person over-the-shoulder shot, whose back is in the foreground: the one the action sees "past", "over"
    or "behind" (only "past" was read once: c6 s44 made the wrong one the back). None, and the shot is drawn as a plain
    two-person shot, when the action names no shoulder, or when that person also acts in it: guessing the first of the
    cast drew Lumian as the back AND taking a magazine off the shelf (c9 s05)."""
    action = _one(scene.get("action") or scene.get("scene"))
    m = re.search(r"\b(?:past|over|behind)\s+(.{0,60}?)'s\s+shoulder", action, re.I)
    if not m:
        return None
    rest = action[:m.start()] + action[m.end():]
    for p in members:
        names = [n for n in (p["display"], p["display"].split()[0]) if n]
        if any(n in m.group(1) for n in names):
            return None if any(re.search(rf"\b{re.escape(n)}\b", rest) for n in names) else p
    return None


SIDE_PHRASE = re.compile(r",?\s*\b(?:on|to|at|from|in|toward|towards)\s+the\s+(?:far\s+|lower\s+|upper\s+)?"
                         r"(?:left|right)(?:-hand)?(?:\s+(?:foreground|background|side|edge|of the frame))?\b", re.I)


def _no_side_words(text: str) -> str:
    """The scene without "on the left" and the like (no place picture: the person's own block says where they are)."""
    return re.sub(r"\s+([.,;])", r"\1", SIDE_PHRASE.sub("", text)).strip()


def _capitals(text: str) -> str:
    """A sentence that now starts with "the ..." (a name replaced by a role) gets its capital back."""
    return re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)


def _no_sides(where: str) -> str:
    """The spot without left/right: with no place picture Qwen lays the room out itself, and 'far left, behind the
    bar' with the bar drawn on the right made it draw the person at both (s60)."""
    kept = [p.strip() for p in where.split(",") if p.strip() and not SIDE.search(p)]
    if kept:
        return ", ".join(kept)
    return " ".join(SIDE.sub(" ", where).split()).strip(" ,")


def _person_line(m: dict, number: int | None, members: list[dict] | None = None, sides: bool = True,
                 back: bool = False) -> str:
    """One person as one block: [WHERE: Name (<ref>) - look; pose; hands; wearing; face]. The others of the picture
    are named by role in it (`members`); `sides` False drops left/right from the position; `back`: this person is the
    over-the-shoulder back in the foreground (no face, no hands to show)."""
    me = m                                      # the member itself: its own name stays in its own line
    if back:
        # no pose either: "standing in the doorway" put the back-view person in a second place as well (c6 s44)
        c = dict(m["cast"], position="near foreground, seen from behind: only the back of the head and one shoulder",
                 pose="", mood="", hands="")
        m = dict(m, cast=c)
    c = m["cast"]
    others = members or []
    fix = lambda t: _unname(_clip(t), others, me) if len(others) > 1 else _clip(t)     # noqa: E731
    who = m["display"] if number is None else f"{m['display']} ({_ref(number)})"
    look = _one(m["look"])
    if number is not None:
        look = CLOTHES.sub("", look)
    bits = [b for b in (_clip(look), fix(c.get("pose"))) if b]
    hands = fix(c.get("hands"))
    if hands and not hands.lower().startswith("empty"):   # empty hands are left out: naming them pulls them into view
        bits.append(f"hands: {hands}")
    if _clip(c.get("wearing")):
        bits.append(f"wearing {_clip(c.get('wearing'))}")
    if fix(c.get("mood")):
        bits.append(f"face: {fix(c.get('mood'))}")
    where = fix(c.get("position"))
    if not sides:
        where = _no_sides(where)
    return f"[{where.upper() + ': ' if where else ''}{who}" + (" - " + "; ".join(bits) if bits else "") + "]"


def _kind(m: dict) -> str:
    """What kind of person, for telling two apart: the look before its first "with", whole comma pieces while they
    stay within six words ("the thin, skinny middle-aged village farmer", "the lean young man, curly red hair")."""
    out: list[str] = []
    for piece in CLOTHES.sub("", _one(m["look"])).split(" with ")[0].split(","):
        if out and sum(len(p.split()) for p in out) + len(piece.split()) > 6:
            break
        out.append(piece.strip())
    return ", ".join(p for p in out if p).rstrip(" .;")


def _people_head(members: list[dict]) -> str:
    if len(members) == 1:
        return f"One named person, drawn once: {members[0]['display']}."
    kinds = "; ".join(f"{m['display']} is the {_kind(m)}" if _kind(m) else m["display"] for m in members)
    return f"{NUMBER.get(len(members), len(members))} DIFFERENT people, each drawn once, clearly unlike each other: {kinds}."


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

    style = _style(bible)
    if not members:                             # no anime eyes asked for in a picture with nobody in it (c6 s11)
        style = style.replace(", expressive anime eyes", "").replace("expressive anime eyes, ", "")
    parts = [style]
    if plate:                                   # the people's references are named in their own blocks
        parts.append(f"{_ref(plate_no)} is the place.")

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

    back = _back_person(scene, members) if shot == "over-the-shoulder" and len(members) == 2 else None
    lines = [_person_line(m, number.get((m["key"], m["var"])) if m["flow_name"] else None, members, sides=plate,
                          back=m is back)
             for m in members]
    if members:
        lines.insert(0, _people_head(members))
        if number:                              # someone has a front-view reference: never copy its stance
            lines.append(REF_POSE)
        if len(members) > 1 and back is None:   # (an over-the-shoulder shot already sets how the two stand)
            lines.append(TWO_ACT)
    others = _clip(scene.get("others"))
    if others and not no_others(scene.get("others")):
        if len(members) > 1:
            others = _unname(others, members)
        # a group ("five thugs in vests") with no faces of its own got the named person's face and clothes: five
        # copies of Pons behind Pons (c8 s32)
        named = " or ".join(f"the {_kind(m)}" for m in members if _kind(m)) or "the named people"
        lines.append(f"In the background, smaller and plainly different from them: {others}. Each of them with a "
                     f"face, age, build, hair and clothes of their own, unlike {named} and unlike each other.")
    if lines:
        parts.append("\n".join(lines))

    # An over-the-shoulder shot of ONE person made the model invent a second copy of them, seen from behind; with two
    # and no clear back (see _back_person) it is drawn as a plain two-person shot.
    shot_text = KSHOT_TEXT["medium" if shot == "over-the-shoulder" and (len(members) < 2 or back is None) else shot]
    if shot == "detail" and members:
        # A hand close-up plus an attached portrait made the model draw a giant extra hand in front of the person.
        shot_text = DETAIL_WITH_PERSON
    elif len(members) == 1 and not (others and not no_others(scene.get("others"))):
        # "the people (around them)" with ONE named person and no extras made the model draw a second copy of them
        # (c13 s15, c14 s45): singular wording when nobody else is in the picture.
        shot_text = (shot_text.replace("the place and the people around them visible behind", "the place visible behind them")
                     .replace("the people full length", "the person full length"))
    if back is not None:
        # "one seen from behind" without saying who: Qwen drew that person's back AND the same person facing the camera
        # (chapter 3 s18, s29). The back is one of the two, named by role, never a third figure.
        front = next(m for m in members if m is not back)
        # a person lying on the ground "faces the camera, from the chest up" came out standing, beside a second copy
        # lying down (c13 s48)
        seen = ("lies on the ground beyond them, as described above, drawn once" if LYING.search(
            f"{front['cast'].get('pose') or ''} {front['cast'].get('position') or ''}") else
            "faces the camera, from the chest up")
        shot_text = (f"over-the-shoulder shot: the near foreground is the back of the {_kind(back)} (back of the head "
                     f"and one shoulder only, not drawn a second time facing the camera); the {_kind(front)} {seen}")
    parts.append(f"SHOT: {shot_text}.")
    action = _offstage(_one(scene.get("action") or scene.get("scene")), bible, members)
    if len(members) > 1:                        # the cast's names stay in their own blocks
        action = _unname(action, members)
    if not plate:                               # "on the left" in the scene against the person's own spot (s31)
        action = _no_side_words(action)
    parts.append(f"SCENE: {_capitals(action)}")
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


def plan(doc: dict, chapter: int, bible: dict, locations: dict, *, plates: bool = True) -> dict:
    """Everything one chapter needs, in drawing order (pure; file names are decided elsewhere):

    {"plates": [{"group", "text", "seed"}, ...]   one per group, in the order the scenes first use them
     "shots": [{"id": "c0024-s01", "n": scene id, "group", "text", "refs", "seed"}, ...]}   in scene order

    plates=False: no place pictures; each shot describes its place in words and the model composes it fresh."""
    if not plates:
        return {"plates": [], "shots": [
            {"id": f"c{chapter:04d}-s{s['id']:02d}", "n": s["id"], "group": s.get("group"),
             **klein_prompt(s, doc, chapter, bible, locations, plate=False)} for s in doc.get("scenes", [])]}
    plates, shots, seen = [], [], set()
    # Groups of one place at one time of day share the first group's plate: five "bedroom" groups made five different
    # rooms (chapter 24). A shot's own "group" stays; only its place reference points at the shared plate.
    first: dict[tuple, str] = {}
    share = {gid: first.setdefault((g.get("loc"), g.get("time")), gid) for gid, g in (doc.get("groups") or {}).items()}
    for s in doc.get("scenes", []):
        gid = s.get("group")
        pid = share.get(gid, gid)
        if pid not in seen:
            seen.add(pid)
            plates.append({"group": pid, **plate_prompt(pid, doc, chapter, bible, locations)})
        k = klein_prompt(s, doc, chapter, bible, locations)
        k["refs"] = [{**r, "group": pid} if r["role"] == "place" else r for r in k["refs"]]
        shots.append({"id": f"c{chapter:04d}-s{s['id']:02d}", "n": s["id"], "group": gid, **k})
    return {"plates": plates, "shots": shots}
