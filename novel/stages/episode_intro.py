"""The episode intro: a short spoken hook (or recap) before the first chapter, drawn as comic panels.

It lives in a pseudo-episode next to the subscribe message, <book>/extras/E9NNN (9000 + the episode number), as
chapter 1, so the normal narration and render paths work on it unchanged:

    novel intro init   --ep 1 --text-file hook.txt     # the Hindi (one paragraph per picture), prints the next steps
    novel tts push     --ep 9001 --chapters 1           # narration on Kaggle, the same voice as the episode
    novel tts pull     --ep 9001
    novel intro images --ep 1                          # Qwen-Image-2.1 on the Kaggle ComfyUI (novel ktrial up ...)
    novel intro render --ep 1                          # extras/E9001/video/c0001.mp4, panels style, no title card
    novel build                                        # the join puts it first, listed as "Intro" at 0:00

Pictures: one per paragraph, described in shots.json in the intro folder:
    [{"id": 1, "cast": ["lumian_lee", "skinless_monster"], "shot": "...", "scene": "..."}, ...]
`cast` are bible keys; their Qwen character sheets (<book>/_work/ktrial/c*/qwen-image-turbo/charsheet/
<key>_sheet_seed*.jpg) go in as references. Pictures are written as images/c0001/sNN-2k.jpg (+ .prompt.txt).
Google Flow is not used here.
"""

from __future__ import annotations

import io
import json
import sys
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
        "No text, speech bubbles, captions or borders. Hands are drawn simply and correctly, five fingers each. "
        "No gore, no blood.")


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
    print(f"next: write {r / 'shots.json'} (one shot per paragraph), then\n"
          f"      novel tts push --ep {number(ep)} --chapters {CH}\n"
          f"      novel tts pull --ep {number(ep)}\n"
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
    """(prompt, bible keys of the reference sheets in <imageN> order)."""
    lines = []
    for i, key in enumerate(shot.get("cast", [])):
        c = chars[key]
        lines.append(f"- {c['display']} (<image{i + 1}>): {c['look']}. <image{i + 1}> is a character reference sheet "
                     f"of {c['display']}: draw {c['display']} exactly as in it (same face, hair, body and clothes), "
                     f"drawn once, in the pose this scene needs.")
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
    rep = render_chapter(number(ep), CH, "20", "fast", workers, None, captions=False, hd=False, style="panels")
    print(f"intro: {rep['file']} {rep['seconds']:.1f} s, {rep['images']} panels, pass={rep['pass']}")
    return r / "video" / rep["file"]
