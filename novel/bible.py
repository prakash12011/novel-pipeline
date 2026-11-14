"""Book-level character/location bible (v2): load, save, convert, resolve, prop check, apply Gemini lines.

Files (in <book>/bible/): characters.json (version 2), locations.json (version 1), names.json.
Every save first copies the current file into bible/history/<name>-<UTC stamp>.json (never deleted).
"""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

CHARACTERS_FILE = "characters.json"
LOCATIONS_FILE = "locations.json"
NAMES_FILE = "names.json"

REASONS = ("age", "outfit", "transformation", "injury", "disguise")
SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
# Devanagari block (incl. danda, digits), ZWJ/ZWNJ, spaces and basic punctuation.
HI_OK = re.compile(r"^[ऀ-ॿ‌‍ \t,.;:!?'()\-]+$")

NEAR_DUP_MAX_DISTANCE = 2      # edit distance on keys with underscores removed
NEAR_DUP_MAX_RATIO = 0.2       # and distance / longer length


class BibleError(ValueError):
    pass


# ---------------------------------------------------------------- paths / io

def bible_dir(book: str) -> Path:
    from novel.core import paths
    return paths.bible_dir(book)


def _load(path: Path, default):
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def load_characters(d: Path) -> dict:
    return _load(Path(d) / CHARACTERS_FILE, {"version": 2, "style": "", "characters": {}, "content_flags": {}})


def load_locations(d: Path) -> dict:
    return _load(Path(d) / LOCATIONS_FILE, {"version": 1, "locations": {}})


def load_names(d: Path) -> dict:
    return _load(Path(d) / NAMES_FILE, {})


def load_state(d: Path) -> dict:
    return {"characters": load_characters(d), "locations": load_locations(d), "names": load_names(d)}


def save_json(path: Path, data) -> Path | None:
    """Atomic save (keeps key order, UTF-8, no \\u escapes). Returns the history copy, if one was made."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    snap = None
    if path.exists():
        hist = path.parent / "history"
        hist.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snap = hist / f"{path.stem}-{stamp}.json"
        snap.write_bytes(path.read_bytes())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return snap


def save_characters(d: Path, doc: dict):
    return save_json(Path(d) / CHARACTERS_FILE, doc)


def save_locations(d: Path, doc: dict):
    return save_json(Path(d) / LOCATIONS_FILE, doc)


def save_names(d: Path, doc: dict):
    return save_json(Path(d) / NAMES_FILE, doc)


def save_state(d: Path, state: dict) -> None:
    save_characters(d, state["characters"])
    save_locations(d, state["locations"])
    save_names(d, state["names"])


# ---------------------------------------------------------------- chapter ranges

def parse_ranges(text: str) -> list[tuple[int, int | None]]:
    """"22-24,34,40-" -> [(22,24),(34,34),(40,None)]. Raises BibleError when unparsable."""
    if not isinstance(text, str) or not text.strip():
        raise BibleError(f"empty chapter range {text!r}")
    out = []
    for part in text.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:(-)(\d*))?", part)
        if not m:
            raise BibleError(f"cannot parse chapter range {text!r} (bad part {part!r})")
        lo = int(m.group(1))
        if m.group(2) is None:
            hi = lo
        elif m.group(3) == "":
            hi = None
        else:
            hi = int(m.group(3))
        if hi is not None and hi < lo:
            raise BibleError(f"chapter range {text!r}: {part!r} ends before it starts")
        out.append((lo, hi))
    return out


def chapters_cover(range_str: str, chapter: int) -> bool:
    return any(lo <= chapter and (hi is None or chapter <= hi) for lo, hi in parse_ranges(range_str))


def _first_chapter_of(text) -> int | None:
    """Smallest chapter in an OLD-format `chapters` value ("", "7", "2-3,5-6", "4-6,11")."""
    nums = [int(n) for n in re.findall(r"\d+", str(text or ""))]
    return min(nums) if nums else None


# ---------------------------------------------------------------- prop detector

CARRIABLE = (
    "cards", "card", "purse", "bag", "satchel", "pouch", "backpack", "suitcase", "briefcase", "basket",
    "pen", "quill", "sword", "blade", "staff", "cane", "walking stick", "lantern", "lamp", "torch", "candle",
    "axe", "hatchet", "pitchfork", "hammer", "spear", "bow", "knife", "dagger", "gun", "shotgun", "rifle",
    "revolver", "pistol", "whip", "club", "mace", "book", "tome", "scroll", "letter", "umbrella", "parasol",
    "pipe", "cigarette", "cigar", "fan", "telescope", "mirror", "flask", "bottle", "cup", "mug", "glass",
    "wand", "shovel", "broom", "rope", "key", "keys", "coin", "coins", "doll", "flower", "flowers",
    "bouquet", "tray", "box", "bucket", "crutch", "crutches",
)
_NOUN = r"(?:" + "|".join(sorted((re.escape(n) for n in CARRIABLE), key=len, reverse=True)) + r")s?"
HELD_VERB = re.compile(
    r"\b(?:hold|holds|holding|carry|carries|carrying|clutch|clutches|clutching|clutched|grip|grips|gripping|"
    r"wield|wields|wielding|wielded)\b", re.I)
IN_HAND = re.compile(r"\bin (?:his|her|their|one|both|each)(?: \w+)? hands?\b", re.I)
# "with a/an/her/his ... <noun>": the gap may not cross , ; . and bags "under" the eyes are not props.
WITH_NOUN = re.compile(
    r"\bwith (?:a|an|her|his|their|the|two|three)\b[^.,;]{0,40}?\b" + _NOUN + r"\b(?!\s+under)", re.I)
# "a sword at his hip", "a bag over her shoulder": a carried noun placed on the body. Weapons and bags
# worn on the body count as props (they would be baked into the reference), so they belong in props.
NOUN_PLACED = re.compile(
    r"\b(?:a|an|his|her|their)\s+(?:[\w-]+\s+){0,3}" + _NOUN +
    r"\s+(?:at|on|across|over|slung|strapped|sheathed|hanging|tucked|hooked)\b", re.I)


def find_props(text: str, context: int = 0) -> list[str]:
    """Phrases in `text` that show a held or carried item. Clothing and jewellery never match.
    context > 0 appends that many following characters (for human review)."""
    out = []
    for rx in (HELD_VERB, IN_HAND, WITH_NOUN, NOUN_PLACED):
        for m in rx.finditer(text or ""):
            out.append(text[m.start():m.end() + context].strip() if context else m.group(0).strip())
    return out


def _base(ch: dict) -> dict:
    return (ch.get("variants") or {}).get("base") or {}


def _base_text(ch: dict) -> tuple[str, str]:
    """(text the base reference was made from, where it lives)."""
    b = _base(ch)
    legacy = (ch.get("legacy") or {}).get("appearance")
    if b.get("ref_version") == 1 and legacy:
        return legacy, "legacy.appearance"
    return ch.get("look") or "", "look"


def prop_report(bible: dict) -> list[dict]:
    """One finding per (character, variant) whose reference text carries a prop:
    {key, var, source, phrases, flow_made_variants}."""
    out = []
    for key, ch in (bible.get("characters") or {}).items():
        text, source = _base_text(ch)
        hits = find_props(text, 40)
        if hits:
            made = [v for v, d in (ch.get("variants") or {}).items()
                    if d.get("flow_made") and not d.get("look")]
            out.append({"key": key, "var": "base", "source": source, "phrases": hits,
                        "flow_made_variants": made})
        for var, d in (ch.get("variants") or {}).items():
            if var == "base" or not d.get("look"):
                continue
            hits = find_props(d["look"], 40)
            if hits:
                out.append({"key": key, "var": var, "source": f"variants.{var}.look", "phrases": hits,
                            "flow_made_variants": [var] if d.get("flow_made") else []})
    return out


# ---------------------------------------------------------------- convert v1 -> v2

_KNOWN_OLD = {"display", "flow_name", "hi", "tier", "minor", "chapters", "appearance", "role", "invented",
              "outfits", "flow_made"}


def convert_v1(old: dict, places=()) -> tuple[dict, dict]:
    places = set(places)
    chars: dict = {}
    locs: dict = {}
    for key, c in (old.get("characters") or {}).items():
        if key in places:
            loc = {"name": c.get("display") or key, "kind": None, "chapters": None,
                   "block": c.get("appearance", ""), "default_light": None,
                   "flow_name": c.get("flow_name"), "flow_made": bool(c.get("flow_made", False))}
            legacy = {k: v for k, v in c.items() if k not in ("display", "appearance", "flow_name", "flow_made")}
            if legacy:
                loc["legacy"] = legacy
            locs[key] = loc
            continue
        first = _first_chapter_of(c.get("chapters"))
        start = first if first is not None else 1
        legacy = {"appearance": c.get("appearance", ""), "chapters": c.get("chapters", "")}
        if c.get("outfits"):
            legacy["outfits"] = c["outfits"]
        legacy["flow_name"] = c.get("flow_name")
        for k, v in c.items():
            if k not in _KNOWN_OLD:
                legacy[k] = v
        new = {"display": c.get("display"), "hi": c.get("hi", ""), "tier": c.get("tier"),
               "minor": bool(c.get("minor", False)), "role": c.get("role", ""), "first_chapter": first,
               "look": c.get("appearance", ""), "props": []}
        if "invented" in c:
            new["invented"] = c["invented"]
        new["variants"] = {"base": {"flow_name": c.get("flow_name"), "chapters": f"{start}-", "look": None,
                                    "reason": "base", "flow_made": bool(c.get("flow_made", False)),
                                    "ref_version": 1}}
        new["legacy"] = legacy
        chars[key] = new
    doc = {"version": 2, "style": old.get("style", ""), "characters": chars,
           "content_flags": old.get("content_flags") or {}}
    if old.get("_README"):
        doc["legacy_readme"] = old["_README"]
    return doc, {"version": 1, "locations": locs}


# ---------------------------------------------------------------- resolve

def resolve(bible: dict, key: str, var: str, chapter: int) -> dict:
    ch = (bible.get("characters") or {}).get(key)
    if ch is None:
        raise BibleError(f"character '{key}' is not in the bible")
    variants = ch.get("variants") or {}
    v = variants.get(var)
    if v is None:
        raise BibleError(f"character '{key}' has no variant '{var}' (has: {', '.join(variants) or 'none'})")
    rng = v.get("chapters", "")
    if not chapters_cover(rng, chapter):
        raise BibleError(f"character '{key}' variant '{var}' covers chapters {rng}, not chapter {chapter}")
    flow = v.get("flow_name")
    return {"look": v.get("look") or ch.get("look") or "", "flow_name": flow, "text_only": flow is None}


# ---------------------------------------------------------------- reference prompts (exact wording)

def portrait_prompt(style: str, look: str) -> str:
    return (f"{style}. Character reference: {look}. Head and shoulders, facing the viewer, neutral "
            "expression, hands not visible, plain warm-gray background, even soft light. "
            "No props, no text, no logo.")


def body_prompt(style: str) -> str:
    return (f"{style}. Full body, standing straight, facing the viewer, arms relaxed at the sides, hands open "
            "and empty, holding nothing, no bag, no weapon, same face, hair and outfit. Plain warm-gray "
            "background, even soft light. No text, no logo.")


# ---------------------------------------------------------------- Gemini BIBLE lines

def _key_norm(k: str) -> str:
    return k.replace("_", "")


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def near_duplicate(key: str, existing) -> str | None:
    """The existing key that `key` is too close to, or None (see NEAR_DUP_* constants)."""
    a = _key_norm(key)
    for other in existing:
        b = _key_norm(other)
        d = _distance(a, b)
        if d <= NEAR_DUP_MAX_DISTANCE and d / max(len(a), len(b)) <= NEAR_DUP_MAX_RATIO:
            return other
    return None


REQUIRED = {
    "character": ("key", "display", "hi", "tier", "minor", "role", "first_chapter", "look", "props"),
    "variant": ("key", "var", "label", "chapters", "reason", "look"),
    "location": ("key", "name", "kind", "chapters", "block", "default_light"),
    "name": ("en", "hi"),
    "flag": ("chapters", "note"),
    "revise": ("key", "look", "props"),
}
_MAY_BE_EMPTY = ("minor", "props")


def _outside(ranges, first, last) -> bool:
    return any(lo < first or lo > last or (hi is not None and hi > last) for lo, hi in ranges)


def _check_look(look, props, errs, where):
    if not isinstance(look, str):
        errs.append(f"{where}: look must be text")
        return
    hit = find_props(look)
    if hit:
        errs.append(f"{where}: look contains a held item ({'; '.join(hit)}); move it to props")
    for p in props or []:
        if isinstance(p, str) and p.strip() and p.strip().lower() in look.lower():
            errs.append(f"{where}: look contains its own prop '{p}'")


def apply_lines(book_state: dict, lines, batch_first: int, batch_last: int,
                allow_revise: bool = False) -> tuple[list[str], list[str]]:
    """Validate every line, then apply them all to book_state (in place). Any error: nothing changes.
    book_state = {"characters": v2 doc, "locations": doc, "names": dict}. Lines are dicts or JSON strings."""
    errs: list[str] = []
    warns: list[str] = []
    work = copy.deepcopy(book_state)
    chars = work["characters"].setdefault("characters", {})
    locs = work["locations"].setdefault("locations", {})
    names = work["names"]
    flags = work["characters"].setdefault("content_flags", {})

    for n, raw in enumerate(lines, 1):
        tag = f"line {n}"
        if isinstance(raw, str):
            if not raw.strip():
                continue
            try:
                raw = json.loads(raw)
            except ValueError as e:
                errs.append(f"{tag}: not valid JSON ({e})")
                continue
        if not isinstance(raw, dict):
            errs.append(f"{tag}: not a JSON object")
            continue
        t = raw.get("type")
        if t not in REQUIRED:
            errs.append(f"{tag}: unknown type {t!r}")
            continue
        tag = f"line {n} ({t} {raw.get('key', raw.get('en', ''))})"
        missing = [f for f in REQUIRED[t]
                   if f not in raw or (raw[f] in (None, "") and f not in _MAY_BE_EMPTY)]
        if missing:
            errs.append(f"{tag}: missing {', '.join(missing)}")
            continue
        e0 = len(errs)
        for f in ("key", "var"):
            if f in raw and not (isinstance(raw[f], str) and SNAKE.match(raw[f])):
                errs.append(f"{tag}: {f} {raw[f]!r} is not snake_case")
        if "hi" in raw and not (isinstance(raw["hi"], str) and HI_OK.match(raw["hi"])):
            errs.append(f"{tag}: hi {raw['hi']!r} must be Devanagari only")
        if "props" in raw and not (isinstance(raw["props"], list) and all(isinstance(p, str) for p in raw["props"])):
            errs.append(f"{tag}: props must be a list of text")
        if len(errs) > e0:
            continue

        if t == "character":
            k = raw["key"]
            if k in chars:
                errs.append(f"{tag}: key '{k}' already exists (existing keys are never redefined)")
                continue
            near = near_duplicate(k, chars)
            if near:
                errs.append(f"{tag}: key '{k}' is a near-duplicate of existing '{near}'")
                continue
            if raw["tier"] not in (1, 2, 3) or isinstance(raw["tier"], bool):
                errs.append(f"{tag}: tier must be 1, 2 or 3")
                continue
            if not isinstance(raw["first_chapter"], int) or isinstance(raw["first_chapter"], bool):
                errs.append(f"{tag}: first_chapter must be a number")
                continue
            _check_look(raw["look"], raw["props"], errs, tag)
            if len(errs) > e0:
                continue
            if not batch_first <= raw["first_chapter"] <= batch_last:
                warns.append(f"{tag}: first_chapter {raw['first_chapter']} is outside the batch "
                             f"{batch_first}-{batch_last}")
            chars[k] = {
                "display": raw["display"], "hi": raw["hi"], "tier": raw["tier"], "minor": bool(raw["minor"]),
                "role": raw["role"], "first_chapter": raw["first_chapter"], "look": raw["look"],
                "props": list(raw["props"]),
                "variants": {"base": {"flow_name": raw["display"] if raw["tier"] in (1, 2) else None,
                                      "chapters": f"{raw['first_chapter']}-", "look": None, "reason": "base",
                                      "flow_made": False, "ref_version": 2}}}
        elif t == "variant":
            ch = chars.get(raw["key"])
            if ch is None:
                errs.append(f"{tag}: unknown character '{raw['key']}'")
                continue
            if raw["var"] in ch["variants"]:
                errs.append(f"{tag}: variant '{raw['var']}' already exists on '{raw['key']}'")
            if raw["reason"] not in REASONS:
                errs.append(f"{tag}: reason {raw['reason']!r} not in {'|'.join(REASONS)}")
            ranges = None
            try:
                ranges = parse_ranges(raw["chapters"])
            except BibleError as e:
                errs.append(f"{tag}: {e}")
            _check_look(raw["look"], ch.get("props"), errs, tag)
            if len(errs) > e0:
                continue
            if _outside(ranges, batch_first, batch_last):
                warns.append(f"{tag}: chapters {raw['chapters']} reach outside the batch {batch_first}-{batch_last}")
            tier3 = ch["tier"] == 3
            ch["variants"][raw["var"]] = {
                "flow_name": None if tier3 else f"{ch['display']} - {raw['label']}", "label": raw["label"],
                "chapters": raw["chapters"], "reason": raw["reason"], "look": raw["look"],
                "flow_made": False, "ref_version": 2}
        elif t == "location":
            k = raw["key"]
            if k in locs:
                errs.append(f"{tag}: key '{k}' already exists (existing keys are never redefined)")
                continue
            near = near_duplicate(k, locs)
            if near:
                errs.append(f"{tag}: key '{k}' is a near-duplicate of existing '{near}'")
            if raw["kind"] not in ("interior", "exterior"):
                errs.append(f"{tag}: kind must be interior or exterior")
            try:
                parse_ranges(raw["chapters"])
            except BibleError as e:
                errs.append(f"{tag}: {e}")
            words = len(str(raw["block"]).split())
            if not 40 <= words <= 120:
                errs.append(f"{tag}: block has {words} words, needs 40-120")
            if len(errs) > e0:
                continue
            locs[k] = {"name": raw["name"], "kind": raw["kind"], "chapters": raw["chapters"],
                       "block": raw["block"], "default_light": raw["default_light"],
                       "flow_name": None, "flow_made": False}
        elif t == "name":
            old = names.get(raw["en"])
            if old is None:
                names[raw["en"]] = raw["hi"]
            elif old != raw["hi"]:
                warns.append(f"{tag}: '{raw['en']}' already maps to '{old}', kept (ignored '{raw['hi']}')")
        elif t == "flag":
            try:
                ranges = parse_ranges(raw["chapters"])
            except BibleError as e:
                errs.append(f"{tag}: {e}")
                continue
            if _outside(ranges, batch_first, batch_last):
                warns.append(f"{tag}: flag for chapters {raw['chapters']} is outside the batch")
            for lo, hi in ranges:
                for c in range(lo, (lo if hi is None else hi) + 1):
                    flags[str(c)] = raw["note"]
        elif t == "revise":
            if not allow_revise:
                errs.append(f"{tag}: revise is not allowed in this batch")
                continue
            ch = chars.get(raw["key"])
            if ch is None:
                errs.append(f"{tag}: unknown character '{raw['key']}'")
                continue
            _check_look(raw["look"], raw["props"], errs, tag)
            if len(errs) > e0:
                continue
            old_text, _ = _base_text(ch)
            legacy = ch.setdefault("legacy", {})
            legacy.setdefault("revisions", []).append({"look": ch.get("look"), "props": ch.get("props", [])})
            ch["look"] = raw["look"]
            ch["props"] = list(raw["props"])
            if _base(ch).get("flow_made") and find_props(old_text):
                ch["needs_new_reference"] = True

    if not errs:
        book_state.clear()
        book_state.update(work)
    return errs, warns
