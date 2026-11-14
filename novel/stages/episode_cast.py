"""Write cast-sheet.md: what the user needs to create each Flow Character.

Generated from bible/characters.json, so the sheet and the names the image
prompts attach can never disagree. Edit the JSON, then run this again.

    python -m abpipe.episode_cast --episode 1
    python -m abpipe.episode_cast --episode 2 --merge workspace/episodes/E002/gemini-cast.json

--merge takes the JSON that Gemini returns when it is told "cast sheet" (see episode_prompt):

    {"characters":    {key: {display, flow_name, hi, tier, minor, chapters, appearance, invented, role}},
     "outfits":       {key: {"<chapters> <label>": "what they wear or carry"}},
     "content_flags": {"<chapter or range>": "what the chapter shows that must be softened"}}

New characters are added, outfits and flags are added or replaced, and a character already in the
cast keeps its look (its Flow Character already exists). bible/characters.json is first saved as
characters.before-merge.json, once. The sheet is then rewritten.
"""

from __future__ import annotations

import argparse
import json
import re

from . import episode as E

# Flow builds a Character from one portrait plus an info line; its own "Create
# body" button makes the full-body image from that portrait (see
# hungry-tree/tools/char.mjs), so there is no separate body prompt.
# The style goes FIRST and is word-for-word the same for every character:
# image models weight the start of a prompt most, and identical wording is
# what keeps twelve separately generated portraits in one art style.
PORTRAIT = ("{style}. Character portrait of a {appearance}. Head and shoulders, facing the "
            "viewer, plain warm-gray background, even soft light. No text, no logo.")

CHARACTER_FIELDS = ("display", "flow_name", "hi", "tier", "minor", "chapters", "appearance", "invented", "role")


def chapter_set(text: str) -> set[int]:
    """{26, 31, 32} from "26,31-32"; anything that is not a number or range is ignored."""
    out: set[int] = set()
    for part in re.split(r"[,\s]+", str(text)):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if m:
            out.update(range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1))
    return out


def load_bible(ep_root) -> dict:
    return json.loads((ep_root / "bible" / "characters.json").read_text(encoding="utf-8"))


def merge(ep_root, source) -> None:
    """Fold a Gemini cast file into the episode's bible (see the module doc)."""
    path = ep_root / "bible" / "characters.json"
    backup = path.with_name("characters.before-merge.json")
    if not backup.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    doc = json.loads(path.read_text(encoding="utf-8"))
    new = json.loads(open(source, encoding="utf-8").read())
    cast = doc["characters"]
    added, kept, unknown = [], [], []
    for key, c in (new.get("characters") or {}).items():
        if key in cast:
            kept.append(key)
            continue
        row = {f: c.get(f) for f in CHARACTER_FIELDS}
        row["minor"] = bool(row["minor"])
        if row["invented"] in (None, "none"):
            row["invented"] = ""
        cast[key] = row
        added.append(key)
    n_outfits = 0
    for key, outfits in (new.get("outfits") or {}).items():
        if key not in cast:
            unknown.append(key)
            continue
        cast[key].setdefault("outfits", {}).update(outfits)
        n_outfits += len(outfits)
    flags = doc.setdefault("content_flags", {})
    n_flags = 0
    for key, text in (new.get("content_flags") or {}).items():
        for n in sorted(chapter_set(key)):          # "23-24" becomes one note for chapter 23 and one for 24
            flags[str(n)] = text
            n_flags += 1
    if "Extended" not in doc.get("_README", ""):
        doc["_README"] = (doc.get("_README", "") + " Extended with Gemini cast files (episode_cast --merge): "
                          "new characters, outfits per chapter and content notes.").strip()
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"merged: {len(added)} new characters {added}; {len(kept)} already in the cast (kept as they are); "
          f"{n_outfits} outfit notes; {n_flags} chapter content notes")
    if unknown:
        print(f"  outfit notes for characters not in the cast, skipped: {unknown}")


def sheet(episode: int) -> None:
    ep_root = E.root(episode)
    doc = load_bible(ep_root)
    style, cast = doc["style"], doc["characters"]
    info = E.info(ep_root)
    here = set(range(info.get("start", 1), info.get("end", info["start"] + E.CHAPTERS_PER_EPISODE - 1) + 1))

    make, made, text_only, later = [], [], [], []
    for k, c in cast.items():
        in_range = not chapter_set(c["chapters"]) or bool(chapter_set(c["chapters"]) & here)
        if c["flow_name"] and c.get("flow_made"):
            made.append((k, c))
        elif not in_range:
            later.append((k, c))
        elif c["flow_name"]:
            make.append((k, c))
        else:
            text_only.append((k, c))

    out = [f"# Episode {episode} cast (chapters {min(here)}-{max(here)})", "",
           f"Create **{len(make)} new Flow Character{'s' if len(make) != 1 else ''}**. Name each one "
           "**exactly** as shown: the image script attaches them by name.", ""]
    if make:
        out += ["| # | Flow name | Chapters | Notes |", "|---|---|---|---|"]
        for i, (k, c) in enumerate(make, 1):
            notes = []
            if c["minor"]:
                notes.append("**minor**: drawn age-appropriate, always fully clothed")
            if c["invented"]:
                notes.append(c["invented"])
            out.append(f"| {i} | `{c['flow_name']}` | {c['chapters']} | {'; '.join(notes)} |")
        out.append("")
    if made:
        out += [f"Already in Flow ({len(made)}): " + ", ".join(c["flow_name"] for _, c in made), ""]
    if text_only:
        out += [f"These {len(text_only)} appear once or twice. They get **no** Flow Character: "
                "each image describes them in its prompt instead.", ""]
        out += [f"- {c['display']} (ch {c['chapters']}){' - minor' if c['minor'] else ''}: "
                f"{c['appearance']}" for k, c in text_only]
        out.append("")
    if later:
        out += ["## Later episodes", "",
                "In the cast already, not needed for these chapters:", ""]
        out += [f"- {c['display']} (ch {c['chapters']}), "
                f"{'Flow Character `' + c['flow_name'] + '`' if c['flow_name'] else 'no Flow Character'}"
                for k, c in later]
        out.append("")
    def key_chapters(key: str) -> set[int]:       # "9-10,13-14,19 dream ruins" -> {9, 10, 13, 14, 19}
        m = re.match(r"^\s*(\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*)", key)
        return chapter_set(m.group(1).replace(" ", "")) if m else set()

    outfit_rows = [(c["display"], key, text) for k, c in cast.items() for key, text in (c.get("outfits") or {}).items()
                   if key_chapters(key) & here]
    if outfit_rows:
        out += ["## Outfits and props that change in these chapters", ""]
        out += [f"- **{who}** ({key}): {text}" for who, key, text in outfit_rows]
        out.append("")
    todo_v, made_v = [], []
    for k, c in cast.items():
        for v in c.get("variants") or []:
            (made_v if v.get("flow_made") else todo_v).append((c, v))
    if todo_v or made_v:
        out += ["## Outfit variants (same person, other clothes: one Flow Character each)", "",
                "A scene attaches the variant instead of the base character when its chapter (and keywords, if any) "
                "match and the variant is marked made. Make each from the base character: generate its portrait "
                "with the base Flow Character attached and the outfit below, then create the Character from that "
                "portrait and add the body.", ""]
        for c, v in todo_v + made_v:
            where = f"ch {v['chapters']}" + (f", scenes with: {v['keywords']}" if v.get("keywords") else "")
            out.append(f"- {'[made] ' if v.get('flow_made') else ''}`{v['flow_name']}` ({where}): {v['outfit']}")
        out.append("")
    notes = {n: t for n, t in (doc.get("content_flags") or {}).items() if n.isdigit() and int(n) in here}
    if notes:
        out += ["## Chapters with content to soften in the pictures", ""]
        out += [f"- Chapter {n}: {t}" for n, t in sorted(notes.items(), key=lambda x: int(x[0]))]
        out.append("")
    if make:
        out += ["## Prompts", "",
                "For each: new Character in Flow, paste the portrait prompt, pick the best result, "
                "set the name and info exactly as below, then **Create body** and save.", ""]
        for i, (k, c) in enumerate(make, 1):
            out += [f"### {i}. {c['flow_name']}", "",
                    "Portrait prompt:", "```",
                    PORTRAIT.format(style=style, appearance=c["appearance"]),
                    "```", f"Info: {c['role']}", ""]
    path = ep_root / "cast-sheet.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {path.resolve()}: {len(make)} to create, {len(made)} already in Flow, "
          f"{len(text_only)} text-only, {len(later)} for later episodes")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--episode", type=int, required=True)
    ap.add_argument("--merge", metavar="FILE", help="fold Gemini's cast JSON into bible/characters.json first")
    args = ap.parse_args()
    if args.merge:
        merge(E.root(args.episode), args.merge)
    sheet(args.episode)


if __name__ == "__main__":
    main()
