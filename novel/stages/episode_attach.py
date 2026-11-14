"""Find scenes where a roster character is in the picture but is not attached to it.

An unattached character gets a random face from Flow ("his big sister" drawn as a stranger, "the three
outsiders" as three men). Step 1, fixed wording rules: a scene whose text names a roster character, or uses a role
phrase for one ("his sister", "the padre", "the three outsiders"), without that character attached, is a candidate.
Step 2, Gemini answers yes/no per candidate: is that character VISIBLY IN the picture (not just mentioned)? Report by default;
--apply writes the additions into scenes/NNNNN.json (at most 4 characters a scene, only roster keys,
nothing is ever removed). Run `episode_scenes plan` afterwards: a prompt whose image already exists is
kept, so delete that image to draw it again.

    py -X utf8 -m abpipe.episode_attach --episode 2 --chapters 13-20            # report
    py -X utf8 -m abpipe.episode_attach --episode 2 --chapters 16-20 --apply    # write the additions
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import episode as E
from .episode_scenes import load_roster, scenes_path
from .episode_translate import MODEL, Gemini, parse_range

MAX_CHARS = 4

# key -> wording that points to the character. Names not listed use the character's full display name.
GROUP = r"three (outsiders|foreigners|investigators|strangers)|\b(outsiders|foreigners|investigators)\b|\bthe trio\b"
ALIASES = {
    "aurore_lee": r"\bAurore\b|\b(big |older |elder |his |her )?sister\b",
    "ryan_koss": rf"\bRyan\b|\bKoss\b|{GROUP}",
    "leah": rf"\bLeah\b|{GROUP}",
    "valentine": rf"\bValentine\b|{GROUP}",
    "guillaume_benet": r"(?<!Deputy )\bpadre\b(?! Michel)|\bpriest\b|Padre Guillaume",
    "madame_pualis": r"\bPualis\b|administrator's wife",
    "pons_benet": r"\bPons\b",
    "tarot_lady": r"\bTarot Lady\b|\btarot lady\b",
    "owl": r"\bowl\b|\bOwl\b",
    "skinless_monster": r"\b[Ss]kinless\b",
    "shotgun_monster": r"\b[Ss]hotgun [Mm]onster\b",
    "three_faced_monster": r"\b[Tt]hree-[Ff]aced\b",
    "guillaume_lizier": r"Guillaume Lizier",
    "guillaume_berry": r"Guillaume Berry",
    "michel_garrigue": r"Michel Garrigue|Deputy Padre",
}
import re

SYSTEM = """You check picture descriptions for an illustrated story video. For each question, decide whether the named character is VISIBLY IN THE PICTURE that the scene text describes.
Answer true when the text shows them: by name, or by a role phrase that points to them ("his sister" means Aurore Lee; "the padre" means Padre Guillaume; "the three outsiders" are Ryan Koss, Leah and Valentine together), including a memory or vision that is drawn as part of the picture.
Answer false when they are only mentioned, discussed, remembered without being drawn, heard, a place or object named after them ("Aurore's bedroom"), a shadow or silhouette, compared with, or off-screen.
ROSTER (key: name - who they are)
{roster}
Reply with JSON only: {{"checks": [{{"id": <scene number>, "key": "<roster key>", "visible": true or false}}]}} with one entry per question."""


def _patterns(roster: dict) -> dict:
    """Wording for each roster key: its listed aliases, else its full name and, when no other character
    shares it, its first name ("Lumian" for Lumian Lee). Matching ignores case."""
    firsts = {}
    for c in roster.values():
        firsts.setdefault(c["display"].split()[0].lower(), 0)
        firsts[c["display"].split()[0].lower()] += 1
    pats = {}
    for key, c in roster.items():
        if key in ALIASES:
            pats[key] = ALIASES[key]
            continue
        parts = [re.escape(c["display"])]
        first = c["display"].split()[0]
        if len(first) >= 4 and firsts[first.lower()] == 1 and first.lower() not in ("madame", "padre", "three", "pierre", "deputy"):
            parts.append(r"\b" + re.escape(first) + r"\b")
        pats[key] = "|".join(parts)
    return pats


def candidates(roster: dict, doc: dict) -> list[tuple[int, str]]:
    pats = _patterns(roster)
    out = []
    for s in doc["scenes"]:
        for key in roster:
            if key not in s["characters"] and re.search(pats[key], s["scene"], re.I):
                out.append((s["id"], key))
    return out


def audit_chapter(g: Gemini, roster: dict, doc: dict) -> list[dict]:
    cands = candidates(roster, doc)
    if not cands:
        return []
    by_id = {s["id"]: s for s in doc["scenes"]}
    lines = [f"- {k}: {c['display']} - {c.get('role', '')}" for k, c in roster.items()]
    system = SYSTEM.format(roster="\n".join(lines))
    msg = "\n".join(f"Scene {i} | {by_id[i]['scene']}\n  Is {roster[k]['display']} ({k}) visibly in this picture?" for i, k in cands)
    data = json.loads(g.ask(system, msg, json_out=True, temperature=0.0))
    yes = {(c["id"], c["key"]) for c in (data["checks"] if isinstance(data, dict) else data) if c.get("visible") is True}
    out = []
    for i in sorted({i for i, _ in cands}):
        s = by_id[i]
        add = [k for (j, k) in cands if j == i and (j, k) in yes]
        if not add:
            continue
        room = MAX_CHARS - len(s["characters"])
        out.append({"id": i, "have": list(s["characters"]), "add": add[:max(room, 0)],
                    "dropped": add[max(room, 0):], "why": "named or pointed to in the scene text", "scene": s["scene"]})
    return [o for o in out if o["add"]]


def apply_fixes(path: Path, doc: dict, fixes: list[dict]) -> None:
    """Write the additions into the scenes file (and into `doc`)."""
    by_id = {f["id"]: f for f in fixes}
    for s in doc["scenes"]:
        if s["id"] in by_id:
            s["characters"] = s["characters"] + [k for k in by_id[s["id"]]["add"] if k not in s["characters"]]
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--episode", type=int, required=True)
    ap.add_argument("--chapters", required=True)
    ap.add_argument("--apply", action="store_true", help="write the additions into the scenes files")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()
    ep_root = E.root(args.episode)
    roster = load_roster(ep_root)["characters"]
    (ep_root / "qa").mkdir(exist_ok=True)
    g = Gemini(args.model, ep_root / "qa" / "attach-gemini.log.jsonl")
    total = 0
    for n in parse_range(args.chapters):
        path = scenes_path(ep_root, n)
        doc = json.loads(path.read_text(encoding="utf-8"))
        fixes = audit_chapter(g, roster, doc)
        done = {s["id"]: os.path.exists(ep_root / "images" / f"c{n:04d}" / f"s{s['id']:02d}-2k.jpg") for s in doc["scenes"]}
        report = ep_root / "qa" / f"{n:05d}.attach.json"
        report.write_text(json.dumps(fixes, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"chapter {n}: {len(fixes)} scenes to fix, {sum(done[f['id']] for f in fixes)} of them already drawn")
        for f in fixes:
            print(f"  s{f['id']:02d} {'DRAWN' if done[f['id']] else 'new  '} {f['have']} + {f['add']}"
                  f"{' (left out: ' + ', '.join(f['dropped']) + ')' if f['dropped'] else ''} | {f['why']}")
        if args.apply and fixes:
            apply_fixes(path, doc, fixes)
        total += len(fixes)
    print(f"\n{total} scenes need characters attached ({g.calls} Gemini calls)" + ("; written" if args.apply else "; report only, nothing changed"))


if __name__ == "__main__":
    main()
