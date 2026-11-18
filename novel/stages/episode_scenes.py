"""Scenes for an episode: where each image goes, who is in it, and its Flow prompt.

Each chapter's final Hindi is split into windows, one image per window
(paragraph 0, the title, is the title card). From chapter 3 on ("pacing": "7s"
in the scene file) no image stays up past about 7 seconds of narration: a long
paragraph gets two or three images, consecutive short ones share one. Chapters
1-2 were written against the older 22 equal windows and keep them. Either way
pacing is fixed from the text before any audio exists - which is what lets
images and narration run in parallel. Claude reads the window's Hindi and writes the scene; this module only
checks what was written and turns it into a prompt.

A scene names its characters by roster KEY (bible/characters.json), never by a
Devanagari alias. The audiobook's alias matching folds Devanagari to nothing,
so every Hindi name silently resolved to the same person; keys can't do that.

Usage:
    python -m abpipe.episode_scenes brief --episode 1 --chapter 1   # what to write scenes from
    python -m abpipe.episode_scenes check --episode 1 --chapter 1
    python -m abpipe.episode_scenes write --episode 2 --chapters 21-40 --plan  # Gemini writes the scenes
    python -m abpipe.episode_scenes plan  --episode 1 --chapters 1  # prompts + flow/plan.json
    python -m abpipe.episode_scenes sheet --episode 1 --chapter 1   # contact sheet for review
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

from . import episode as E
from .episode_translate import parse_range
from .heading import body_start, heading_of
from .scenebuild import paragraphs


def chapter_title(ep_root: Path, n: int) -> str:
    """The chapter's title from manifest.json (for texts without a heading line)."""
    from .manifest import Manifest
    try:
        man = Manifest.load(Path(ep_root) / "manifest.json")
        return next((c.title for c in man.chapters if c.idx == n), f"Chapter {n}")
    except (OSError, ValueError):
        return f"Chapter {n}"

SCENES_PER_CHAPTER = 22      # legacy pacing (chapters 1-2): 22 equal windows, ~13 s an image
PACING = "7s"                # current pacing: written into every new scene file
PACED_SECONDS = 7.0          # aim: a paragraph longer than this gets another image
MAX_SECONDS = 7.5            # hard cap per image (user rule), estimated from the text
CHARS_PER_SECOND = 15.3      # Maya 2 / Anirban on the Gemini-app Hinglish, measured on chapters 1-2
SENTENCE_END = re.compile(r"[।?!.]+[\"'”’]?\s+")
MAX_CHARACTERS = 4
SHOTS = ("establishing", "wide", "medium", "close-up", "over-the-shoulder",
         "low-angle", "high-angle", "detail")
# Scene files v2 (written by `novel import`): no close-up (Flow filled the frame with a face).
SHOTS_V2 = ("wide", "establishing", "medium", "over-the-shoulder", "low-angle", "high-angle", "detail")
# Comic-panel shapes a scene may ask for with an optional "panel" key (video.style: panels).
PANEL_SHAPES = ("wide", "landscape", "square", "portrait", "tall")
# Image prompts never ask for these. The book has violence; the pictures show
# its moment, not its gore.
BANNED = re.compile(r"(?i)\b(nude|naked|nudity|sex|sexual|breasts?|lingerie|underwear|"
                    r"topless|shirtless|gore|gory|dismember\w*|severed|entrails|"
                    r"blood-soaked|disembowel\w*)\b")
DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# The look of every image, in one place. "used prompt" detection in
# flow/gen.mjs looks for HEADER_MARK, so keep it at the start.
HEADER_MARK = "Wide 16:9 illustration for an audiobook video"
STYLE = (HEADER_MARK + ", drawn in exactly the same anime art style as the attached "
         "characters. {style}.")
# Off (user, 2026-11-11): clothes in the prompt made pictures of one character disagree with each other and
# with the reference. The Flow Character's own look decides what they wear. The cast notes stay in the bible.
OUTFITS_IN_PROMPTS = False
FOOTER = ("Keep every attached character exactly like their reference: same face, "
          "hair, build and clothing unless the scene says otherwise.\n"
          "FRAMING: shoot it as a WIDE shot, with the camera far back. The picture is cropped and zoomed in "
          "afterwards (the bottom 17% and 9% off each side are cut away), so keep every character, face, "
          "hand, foot and important prop inside the SAFE AREA: the middle 80% of the width and the top "
          "78% of the height. Inside it, a standing person is full length, head to feet, about half the "
          "picture height at most, with the head under a sixth of the height. Show plenty of the place "
          "around them (street, room, sky, buildings) on both sides and behind. Never a close-up, never a "
          "face filling the frame, never a person cut by the edge. The bottom fifth and the outer edges are "
          "only ordinary, uneventful scenery in the same scene and lighting (street, floor, ground, grass, "
          "water, mist, walls). The picture fills the whole frame, edge to edge: no black bars, "
          "letterboxing, borders, dark band or fade at any edge, and nothing painted in the corners. "
          "Keep the main action in the centre. "
          "No text, captions, speech bubbles, logos or signatures. No gore and no nudity.")
# "No ... watermarks" is gone on purpose: chapter 1 scene 8 came back with a large
# copy of Flow's sparkle PAINTED into the art, which no crop can remove, and
# naming the thing may invite it. episode_video.painted_marks catches the rest.


def windows(paras: list[str], n: int = SCENES_PER_CHAPTER) -> list[tuple[int, int]]:
    """n runs of consecutive paragraphs, 1..len-1, of about equal character length.

    Paragraph 0 is skipped when it is the chapter heading (older chapters; heading.body_start). Returns
    inclusive (first, last) paragraph numbers. Fewer than n windows only when
    the chapter has fewer than n body paragraphs.
    """
    body = list(range(body_start(paras), len(paras)))
    n = min(n, len(body))
    total = sum(len(paras[i]) for i in body)
    out, start, acc = [], body[0], 0
    for k, i in enumerate(body):
        acc += len(paras[i])
        left_windows = n - len(out) - 1
        left_paras = len(body) - k - 1
        if left_windows and (acc >= total * (len(out) + 1) / n or left_paras == left_windows):
            out.append((start, i))
            start = i + 1
    out.append((start, body[-1]))
    return out


def _cuts(text: str, k: int, max_len: float) -> list[int]:
    """k-1 offsets that cut text into k pieces of about equal length, none longer
    than max_len: at a sentence end when one is near (within a third of a
    piece) and keeps every piece under max_len, else at the nearest space."""
    if k <= 1:
        return []
    n = len(text)
    ends = [m.end() for m in SENTENCE_END.finditer(text)]
    spaces = [m.end() for m in re.finditer(r"\s+", text)]
    step, out, prev = n / k, [], 0
    for j in range(1, k):
        ideal = prev + (n - prev) / (k - j + 1)      # rebalance what is left
        def fits(e: int) -> bool:
            return prev < e < n and e - prev <= max_len and n - e <= (k - j) * max_len
        near = [e for e in ends if abs(e - ideal) <= step / 3 and fits(e)]
        pool = near or [e for e in spaces if fits(e)] or [round(ideal)]
        prev = min(pool, key=lambda e: abs(e - ideal))
        out.append(prev)
    return out


def paced_windows(paras: list[str]) -> list[tuple[int, int, int, int]]:
    """Windows of at most ~PACED_SECONDS of narration, estimated from the text.

    Returns (first_para, start_char, last_para, end_char), end exclusive. A
    paragraph longer than PACED_SECONDS is cut into equal pieces at sentence
    ends; whole paragraphs that are short together share one window.
    """
    limit = PACED_SECONDS * CHARS_PER_SECOND
    out: list[tuple[int, int, int, int]] = []
    for i in range(body_start(paras), len(paras)):
        n = len(paras[i])
        k = max(1, -(-n // int(limit)))          # ceil
        if k == 1 and out:
            a, sa, b, eb = out[-1]
            whole_prev = sa == 0 and eb == len(paras[b])
            span = sum(len(paras[x]) for x in range(a, b + 1))
            if whole_prev and span + n <= limit:
                out[-1] = (a, 0, i, n)
                continue
        bounds = [0, *_cuts(paras[i], k, MAX_SECONDS * CHARS_PER_SECOND), n]
        out += [(i, bounds[j], i, bounds[j + 1]) for j in range(k)]
    return out


def chapter_windows(paras: list[str], pacing: str | None) -> list[tuple[int, int, int, int]]:
    """The windows a scene file was written against: (first_para, start_char, last_para, end_char)."""
    if pacing == PACING:
        return paced_windows(paras)
    if pacing:
        raise SystemExit(f"unknown pacing {pacing!r} (this code knows {PACING!r} and none)")
    return [(a, 0, b, len(paras[b])) for a, b in windows(paras)]


def window_text(paras: list[str], w: tuple[int, int, int, int]) -> list[tuple[int, str]]:
    a, sa, b, eb = w
    return [(i, paras[i][sa if i == a else 0: eb if i == b else len(paras[i])].strip())
            for i in range(a, b + 1)]


def load_roster(ep_root: Path) -> dict:
    """The episode's own (v1) bible; without one, the book-level bible v2 (<book>/bible/characters.json,
    the episode being <book>/episodes/E###)."""
    own = ep_root / "bible" / "characters.json"
    if own.exists():
        return json.loads(own.read_text(encoding="utf-8"))
    from novel import bible as book_bible
    shared = Path(ep_root).resolve().parents[1] / "bible"
    if not (shared / book_bible.CHARACTERS_FILE).exists():
        raise SystemExit(f"no character bible: neither {own} nor {shared / book_bible.CHARACTERS_FILE} exists")
    return book_bible.load_characters(shared)


def _attached(cast: list, roster: dict) -> int:
    """Cast members of a v2 scene that are Flow Characters (their variant has a flow_name)."""
    k = 0
    for c in cast:
        ch = roster.get(c.get("key")) or {}
        if isinstance(ch.get("variants"), dict):
            k += bool((ch["variants"].get(c.get("var") or "base") or {}).get("flow_name"))
        else:
            k += bool(ch.get("flow_name"))
    return k


def chapter_paras(ep_root: Path, n: int) -> list[str]:
    return paragraphs((ep_root / "hindi" / f"{n:05d}.txt").read_text(encoding="utf-8"))


def scenes_path(ep_root: Path, n: int) -> Path:
    return ep_root / "scenes" / f"{n:05d}.json"


def check_chapter(ep_root: Path, n: int) -> list[str]:
    """Every problem with a chapter's scene list; empty means usable."""
    E.check_lock(ep_root, n)
    roster = load_roster(ep_root)["characters"]
    paras = chapter_paras(ep_root, n)
    doc = json.loads(scenes_path(ep_root, n).read_text(encoding="utf-8"))
    wins = chapter_windows(paras, doc.get("pacing"))
    lock = json.loads(E.lock_path(ep_root).read_text(encoding="utf-8"))
    problems = []
    if doc.get("hindi_sha256") != lock[f"{n:05d}.txt"]:
        problems.append("written against a different Hindi text - the windows have moved")
    scenes = doc.get("scenes", [])
    v2 = doc.get("version") == 2
    shots = SHOTS_V2 if v2 else SHOTS
    if len(scenes) != len(wins):
        problems.append(f"{len(scenes)} scenes, expected {len(wins)}")
    for k, (s, (a, _, b, _)) in enumerate(zip(scenes, wins), 1):
        tag = f"scene {k}"
        if s.get("id") != k:
            problems.append(f"{tag}: id {s.get('id')}, expected {k}")
        if not a <= s.get("para", -1) <= b:
            problems.append(f"{tag}: para {s.get('para')} outside its window {a}-{b}")
        if s.get("shot") not in shots:
            problems.append(f"{tag}: shot {s.get('shot')!r} not one of {shots}")
        chars = s.get("characters", [])
        unknown = [c for c in chars if c not in roster]
        if unknown:
            problems.append(f"{tag}: not in the roster: {unknown}")
        if v2:
            if s.get("group") not in (doc.get("groups") or {}):
                problems.append(f"{tag}: group {s.get('group')!r} is not in the file's groups")
            if chars != [c.get("key") for c in s.get("cast", [])]:
                problems.append(f"{tag}: characters do not match the cast")
            attached = _attached(s.get("cast", []), roster)
            if attached > MAX_CHARACTERS:
                problems.append(f"{tag}: {attached} attached characters, at most {MAX_CHARACTERS}")
        elif len(chars) > MAX_CHARACTERS:
            problems.append(f"{tag}: {len(chars)} characters, at most {MAX_CHARACTERS}")
        text = s.get("scene", "")
        if not 60 <= len(text) <= 900:
            problems.append(f"{tag}: scene text is {len(text)} characters (60-900)")
        if DEVANAGARI.search(text):
            problems.append(f"{tag}: scene text must be English")
        if BANNED.search(text):
            problems.append(f"{tag}: scene text asks for {BANNED.search(text).group(0)!r}")
        focus = s.get("focus", [0.5, 0.5])
        if not (isinstance(focus, list) and len(focus) == 2
                and all(isinstance(f, (int, float)) and 0 <= f <= 1 for f in focus)):
            problems.append(f"{tag}: focus must be [x, y] between 0 and 1")
        if "panel" in s and s["panel"] not in PANEL_SHAPES:
            problems.append(f"{tag}: panel {s['panel']!r} not one of {PANEL_SHAPES}")
    return problems


def variant_for(character: dict, chapter: int, scene_text: str) -> dict | None:
    """The outfit variant (a Flow Character that exists) of this person that applies here, if any.

    A variant lists its chapters and, optionally, words the scene must contain (so "in the dream ruins"
    is a different outfit from "waking in bed" in the same chapter). Variants not yet created in Flow
    (flow_made false) are ignored, so defining them early changes nothing.
    """
    from .episode_cast import chapter_set
    for v in character.get("variants") or []:
        if not v.get("flow_made") or chapter not in chapter_set(v.get("chapters", "")):
            continue
        if v.get("keywords") and not re.search(v["keywords"], scene_text, re.I):
            continue
        return v
    return None


def prompt_for(scene: dict, roster: dict, style: str, chapter: int | None = None) -> tuple[str, list[str]]:
    """The full Flow prompt, and the Flow Characters to attach.

    Minor characters have no Flow Character (flow_name null); they are drawn
    from their written description instead, which is enough for someone who
    appears once.
    """
    chars = [roster[c] for c in scene["characters"]]
    # An outfit variant is its own Flow Character (same person, other clothes): when one applies to this
    # chapter and scene, attach it instead of the base character and skip the outfit text for that person.
    dressed = {}
    for c in chars:
        v = variant_for(c, chapter, scene["scene"]) if chapter is not None and c.get("flow_name") else None
        if v:
            dressed[c["display"]] = v["flow_name"]
    names = [dressed.get(c["display"], c["flow_name"]) for c in chars if c.get("flow_name")]
    described = [c for c in chars if not c.get("flow_name")]
    parts = [STYLE.format(style=style)]
    if names:
        parts.append("CAST (attached Flow characters, keep their look exactly): "
                     + ", ".join(names) + ".")
    for c in described:
        parts.append(f"ALSO IN THE PICTURE: {c['display']}: {c['appearance']}")
    if chapter is not None and OUTFITS_IN_PROMPTS:
        # The reference image carries one outfit. When the story changes it, say so here whether or not
        # the scene text does, so every picture of the chapter wears the same thing.
        worn = []
        for c in chars:
            notes = outfits_for(c, chapter) if c["display"] not in dressed else []
            if notes:
                worn.append(f"{c['display']}: " + " | ".join(notes))
        if worn:
            parts.append("OUTFITS IN THIS CHAPTER (draw exactly this, it overrides the clothes in the reference "
                         "image; when several are listed, use the one that fits the scene): " + "; ".join(worn) + ".")
    if not chars:
        parts.append("No named characters in this picture.")
    shot = scene["shot"]
    if shot == "close-up":   # user 2026-11-12: Flow's close-ups filled the frame with a face
        shot = "wide, camera far back, full-length figures, surroundings clearly visible"
    elif shot == "detail":
        shot = "detail of the object, still shown in its surroundings, not filling the frame"
    parts.append(f"SHOT: {shot}.")
    parts.append(f"SCENE: {scene['scene']}")
    parts.append(FOOTER)
    return "\n\n".join(parts) + "\n", names


# ------------------------------------------------------------------ commands


def cmd_brief(args) -> None:
    ep_root = E.root(args.episode)
    paras = chapter_paras(ep_root, args.chapter)
    roster = load_roster(ep_root)["characters"]
    lock = json.loads(E.lock_path(ep_root).read_text(encoding="utf-8"))
    lines = [f"# Chapter {args.chapter}: scene brief",
             f"hindi_sha256: {lock[f'{args.chapter:05d}.txt']}", "",
             "Roster keys: " + ", ".join(roster), "", f"Title: {heading_of(paras) or chapter_title(ep_root, args.chapter)}", ""]
    existing = scenes_path(ep_root, args.chapter)
    pacing = PACING
    if existing.exists():
        pacing = json.loads(existing.read_text(encoding="utf-8")).get("pacing")
    wins = chapter_windows(paras, pacing)
    if pacing:
        lines += [f'Pacing: write `"pacing": "{pacing}"` at the top of the scene file. One scene per '
                  f"window below (about {PACED_SECONDS:g} s of narration each). When a paragraph is "
                  "split across windows, each window's image shows ITS part of the paragraph - "
                  "a different moment, angle or detail, never the same picture twice. "
                  "`para` = the window's first paragraph.", ""]
    for k, w in enumerate(wins, 1):
        a, sa, b, eb = w
        secs = sum(len(t) for _, t in window_text(paras, w)) / CHARS_PER_SECOND
        part = "" if (sa == 0 and eb == len(paras[b])) else (
            f" (part {sum(1 for x in wins[:k] if x[0] == a)} of {sum(1 for x in wins if x[0] == a)})")
        lines.append(f"## Scene {k}: paragraph{'s' if b > a else ''} {a}{f'-{b}' if b > a else ''}{part}, ~{secs:.0f} s")
        lines += [f"[{i}] {t}" for i, t in window_text(paras, w)]
        lines.append("")
    out = ep_root / "scenes" / f"{args.chapter:05d}.brief.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out} ({len(wins)} windows, {len(paras)} paragraphs, pacing {pacing or 'legacy 22'})")


def cmd_check(args) -> None:
    problems = check_chapter(E.root(args.episode), args.chapter)
    print("\n".join(problems) or "ok")
    sys.exit(1 if problems else 0)


def cmd_plan(args) -> None:
    ep_root = E.root(args.episode)
    info = E.info(ep_root)
    if not info.get("flow_project"):
        sys.exit("set flow_project (the Flow project URL) in episode.json first")
    roster_doc = load_roster(ep_root)
    roster, style = roster_doc["characters"], roster_doc["style"]
    plan_path = ep_root / "flow" / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
    shots = {s["id"]: s for s in plan.get("shots", [])}
    for n in parse_range(args.chapters):
        if json.loads(scenes_path(ep_root, n).read_text(encoding="utf-8")).get("version") == 2:
            sys.exit(f"chapter {n} has a v2 scene file: its prompts and the Flow plan come from "
                     f"`novel approve --chapters {n}`, not from this old command")
        problems = check_chapter(ep_root, n)
        if problems:
            sys.exit(f"chapter {n} scenes have problems - run `check`:\n  " + "\n  ".join(problems))
        from .episode_attach import candidates
        gaps = candidates(roster, json.loads(scenes_path(ep_root, n).read_text(encoding="utf-8")))
        if gaps:     # wording only: Gemini decides in `episode_attach`, so some of these are just mentions
            print(f"  warning: chapter {n}: {len(gaps)} scene(s) name a roster character who is not attached "
                  f"(run episode_attach to check): " + ", ".join(f"s{i}:{k}" for i, k in gaps[:10]))
        img_dir = ep_root / "images" / f"c{n:04d}"
        img_dir.mkdir(parents=True, exist_ok=True)
        for s in json.loads(scenes_path(ep_root, n).read_text(encoding="utf-8"))["scenes"]:
            sid = f"c{n:04d}-s{s['id']:02d}"
            text, names = prompt_for(s, roster, style, n)
            stem = img_dir / f"s{s['id']:02d}"
            prompt_file = stem.with_suffix(".prompt.txt")
            old = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else None
            if old is not None and old != text and Path(f"{stem}-2k.jpg").exists():
                print(f"  {sid}: prompt changed but its image exists - kept the old "
                      f"prompt; delete {stem}-2k.jpg to regenerate")
                continue
            prompt_file.write_text(text, encoding="utf-8")
            shots[sid] = {"id": sid, "chars": names,
                          "prompt": prompt_file.as_posix(), "out": f"{stem.as_posix()}-2k.jpg",
                          "meta": f"{stem.as_posix()}.flow.json",
                          "used": f"{stem.as_posix()}.used.txt"}
    plan = {"project": info["flow_project"], "header_mark": HEADER_MARK,
            "journal": (ep_root / "flow" / "journal.jsonl").as_posix(),
            "shots": [shots[k] for k in sorted(shots)]}
    plan_path.parent.mkdir(exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{plan_path}: {len(plan['shots'])} shots")


def cmd_sheet(args) -> None:
    """A page to review a chapter's images against what each was meant to show."""
    ep_root = E.root(args.episode)
    n = args.chapter
    img_dir = ep_root / "images" / f"c{n:04d}"
    doc = json.loads(scenes_path(ep_root, n).read_text(encoding="utf-8"))
    paras = chapter_paras(ep_root, n)
    # Show what the video will show: the mark-free crops, made here if missing.
    from .episode_video import clean_chapter      # late: episode_video imports this module
    clean_chapter(ep_root, n)
    cards = []
    for s in doc["scenes"]:
        stem = img_dir / f"s{s['id']:02d}"
        meta = json.loads(Path(f"{stem}.flow.json").read_text(encoding="utf-8")) \
            if Path(f"{stem}.flow.json").exists() else {}
        img = img_dir / "clean" / f"s{s['id']:02d}.jpg"
        pic = (f'<img src="clean/{img.name}">' if img.exists()
               else '<div class="missing">not generated</div>')
        used = meta.get("flow_reported_ingredients", [])
        cards.append(
            f'<figure>{pic}<figcaption><b>s{s["id"]:02d}</b> · {html.escape(s["shot"])} · '
            f'{html.escape(", ".join(s["characters"]) or "no characters")}'
            f'{" · Flow used: " + html.escape(", ".join(used)) if used else ""}'
            f'<p>{html.escape(s["scene"])}</p>'
            f'<p class="hi">[{s["para"]}] {html.escape(paras[s["para"]][:240])}</p>'
            f'</figcaption></figure>')
    page = f"""<!doctype html><meta charset="utf-8"><title>Chapter {n} contact sheet</title>
<style>body{{font:14px system-ui;background:#111;color:#ddd;margin:16px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:16px}}
figure{{margin:0;background:#1c1c1c;border-radius:6px;overflow:hidden}}
img{{width:100%;display:block;aspect-ratio:16/9;object-fit:cover}}
.missing{{aspect-ratio:16/9;display:grid;place-items:center;color:#f88}}
figcaption{{padding:8px 10px}} p{{margin:6px 0}} .hi{{color:#9a9}}</style>
<h1>Chapter {n}: {html.escape(heading_of(paras) or chapter_title(ep_root, n))}</h1><main>{''.join(cards)}</main>"""
    out = img_dir / "contact.html"
    out.write_text(page, encoding="utf-8")
    print(f"open {out.resolve()}")


# ------------------------------------------------------------------ Gemini scene writer

SCENE_SYSTEM = """You write the picture list for a narrated story video. A Hindi/Hinglish retelling of a gothic fantasy novel is cut into numbered windows of about 7 seconds of narration. For EACH window write one illustration brief; an image generator draws it and the picture stays on screen for that window.

OUTPUT: JSON only, exactly {{"scenes": [...]}} with one object per window, in order, {n} in total:
  {{"id": <window number>, "characters": [<roster keys>], "shot": "<shot>", "focus": [x, y], "scene": "<English>"}}

RULES
- "scene": English, 150-420 characters. One concrete, drawable moment: who is where, doing what, the mood and the light. Name each character as the roster does ("Lumian", "Aurore"). Draw only what the window says; never invent plot.
- When one paragraph is split over several windows, each window draws ITS part: a different moment, angle or detail, never the same picture twice.
- "characters": roster keys of the people who are visibly IN the picture, at most 4. Use [] for landscapes, objects, crowds of unnamed people and empty rooms. Someone only mentioned or remembered is not in the picture unless a flashback is drawn.
- "shot" is one of: {shots}. Vary them: use "medium" for at most a third of the windows, never the same shot more than twice in a row, and use close-up, wide, over-the-shoulder, low-angle, high-angle and detail often.
- "focus": where the main subject sits, [0..1, 0..1] from the top-left (a centred face is [0.5, 0.4]).
- Never describe what a character wears: their reference images decide their clothes, so leave clothing, hats and hair styling out of "scene" (props they hold or carry are fine).
- Pictures never contain readable text: for cards, letters, books, signs, coins write "no words" or "unreadable".
- Family-friendly. NEVER use the words: nude, naked, sex, sexual, breasts, lingerie, underwear, topless, shirtless, gore, gory, dismember, severed, entrails, blood-soaked, disembowel. Violence is drawn as the threat, the swing or a silhouette, never wounds. Everyone is fully clothed.
- Do not mention Devanagari or Hindi in "scene". No markdown.

ROSTER (key: who they are)
{roster}
{outfits}{flag}"""

OUTFIT_KEY = re.compile(r"^\s*(\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*)")


def outfits_for(character: dict, n: int) -> list[str]:
    """Outfit notes of a roster character that apply in chapter n.

    Outfit keys start with chapter numbers or ranges, "1-3", "9-10,13-14,19 dream ruins":
    the text after the numbers is kept as a label.
    """
    out = []
    for key, text in (character.get("outfits") or {}).items():
        m = OUTFIT_KEY.match(key)
        if not m:
            continue
        hit = False
        for part in m.group(1).split(","):
            lo, _, hi = part.partition("-")
            lo, hi = int(lo), int(hi or lo)
            hit = hit or lo <= n <= hi
        if hit:
            label = key[m.end():].strip()
            out.append(f"{label + ': ' if label else ''}{text}")
    return out


def writer_prompts(ep_root: Path, n: int) -> tuple[str, str, list]:
    """(system prompt, user message, windows) for chapter n."""
    paras = chapter_paras(ep_root, n)
    wins = paced_windows(paras)
    doc = load_roster(ep_root)
    roster = doc["characters"]
    lines, outfit_lines = [], []
    for key, c in roster.items():
        lines.append(f"- {key}: {c['display']} - {c.get('role', '')}")
        o = outfits_for(c, n)
        if o:
            outfit_lines.append(f"- {key}: " + " | ".join(o))
    flag = doc.get("content_flags", {}).get(str(n))
    system = SCENE_SYSTEM.format(
        n=len(wins), shots=", ".join(SHOTS), roster="\n".join(lines),
        outfits=("\nOUTFITS IN THIS CHAPTER\n" + "\n".join(outfit_lines) + "\n") if outfit_lines and OUTFITS_IN_PROMPTS else "",
        flag=(f"\nCONTENT NOTE FOR THIS CHAPTER: {flag}\n") if flag else "")
    msg = [f"Chapter {n}. Title: {heading_of(paras) or chapter_title(ep_root, n)}", ""]
    for k, w in enumerate(wins, 1):
        a, sa, b, eb = w
        part = ""
        if not (sa == 0 and eb == len(paras[b])):
            part = (f" (part {sum(1 for x in wins[:k] if x[0] == a)} of "
                    f"{sum(1 for x in wins if x[0] == a)} of one paragraph)")
        msg.append(f"## Window {k}{part}")
        msg += [t for _, t in window_text(paras, w)]
        msg.append("")
    return system, "\n".join(msg), wins


def _focus(v) -> list:
    """[x, y] as numbers; the model sometimes sends "0.5" strings. Anything else is kept as is
    so check_chapter reports it."""
    try:
        return [float(x) for x in v] if v is not None else [0.5, 0.5]
    except (TypeError, ValueError):
        return v


def scenes_from_reply(raw: str, wins: list) -> list[dict]:
    """The model's JSON as scene dicts; `para` comes from the window, not from the model."""
    from .scenewrite import _extract_json
    data = _extract_json(raw)
    items = data["scenes"] if isinstance(data, dict) else data
    if len(items) != len(wins):
        raise ValueError(f"{len(items)} scenes for {len(wins)} windows")
    return [{"id": k, "para": w[0], "characters": list(it.get("characters", [])),
             "shot": it.get("shot", ""), "focus": _focus(it.get("focus")),
             "scene": " ".join(str(it.get("scene", "")).split())}
            for k, (it, w) in enumerate(zip(items, wins), 1)]


def cmd_write(args) -> None:
    from .episode_translate import MODEL, Gemini, parse_range
    ep_root = E.root(args.episode)
    for sub in ("qa", "scenes"):          # a fresh episode has neither folder yet
        (ep_root / sub).mkdir(exist_ok=True)
    g = Gemini(args.model or MODEL, ep_root / "qa" / "scenes-gemini.log.jsonl")
    lock = json.loads(E.lock_path(ep_root).read_text(encoding="utf-8"))
    done, failed = [], []
    for n in parse_range(args.chapters):
        path = scenes_path(ep_root, n)
        if path.exists() and not args.force:
            print(f"chapter {n}: scenes already written (use --force to redo)")
            continue
        E.check_lock(ep_root, n)
        system, msg, wins = writer_prompts(ep_root, n)
        note, scenes, problems = "", [], ["no reply yet"]
        for attempt in range(1, 4):
            try:
                raw = g.ask(system, msg + note, json_out=True, temperature=0.6)
                scenes = scenes_from_reply(raw, wins)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                problems = [str(e)]
                note = (f"\n\nYour last reply was unusable ({e}). "
                        f"Return exactly {len(wins)} scenes as specified.")
                print(f"  chapter {n} try {attempt}: {e}", flush=True)
                continue
            doc = {"version": 1, "chapter": n, "pacing": PACING,
                   "hindi_sha256": lock[f"{n:05d}.txt"],
                   "note": f"Written by Gemini ({g.model}) from the brief; one image per ~7 s window.",
                   "scenes": scenes}
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            problems = check_chapter(ep_root, n)
            if not problems and not args.no_attach_check:
                # A person who is in the picture but not attached is drawn with a random face:
                # attach them now (wording rules find the candidates, Gemini confirms each one).
                from .episode_attach import apply_fixes, audit_chapter
                fixes = audit_chapter(g, load_roster(ep_root)["characters"], doc)
                if fixes:
                    apply_fixes(path, doc, fixes)
                    shown = ", ".join("s%d +%s" % (f["id"], "+".join(f["add"])) for f in fixes[:8])
                    print(f"  chapter {n}: attached missing characters in {len(fixes)} scenes ({shown})", flush=True)
                    problems = check_chapter(ep_root, n)
            if not problems:
                break
            print(f"  chapter {n} try {attempt}: {len(problems)} problems", flush=True)
            note = ("\n\nYour last reply had these problems; return the FULL corrected list:\n- "
                    + "\n- ".join(problems[:20]))
        if problems:
            if path.exists():
                path.replace(path.with_suffix(".rejected.json"))
            failed.append(n)
            print(f"chapter {n}: NOT saved - {problems[:3]}")
        else:
            done.append(n)
            print(f"chapter {n}: {len(scenes)} scenes saved ({g.calls} Gemini calls so far)")
    if done and args.plan:
        from types import SimpleNamespace
        cmd_plan(SimpleNamespace(episode=args.episode, chapters=",".join(map(str, done))))
    print(f"written {done or 'none'}; failed {failed or 'none'}")
    sys.exit(1 if failed else 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("brief", cmd_brief), ("check", cmd_check), ("sheet", cmd_sheet)):
        p = sub.add_parser(name)
        p.add_argument("--episode", type=int, required=True)
        p.add_argument("--chapter", type=int, required=True)
        p.set_defaults(fn=fn)
    p = sub.add_parser("plan")
    p.add_argument("--episode", type=int, required=True)
    p.add_argument("--chapters", required=True)
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("write", help="have Gemini write the scenes (7 s pacing), check them, save")
    p.add_argument("--episode", type=int, required=True)
    p.add_argument("--chapters", required=True)
    p.add_argument("--model", default=None, help="default gemini-3.5-flash-lite (500 requests a day)")
    p.add_argument("--force", action="store_true", help="overwrite scenes that already exist")
    p.add_argument("--plan", action="store_true", help="also add the chapters to the Flow plan")
    p.add_argument("--no-attach-check", action="store_true",
                   help="skip the check that attaches characters named in a scene but missing from its list")
    p.set_defaults(fn=cmd_write)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
