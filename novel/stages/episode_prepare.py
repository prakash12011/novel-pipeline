"""Get an episode ready for images in one command: setup, cast, scenes, Flow plan.

Run after the Hindi is locked (episode_translate draft/lock). Each step is skipped when
it is already current, so re-running is cheap:

1. Setup: no bible/characters.json yet -> copied from the latest earlier episode; no
   flow_project in episode.json -> the earlier episode's.
2. Cast (1 Gemini call per episode): reads the episode's English chapters and adds
   characters that are not in the bible yet (recurring ones get a Flow Character name,
   one-off ones are described in the prompt instead). cast-sheet.md is rewritten.
3. Scenes (1 Gemini call per chapter): written for chapters without them, and again for
   chapters whose Hindi changed after their scenes were written - unless that chapter
   already has images, which this never throws away (it tells you instead).
4. Flow plan: every chapter's prompts into flow/plan.json.

It ends by printing what to run next (Flow Characters to create, then the image run).

    py -m abpipe.episode_prepare --episode 3
    py -m abpipe.episode_prepare --episode 3 --chapters 41-45
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from . import episode as E
from .episode_scenes import check_chapter, cmd_plan, cmd_write, scenes_path
from .episode_translate import MODEL, Gemini, clean_paragraphs, parse_range
from .manifest import Manifest
from .scenewrite import _extract_json

CAST_PROMPT = """You keep the character bible for an anime-style video adaptation of the novel
"Circle of Inevitability". You get the bible's current characters and the English text of new
chapters. List every character who appears in the new chapters and is NOT in the bible yet:
people, and creatures or spirits that are seen (monsters, a spirit-world creature, a cursed animal).
Skip people who are only mentioned and never seen.

For each, return:
- key: lowercase_with_underscores, unique, not one of the existing keys
- display: name as written in English (or a short description for an unnamed creature)
- hi: the name in Devanagari as a Hindi narrator would say it
- minor: true if under 18
- chapters: the chapter numbers they are seen in, e.g. "44, 50-52"
- recurring: true if seen in 2 or more chapters or central to a scene
- appearance: one sentence a painter can use, ONLY from what the text says (age, build, hair,
  eyes, face, clothes); if the text says nothing, a plain fitting look for their role and era
  (a 1900s-style Intis/French countryside); never invent scars or striking features
- role: one short sentence: who they are in the story so far, no spoilers past these chapters

Return JSON: {"new": [ ... ]}. Return {"new": []} if everyone seen is already in the bible."""


def setup(ep_root: Path, episode: int) -> None:
    info_path = ep_root / "episode.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    earlier = [E.root(e) for e in range(episode - 1, 0, -1) if E.root(e).exists()]
    bible = ep_root / "bible" / "characters.json"
    if not bible.exists():
        src = next((r / "bible" / "characters.json" for r in earlier
                    if (r / "bible" / "characters.json").exists()), None)
        if not src:
            sys.exit("no bible/characters.json in this or any earlier episode")
        bible.parent.mkdir(exist_ok=True)
        shutil.copy2(src, bible)
        print(f"setup: bible copied from {src.parent.parent.name}")
    if not info.get("flow_project"):
        for r in earlier:
            prev = json.loads((r / "episode.json").read_text(encoding="utf-8"))
            if prev.get("flow_project"):
                info["flow_project"] = prev["flow_project"]
                info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"setup: flow_project from {r.name}")
                break
        else:
            sys.exit(f"set flow_project (the Flow project URL) in {info_path}")


def cast(ep_root: Path, episode: int, man: Manifest) -> list[str]:
    """Add characters first seen in this episode. Returns the new keys that need Flow Characters."""
    path = ep_root / "bible" / "characters.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    covered = set(doc.get("covers_episodes", [1] if episode > 1 else []))
    if episode in covered:
        print("cast: bible already covers this episode")
        return []
    roster = doc["characters"]
    english = []
    for c in man.chapters:
        paras = clean_paragraphs((ep_root / "chapters" / c.source).read_text(encoding="utf-8"))
        english.append(f"=== CHAPTER {c.idx} ===\n" + "\n".join(paras))
    known = "\n".join(f"- {k}: {c['display']} - {c.get('role', '')}" for k, c in roster.items())
    g = Gemini(MODEL, ep_root / "qa" / "cast-gemini.log.jsonl")
    (ep_root / "qa").mkdir(exist_ok=True)
    raw = g.ask(CAST_PROMPT, "BIBLE (existing keys):\n" + known + "\n\nNEW CHAPTERS:\n" + "\n\n".join(english),
                json_out=True, temperature=0.2)
    new = _extract_json(raw).get("new", [])
    need_flow = []
    for c in new:
        key = c.get("key", "").strip()
        if not key or key in roster:
            continue
        flow = c["display"] if c.get("recurring") else None
        roster[key] = {"display": c["display"], "flow_name": flow, "hi": c.get("hi", ""),
                       "tier": 3, "minor": bool(c.get("minor")), "chapters": str(c.get("chapters", "")),
                       "appearance": c.get("appearance", ""), "role": c.get("role", ""),
                       "invented": "", "added_episode": episode}
        if flow:
            need_flow.append(key)
        print(f"cast: + {key} ({c['display']}, ch {c.get('chapters')})"
              f"{' -> Flow Character' if flow else ' (described in prompts)'}")
    doc["covers_episodes"] = sorted(covered | {episode})
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    from . import episode_cast
    argv, sys.argv = sys.argv, ["episode_cast", "--episode", str(episode)]
    try:
        episode_cast.main()
    finally:
        sys.argv = argv
    print(f"cast: {len(new)} new characters, {len(need_flow)} need a Flow Character")
    return need_flow


def scenes(ep_root: Path, episode: int, chapters: list[int]) -> list[int]:
    """Write missing or stale scenes. Returns chapters left out because their images exist."""
    lock = json.loads(E.lock_path(ep_root).read_text(encoding="utf-8"))
    todo, blocked = [], []
    for n in chapters:
        path = scenes_path(ep_root, n)
        if not path.exists():
            todo.append(n)
            continue
        current = json.loads(path.read_text(encoding="utf-8")).get("hindi_sha256") == lock.get(f"{n:05d}.txt")
        if current and not check_chapter(ep_root, n):
            continue                                   # current and passes its check
        img = ep_root / "images" / f"c{n:04d}"
        if img.exists() and any(img.glob("s*-2k.jpg")):
            blocked.append(n)
            continue
        old = ep_root / "old" / f"scenes-{datetime.now():%Y%m%dT%H%M%S}"
        old.mkdir(parents=True, exist_ok=True)
        path.replace(old / path.name)
        todo.append(n)
    if todo:
        print(f"scenes: writing {len(todo)} chapters (1 Gemini call each)")
        try:
            cmd_write(SimpleNamespace(episode=episode, chapters=",".join(map(str, todo)),
                                      model=None, force=False, plan=False))
        except SystemExit as e:
            if e.code:
                sys.exit("scenes: some chapters failed (see above); fix and run this again")
    else:
        print("scenes: all current")
    return blocked


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--episode", type=int, required=True)
    ap.add_argument("--chapters", default=None, help="e.g. 41-45; default the whole episode")
    a = ap.parse_args()
    ep_root = E.root(a.episode)
    man = Manifest.load(ep_root / "manifest.json")
    chapters = parse_range(a.chapters) if a.chapters else [c.idx for c in man.chapters]
    for n in chapters:
        E.check_lock(ep_root, n)                       # scenes need the final, locked Hindi

    setup(ep_root, a.episode)
    cast(ep_root, a.episode, man)
    roster = json.loads((ep_root / "bible" / "characters.json").read_text(encoding="utf-8"))["characters"]
    need_flow = [k for k, c in roster.items()      # added by this episode, no Flow Character made yet
                 if c.get("flow_name") and c.get("added_episode") == a.episode
                 and not (ep_root / "bible" / "flow" / f"{k}-body.jpg").exists()]
    blocked = scenes(ep_root, a.episode, chapters)
    ready = [n for n in chapters if n not in blocked]
    cmd_plan(SimpleNamespace(episode=a.episode, chapters=",".join(map(str, ready))))

    rel = ep_root.relative_to(Path.cwd()).as_posix() if ep_root.is_relative_to(Path.cwd()) else ep_root.as_posix()
    print("\nNEXT")
    if blocked:
        print(f"  ! chapters {blocked}: Hindi changed after their scenes, but images exist. "
              f"Move images/cNNNN to old/ and run this again to redo them.")
    step = 1
    print(f"  {step}. powershell -File flow\\start-chrome.ps1   (open the Flow project)")
    if need_flow:
        step += 1
        print(f"  {step}. node flow/char.mjs {rel} {' '.join(need_flow)}   "
              f"(new Flow Characters; check {rel}/cast-sheet.md)")
    print(f"  {step + 1}. node flow/preflight.mjs {rel}/flow/plan.json")
    print(f"  {step + 2}. node flow/run.mjs {rel}/flow/plan.json --batch 5")


if __name__ == "__main__":
    main()
