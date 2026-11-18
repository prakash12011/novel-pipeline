"""`novel ktrial front`: one detailed front view per character (bible/qwen/front/<key>_front.jpg), 1088x1472, text only.

Faces were coming out alike (one seed for everyone, and bible looks that name hair, eyes and clothes but not the face),
so every character gets:
  - a `face` (face shape, nose, brows, lips, build, age marks, one telling feature) and a resting `expression`, written
    for the whole cast in one Gemini call so they differ from each other, stored in the bible;
  - a seed of their own (from the key).
No picture QA: character pictures are a manual step, checked by eye.
"""

from __future__ import annotations

import json
import re
import zlib
from pathlib import Path

from novel import bible as B, kcomfy, kprompts

WIDTH, HEIGHT = 1088, 1472
FRONT_DIR = "front"

FACE_API = """You describe the faces of characters of the web novel "Circle of Inevitability" (Lord of Mysteries 2) \
for an anime image model that draws every face alike unless told exactly how each one differs.

For each character of the list return:
- "face": 25-45 words, concrete and visual only: face shape, cheekbones and jaw, nose, brows, eye shape, lips, skin, \
build and height, age marks, and one feature that sets this person apart. Make every face clearly unlike the others in \
the list (vary face shape, nose, brows, build and age). Follow the novel where it says something; invent the rest. For a \
creature, its head and body.
- "expression": 4-10 words, a small resting expression that fits who they are (never "neutral", never a bare feeling \
word), e.g. "a wry, lopsided half-smile, one brow slightly raised".
- "creature": true for a monster, spirit or animal, false for a person.
- "look": the given look, unchanged, unless an image model would draw part of it wrong; then reword only that part \
(for example "fine downy facial hair" on a girl is drawn as a beard; a "red nose" alone is drawn as a clown's ball).

Answer with JSON only: {"<key>": {"face": "...", "expression": "...", "creature": false, "look": "..."}, ...}"""

HEAD = ("The head is upright and level, not tilted and not turned: the face square to the viewer, both eyes looking "
        "straight at the viewer, chin level.")
CALM = ("HEAD AND FACE: like an ID photo: the head perfectly upright and straight, not tilted to either side, not "
        "turned, chin level; both eyes at the same height, looking straight at the viewer; a calm, relaxed face with "
        "the mouth closed.")
HUMAN_POSE = ("POSE: front view, facing the viewer straight on, standing straight in a relaxed natural pose, arms at "
              "the sides, hands relaxed with five fingers each, feet visible on the floor. " + HEAD + " The whole body "
              "from the top of the head to the feet is in the frame, with a little space above and below.")
CREATURE_POSE = ("POSE: front view, facing the viewer straight on, in a calm natural stance, the head level and facing "
                 "the viewer. The whole body is in the frame, with a little space around it.")
ON_FOOT = "Alone and on foot: no horse, mount or other animal with them."
MOUNT = re.compile(r",?\s*\b(?:mounted on|riding|astride|on horseback)\b[^,.;]*", re.I)
DETAIL = ("DETAIL: high detail, crisp clean line art, a carefully drawn face and eyes, clear fabric folds, materials "
          "and textures, even soft light from the front, no harsh shadows.")
END = ("One single figure, no other views. No background scenery, no text, no letters, no labels, no captions, no "
       "borders. Clean eyes, no white specks.")


def seed_of(key: str) -> int:
    """Each character's own seed: one shared seed gave everyone the same face."""
    return zlib.crc32(f"front-{key}".encode()) & 0x7FFFFFFF


def front_prompt(bible: dict, key: str) -> str:
    c = bible["characters"][key]
    style = kprompts._style(bible).replace(
        "16:9 illustration.", "Tall 3:4 full-body character reference on a plain, flat, light neutral gray background.", 1)
    look = kprompts._one(c.get("look") or c.get("role") or "").rstrip(" .")
    mounted = bool(MOUNT.search(look))          # a reference shows the rider alone: the horse merged into the body
    look = MOUNT.sub("", look).rstrip(" ,.")
    face = kprompts._one(c.get("face") or "").rstrip(" .")
    # The bible's resting expression is left out on purpose: smiles and half-grins came out as an anime head tilt
    # (2026-11-18). A reference keeps a calm face; the scene pictures give the mood.
    parts = [style, f"CHARACTER: {c['display']}, {look}."]
    if face:
        parts.append(f"FACE: {face}.")
    if not c.get("creature") or mounted:                # a monster's maw or a beak is not "a closed mouth"
        parts.append(CALM)
    pose = CREATURE_POSE if c.get("creature") and not mounted else HUMAN_POSE
    parts += [pose + (" " + ON_FOOT if mounted else ""), DETAIL,
              END.replace("One single figure", "Only this subject") if c.get("creature") else END]
    return "\n\n".join(parts) + "\n"


def face_request(bible: dict, keys: list[str]) -> str:
    rows = []
    for k in keys:
        c = bible["characters"][k]
        legacy = kprompts._one((c.get("legacy") or {}).get("appearance") or "")
        rows.append({"key": k, "name": c["display"], "role": c.get("role") or "", "look": c.get("look") or "",
                     **({"novel_appearance": legacy[:400]} if legacy and legacy != c.get("look") else {})})
    return "THE CHARACTERS:\n" + json.dumps(rows, ensure_ascii=False, indent=1)


def apply_faces(bible: dict, answer: dict, keys: list[str], say=print) -> list[str]:
    """Write face, expression and creature into the bible; a reworded look replaces the old one (said aloud).
    Returns the keys that got a face."""
    done = []
    for k in keys:
        a = answer.get(k)
        if not isinstance(a, dict) or not str(a.get("face") or "").strip():
            say(f"  {k}: no face in the answer")
            continue
        c = bible["characters"][k]
        c["face"] = " ".join(str(a["face"]).split())
        c["expression"] = " ".join(str(a.get("expression") or "").split())
        c["creature"] = bool(a.get("creature"))
        new_look = " ".join(str(a.get("look") or "").split())
        if new_look and c.get("look") and new_look != c["look"]:
            say(f"  {k}: look reworded\n    was: {c['look']}\n    now: {new_look}")
            c["look"] = new_look
        done.append(k)
    return done


FACE_GROUP = 8                  # characters per call: all 43 at once was refused (PROHIBITED_CONTENT, 2026-11-18)


def add_faces(book: str, keys: list[str], say=print) -> int:
    """Gemini writes a face for the characters of `keys` that have none yet, FACE_GROUP at a time, told the faces
    already written so the new ones differ. A refused group is asked again one character at a time; a character
    still refused keeps no face (drawn from the look alone). The bible is saved after each group (history kept).
    Returns how many are still without a face."""
    from novel.core import settings
    from . import episode_translate as T
    bdir = B.bible_dir(book)
    bible = B.load_characters(bdir)
    need = [k for k in keys if not (bible["characters"][k].get("face") or "").strip()]
    if not need:
        return 0
    model = settings.get("gemini.api_model", book=book)
    say(f"writing faces for {len(need)} characters ({model}, {FACE_GROUP} per call)")
    g = T.Gemini(model, bdir / "qwen" / "faces.log")

    def ask(group: list[str]) -> list[str]:
        known = {c["display"]: c["face"] for c in bible["characters"].values() if (c.get("face") or "").strip()}
        text = face_request(bible, group) + ("\n\nFACES ALREADY GIVEN (make the new ones unlike these):\n"
                                             + json.dumps(known, ensure_ascii=False) if known else "")
        out = g.ask(FACE_API, text, json_out=True, temperature=0.7)
        done = apply_faces(bible, json.loads(out[out.find("{"):out.rfind("}") + 1]), group, say)
        B.save_characters(bdir, bible)
        return done

    left = []
    for i in range(0, len(need), FACE_GROUP):
        group = need[i:i + FACE_GROUP]
        try:
            left += [k for k in group if k not in ask(group)]
        except ValueError as e:                     # refused (finish != STOP) or not JSON: one at a time
            say(f"  group {', '.join(group)}: {e}; asking one by one")
            for k in group:
                try:
                    if not ask([k]):
                        left.append(k)
                except ValueError as e1:
                    say(f"  {k}: {e1}; drawn from the look alone")
                    left.append(k)
    say(f"faces written for {len(need) - len(left)} of {len(need)}"
        + (f"; none for {', '.join(left)}" if left else ""))
    return len(left)


def cmd_front(args, trial: Path) -> int:
    from . import ktrial_run as R
    bdir = B.bible_dir(args.book)
    out_dir = bdir / "qwen" / FRONT_DIR
    chars = B.load_characters(bdir)["characters"]
    if args.keys:
        keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    else:                                                           # everyone with a Qwen sheet
        keys = list(R._read_json(bdir / "qwen" / "manifest.json", {}) or {})
    unknown = [k for k in keys if k not in chars]
    if unknown:
        print(f"not in the bible: {', '.join(unknown)}")
        return 1
    if add_faces(args.book, keys):
        print("some characters have no face line (see above): they are drawn from their look alone")
    bible = B.load_characters(bdir)
    server = R.answering(R.load_server(trial))
    if server is None or server.get("model") != kcomfy.QWEN:
        print("no Qwen server answers for this chapter: run `novel ktrial up --model qwen-image-turbo` first")
        return 1
    run = R._Run(R._comfy(server), out_dir, {"width": WIDTH, "height": HEIGHT, "chapter": 0}, kcomfy.QWEN)
    run.root = out_dir
    todo = [k for k in keys if args.redo or not R._valid_jpeg(out_dir / f"{k}_front.jpg")]
    run.total = len(todo)
    stamp = R._stamp()
    for k in todo:
        try:
            meta = run.make("front", k, {"out": f"{k}_front.jpg"}, front_prompt(bible, k), seed_of(k), [],
                            stamp if args.redo else None)
            run.progress(k, meta)
        except kcomfy.ComfyError as e:
            run.failed += 1
            print(f"  {k}: failed: {e}")
    run.summary()
    print("check them by eye (no picture QA here: character pictures are chosen by hand); "
          "`--keys a,b --redo` draws chosen ones again")
    return 1 if run.failed else 0
