"""Draft scene lists from chapter text, for a human to edit.

The plan assumed scene lists would be hand-authored in the Gemini app. This
does the mechanical 80%: pick which paragraphs deserve an image, work out who
is in them, and write a first-pass prompt. What it CANNOT do is judge whether a
moment is worth drawing - so treat the output as a draft, not an answer.

Three things it gets right that hand-authoring gets wrong:

  * Spacing. Scenes are chosen against the real timing map, so two never land
    closer than MIN_SLIDE_SEC. Phase 4 would merge those anyway, and the second
    image would have been generated for nothing.
  * Character detection uses the alias resolver, so a paragraph naming "Queen
    Mystic" or "Gehrman Sparrow" resolves to the right persona and therefore
    the right face. Matching on bare given names would miss most mentions.
  * It never invents a character. Only names actually present in the paragraph
    are tagged, so an image is never conditioned on someone who is not there.

The prompt text WAS the weak part: make_prompt() extracts a clause from prose
rather than composing a description, and the drafts it produced were narrative
fragments ("sat, road") that describe nothing drawable. That job now belongs to
abpipe.scenewrite, which composes tags with an LLM; `--writer extract` still
selects the old path, kept for offline use and comparison, not because it
works. Selection, spacing and character tagging are unchanged either way.

Usage:
    python -m abpipe.scenebuild --start 850 --end 859           # dry run
    python -m abpipe.scenebuild --start 850 --end 859 --apply
    python -m abpipe.scenebuild --start 850 --end 859 --writer extract
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .scenes import MIN_SLIDE_SEC, load_bible, resolve_character

# Paragraphs that read as visual get picked; dialogue and interiority do not.
# These are weights, not rules - the aim is to rank, not to classify.
VISUAL = re.compile(
    r"\b(stood|stepped|walked|ran|sat|knelt|rose|turned|entered|emerged|"
    r"opened|closed|raised|drew|held|reached|climbed|descended|flew|fell|"
    r"street|road|door|window|room|hall|castle|church|tower|bridge|dock|"
    r"harbou?r|ship|carriage|forest|mountain|sea|sky|moon|sun|lamp|lantern|"
    r"candle|fire|flame|smoke|fog|mist|rain|snow|blood|mirror|sword|gun|"
    r"table|chair|bed|stair|corridor|garden|square|market|alley)\b", re.I)
ABSTRACT = re.compile(
    r"\b(thought|wondered|realized|realised|remembered|considered|understood|"
    r"believed|decided|knew|felt that|seemed to think|meant that|in other words)\b", re.I)


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def score(para: str) -> float:
    """How worth drawing is this paragraph? Higher is better."""
    if len(para) < 60:
        return 0.0
    vis = len(VISUAL.findall(para))
    abst = len(ABSTRACT.findall(para))
    # Dialogue-heavy paragraphs make poor images: quoted speech is most of the
    # text and the visual content is whatever little surrounds it.
    quoted = para.count('"') + para.count('“')
    return vis * 1.0 - abst * 0.8 - quoted * 0.4


def characters_in(para: str, bible: dict) -> list[str]:
    """Persona keys for every character actually named in this paragraph.

    Longest names first so "Zhou Mingrui" is consumed before "Zhou", and each
    character is tagged once even if several of their aliases appear.
    """
    found: dict[str, str] = {}
    names = []
    for ckey, rec in bible.items():
        for pkey, p in (rec.get("personas") or {}).items():
            for a in p.get("aliases", []):
                names.append((a, ckey, pkey))
    for alias, ckey, pkey in sorted(names, key=lambda n: -len(n[0])):
        if len(alias) < 4:
            continue
        if re.search(rf"\b{re.escape(alias)}\b", para) and ckey not in found:
            found[ckey] = pkey
    return [f"{c}/{p}" for c, p in found.items()]


def make_prompt(para: str, names: set[str]) -> str:
    """First-pass visual description. Mechanical - expect to rewrite it.

    Character names are STRIPPED. The IP-Adapter reference already supplies who
    is in frame, so a name in the prompt buys nothing and costs two ways: it
    spends tokens from CLIP's 77, and SDXL will sometimes try to render an
    unfamiliar proper noun as lettering in the image.

    Quoted speech is dropped first - it describes nothing visible - then the
    clause richest in concrete nouns is kept, capped short because the prompt
    shares its budget with the style prefix and the character description.
    """
    body = re.sub(r'"[^"]*"', " ", para)
    body = re.sub(r"“[^”]*”", " ", body)
    for n in sorted(names, key=len, reverse=True):
        if len(n) >= 4:
            # \b matters: without it "Fors" would eat the middle of other words.
            body = re.sub(rf"\b{re.escape(n)}\b('s)?", " ", body, flags=re.I)
    body = re.sub(r"\s+", " ", body)

    clauses = [c.strip() for c in re.split(r"[,.;]", body) if len(c.strip()) > 20]
    best = max(clauses, key=lambda c: len(VISUAL.findall(c))) if clauses else ""
    if len(VISUAL.findall(best)) < 2:
        # Thin clause: fall back to the concrete nouns themselves, which read
        # more like an image prompt than a fragment of narration does.
        seen, keys = set(), []
        for m in VISUAL.finditer(body):
            w = m.group(0).lower()
            if w not in seen:
                seen.add(w); keys.append(w)
        if len(keys) >= 2:
            return ", ".join(keys[:6])
    words = [w for w in best.split() if w.lower() not in
             ("the","a","an","and","but","of","to","that","which","who","his","her")]
    return " ".join(words[:12]).strip().lower()


def build_chapter(root: Path, ch: dict, bible: dict, target: int,
                  writer: str = "extract") -> dict | None:
    src = root / "chapters" / ch["source"]
    tmap_p = root / "timing" / f"{ch['idx']:05d}.json"
    if not src.exists() or not tmap_p.exists():
        return None
    paras = paragraphs(src.read_text(encoding="utf-8"))
    tmap = json.loads(tmap_p.read_text(encoding="utf-8"))
    starts: dict[int, float] = {}
    for c in tmap["chunks"]:
        starts.setdefault(c["para"], c["start_sec"])

    ranked = sorted(
        ((score(p), i) for i, p in enumerate(paras) if i in starts),
        reverse=True)
    chosen: list[int] = []
    for s, i in ranked:
        if s <= 0 or len(chosen) >= target:
            continue
        # Never place two scenes closer than the video can show them.
        if all(abs(starts[i] - starts[j]) >= MIN_SLIDE_SEC for j in chosen):
            chosen.append(i)
    chosen.sort()

    all_names = {a for rec in bible.values()
                 for pp in (rec.get("personas") or {}).values()
                 for a in pp.get("aliases", [])}

    # Only the PROMPT is delegated. Which paragraphs get an image, and who is
    # in them, stay here: the spacing above is enforced against the real timing
    # map, and characters_in() below tags only names the paragraph actually
    # contains. A writer that could add either could place an image at a
    # timestamp that does not exist, or condition a scene on someone who is not
    # in it - the two failures the mechanical layer exists to prevent.
    # Resolved once and reused: it decides both which subject tag the writer is
    # told to use and who the finished scene records as present.
    chars = {i: characters_in(paras[i], bible) for i in chosen}

    if writer == "llm":
        from .scenewrite import write_prompts
        items = []
        for i in chosen:
            # The persona's decided subject tag, not the wiki's character-level
            # sex. Lumian is "Hermaphrodite" in the wiki and has both a male and
            # a female persona; leaving the model to infer it from prose gave
            # 1girl on one paragraph and 1boy on the next, which makes the same
            # character change sex between slides.
            subs = [s for key in chars[i]
                    for s in [((bible.get(key.partition("/")[0], {})
                                .get("personas") or {})
                               .get(key.partition("/")[2], {}) or {})
                              .get("subject")] if s]
            items.append({"para": i, "text": paras[i], "subjects": subs})
        prompts = write_prompts(items)
        declined = len(chosen) - len(prompts)
        if declined:
            print(f"    writer declined {declined} of {len(chosen)} paragraphs "
                  f"as not worth drawing")
    else:
        prompts = {i: make_prompt(paras[i], all_names) for i in chosen}

    scenes = []
    for i in chosen:
        prompt = prompts.get(i, "")
        if not prompt:
            continue
        # id assigned AFTER filtering, so the sequence has no gaps - a missing
        # sNNN.png should mean "not generated yet", not "never existed".
        scenes.append({"id": len(scenes), "para": i,
                       "characters": chars[i],
                       "prompt": prompt, "start_sec": round(starts[i], 1)})

    note = ("Prompts composed by abpipe.scenewrite (Gemini). Selection, "
            "spacing and character tagging are still mechanical."
            if writer == "llm" else
            "DRAFT - prompts are extracted from prose, not composed. "
            "Edit before generating.")
    return {"version": 1, "source": ch["source"],
            "generated": f"abpipe.scenebuild ({writer})",
            "note": note, "scenes": scenes}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="workspace")
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--target", type=int, default=22, help="scenes per chapter")
    # llm composes a description; extract pulls a clause out of the prose and
    # is kept only as the offline path - its output was measured unusable, see
    # this module's docstring.
    ap.add_argument("--writer", choices=["llm", "extract"], default="llm")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    bible = load_bible(root)
    man = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    todo = [c for c in man["chapters"]
            if c["source"][:4].isdigit() and args.start <= int(c["source"][:4]) <= args.end]

    total, tagged = 0, 0
    for ch in todo:
        doc = build_chapter(root, ch, bible, args.target, writer=args.writer)
        if doc is None:
            print(f"  skip {ch['source']}: no text or no timing map")
            continue
        n_ch = sum(1 for s in doc["scenes"] if s["characters"])
        total += len(doc["scenes"]); tagged += n_ch
        print(f"  {ch['source'][:4]}  {len(doc['scenes']):3} scenes, "
              f"{n_ch:3} with characters")
        if args.apply:
            out = root / "scenes" / f"{ch['idx']:05d}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"\n{total} scenes across {len(todo)} chapters, {tagged} with characters")
    if not args.apply:
        print("dry run - nothing written. re-run with --apply")


if __name__ == "__main__":
    main()
