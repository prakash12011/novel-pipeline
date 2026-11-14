"""Scene lists: which image goes where in a chapter.

A scene list is hand-authored (Gemini app, pasted into a file) and says, for a
handful of moments in a chapter, what to draw and who is in it. It is joined to
the timing map by paragraph index, which is what turns a scene into a timestamp:

    scene.para -> timing/NNNNN.json -> start_sec -> a slide in the video

This module never generates anything. It exists to catch the errors that are
invisible until much later:

  * A `para` past the end of the chapter. Generates a perfectly good image that
    is never shown, because no slide is ever scheduled for it.
  * A character slug that is not in the bible, or is but has no approved
    reference image. This is the expensive one: without a reference the scene
    still renders, in the right style, with the wrong person's face - which is
    the exact failure the bible exists to prevent, and it does not look like an
    error on a contact sheet.
  * Scenes packed closer than the video can show them. Phase 4 merges anything
    under ~8 seconds apart, so two scenes one paragraph apart means one of the
    two images was generated for nothing.

Usage:
    python -m abpipe.scenes --check              # every scene list
    python -m abpipe.scenes --check --idx 48     # just one chapter
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# `yaml` and `Manifest` are imported inside main(), not here. Colab needs only
# load_bible/load_scenes out of this module, and both are plain JSON - keeping
# the CLI's dependencies out of the import path means the GPU side copies two
# files instead of three and cannot drift on a module it never calls.

# Phase 4 merges two scenes landing closer than this; anything under it is a
# flicker rather than a slide. Kept here because it is a property of the scene
# list, not of the assembler that will later enforce it.
MIN_SLIDE_SEC = 8.0


def load_bible(root: Path) -> dict[str, dict]:
    """Character key -> record. Missing bible is an empty one, not an error:
    scene lists with no characters at all are legitimate (establishing shots).

    Records hold PERSONAS, not a single appearance. One character can appear on
    screen as several different people - Klein Moretti is also Gehrman Sparrow,
    Sherlock Moriarty and The Fool - and each needs its own face. The reference
    image therefore lives on the persona.
    """
    path = root / "bible" / "characters.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    chars = raw["characters"] if isinstance(raw, dict) and "characters" in raw else raw
    if isinstance(chars, list):          # pre-persona format
        return {c["slug"]: c for c in chars}
    return chars


def _norm(name: str) -> str:
    """Fold a name to a comparison key: lowercase, non-alphanumerics dropped.

    So "Queen Mystic", "queen-mystic" and "QueenMystic" all match, and a scene
    list can be written in whatever form reads naturally.
    """
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def resolve_character(bible: dict[str, dict], name: str) -> tuple[str | None, str | None]:
    """Any name or alias -> (character_key, persona_key). (None, None) if unknown.

    Two problems at once. Characters carry many names - Bernadette Gustav is
    "Queen Mystic"; Fors Wall is "Madam Magician" - and a model cannot infer
    that from prose: mining "known as" sentences returns world-building ("the
    Five Seas", "the Iron Age"), not people. And some of those names are not
    just labels but DIFFERENT FACES: Gehrman Sparrow does not look like Klein
    Moretti, so they cannot share one reference image.

    Resolving to a persona answers both. A name that identifies the character
    generically falls back to `default_persona`.

    Getting this wrong is expensive in a specific way: an unresolved name means
    a scene renders with no reference image - a stylistically perfect picture of
    the wrong person, the exact failure the bible exists to prevent.
    """
    # A scene list may address a persona directly as "character/persona".
    # scenebuild writes that form, and it is unambiguous - accept it before
    # falling back to alias matching, which would fold the slash into one word
    # and match nothing.
    if "/" in str(name):
        ck, _, pk = str(name).partition("/")
        if ck in bible and pk in (bible[ck].get("personas") or {}):
            return ck, pk

    want = _norm(name)
    for ckey, rec in bible.items():
        personas = rec.get("personas") or {}
        for pkey, p in personas.items():
            if want == _norm(pkey) or any(want == _norm(a) for a in p.get("aliases", [])):
                return ckey, pkey
        if want == _norm(ckey) or want == _norm(rec.get("display", "")):
            return ckey, rec.get("default_persona") or next(iter(personas), None)
    return None, None


def persona_ref(bible: dict[str, dict], ckey: str, pkey: str) -> str | None:
    """The reference image path for one persona, or None if not approved yet."""
    return ((bible.get(ckey, {}).get("personas") or {}).get(pkey) or {}).get("ref")


def load_scenes(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc["scenes"] if isinstance(doc, dict) else doc


def check_chapter(root: Path, ch, bible: dict[str, dict]) -> list[str]:
    """Validate one chapter's scene list. Returns a list of problems."""
    scenes_path = root / "scenes" / f"{ch.idx:05d}.json"
    if not scenes_path.exists():
        return []                      # not authored yet; not an error

    problems: list[str] = []
    scenes = load_scenes(scenes_path)
    if not scenes:
        return [f"{scenes_path.name}: no scenes"]

    timing_path = root / "timing" / f"{ch.idx:05d}.json"
    if not timing_path.exists():
        return [f"{scenes_path.name}: no timing map at {timing_path.name} - "
                f"run the chapter through TTS, or `abpipe.timing --backfill`"]
    tmap = json.loads(timing_path.read_text(encoding="utf-8"))

    # First chunk of each paragraph is when that paragraph starts being read.
    starts: dict[int, float] = {}
    for c in tmap["chunks"]:
        starts.setdefault(c["para"], c["start_sec"])
    last_para = max(starts)

    seen_ids: set[int] = set()
    placed: list[tuple[float, dict]] = []
    for s in scenes:
        sid = s.get("id")
        if sid in seen_ids:
            problems.append(f"{scenes_path.name}: duplicate scene id {sid}")
        seen_ids.add(sid)

        para = s.get("para")
        if para is None or para not in starts:
            problems.append(
                f"{scenes_path.name} scene {sid}: para {para} is not in this "
                f"chapter (it has paragraphs 0-{last_para})"
            )
            continue

        for name in s.get("characters", []):
            # Resolve through aliases AND personas: a scene may say "Queen
            # Mystic" or "Gehrman Sparrow", which name a persona, not a slug.
            ckey, pkey = resolve_character(bible, name)
            if ckey is None:
                problems.append(
                    f"{scenes_path.name} scene {sid}: '{name}' is not in the "
                    f"bible under any name, alias or persona - the scene would "
                    f"render with the wrong face and no error"
                )
                continue
            ref = persona_ref(bible, ckey, pkey)
            if not ref or not (root / "bible" / ref).exists():
                problems.append(
                    f"{scenes_path.name} scene {sid}: '{name}' -> "
                    f"{ckey}/{pkey} has no reference image "
                    f"({ref or 'unset'}) - add one to bible/refs/"
                )

        if not s.get("prompt", "").strip():
            problems.append(f"{scenes_path.name} scene {sid}: empty prompt")

        placed.append((starts[para], s))

    # Too close together: the video cannot show both.
    placed.sort(key=lambda p: p[0])
    for (t0, a), (t1, b) in zip(placed, placed[1:]):
        if t1 - t0 < MIN_SLIDE_SEC:
            problems.append(
                f"{scenes_path.name}: scenes {a.get('id')} and {b.get('id')} "
                f"are {t1 - t0:.1f}s apart (min {MIN_SLIDE_SEC:.0f}s) - phase 4 "
                f"would merge them, so one image is generated for nothing"
            )
    return problems


def report_chapter(root: Path, ch, bible: dict[str, dict]) -> None:
    """Print the slide schedule a valid scene list implies."""
    scenes = load_scenes(root / "scenes" / f"{ch.idx:05d}.json")
    tmap = json.loads((root / "timing" / f"{ch.idx:05d}.json").read_text(encoding="utf-8"))
    starts: dict[int, float] = {}
    for c in tmap["chunks"]:
        starts.setdefault(c["para"], c["start_sec"])

    placed = sorted(((starts[s["para"]], s) for s in scenes), key=lambda p: p[0])
    total = tmap["total_seconds"]
    print(f"\n{ch.source}  ({total/60:.1f} min, {len(scenes)} scenes, "
          f"{'estimated' if not tmap.get('measured') else 'measured'} timing)")
    for i, (t, s) in enumerate(placed):
        end = placed[i + 1][0] if i + 1 < len(placed) else total
        who = ", ".join(s.get("characters", [])) or "-"
        print(f"  {t/60:5.1f}m  {end - t:5.1f}s  para {s['para']:>3}  "
              f"[{who}]  {s.get('prompt', '')[:44]}")


def main() -> None:
    import yaml

    from .manifest import Manifest

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--root", default=None, help="default: paths.root from config")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--idx", type=int, help="only this chapter index")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    root = Path(args.root) if args.root else Path(cfg["paths"]["root"])

    if not args.check:
        ap.error("nothing to do - pass --check")

    bible = load_bible(root)
    # Count PERSONAS, not characters. References live on the persona (that is
    # the whole point of the persona schema - Klein and Gehrman Sparrow need
    # different faces), so counting a character-level `ref` reports 0 for a
    # bible with 23 approved portraits in it. The per-scene check below always
    # used persona_ref() and was right; only this summary line was lying, which
    # is worse than useless - it says the bible is empty when it is not.
    personas = [p for c in bible.values()
                for p in (c.get("personas") or {}).values()]
    print(f"bible: {len(bible)} characters, {len(personas)} personas, "
          f"{sum(1 for p in personas if p.get('ref'))} with a reference")

    man = Manifest.load(root / "manifest.json")
    chapters = [c for c in man.chapters if args.idx is None or c.idx == args.idx]
    authored = [c for c in chapters
                if (root / "scenes" / f"{c.idx:05d}.json").exists()]
    if not authored:
        raise SystemExit(f"no scene lists found in {root / 'scenes'}")

    problems: list[str] = []
    for ch in authored:
        problems += check_chapter(root, ch, bible)

    for p in problems:
        print(f"  PROBLEM {p}")

    if problems:
        # Printed rather than passed to SystemExit so it lands after the list on
        # stdout; as an exit message it goes to stderr and readers see the
        # summary above the problems it summarises.
        print(f"\n{len(problems)} problem(s) across {len(authored)} scene "
              f"list(s) - fix before generating")
        raise SystemExit(1)

    for ch in authored:
        report_chapter(root, ch, bible)
    print(f"\n{len(authored)} scene list(s) OK")


if __name__ == "__main__":
    main()
