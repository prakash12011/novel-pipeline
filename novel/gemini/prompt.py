"""The Gemini prompt and its source files for one batch of chapters.

`generate()` writes, under <book>/gemini/<batch>/:
    prompt.txt                     paste this into the Gemini chat app
    sources/chapters-A-B.txt       the English chapters (upload)
    sources/bible.txt              names, characters, locations from earlier batches (upload)
    sources/windows-A-B.txt        scenes mode only: the Hindi cut into numbered windows (upload)
    batch.json                     mode, chapters, frozen window parameters, hashes (read by `novel import`)
A `raw/` folder and `progress.json` in that folder are never touched.

Placeholders in the template are `{name}`: a brace, a letter or underscore, word characters, a brace
(PLACEHOLDER below). Everything else with braces is literal: the JSON schema and example lines
(a brace followed by a quote) and the picture markers {1}, {2} (a brace followed by a digit).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from novel import bible as B

PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*)\}")


class PromptError(Exception):
    """A problem the user can fix; the message says how."""


# ------------------------------------------------------------------ small helpers

def parse_chapters(spec: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", spec or "")
    if not m:
        raise PromptError(f"--chapters must look like 61-70 (or 61), not {spec!r}")
    a = int(m.group(1))
    b = int(m.group(2) or a)
    if a < 1 or b < a:
        raise PromptError(f"--chapters {spec!r}: need 1 <= first <= last")
    return a, b


def batch_id(first: int, last: int) -> str:
    width = max(3, len(str(first)), len(str(last)))
    return f"B{first:0{width}d}-{last:0{width}d}"


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fill(template: str, values: dict) -> str:
    """Replace every {name} in one pass. An unknown name is a bug in the template: fail loudly."""
    def rep(m):
        k = m.group(1)
        if k not in values:
            raise PromptError(f"internal: template placeholder {{{k}}} has no value")
        return str(values[k])
    return PLACEHOLDER.sub(rep, template)


def banned_words() -> list[str]:
    """The words of episode_scenes.BANNED as a plain list (suffix patterns lose their regex part)."""
    from novel.stages import episode_scenes as S
    m = re.search(r"\\b\((.*)\)\\b", S.BANNED.pattern)       # the alternation between the two \b
    words = []
    for alt in m.group(1).split("|"):
        w = alt.replace(r"\w*", "").replace("?", "")
        if w and w not in words:
            words.append(w)
    return words


# ------------------------------------------------------------------ chapters and episodes

def chapter_file(book_dir: Path, n: int) -> Path:
    found = sorted((Path(book_dir) / "chapters").glob(f"{n:04d}_*.txt"))
    if not found:
        raise PromptError(f"chapter {n}: no English chapter file in {Path(book_dir) / 'chapters'}. "
                          f"Fix: novel extract")
    return found[0]


def chapters_text(book_dir: Path, first: int, last: int) -> str:
    """Each chapter under `##### CHAPTER N: title #####`; the body is the file's text unchanged
    (only the file's own first line `Chapter N: title` is dropped: the header carries it)."""
    parts = []
    for n in range(first, last + 1):
        raw = chapter_file(book_dir, n).read_text(encoding="utf-8")
        head, _, rest = raw.partition("\n")
        m = re.match(rf"\s*Chapter\s+{n}\b\s*[:.\-]?\s*(.*)$", head, re.I)
        if m:
            title = m.group(1).strip()
        else:
            title, rest = "", raw
        header = f"##### CHAPTER {n}: {title} #####" if title else f"##### CHAPTER {n} #####"
        parts.append(f"{header}\n\n{rest.strip()}\n")
    return "\n".join(parts)


def episode_of(book_dir: Path) -> dict[int, Path]:
    """{chapter: episode folder} from each episode.json (start, end) under <book>/episodes/."""
    out: dict[int, Path] = {}
    for f in sorted((Path(book_dir) / "episodes").glob("E*/episode.json")):
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
            lo, hi = int(info["start"]), int(info["end"])
        except (ValueError, KeyError, TypeError):
            continue
        for n in range(lo, hi + 1):
            out.setdefault(n, f.parent)
    return out


def hindi_state(ep_root: Path | None, n: int) -> str:
    """locked | stale (changed after the lock) | unlocked (file, no lock entry) | none."""
    if ep_root is None:
        return "none"
    hf = Path(ep_root) / "hindi" / f"{n:05d}.txt"
    if not hf.exists():
        return "none"
    lf = Path(ep_root) / "hindi.lock.json"
    lock = json.loads(lf.read_text(encoding="utf-8")) if lf.exists() else {}
    want = lock.get(f"{n:05d}.txt")
    if want is None:
        return "unlocked"
    return "locked" if sha256(hf) == want else "stale"


def choose_mode(book_dir: Path, first: int, last: int, mode: str) -> tuple[str, dict[int, Path | None]]:
    eps = episode_of(book_dir)
    chapters = list(range(first, last + 1))
    state = {n: hindi_state(eps.get(n), n) for n in chapters}
    stale = [n for n, s in state.items() if s == "stale"]
    if stale:
        raise PromptError(f"chapters {stale}: the Hindi file changed after it was locked. "
                          f"Restore it (its hash is in hindi.lock.json) before writing a prompt.")
    locked = [n for n, s in state.items() if s == "locked"]
    others = [n for n in chapters if n not in locked]
    if mode == "auto":
        if locked and others:
            raise PromptError(
                f"chapters {first}-{last} are mixed: locked Hindi in {locked}, none in {others}. "
                f"Use a range where every chapter is locked (scenes) or none is (full).")
        mode = "scenes" if locked else "full"
    elif mode == "scenes" and others:
        raise PromptError(f"--mode scenes needs locked Hindi, but chapters {others} have none. "
                          f"Lock them first, or use --mode full.")
    return mode, {n: eps.get(n) for n in chapters}


# ------------------------------------------------------------------ windows

def chapter_windows(ep_root: Path, n: int) -> list[str]:
    """The window texts of chapter n, cut by the same code `check_chapter` and `cmd_brief` use for a
    scene file with pacing "7s": chapter_windows(paras, PACING) -> paced_windows(paras)."""
    from novel.stages import episode_scenes as S
    paras = S.chapter_paras(Path(ep_root), n)
    wins = S.chapter_windows(paras, S.PACING)
    return [" ".join(t for _, t in S.window_text(paras, w) if t) for w in wins]


def windows_text(per_chapter: dict[int, list[str]]) -> str:
    out = []
    for n, wins in per_chapter.items():
        out.append(f"##### CHAPTER {n}: {len(wins)} WINDOWS #####\n")
        out += [f"[w {k}] {t}\n" for k, t in enumerate(wins, 1)]
    return "\n".join(out)


# ------------------------------------------------------------------ bible.txt

def _review_entries(chars_doc: dict) -> list[dict]:
    flagged = {f["key"]: f for f in B.prop_report(chars_doc)
               if f["var"] == "base" and f["source"] == "legacy.appearance"}
    out = []
    for key, ch in (chars_doc.get("characters") or {}).items():
        legacy = ch.get("legacy") or {}
        if legacy.get("revisions"):
            continue                      # a `revise` line already cleaned this character
        outfits = legacy.get("outfits") or {}
        if outfits or key in flagged:
            out.append({"key": key, "ch": ch, "outfits": outfits,
                        "phrases": (flagged.get(key) or {}).get("phrases", [])})
    return out


def names_lines(state: dict) -> list[str]:
    """The NAMES section of bible.txt as lines."""
    chars = state["characters"].get("characters") or {}
    names = dict(state.get("names") or {})
    for ch in chars.values():
        if ch.get("display") and ch.get("hi"):
            names.setdefault(ch["display"], ch["hi"])
    lines = ["NAMES (English = Devanagari; spell exactly like this)"]
    return lines + ([f"{en} = {hi}" for en, hi in names.items()] or ["(none yet)"])


def bible_text(state: dict) -> tuple[str, bool]:
    """(text, has_review_section)."""
    chars_doc, locs_doc = state["characters"], state["locations"]
    chars = chars_doc.get("characters") or {}
    locs = locs_doc.get("locations") or {}
    lines = names_lines(state)

    lines += ["", "CHARACTER BIBLE"]
    if not chars:
        lines.append("(none yet)")
    for key, c in chars.items():
        first = c.get("first_chapter")
        lines.append(f"- key: {key} | display: {c.get('display')} | hi: {c.get('hi') or '-'} | "
                     f"tier: {c.get('tier')} | minor: {'yes' if c.get('minor') else 'no'} | "
                     f"first chapter: {'-' if first is None else first}")
        lines.append(f"  role: {c.get('role') or '-'}")
        lines.append(f"  look: {c.get('look') or '-'}")
        lines.append(f"  props: {'; '.join(c.get('props') or []) or 'none'}")
        lines.append("  variants:")
        for var, v in (c.get("variants") or {}).items():
            row = (f"    - {var} | label: {v.get('label') or var} | chapters: {v.get('chapters') or '-'} | "
                   f"reason: {v.get('reason') or '-'}")
            if var != "base" and v.get("look"):
                row += f" | look: {v['look']}"
            lines.append(row)

    lines += ["", "LOCATION REGISTRY"]
    if not locs:
        lines.append("(none yet)")
    for key, loc in locs.items():
        lines.append(f"- key: {key} | name: {loc.get('name') or key} | kind: {loc.get('kind') or '-'} | "
                     f"chapters: {loc.get('chapters') or '-'}")
        lines.append(f"  block: {loc.get('block') or '-'}")
        lines.append(f"  default light: {loc.get('default_light') or '-'}")

    review = _review_entries(chars_doc)
    if review:
        lines += ["", "TO REVIEW (described before the current rules; see the REVIEW TASK)"]
        for r in review:
            ch = r["ch"]
            legacy = ch.get("legacy") or {}
            lines.append(f"- key: {r['key']}")
            lines.append(f"  old text: {legacy.get('appearance') or ch.get('look') or '-'}")
            if r["phrases"]:
                lines.append("  held or carried items found: " + " | ".join(r["phrases"]))
            for okey, otext in r["outfits"].items():
                lines.append(f"  old outfit note [{okey}]: {otext}")
    return "\n".join(lines) + "\n", bool(review)


# ------------------------------------------------------------------ the prompt

SCHEMA_NOTE = ('(Schema descriptions, not literal values: <...> is a placeholder and a|b means "one of". '
               "Your own lines are real JSON.)")

ROLE = """ROLE
You are the writer and the art director for the YouTube channel "{channel}". We turn the novel "{title}" into long
Hinglish story episodes: a narrator reads your Hindi text aloud and a picture stays on screen for every few seconds of
narration. In this conversation you work on chapters {first} to {last}.
"""

SOURCES = """SOURCES (attached)
- "{chapters_file}": the English chapters {first}-{last}. The story comes only from these chapters.
{windows_line}- "{bible_file}": what earlier batches decided: the NAMES list, the CHARACTER BIBLE and the LOCATION REGISTRY.
  Reuse these keys and spellings exactly. Never rename, re-describe or delete an existing key.
"""

WINDOWS_LINE = ('- "{windows_file}": the Hindi narration of each chapter, already final, cut into numbered windows.\n')

HOW_FULL = """HOW WE WORK
1. Now: reply with the BIBLE block for the whole batch, and nothing else.
2. Each time I write "next": the next chapter in order: its HINDI block, then its IMAGES block.
3. If a reply would get too long, finish the block you are in, write on its own line  >>> MORE c{ex4} IMAGES  and stop.
   When I write "next", send that block.
4. If I write "repeat c{ex4} IMAGES" or paste a FIX list, answer with exactly the blocks I ask for.
Never stop inside a block. Write nothing outside blocks except a ">>> MORE" line.
"""

HOW_SCENES = HOW_FULL.replace(
    '2. Each time I write "next": the next chapter in order: its HINDI block, then its IMAGES block.',
    '2. Each time I write "next": only the IMAGES block of the next chapter in order (the Hindi is already final).')

BLOCK_FORMAT = """BLOCK FORMAT (a tool reads it; follow it exactly)
A block starts with the line  ===== BEGIN <KIND> <ID> -> inbox/<file> =====  and ends with  ===== END <KIND> <ID> =====
  BIBLE {batch}   -> inbox/bible-{batch}.jsonl
{hindi_kind}  IMAGES c{ex4}    -> inbox/c{ex4}.images.jsonl
BIBLE and IMAGES blocks: inside a ```jsonl code fence, one JSON object per line, double quotes, no comments,
no trailing commas.{hindi_plain}
"""

HINDI_KIND = "  HINDI c{ex4}     -> inbox/c{ex4}.hindi.txt      (4-digit chapter number)\n"
HINDI_PLAIN = " The HINDI block is plain text, not in a code fence."

PART_A_FULL_TEXT = """PART A - THE NARRATION (HINDI block)
- Retell the chapter as a Hinglish narration for friends and family. Write the narration as a detailed recap of about
  {target} Hindi words (between {low} and {high} words). Leave out character descriptions (looks, clothes, background
  about who someone is) and other side information that does not move the story; keep every plot event, decision,
  reveal, clue and line of dialogue that matters. Action scenes stay detailed: tell them move by move, in order. Add
  nothing that is not in the chapter.
- Cut repeated inner thoughts and filler.
- ACTION SCENES (fights, chases, traps, escapes, powers in use): never summarise. Tell them blow by blow, as if live:
  where everyone stands, what each does next and why, which weapon, trick or power, how the enemy reacts, near misses,
  what goes wrong and exactly how it ends. One move per sentence, in order. These parts get the most words.
- Sexual content and nudity: one plain sentence that it happened, no physical detail.
- Language: {HINGLISH}
- Storyteller voice: short spoken sentences, mostly present tense, a natural "तो...", "अब देखो...", "सोचो..." now
  and then. Dialogue is retold by the narrator ("<नाम> कहता है कि ..."), no quotation marks.
- Paragraphs: one idea in two or three short sentences (about 150-250 characters), separated by one blank line.
- Numbers as Hindi words (तीन दिन), never digits. Devanagari only: no Latin letters at all.
- Names exactly as in the NAMES list. A new name: spell it as it sounds, add it to the BIBLE as a "name" line,
  and keep that spelling everywhere.
- First line: चैप्टर <chapter number in Hindi words>: <title in Hinglish>. No other headings, notes or summaries.
- End each chapter on a small hook.
"""

PICTURE_MARKERS = """- PICTURE MARKERS: put {1}, {2}, {3} ... in the text where a new picture starts. The first paragraph after the
  heading starts with {1}. Put a new marker whenever what we should SEE changes (new place, new person acting or
  speaking, new action, a jump in time), and at the latest every two sentences (about {paced} seconds, ~{chars} letters).
  A marker always stands at the start of a sentence, never inside one, never two in a row. Numbers run from 1 in
  every chapter with no gaps. The heading line has no marker.
"""

PART_A_FULL = PART_A_FULL_TEXT + PICTURE_MARKERS

PART_A_SCENES = """PART A - THE NARRATION (already final)
The Hindi narration is final; do not rewrite it. The attached windows file cuts each chapter into numbered WINDOWS of
about {paced} seconds. Write exactly one image line per window: "n" is the window number. Read the English chapter for
visual detail (place, who is there, what things look like) and the Hindi window for what is being said at that moment.
Windows per chapter:
{window_list}
"""

PART_B = """PART B - THE PICTURES (IMAGES block). One line per GROUP and one line per IMAGE, in story order.
A GROUP is one continuous moment in ONE place: a conversation, a fight, a walk down one street. Every image of a group
happens in the same place, at the same time of day, in the same light; only the camera and the poses change. Start a
new group only when the place or the time changes, or for a flashback or dream.
""" + SCHEMA_NOTE + """
  {"type":"group","group":"g01","loc":"<location key>","time":"dawn|day|dusk|night","where":"indoor|outdoor",
   "light":"...","weather":"...","present":["<character keys in this place>"],
   "staging":"where each person is in the place and what is between them, e.g. one at the corner table by the
              window facing the room; the other opposite, back to the bar; a candle between them"}
  {"type":"image","n":<{n_label}>,"group":"g01","shot":"<shot>",
   "cast":[{"key":"<character key>","var":"<variant key, usually base>","position":"<where in the frame and the place>",
            "pose":"...","hands":"...","mood":"...","wearing":null}],
   "others":"unnamed people in the picture, or empty","action":"<English, 150-450 characters>","focus":[x,y]}
Rules:
- {first_rule}
- "time" and "where" are required on EVERY group line. "where" is "indoor" when the camera stands inside a building,
  cave, tent or vehicle, and "outdoor" when it stands in the open (street, yard, field, roof, shore). Decide it for
  each group from the narration of that moment, NOT from the location's kind: one location can have both an inside and
  an outside (a house and its yard). A doorway or a window view is whichever side the camera stands on. "time" says
  when it happens. "light" must agree with both: no sunlight at night; no moon or stars indoors unless a window is
  mentioned; by day outdoors the light is the sky, not lamps.
- "present" (group): EVERY named character who is in this place during the group, by key, listed first. It is required
  and not empty, unless the group truly has nobody (an empty street): then it is [] and every image of the group has
  an empty cast.
- "cast": the people VISIBLY in the picture (at most {max_characters}), by key. Everyone in "cast" must be in the
  group's "present": a cast member who is not listed in "present" makes the whole block be rejected. Someone only
  mentioned, remembered or heard is not in the picture. Fewer people is better: show who the moment is about. A
  window in which nobody is visible (a description of a place, an object, a letter) gets an empty cast [] and an
  "establishing" or "detail" shot.
- "position" (each cast member): where that person is in the frame and in the place, in plain words: "left foreground,
  by the door", "centre, seated at the table", "far right, in the background". Required. Two people in one picture
  never share a position, and the positions stay the same inside a group unless the narration moves someone.
- "var": the variant the bible defines for this chapter (a variant applies only inside its chapter range).
- "hands": for EVERY cast member say what the hands do or hold: "empty, at her sides", "gripping an axe",
  "holding one card". A prop appears ONLY when the narration of that moment needs it. Never give someone their
  signature prop by habit: a fortune teller does not hold cards unless she is using them in this moment.
- "wearing": null almost always (the reference decides clothes). Only for a one-off change inside one group
  (pyjamas when waking). A lasting change is a BIBLE variant, not "wearing".
- "shot": one of wide, establishing, medium, over-the-shoulder, low-angle, high-angle, detail. Inside a group, change
  the camera between images; never the same shot three times in a row; "medium" for at most a third.
- "action": English. What is VISIBLE at that moment: who does what, where, with what expression, using display
  names, never keys. Do not describe characters' clothes or faces. Cards, letters, books and signs show no readable
  words. Family-friendly: violence is the threat, the swing, the impact or a silhouette, never wounds; never the
  words {BANNED}. Everyone fully clothed.
- ACTION SCENES: each image is one beat (who strikes, from where, with what; how the other reacts). Keep the geography
  of the fight identical inside the group: the same person stays on the same side unless the text moves them.
- "focus": where the main subject is, [0..1, 0..1] from the top-left.
"""


def _swap(text: str, old: str, new: str) -> str:
    """text with `old` replaced by `new`; asserts `old` is there, so a later edit of PART_B cannot silently
    break a variant built from it."""
    assert old in text, f"PART_B changed: cannot find {old[:50]!r} to swap"
    return text.replace(old, new)


def _tight_part_b(part_b: str) -> str:
    """PART_B for an image model that does better with a close camera and at most two people (framing "tight"):
    only the cast, position, shot and action bullets change; the schema lines and field names stay the same."""
    part_b = _swap(
        part_b,
        '''Someone only
  mentioned, remembered or heard is not in the picture. Fewer people is better: show who the moment is about. A
  window in which nobody is visible (a description of a place, an object, a letter) gets an empty cast [] and an
  "establishing" or "detail" shot.''',
        '''Someone only
  mentioned, remembered or heard is not in the picture. The camera is CLOSE in this series: one person per picture is
  best, two only when the moment is between those two; show who the moment is about. Never a crowd: anyone else goes in
  "others", as blurred background at most. A window in which nobody is visible (a description of a place, an object, a
  letter) gets an empty cast [] and an "establishing" or "detail" shot.''')
    part_b = _swap(
        part_b,
        '''- "position" (each cast member): where that person is in the frame and in the place, in plain words: "left foreground,
  by the door", "centre, seated at the table", "far right, in the background". Required. Two people in one picture
  never share a position, and the positions stay the same inside a group unless the narration moves someone.''',
        '''- "position" (each cast member): where that person is in the FRAME only, in plain words: "left", "centre" or "right",
  plus "in front" or "behind" when two people overlap in depth. Required. Two people in one picture never share a
  position, and each person keeps their side inside a group unless the narration moves them.''')
    part_b = _swap(
        part_b,
        '''- "shot": one of wide, establishing, medium, over-the-shoulder, low-angle, high-angle, detail. Inside a group, change
  the camera between images; never the same shot three times in a row; "medium" for at most a third.''',
        '''- "shot": one of wide, establishing, medium, over-the-shoulder, low-angle, high-angle, detail. The camera stays close.
  "medium" is one or two people from the waist up and is the normal shot. "over-the-shoulder" is two people from the
  chest up, one of them seen from behind. "low-angle" and "high-angle" are one person from the knees up. "detail" is an
  OBJECT ALONE: its cast is empty [], and no person and no hand is in the picture; when a person holds or uses the
  object, use "medium" instead. "wide" and "establishing" are ONLY for a window with an empty cast, or for the first
  image of a group. Inside a group, change the camera between images; never the same shot three times in a row.''')
    return _swap(
        part_b,
        '''- "action": English. What is VISIBLE at that moment: who does what, where, with what expression, using display
  names, never keys. Do not describe characters' clothes or faces. Cards, letters, books and signs show no readable
  words. Family-friendly: violence is the threat, the swing, the impact or a silhouette, never wounds; never the
  words {BANNED}. Everyone fully clothed.''',
        '''- "action": English. What is VISIBLE at that moment: who does what, where, with what expression, using display names,
  never keys. Name the spot inside the place where this picture happens (by the oven, at the doorway, beside the fallen
  pillar): the image model is told nothing else about where people stand. Do not describe characters' clothes or faces.
  Cards, letters, books and signs show no readable words. Family-friendly: violence is the threat, the swing, the
  impact or a silhouette, never wounds; never the words {BANNED}. Everyone fully clothed.''')


PART_B_TIGHT = _tight_part_b(PART_B)

FIRST_RULE_FULL = ("Exactly one image line for every marker ({1}, {2}, ...) in the HINDI block, same numbers.")
FIRST_RULE_SCENES = "Exactly one image line for every window, same numbers."

PART_C = """PART C - THE BIBLE (BIBLE block, JSON Lines). Only what is NEW in chapters {first}-{last}:
""" + SCHEMA_NOTE + """
  {"type":"character","key":"snake_case","display":"...","hi":"Devanagari","tier":1|2|3,"minor":false,
   "role":"one sentence, no spoilers past these chapters","first_chapter":N,
   "look":"for an illustrator: age, build, face, hair, eyes, usual clothes. NOTHING in the hands, no bag, no weapon,
           no cards. Neutral.","props":["things they often carry, listed here and never in look"]}
  {"type":"variant","key":"<existing character key>","var":"snake_case","label":"short label","chapters":"22-24,34",
   "reason":"age|outfit|transformation|injury|disguise","look":"the full neutral look in this variant"}
     Make a variant ONLY for a MAJOR change that lasts at least one full group: a time skip that ages them, a lasting
     outfit change, a transformation, a lasting injury, a disguise. Otherwise reuse the existing definition.
  {"type":"location","key":"snake_case","name":"...","kind":"interior|exterior","chapters":"61-63",
   "block":"40-120 words that never change: layout, materials, fixed furniture or landmarks, light sources, colours",
   "default_light":"..."}
  {"type":"name","en":"English","hi":"Devanagari"}
  {"type":"flag","chapters":"63","note":"what a picture must soften"}
Existing characters and locations are NOT repeated. tier: 1 lead, 2 in three or more chapters, 3 in one or two
(tier 3 gets no reference image and is drawn from "look"). minor: under eighteen, always fully clothed.
"""

REVIEW_TASK = """
REVIEW TASK (add these lines to the BIBLE block)
Some existing characters were described before these rules existed. For each character in the TO REVIEW section of the
bible file, add one line {"type":"revise","key":"<existing key>","look":"<neutral look, nothing held>","props":["..."]}
that moves every held or carried item out of the look into props. Where TO REVIEW lists old outfit notes for a
character, decide for each note: a lasting change becomes a "variant" line with its chapter range; a one-off does not
(use "wearing" on those images instead).
"""

EXAMPLE_GROUP = {
    "type": "group", "group": "g01", "loc": "harbour_market", "time": "dusk", "where": "outdoor",
    "light": "low orange sun from the left, long shadows", "weather": "clear",
    "present": ["maren", "tobias"],
    "staging": ("Maren stands behind a fruit stall on the left; Tobias faces her from the other side of the "
                "stall, a crate of apples between them, the harbour wall behind him"),
}
EXAMPLE_IMAGE = {
    "type": "image", "n": 1, "group": "g01", "shot": "wide",
    "cast": [
        {"key": "maren", "var": "base", "position": "left middle distance, behind the stall",
         "pose": "standing straight behind the stall",
         "hands": "empty, resting flat on the stall", "mood": "wary", "wearing": None},
        {"key": "tobias", "var": "base", "position": "right middle distance, in front of the stall",
         "pose": "leaning forward over the crate",
         "hands": "empty, one pointing at the crate", "mood": "curious", "wearing": None},
    ],
    "others": "a few shoppers far behind, blurred",
    "action": ("Maren waits behind the fruit stall while Tobias leans toward her across a crate of apples and "
               "points at it with one finger, asking a question. Gulls circle above the harbour wall and the "
               "low sun lights the scene from the left."),
    "focus": [0.5, 0.45],
}


# The same moment with a close camera: positions are sides of the frame and the action names the spot.
EXAMPLE_IMAGE_TIGHT = {
    **EXAMPLE_IMAGE, "shot": "medium",
    "cast": [
        {**EXAMPLE_IMAGE["cast"][0], "position": "left"},
        {**EXAMPLE_IMAGE["cast"][1], "position": "right"},
    ],
    "action": ("Maren stands behind the fruit stall, beside the scale, while Tobias leans toward her across a crate "
               "of apples and points at it with one finger, asking a question. Gulls circle above the harbour wall "
               "and the low sun lights the scene from the left."),
}
FRAMINGS = ("wide", "tight")


def example_block(framing: str = "wide") -> str:
    g = json.dumps(EXAMPLE_GROUP, ensure_ascii=False, separators=(",", ":"))
    i = json.dumps(EXAMPLE_IMAGE_TIGHT if framing == "tight" else EXAMPLE_IMAGE, ensure_ascii=False,
                   separators=(",", ":"))
    return ("EXAMPLE (format only: invented content, not from any real novel)\n"
            "===== BEGIN IMAGES c0001 -> inbox/c0001.images.jsonl =====\n"
            "```jsonl\n" + g + "\n" + i + "\n```\n"
            "===== END IMAGES c0001 =====\n")


def word_band(target: int, tolerance: float) -> tuple[int, int]:
    """(low, high): the target minus and plus the tolerance, rounded to the nearest 10 words."""
    return (round(target * (1 - tolerance) / 10) * 10, round(target * (1 + tolerance) / 10) * 10)


def translate_band(book: str) -> dict:
    """translate.target_words and translate.words_tolerance as the {target}, {low} and {high} of the narration rules."""
    from novel.core import settings
    target = int(settings.get("translate.target_words", book=book, default=800))
    low, high = word_band(target, float(settings.get("translate.words_tolerance", book=book, default=0.20)))
    return {"target": target, "low": low, "high": high}


def build_prompt(mode: str, *, channel: str, title: str, first: int, last: int,
                 paced: float, chars_per_second: float, max_characters: int,
                 window_counts: dict[int, int] | None = None, review: bool = False,
                 chapters_file: str | None = None, bible_file: str = "bible.txt",
                 windows_file: str | None = None, target: int = 800, low: int = 640, high: int = 960) -> str:
    from novel.stages import episode_translate as T
    scenes = mode == "scenes"
    chapters_file = chapters_file or f"chapters-{first}-{last}.txt"
    windows_file = windows_file or f"windows-{first}-{last}.txt"
    window_list = "\n".join(f"- chapter {n}: {c} windows" for n, c in (window_counts or {}).items())
    parts = [
        ROLE,
        SOURCES.replace("{windows_line}", WINDOWS_LINE if scenes else ""),
        HOW_SCENES if scenes else HOW_FULL,
        BLOCK_FORMAT.replace("{hindi_kind}", "" if scenes else HINDI_KIND)
                    .replace("{hindi_plain}", "" if scenes else HINDI_PLAIN),
        PART_A_SCENES if scenes else PART_A_FULL,
        PART_B,
        PART_C + (REVIEW_TASK if review else ""),
        example_block(),
    ]
    values = {
        "channel": channel, "title": title, "first": first, "last": last,
        "chapters_file": chapters_file, "bible_file": bible_file, "windows_file": windows_file,
        "batch": batch_id(first, last), "ex4": f"{first:04d}",
        "paced": f"{paced:g}", "chars": round(paced * chars_per_second),
        "max_characters": max_characters,
        "HINGLISH": T.HINGLISH, "BANNED": ", ".join(banned_words()), "target": target, "low": low, "high": high,
        "n_label": "window number" if scenes else "marker number",
        "first_rule": FIRST_RULE_SCENES if scenes else FIRST_RULE_FULL,
        "window_list": window_list,
    }
    # fill() is one pass: values (which hold literal {1}, {2} markers) are never rescanned.
    return fill("\n".join(parts), values)


# ------------------------------------------------------------------ rules for one API call

ROLE_API = """ROLE
You are the writer and the art director for the YouTube channel "{channel}". We turn the novel "{title}" into long
Hinglish story episodes: a narrator reads Hindi text aloud and a picture stays on screen for every few seconds of
narration.
"""

HOW_API = """HOW THIS REQUEST WORKS
This request is complete in itself; nothing is remembered from earlier requests. The REQUEST section at the end names
exactly what to return. Return only that, in the block format below, and write nothing outside the block. If the
answer would get too long, finish the line you are in, close the block with its END line, write on its own line
>>> MORE c{ex4} IMAGES  and stop: the next request asks for the rest.
"""

BLOCK_FORMAT_API = """BLOCK FORMAT (a tool reads it; follow it exactly)
A block starts with the line  ===== BEGIN <KIND> <ID> -> inbox/<file> =====  and ends with  ===== END <KIND> <ID> =====
  BIBLE {batch}  (extra lines for one chapter: BIBLE {batch}-c{ex4})  -> inbox/bible-{batch}.jsonl
  IMAGES c{ex4}  -> inbox/c{ex4}.images.jsonl
Inside a ```jsonl code fence, one JSON object per line, double quotes, no comments, no trailing commas.
"""

NARRATION_API = """HOW YOU ANSWER
Return the Hinglish narration of the chapter as plain text: the heading line, then the paragraphs. No code fence, no
block lines, no notes, no commentary, no picture markers. If your answer gets cut off, the next request asks you to
continue from the last complete paragraph.
"""


def build_rules(part: str, *, channel: str, title: str, first: int, last: int, paced: float,
                chars_per_second: float, max_characters: int, chapter: int | None = None,
                window_count: int | None = None, review: bool = False, target: int = 800, low: int = 640,
                high: int = 960, framing: str = "wide") -> str:
    """The rules text of ONE API call, taken from the same sections `build_prompt` uses but without the
    conversation part ("how we work", "type next"): part is "translate" (the Hinglish narration of a chapter),
    "bible" (the BIBLE block of a batch) or "images" (the IMAGES block of one chapter, scenes mode).
    target / low / high: translate.target_words and the band around it (words of the narration).
    framing (images only): "wide" is the camera far back (Google Flow); "tight" is a close camera, at most two
    people and no group staging (an open image model)."""
    from novel.stages import episode_translate as T
    if framing not in FRAMINGS:
        raise PromptError(f"framing must be one of {', '.join(FRAMINGS)}, not {framing!r}")
    ex = chapter if chapter is not None else first
    values = {
        "channel": channel, "title": title, "first": first, "last": last, "batch": batch_id(first, last),
        "ex4": f"{ex:04d}", "paced": f"{paced:g}", "chars": round(paced * chars_per_second),
        "max_characters": max_characters, "HINGLISH": T.HINGLISH, "BANNED": ", ".join(banned_words()),
        "target": target, "low": low, "high": high,
        "n_label": "window number", "first_rule": FIRST_RULE_SCENES,
        "window_list": (f"- chapter {chapter}: {window_count} windows" if chapter is not None else ""),
    }
    if part == "translate":
        narration = PART_A_FULL_TEXT.replace(
            ' A new name: spell it as it sounds, add it to the BIBLE as a "name" line,\n'
            "  and keep that spelling everywhere.",
            " A name that is not in the list: spell it as it sounds\n  and keep that spelling everywhere.").replace(
            "PART A - THE NARRATION (HINDI block)", "THE NARRATION")
        assert "BIBLE" not in narration, "the narration rules must not mention the BIBLE"
        parts = [ROLE_API, narration, NARRATION_API]
    elif part == "bible":
        parts = [ROLE_API, HOW_API, BLOCK_FORMAT_API, PART_C + (REVIEW_TASK if review else "")]
    elif part == "images":
        scenes_rules = PART_A_SCENES.replace("The attached windows file cuts", "The WINDOWS section of the request cuts")
        parts = [ROLE_API, HOW_API, BLOCK_FORMAT_API, scenes_rules, PART_B_TIGHT if framing == "tight" else PART_B,
                 PART_C, example_block(framing)]
    else:
        raise PromptError(f"internal: unknown rules part {part!r}")
    return fill("\n".join(parts), values)


# ------------------------------------------------------------------ generate

def generate(book: str, first: int, last: int, mode: str = "auto", out_dir: Path | None = None,
             write: bool = True) -> dict:
    """Write the batch folder. Returns a dict describing what was written (write=False: describes it and
    writes nothing)."""
    from novel.core import paths, settings
    from novel.stages import episode_scenes as S

    book_dir = paths.book_dir(book)
    mode, eps = choose_mode(book_dir, first, last, mode)
    english = chapters_text(book_dir, first, last)          # raises with the fix when a file is missing

    state = B.load_state(B.bible_dir(book))
    bible, review = bible_text(state)

    per_chapter: dict[int, list[str]] = {}
    shas: dict[str, str] = {}
    if mode == "scenes":
        for n in range(first, last + 1):
            wins = chapter_windows(eps[n], n)
            if not wins:
                raise PromptError(f"chapter {n}: the Hindi has no narration after its title line")
            per_chapter[n] = wins
            shas[str(n)] = sha256(Path(eps[n]) / "hindi" / f"{n:05d}.txt")

    get = lambda k, d=None: settings.get(k, book=book, default=d)
    notes = []
    title = get("book.title")
    if not title:
        title = book
        notes.append(f"book.title is not set; using '{book}'. Set it in {book_dir / 'book.yaml'} (book: {{title: ...}}).")
    paced = float(get("images.paced_seconds", S.PACED_SECONDS))
    cps = float(get("images.chars_per_second", S.CHARS_PER_SECOND))
    if mode == "scenes" and (paced, float(get("images.max_seconds", S.MAX_SECONDS)), cps) != (
            S.PACED_SECONDS, S.MAX_SECONDS, S.CHARS_PER_SECOND):
        notes.append("settings images.paced_seconds / max_seconds / chars_per_second differ from the constants in "
                     "episode_scenes, which cut the windows and which the renderer uses; the windows follow the "
                     "constants, the prompt text follows the settings.")
    prompt = build_prompt(
        mode, channel=get("channel.name", "") or "", title=title, first=first, last=last, paced=paced,
        chars_per_second=cps, max_characters=int(get("images.max_characters", S.MAX_CHARACTERS)),
        window_counts={n: len(w) for n, w in per_chapter.items()}, review=review,
        **translate_band(book))

    batch = batch_id(first, last)
    folder = Path(out_dir) if out_dir else book_dir / "gemini" / batch
    src = folder / "sources"
    if write:
        src.mkdir(parents=True, exist_ok=True)
    files = {"prompt": folder / "prompt.txt",
             "chapters": src / f"chapters-{first}-{last}.txt",
             "bible": src / "bible.txt"}
    if write:
        files["prompt"].write_text(prompt, encoding="utf-8")
        files["chapters"].write_text(english, encoding="utf-8")
        files["bible"].write_text(bible, encoding="utf-8")
    win_file = src / f"windows-{first}-{last}.txt"
    if mode == "scenes":
        if write:
            win_file.write_text(windows_text(per_chapter), encoding="utf-8")
        files["windows"] = win_file
    elif write and win_file.exists():
        win_file.unlink()                                   # derived file of an earlier scenes run
    meta = {
        "batch": batch, "book": book, "mode": mode, "first": first, "last": last,
        "chapters": list(range(first, last + 1)),
        "review": review,
        "params": {"paced_seconds": S.PACED_SECONDS, "max_seconds": S.MAX_SECONDS,
                   "chars_per_second": S.CHARS_PER_SECOND, "pacing": S.PACING},
        "windows": {str(n): len(w) for n, w in per_chapter.items()},
        "hindi_sha256": shas,
    }
    if write:
        (folder / "batch.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files["batch.json"] = folder / "batch.json"
    return {"batch": batch, "mode": mode, "folder": folder, "files": files, "prompt": prompt, "meta": meta,
            "windows": {n: len(w) for n, w in per_chapter.items()}, "review": review, "notes": notes,
            "upload": [files["chapters"], files["bible"]] + ([files["windows"]] if mode == "scenes" else [])}
