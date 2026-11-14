"""The text Flow receives for a v2 scene file (pure functions, no files, no network).

A picture belongs to a GROUP (one continuous moment in one place). Every picture of a group gets the same
PLACE and STAGING lines, word for word, so Flow draws the second picture of a conversation in the same room.
Attached people (a Flow Character, matched by exact name) are never described: their reference decides how
they look. Text-only people (no Flow name) are described from their bible `look`. Hands are always said.

Layout (see the plan, Appendix A "Assembled Flow prompt"):

    <HEADER_MARK ... art style>
    CAST (attached Flow characters, keep their look exactly): ...
    PLACE: <name>. <fixed block>. Time: ... Light: ...[ Weather: ...]
    STAGING (the same in every picture of this moment): ...
    CONTINUITY: ...                                   (2nd and later picture of a group)
    IN THE PICTURE:
    - <name>: <pose>; hands: <hands>; mood: <mood>.
    ALSO IN THE PICTURE: <text-only looks>; <others>
    WEARING (this picture only): <name>: <wearing>
    SHOT: ...
    SCENE: <action, one line>
    <footer>

Two other scripts depend on this layout: flow/gen.mjs finds Flow's "used prompt" by HEADER_MARK (it must be
the first text), and flow/adopt.mjs matches a tile by the single line after `SCENE:` (it must be one line,
and the label must not appear earlier in the file).
"""

from __future__ import annotations

from novel import bible as B
from novel.stages import episode_scenes as S

HEADER_MARK = S.HEADER_MARK

# v2 shot names (S.SHOTS_V2) -> one instruction each. Same spirit as the v1 rewrites: Flow filled the frame
# with a face when asked for close-ups, so every shot keeps the camera back and the place visible.
SHOT_TEXT = {
    "wide": "wide, camera far back, full-length figures, surroundings clearly visible",
    "establishing": ("establishing view of the whole place, camera far back and slightly high, the people small "
                     "within it, the room or street itself the main subject"),
    "medium": ("medium-wide, camera well back, each person shown from the head down to the knees at the closest, "
               "a good part of the place visible around and behind them, no face filling the frame"),
    "over-the-shoulder": ("over-the-shoulder view from behind one person, camera pulled back so both people are "
                          "seen at least from the waist up and the place stays visible, faces small in the frame"),
    "low-angle": ("low camera looking slightly up at the figures, still far back, full-length, ceiling or sky "
                  "visible above them, no face filling the frame"),
    "high-angle": ("high camera looking down on the scene from above, far back, figures small and full-length, "
                   "the floor or ground and the layout of the place clearly visible"),
    "detail": "detail of the object, still shown in its surroundings, not filling the frame",
}
assert tuple(SHOT_TEXT) == S.SHOTS_V2

CONTINUITY = ("CONTINUITY: same place, same furniture, same light as the previous picture of this moment; "
              "only the camera moves.")
FOOTER_V2_EXTRA = (" Each person holds only what is written for them under IN THE PICTURE; hands described as "
                   "empty are empty. Never add a prop that is not listed.")


class PromptError(ValueError):
    pass


def _one(text) -> str:
    """One line of text: whitespace collapsed, and no second `SCENE:` label that adopt.mjs could trip over."""
    s = " ".join(str(text or "").split())
    return s.replace("SCENE:", "scene:")


def _sentence(text: str) -> str:
    return text if text[-1:] in ".!?" else text + "."


def positions(doc: dict) -> dict[int, int]:
    """{scene id: index among the pictures of its group, in chapter order}. A group that resumes after another
    group keeps counting, so its first picture after the interruption is not index 0 (it gets CONTINUITY)."""
    seen: dict[str, int] = {}
    out = {}
    for s in doc.get("scenes", []):
        g = s.get("group")
        out[s["id"]] = seen.get(g, 0)
        seen[g] = seen.get(g, 0) + 1
    return out


def _locations(locations: dict) -> dict:
    return locations.get("locations", locations) if isinstance(locations, dict) else {}


def _cast(scene: dict, chapter: int, bible: dict) -> list[dict]:
    """Each cast member with the bible data resolved for this chapter."""
    out = []
    for c in scene.get("cast", []):
        key, var = c.get("key"), c.get("var") or "base"
        try:
            r = B.resolve(bible, key, var, chapter)
        except B.BibleError as e:
            raise PromptError(f"scene {scene.get('id')}: {e}") from e
        ch = bible["characters"][key]
        out.append({"key": key, "var": var, "display": ch.get("display") or key, "look": r["look"],
                    "flow_name": r["flow_name"], "cast": c})
    return out


def flow_names(scene: dict, chapter: int, bible: dict) -> list[str]:
    """The Flow Characters to attach to this picture, in cast order, each once."""
    out: list[str] = []
    for m in _cast(scene, chapter, bible):
        if m["flow_name"] and m["flow_name"] not in out:
            out.append(m["flow_name"])
    return out


def place_lines(scene: dict, doc: dict, locations: dict) -> tuple[str, str]:
    """(PLACE line, STAGING line): identical for every picture of one group."""
    gid = scene.get("group")
    g = (doc.get("groups") or {}).get(gid)
    if g is None:
        raise PromptError(f"scene {scene.get('id')}: group {gid!r} is not in the file's groups")
    loc = _locations(locations).get(g.get("loc"))
    if loc is None:
        raise PromptError(f"group {gid}: place {g.get('loc')!r} is not in the location registry")
    light = _one(g.get("light") or loc.get("default_light"))
    place = f"PLACE: {_one(loc.get('name') or g['loc'])}. {_sentence(_one(loc.get('block')))} " \
            f"Time: {_one(g.get('time'))}. Light: {light}."
    weather = _one(g.get("weather"))
    if weather and weather.lower() != "still":
        place += f" Weather: {weather}."
    return place, f"STAGING (the same in every picture of this moment): {_one(g.get('staging'))}"


def flow_prompt(scene: dict, doc: dict, chapter: int, bible: dict, locations: dict,
                settings: dict | None, position_in_group: int) -> str:
    """The full Flow prompt for one picture of a v2 scene file. `settings` is reserved (nothing in the
    layout is configurable yet)."""
    shot = scene.get("shot")
    if shot not in SHOT_TEXT:
        raise PromptError(f"scene {scene.get('id')}: shot {shot!r} is not one of {tuple(SHOT_TEXT)}")
    members = _cast(scene, chapter, bible)
    parts = [S.STYLE.format(style=_one(bible.get("style")))]
    names = flow_names(scene, chapter, bible)
    if names:
        parts.append("CAST (attached Flow characters, keep their look exactly): " + ", ".join(names) + ".")
    place, staging = place_lines(scene, doc, locations)
    block = [place, staging]
    if position_in_group > 0:
        block.append(CONTINUITY)
    parts.append("\n".join(block))

    if members:
        lines = ["IN THE PICTURE:"]
        for m in members:
            c = m["cast"]
            lines.append(f"- {m['display']}: {_one(c.get('pose'))}; hands: {_one(c.get('hands'))}; "
                         f"mood: {_one(c.get('mood'))}.")
    else:
        lines = ["IN THE PICTURE: no named characters."]
    also = [f"{m['display']}: {_one(m['look'])}" for m in members if not m["flow_name"]]
    if _one(scene.get("others")):
        also.append(_one(scene["others"]))
    if also:
        lines.append("ALSO IN THE PICTURE: " + "; ".join(also))
    wearing = [f"{m['display']}: {_one(m['cast'].get('wearing'))}" for m in members
               if _one(m["cast"].get("wearing"))]
    if wearing:
        lines.append("WEARING (this picture only): " + "; ".join(wearing))
    parts.append("\n".join(lines))

    parts.append(f"SHOT: {SHOT_TEXT[shot]}.")
    parts.append(f"SCENE: {_one(scene.get('action') or scene.get('scene'))}")
    parts.append(S.FOOTER + FOOTER_V2_EXTRA)
    return "\n\n".join(parts) + "\n"


def reference_plan(doc: dict, chapter: int, bible: dict) -> dict:
    """Which (key, variant) pairs with a Flow name this chapter needs.

    {"needed": [(key, var), ...], "not_made": [...], "needs_new_reference": [...]} in first-use order.
    `needs_new_reference` lists the pairs whose character (or variant) carries that flag: the old reference
    has a prop baked in, so the Flow Character must be made again.
    """
    needed, not_made, renew = [], [], []
    for s in doc.get("scenes", []):
        for c in s.get("cast", []):
            key, var = c.get("key"), c.get("var") or "base"
            if (key, var) in needed:
                continue
            try:
                r = B.resolve(bible, key, var, chapter)
            except B.BibleError as e:
                raise PromptError(f"scene {s.get('id')}: {e}") from e
            if not r["flow_name"]:
                continue
            ch = bible["characters"][key]
            v = ch["variants"][var]
            needed.append((key, var))
            if not v.get("flow_made"):
                not_made.append((key, var))
            if ch.get("needs_new_reference") or v.get("needs_new_reference"):
                renew.append((key, var))
    return {"needed": needed, "not_made": not_made, "needs_new_reference": renew}


def scene_sentence(prompt_text: str) -> str:
    """What flow/adopt.mjs extracts: the text after the first `SCENE:` up to the end of that line, trimmed."""
    return " ".join((prompt_text.split("SCENE:")[1].split("\n")[0] if "SCENE:" in prompt_text else "").split())
