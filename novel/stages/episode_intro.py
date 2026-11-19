"""The episode intro: a short spoken hook (or recap) before the first chapter, drawn as comic panels.

It lives in a pseudo-episode next to the subscribe message, <book>/extras/E9NNN (9000 + the episode number), as
chapter 1, so the normal narration and render paths work on it unchanged:

    novel intro init   --ep 1 --text-file hook.txt     # the Hindi (one paragraph per picture), prints the next steps
    novel intro shots  --ep 1                          # shots.json: one shot per paragraph, written by Claude
    novel tts push     --ep 1                           # narration on Kaggle, with the episode's chapters (one run)
    novel tts pull     --ep 1
    novel intro images --ep 1                          # Qwen-Image-2.1 on the Kaggle ComfyUI (novel ktrial up ...)
    novel intro render --ep 1                          # extras/E9001/video/c0001.mp4, full screen, no title card
    novel build                                        # the join puts it first, listed as "Intro" at 0:00

Pictures: one per paragraph, described in shots.json in the intro folder (`novel intro shots` writes it; it can also
be written by hand):
    [{"id": 1, "cast": ["lumian_lee", "skinless_monster"], "shot": "...", "scene": "...",
      "people": {"lumian_lee": {"where": "...", "pose": "...", "face": "..."}}}, ...]
`cast` are bible keys; their references (the approved front view in <book>/bible/qwen/front/, else a character
sheet) go in. Pictures are written as images/c0001/sNN-2k.jpg (+ .prompt.txt). Google Flow is not used here.
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

from . import episode as E
from .manifest import Chapter, Manifest

INTRO_FROM = 9000
CH = 1
WIDTH, HEIGHT = 1376, 768

STYLE = ("16:9 illustration. Anime art style: 2D anime illustration with clean, confident line art, cel shading with "
         "soft gradients, expressive anime eyes and detailed painted backgrounds. Gothic dark fantasy in a French "
         "mountain village of the late 1800s, with gas and kerosene lamps. Warm amber and brown palette, soft "
         "cinematic lighting. Not 3D, not photo-realistic, not semi-realistic.")
TAIL = ("Draw only ONE of each named character, in a single scene: this is not a reference sheet, no multiple views. "
        "No text, speech bubbles, captions or borders; no lettering anywhere: every sign, page and label is blank. "
        "Hands are drawn simply and correctly, five fingers each. Clean eyes, no white specks. No gore, no blood.")
NOT_REF_POSE = ("The reference shows a person standing straight and calm: copy only their face, hair, body and "
                "clothes, never that pose or that calm face.")


def number(ep: int) -> int:
    return INTRO_FROM + int(ep)


def root(ep: int) -> Path:
    return E.root(number(ep))


def clip(ep: int) -> Path | None:
    """The rendered intro of episode ep, or None."""
    p = root(ep) / "video" / f"c{CH:04d}.mp4"
    return p if p.exists() else None


def book_dir() -> Path:
    return E.EPISODES.parent


# ------------------------------------------------------------------ init

def init(ep: int, text: str) -> Path:
    from .scenebuild import paragraphs
    from .heading import strip_heading
    paras = [" ".join(p.split()) for p in paragraphs(strip_heading(text.replace("\r\n", "\n")))]
    if not paras:
        sys.exit("the intro text is empty")
    r = root(ep)
    for sub in ("hindi", "chapters", "qa", "scenes"):
        (r / sub).mkdir(parents=True, exist_ok=True)
    cfg = E.cfg(ep)
    hindi = r / "hindi" / f"{CH:05d}.txt"
    hindi.write_text("\n\n".join(paras) + "\n", encoding="utf-8")
    src = f"{CH:04d}_intro.txt"
    (r / "chapters" / src).write_text("Intro\n", encoding="utf-8")
    Manifest(path=r / "manifest.json", chapters=[Chapter(idx=CH, title="Intro", source=src)]).save()
    (r / "episode.json").write_text(json.dumps({
        "episode": number(ep), "start": CH, "end": CH, "extra": True, "intro_for": int(ep),
        "voice": cfg["tts"]["voice"], "note": f"Intro hook of episode {ep}. Not a real episode."}, indent=2),
        encoding="utf-8")
    E.lock_path(r).write_text(json.dumps({hindi.name: E.sha256(hindi)}, indent=1), encoding="utf-8")
    words = sum(len(p.split()) for p in paras)
    print(f"intro of episode {ep}: {len(paras)} paragraphs (one picture each), {words} words -> {hindi}")
    print(f"next: novel intro shots --ep {ep}   (writes {r / 'shots.json'}, one shot per paragraph), then\n"
          f"      novel tts push --ep {ep}   (the intro goes with the episode's chapters; "
          f"or --ep {number(ep)} --chapters {CH} alone)\n"
          f"      novel tts pull --ep {ep}\n"
          f"      novel intro images --ep {ep}\n"
          f"      novel intro render --ep {ep}")
    return hindi


# ------------------------------------------------------------------ pictures (Qwen on Kaggle)

def _server() -> dict:
    """The newest ktrial server.json of the book (the running Kaggle ComfyUI). Never printed: it holds a password."""
    found = sorted((book_dir() / "_work" / "ktrial").glob("c*/server.json"), key=lambda p: p.stat().st_mtime)
    if not found:
        sys.exit("no Kaggle ComfyUI known: start it with `novel ktrial up --chapter N --model qwen-image-turbo`")
    return json.loads(found[-1].read_text(encoding="utf-8"))


def _sheet(key: str) -> Path:
    from .ktrial import qwen_sheet
    found = qwen_sheet(book_dir() / "_work" / "ktrial", key)
    if not found:
        sys.exit(f"no Qwen character sheet for '{key}': make one with qwen_charsheet.py first")
    return found


def shots(ep: int) -> list[dict]:
    f = root(ep) / "shots.json"
    if not f.exists():
        sys.exit(f"no {f}: write one shot per paragraph first (see `novel intro --help`)")
    return json.loads(f.read_text(encoding="utf-8"))


SHOTS_RULES = """You plan the pictures of a short spoken recap for an anime-style YouTube story channel. You get the \
recap, cut into numbered paragraphs (read in Hindi/Hinglish), and the list of people you may draw. Answer with one \
shot per paragraph: the picture shown while that paragraph is spoken.

Each shot is full screen 16:9 (the camera pans and zooms over it). Rules:
- "cast": the keys of the named people in the picture, from the list only, at most {max_people}. Choose the ones the \
paragraph is about; an empty list is fine for a place or an object.
- "shot": the framing in a few words ("wide view, both figures full length", "medium shot", "close on the face").
- "scene": 2-4 English sentences: the place, the moment, the light and mood, drawn from what the paragraph says (not \
from other paragraphs). Name each person by their display name. Anyone else is an unnamed extra: say plainly who \
(age, build, clothes) and keep them in the background. No text, signs or lettering in the picture, no gore.
- "people": for EVERY key in cast: {{"where": the spot in the picture, "pose": the body and hands, "face": the \
expression naming the mouth, the eyes and the brows}}. Never only "calm" or "focused". Nobody smiles unless the \
paragraph says so. The pose is not a straight standing pose unless the moment calls for it.
- Every shot looks different from the one before: another place, distance or composition.
- A paragraph that asks a question or ends on a cliffhanger gets a tense, open picture, not an answer.

Answer with JSON only: {{"shots": [{{"id": 1, "cast": ["key"], "shot": "...", "scene": "...", "people": {{"key": \
{{"where": "...", "pose": "...", "face": "..."}}}}}}, ...]}} with exactly {n} shots, ids 1 to {n} in order."""


def _front_keys() -> list[str]:
    return sorted(p.name[:-len("_front.jpg")] for p in (book_dir() / "bible" / "qwen" / "front").glob("*_front.jpg"))


def shots_problems(doc, n: int, allowed: set[str], max_people: int) -> list[str]:
    """Every reason an answer cannot be used as shots.json; empty means usable."""
    sh = doc.get("shots") if isinstance(doc, dict) else None
    if not isinstance(sh, list) or len(sh) != n:
        return [f"answer with a \"shots\" list of exactly {n} shots (got {len(sh) if isinstance(sh, list) else 'none'})"]
    bad = []
    for i, s in enumerate(sh, 1):
        if not isinstance(s, dict) or s.get("id") != i:
            bad.append(f"shot {i}: its id must be {i}")
            continue
        cast = s.get("cast")
        if not isinstance(cast, list) or any(k not in allowed for k in cast):
            bad.append(f"shot {i}: cast must be a list of keys from the list given (got {cast!r})")
            continue
        if len(cast) > max_people:
            bad.append(f"shot {i}: at most {max_people} people in cast (got {len(cast)})")
        for f in ("shot", "scene"):
            if not str(s.get(f) or "").strip():
                bad.append(f"shot {i}: {f} is empty")
        ppl = s.get("people") if isinstance(s.get("people"), dict) else {}
        for k in cast:
            if not all(str((ppl.get(k) or {}).get(f) or "").strip() for f in ("where", "pose", "face")):
                bad.append(f"shot {i}: people[{k!r}] needs where, pose and face")
    return bad


def write_shots(ep: int, g=None, redo: bool = False) -> Path:
    """shots.json for the intro/recap: one shot per paragraph window, written by the scene writer (Claude first, then
    the usual fallbacks; `g` = anything with Gemini.generate, for tests). An existing shots.json is kept unless
    `redo` (it moves to shots.before-<stamp>.json). One retry tells the writer what was wrong; then it stops with
    the problems and keeps the reply in shots.reply.txt."""
    from .episode_scenes import chapter_paras, chapter_windows, window_text
    r = root(ep)
    out = r / "shots.json"
    if out.exists() and not redo:
        sys.exit(f"{out} exists: --redo writes it again (the old one is kept as shots.before-<stamp>.json)")
    E.check_lock(r, CH)
    paras = chapter_paras(r, CH)
    wins = chapter_windows(paras, None)
    chars = json.loads((book_dir() / "bible" / "characters.json").read_text(encoding="utf-8"))["characters"]
    keys = [k for k in _front_keys() if k in chars]
    if not keys:
        sys.exit("no character has a front view yet: novel ktrial front --chapter N")
    max_people = int((E.cfg(ep).get("images") or {}).get("max_characters") or 2)
    n = len(wins)
    system = SHOTS_RULES.format(n=n, max_people=max_people)
    body = ("RECAP, one numbered paragraph per picture:\n" + "\n".join(
        f"{i}. " + " ".join(t for _, t in window_text(paras, w)) for i, w in enumerate(wins, 1))
        + "\n\nPEOPLE YOU MAY DRAW (key | display name | look):\n" + "\n".join(
        f"{k} | {chars[k]['display']} | {str(chars[k].get('look') or '')[:110]}" for k in keys))
    if g is None:
        from novel.core import settings
        from . import episode_translate as T
        (r / "qa").mkdir(exist_ok=True)
        g = T.Gemini(settings.get("gemini.api_model", default=None), r / "qa" / "shots-gemini.log.jsonl")
    problems, text = [], body
    for attempt in (1, 2):
        res = g.generate(system, text, json_out=True, temperature=0.7, scenes=True)
        raw = res.text
        try:
            doc = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            problems = shots_problems(doc, n, set(keys), max_people)
        except ValueError:
            problems = ["answer with JSON only, in the shape given"]
        if res.finish not in (None, "STOP"):
            problems.append(f"the answer was cut off ({res.finish})")
        if not problems:
            break
        text = body + "\n\nYour last answer could not be used:\n- " + "\n- ".join(problems) + "\nAnswer again, whole."
    if problems:
        (r / "shots.reply.txt").write_text(raw, encoding="utf-8")
        sys.exit("the writer's shots could not be used (reply kept in shots.reply.txt):\n- " + "\n- ".join(problems))
    if out.exists():
        out.rename(r / f"shots.before-{time.strftime('%Y%m%dT%H%M%S')}.json")
    out.write_text(json.dumps(doc["shots"], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"intro of episode {ep}: {n} shots -> {out}")
    for s in doc["shots"]:
        print(f"  s{s['id']:02d}  {', '.join(s['cast']) or '(no one)'}  |  {s['scene'][:90]}")
    print(f"look at them, then: novel intro images --ep {ep}")
    return out


def write_scenes(ep: int) -> Path:
    """scenes/00001.json from shots.json: one scene per paragraph window, against the locked Hindi."""
    from .episode_scenes import chapter_paras, chapter_windows
    r = root(ep)
    E.check_lock(r, CH)
    paras = chapter_paras(r, CH)
    wins = chapter_windows(paras, None)
    sh = shots(ep)
    if len(sh) != len(wins):
        sys.exit(f"shots.json has {len(sh)} shots but the text has {len(wins)} paragraphs: one shot per paragraph")
    chars = json.loads((book_dir() / "bible" / "characters.json").read_text(encoding="utf-8"))["characters"]
    lock = json.loads(E.lock_path(r).read_text(encoding="utf-8"))
    scenes = [{"id": s["id"], "para": w[0], "characters": [chars[k]["display"] for k in s.get("cast", [])],
               "shot": s.get("panel_shot", "establishing" if not s.get("cast") else "medium"),
               "focus": s.get("focus", [0.5, 0.5]), "scene": s["scene"],
               **({"panel": s["panel"]} if s.get("panel") else {})} for s, w in zip(sh, wins)]
    out = r / "scenes" / f"{CH:05d}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"version": 1, "chapter": CH, "hindi_sha256": lock[f"{CH:05d}.txt"],
                               "note": "intro hook, from shots.json", "scenes": scenes},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def prompt_for(shot: dict, chars: dict) -> tuple[str, list[str]]:
    """(prompt, bible keys of the reference pictures in <imageN> order). A shot's "people" ({key: {"where", "pose",
    "face"}}) give each person their spot, pose and expression: without them Qwen copied the front view's straight
    stance and calm smile (a smiling Lumian running from the monster)."""
    lines = []
    for i, key in enumerate(shot.get("cast", [])):
        c, p = chars[key], (shot.get("people") or {}).get(key) or {}
        line = (f"- {c['display']} (<image{i + 1}>): {c['look']}. <image{i + 1}> is a reference picture of "
                f"{c['display']} (their front view): draw {c['display']} exactly as in it (same face, hair, body "
                f"and clothes), drawn once.")
        line += "".join(f" {label}: {p[k].rstrip('.')}." for k, label in (("where", "WHERE"), ("pose", "POSE"),
                                                                         ("face", "FACE")) if p.get(k))
        lines.append(line)
    if lines:
        lines.append(NOT_REF_POSE)
    parts = [STYLE] + (["CHARACTERS:\n" + "\n".join(lines)] if lines else []) + \
        [f"SHOT: {shot['shot']}", f"SCENE: {shot['scene']}", TAIL]
    return "\n\n".join(parts), list(shot.get("cast", []))


def images(ep: int, only: list[str] | None = None, seed: int | None = None, redo: bool = False) -> list[Path]:
    from novel import kcomfy
    from PIL import Image
    r = root(ep)
    out_dir = r / "images" / f"c{CH:04d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    chars = json.loads((book_dir() / "bible" / "characters.json").read_text(encoding="utf-8"))["characters"]
    srv = _server()
    comfy = kcomfy.Comfy(srv["url"], srv["comfy_user"], srv["password"], timeout=60, retries=6)
    try:
        comfy.ping()
    except Exception as e:                                # noqa: BLE001 - any failure means: not running
        sys.exit(f"the Kaggle ComfyUI does not answer ({type(e).__name__}): start it with "
                 f"`novel ktrial up --chapter 24 --model qwen-image-turbo`")
    uploaded: dict[str, str] = {}
    made = []
    for s in shots(ep):
        sid = f"s{s['id']:02d}"
        dest = out_dir / f"{sid}-2k.jpg"
        if only and sid not in only:
            continue
        if dest.exists() and not redo and not only and seed is None:
            continue
        prompt, keys = prompt_for(s, chars)
        refs = []
        for k in keys:
            if k not in uploaded:
                uploaded[k] = comfy.upload_image(_sheet(k), f"intro_{k}.jpg")
            refs.append(uploaded[k])
        use_seed = seed if seed is not None else int(s.get("seed", 202))
        data, took = comfy.generate(prompt, use_seed, WIDTH, HEIGHT, refs, f"intro-e{ep:03d}-{sid}",
                                    timeout=kcomfy.picture_timeout(kcomfy.QWEN), model=kcomfy.QWEN)
        Image.open(io.BytesIO(data)).convert("RGB").save(dest, "JPEG", quality=94)
        dest.with_suffix(".prompt.txt").write_text(prompt, encoding="utf-8")
        (out_dir / f"{sid}.meta.json").write_text(json.dumps({"id": sid, "seed": use_seed, "seconds": round(took, 1),
                                                              "refs": keys, "width": WIDTH, "height": HEIGHT,
                                                              **kcomfy.model_info(kcomfy.QWEN)}, indent=1),
                                                  encoding="utf-8")
        print(f"  {sid} seed {use_seed}: {took:.0f} s -> {dest}", flush=True)
        made.append(dest)
    write_scenes(ep)
    return made


# ------------------------------------------------------------------ render

def render(ep: int, workers: int = 6) -> Path:
    from .episode_render import render as render_chapter
    r = root(ep)
    if not (r / "audio" / f"{CH:05d}.mp3").exists():
        sys.exit(f"no narration yet: novel tts push --ep {number(ep)} --chapters {CH}, then novel tts pull --ep {number(ep)}")
    write_scenes(ep)
    # full screen with pan and zoom, like the chapters: the user wants the hook and the recap never as comic panels
    rep = render_chapter(number(ep), CH, "20", "fast", workers, None, captions=False, hd=False, style="kenburns")
    print(f"intro: {rep['file']} {rep['seconds']:.1f} s, {rep['images']} pictures, full screen, pass={rep['pass']}")
    return r / "video" / rep["file"]
