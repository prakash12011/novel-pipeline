"""`novel images` with images.engine: qwen - the chapters' pictures from Qwen-Image-2.1-Turbo on the Kaggle ComfyUI
server, written straight into the episode as images/cNNNN/sNN-2k.jpg (the name the HD step and the render read).

The picture lines are the production scene file (`novel write`, which adds the Qwen rules to its IMAGES request). The
prompts, the character-sheet references, the server and the drawing are the ones `novel ktrial` proved: each chapter
works in <book>/_work/ktrial/cNNNN (scene copy, prompts, refs, plan.json, server.json, qwen-image-turbo/images/), and
every finished picture is copied into the episode. Missing character sheets are made first (bible/qwen).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

from novel import kcomfy
from novel.core import paths

PICTURE = re.compile(r"^s\d+\.jpg$")


def chapter_range(spec: str | None, lo: int, hi: int) -> list[int]:
    if not spec:
        return list(range(lo, hi + 1))
    a, _, b = str(spec).partition("-")
    return list(range(int(a), int(b or a) + 1))


def missing(ep_root: Path, n: int) -> list[int] | None:
    """Scene ids of chapter n without a picture in the episode; None when the chapter has no picture lines yet."""
    from . import episode_scenes as S
    from .ktrial_run import _valid_jpeg
    f = S.scenes_path(ep_root, n)
    if not f.exists():
        return None
    doc = json.loads(f.read_text(encoding="utf-8"))
    img_dir = ep_root / "images" / f"c{n:04d}"
    return [s["id"] for s in doc["scenes"] if not _valid_jpeg(img_dir / f"s{s['id']:02d}-2k.jpg")]


def adopt(trial: Path, img_dir: Path) -> int:
    """Copy the chapter's finished Qwen pictures into the episode as sNN-2k.jpg (+ the prompt as sent). Newer only."""
    src = trial / kcomfy.QW_DIR / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in sorted(src.glob("s*.jpg")):
        if not PICTURE.match(p.name):
            continue
        dest = img_dir / f"{p.stem}-2k.jpg"
        if not dest.exists() or dest.stat().st_mtime < p.stat().st_mtime:
            shutil.copy2(p, dest)
            if p.with_suffix(".prompt.txt").exists():
                shutil.copy2(p.with_suffix(".prompt.txt"), img_dir / f"{p.stem}.prompt.txt")
            n += 1
    return n


def live_server(book: str) -> dict | None:
    """The newest recorded Qwen server of the book that still answers, else None."""
    from novel.kcomfy import ComfyError
    from .ktrial_run import _comfy
    found = sorted((paths.book_dir(book) / "_work" / "ktrial").glob("c*/server.json"),
                   key=lambda p: -p.stat().st_mtime)
    for f in found:
        s = json.loads(f.read_text(encoding="utf-8"))
        if s.get("model") == kcomfy.QWEN and s.get("url"):
            try:
                _comfy(s, retries=1, backoff=0.5).ping()
                return s
            except ComfyError:
                continue
    return None


def run(book: str, ep: int, chapters: str | None = None, limit: int | None = None, dry: bool = False) -> int:
    from . import episode_scenes as S
    from . import ktrial as K
    from . import ktrial_run as KR
    from novel.gemini import write as W
    ep_root = paths.ep_root(book, ep)
    lo, hi = W.Runner.episode_chapters(SimpleNamespace(ep_root=ep_root, book=book))
    todo = []
    for n in chapter_range(chapters, lo, hi):
        m = missing(ep_root, n)
        if m is None:
            print(f"chapter {n}: no picture lines yet -> novel write --chapters {n}")
        elif m:
            print(f"chapter {n}: {len(m)} pictures to make")
            todo.append(n)
    if not todo:
        print("nothing to do: every chapter with picture lines has its pictures")
        return 0
    if dry:
        return 0

    server = live_server(book)
    if server is None:
        print("no Qwen server is running: starting one on a free Kaggle account (about 7-10 minutes)")
        up = SimpleNamespace(book=book, chapter=todo[0], ep=ep, model=kcomfy.QWEN, account=None, tunnel="ngrok",
                             domain=None, idle_minutes=20, max_hours=6, gpu=1, comfy_arg=[], dry_run=False,
                             no_wait=False, ngrok_dataset=None)
        if KR.cmd_up(up, K.trial_dir(book, todo[0])) != 0:
            return 1
        server = KR.load_server(K.trial_dir(book, todo[0]))

    failed = 0
    for n in todo:
        trial = K.trial_dir(book, n)
        src, dst = S.scenes_path(ep_root, n), K.scenes_file(trial, n)
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            old = trial / kcomfy.QW_DIR / "images"
            if old.exists() and any(old.iterdir()):         # pictures of older picture lines: set aside, never reused
                old.rename(trial / kcomfy.QW_DIR / f"images_before_{src.stat().st_mtime_ns}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        KR.save_server(trial, server)
        ch = SimpleNamespace(book=book, chapter=n, ep=ep, keys="", missing=True)
        KR.cmd_charsheet(ch, trial)                        # a new person gets a sheet before their first picture
        if K.cmd_prompts(ch) != 0:
            failed += 1
            continue
        rc = KR.cmd_run(SimpleNamespace(only=None, limit=limit, reroll=False, redo=False, plates_only=False,
                                        via="kaggle", resolution=None, budget=None), trial)
        copied = adopt(trial, ep_root / "images" / f"c{n:04d}")
        print(f"chapter {n}: {copied} pictures copied into {ep_root / 'images' / f'c{n:04d}'}")
        failed += rc != 0
    print("next: novel hd (the 1920x1080 copies), then novel render / novel build")
    return 1 if failed else 0
