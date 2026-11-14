"""Compose scene prompts with an LLM, because extracting them from prose failed.

`scenebuild.make_prompt()` picks the clause richest in concrete nouns out of a
paragraph and hands that to the image model. The result is on record, in the
note left at the top of workspace/scenes/00048.json:

    "The scenebuild drafts were narrative fragments ('sat, road') that
     describe nothing drawable."

A fragment of narration is not a description of a picture, and no amount of
regex tuning turns one into the other - the missing step is composition, which
is a language task. The ten chapters of scene lists that exist were therefore
written by hand, and hand-authoring does not reach 227 chapters.

What this module does NOT do is choose which paragraphs get an image, or work
out who is in them. scenebuild already does both, against the real timing map
and the alias resolver, and both are mechanical guarantees worth keeping:
scenes never land closer than MIN_SLIDE_SEC, and a character is never tagged
unless the paragraph actually names them. This module only writes the words.

The model may decline a paragraph by omitting it. That is deliberate - the
selector ranks by a regex heuristic and cannot tell that a paragraph is all
interiority, so letting the writer drop one is the cheaper half of the fix.

Prompts come back as Danbooru-style tags, not prose, because Animagine XL 4.0
is tag-trained and documents natural-language input as "may not be effective".
Style and quality tags are NOT the writer's job - images._prompt_for adds those
from the model profile, so they stay defined in exactly one place.

Usage:
    export GEMINI_API_KEY=...        # or put it in .env
    python -m abpipe.scenewrite --idx 48            # print, write nothing
    python -m abpipe.scenewrite --idx 48 --apply
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

# gemini-2.5-flash is retired for new API keys - it returns 404 with a message
# naming this as the replacement. Kept as a named constant because the failure
# is a clean 404 rather than a silent fallback, so a future retirement will
# say so too.
MODEL = "gemini-3.6-flash"

# Tag budget. CLIP truncates at 77 tokens and the profile's style prefix and
# quality suffix are charged against every prompt (~30 tokens together for the
# Animagine profile), so the body gets roughly 45. assert_prompt_fits is the
# hard gate; this keeps us clear of it rather than discovering it per image.
MAX_TAGS = 16

INSTRUCTIONS = """\
You write image-generation prompts for an anime-styled illustrated audiobook.

For each numbered paragraph below, write ONE prompt describing a single
drawable moment from it.

Rules:
- Output Danbooru-style TAGS, comma-separated, lowercase. Not a sentence.
- Begin with the subject-count tag. When a paragraph is labelled SUBJECTS,
  use EXACTLY those tags and nothing else for the count. They are decided per
  character and must not vary between paragraphs: the same person rendered as
  1girl on one slide and 1boy on the next stops being the same person, which
  is the whole thing these images are for. Where no SUBJECTS are given, use
  "no humans" unless the paragraph plainly shows someone unnamed.
- NEVER use a character's name or any proper noun. A reference portrait
  supplies who the person is; a name here wastes prompt budget and the image
  model will sometimes render it as lettering inside the picture.
- Describe only what is VISIBLE: pose, clothing, expression, objects,
  setting, time of day, weather, lighting.
- Do NOT include art-style tags (gothic, victorian, cinematic lighting) or
  quality tags (masterpiece, absurdres). Those are added separately.
- At most %d tags.
- Aim to write a prompt for EVERY paragraph listed. Nearly all of them hold
  something drawable - a room, a street, the weather, how a person is standing
  - and a slide that only establishes the setting is still worth having.
  Omit a paragraph ONLY when it is pure abstract reasoning with no physical
  setting whatsoever.

Return JSON only, no markdown fence:
{"scenes": [{"para": <the paragraph number>, "prompt": "<tags>"}]}
""" % MAX_TAGS


def api_key() -> str:
    """GEMINI_API_KEY from the environment, or from .env beside the repo."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    from novel.core import paths, secrets
    keys = secrets.gemini_keys()
    if keys:
        return keys[0]
    raise SystemExit(
        f"no GEMINI_API_KEY - put it in {paths.secrets_file()} or export it")


def _client():
    try:
        from google import genai
    except ImportError:
        raise SystemExit("pip install google-genai")
    return genai.Client(api_key=api_key())


def _extract_json(text: str) -> dict:
    """Parse the reply, tolerating a markdown fence around it.

    response_mime_type asks for bare JSON and usually gets it, but a fence
    slipping through would otherwise fail the whole chapter for a cosmetic
    reason.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    return json.loads(text)


def _clean(prompt: str) -> str:
    """Normalise one prompt to bare tags, and cap the tag count.

    The cap is enforced here rather than trusted to the instructions: an
    over-long prompt does not fail, it gets SILENTLY TRUNCATED by CLIP at 77
    tokens, which is the failure mode assert_prompt_fits exists to catch and
    the one that produced beautiful pictures of the wrong character.
    """
    prompt = prompt.strip().strip(".").lower()
    prompt = re.sub(r"\s+", " ", prompt)
    tags, seen = [], set()
    for t in prompt.split(","):
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
    return ", ".join(tags[:MAX_TAGS])


def _retryable(exc: Exception) -> bool:
    """Is waiting and trying again worth it?

    Only rate limits and server faults are. A 404 (retired model), 400 (bad
    request) or 403 (bad key) fails identically every time, and backing off
    four times turns a clear message into 35 wasted seconds per chapter with
    the reason buried above the retries. Measured: that is exactly what the
    gemini-2.5-flash retirement did on the first run of this module.

    An error with no status code is a transport or JSON-decode failure, which
    IS worth retrying.
    """
    code = getattr(exc, "code", None)
    if not isinstance(code, int):
        return True
    return code == 429 or code >= 500


def write_prompts(items: list[dict], *, model: str = MODEL,
                  retries: int = 4) -> dict[int, str]:
    """paragraph index -> tag prompt, for the paragraphs worth drawing.

    `items` is [{"para": int, "text": str}]. Paragraphs the model declines are
    simply absent from the result; the caller decides what that means.
    """
    if not items:
        return {}
    from google.genai import types

    blocks = []
    for it in items:
        subs = ", ".join(it.get("subjects") or [])
        head = f"[{it['para']}]" + (f"  SUBJECTS: {subs}" if subs else "")
        blocks.append(f"{head}\n{it['text']}")
    body = "\n\n".join(blocks)
    client = _client()

    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=INSTRUCTIONS + "\n\n" + body,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.4,
                ),
            )
            data = _extract_json(resp.text)
            break
        except Exception as e:                      # transport, quota, or JSON
            if not _retryable(e) or attempt == retries - 1:
                raise SystemExit(f"gemini: {e}")
            # Free tier is rate-limited per minute; back off rather than
            # hammering it and burning the day's quota on 429s.
            wait = 5 * (2 ** attempt)
            print(f"    gemini error ({type(e).__name__}), retrying in {wait}s")
            time.sleep(wait)

    valid = {it["para"] for it in items}
    out: dict[int, str] = {}
    for s in data.get("scenes", []):
        para, prompt = s.get("para"), (s.get("prompt") or "").strip()
        # A para the model invented would place an image at a timestamp that
        # does not exist. Only paragraphs we asked about are accepted.
        if para in valid and prompt:
            out[int(para)] = _clean(prompt)
    return out


def main() -> None:
    import argparse

    import yaml

    from .scenes import load_bible
    from .scenebuild import build_chapter

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--root", default=None)
    ap.add_argument("--idx", type=int, required=True, help="chapter index")
    ap.add_argument("--target", type=int, default=22)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    root = Path(args.root) if args.root else Path(cfg["paths"]["root"])

    man = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    ch = next((c for c in man["chapters"] if c["idx"] == args.idx), None)
    if ch is None:
        raise SystemExit(f"no chapter with idx {args.idx}")

    doc = build_chapter(root, ch, load_bible(root), args.target, writer="llm")
    if doc is None:
        raise SystemExit(f"chapter {args.idx}: no source text or no timing map")

    print(f"\n{ch['source']}  {len(doc['scenes'])} scenes\n")
    for s in doc["scenes"]:
        who = ", ".join(s["characters"]) or "-"
        print(f"  s{s['id']:03d}  para {s['para']:>3}  {s['start_sec']:7.1f}s  "
              f"[{who}]\n        {s['prompt']}")

    if args.apply:
        out = root / "scenes" / f"{args.idx:05d}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\nwrote {out}")
    else:
        print("\ndry run - nothing written. re-run with --apply")


if __name__ == "__main__":
    main()
