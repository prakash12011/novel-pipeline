"""`novel images` with images.engine: qwen - the chapters' pictures from Qwen-Image-2.1-Turbo on the Kaggle ComfyUI
server, written straight into the episode as images/cNNNN/sNN-2k.jpg (the name the HD step and the render read).

The picture lines are the production scene file (`novel write`, which adds the Qwen rules to its IMAGES request). The
prompts, the character-sheet references, the server and the drawing are the ones `novel ktrial` proved: each chapter
works in <book>/_work/ktrial/cNNNN (scene copy, prompts, refs, plan.json, server.json and server-2.json for the two
notebooks, qwen-image-turbo/images/), and every finished picture is copied into the episode. Missing character sheets are made first (bible/qwen).
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
    if (src.parent / "qa.json").exists():                  # the picture QA record travels with the pictures
        shutil.copy2(src.parent / "qa.json", img_dir / "qa.json")
    return n


BOOT_HOURS = 6         # a notebook 2 pushed this recently may still be starting (images.max_hours of a session)


def _recent(server: dict, hours: float = BOOT_HOURS) -> bool:
    from datetime import datetime, timezone
    try:
        pushed = datetime.strptime(str(server.get("pushed")), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - pushed).total_seconds() < hours * 3600


def live_servers(book: str) -> list[dict]:
    """The newest recorded Qwen server of each notebook (1, 2) of the book that still answers. A notebook 2 pushed in
    the last BOOT_HOURS that does not answer yet is kept too: `run` makes it join when it answers (it boots after 1)."""
    from .ktrial_run import answering
    found = sorted((paths.book_dir(book) / "_work" / "ktrial").glob("c*/server*.json"),
                   key=lambda p: -p.stat().st_mtime)
    live: dict[int, dict] = {}
    for f in found:
        n = int(f.stem.rsplit("-", 1)[1]) if f.name != "server.json" else 1
        s = json.loads(f.read_text(encoding="utf-8"))
        if n not in live and s.get("model") == kcomfy.QWEN and s.get("url"):
            if (a := answering(s)) is not None:
                live[n] = dict(a, n=n)
            elif n >= 2 and _recent(s):
                live[n] = dict(s, n=n)
    if 1 not in live:
        live.clear()                            # a booting notebook 2 or 3 alone is no server: `up` starts both again
    return [live[n] for n in sorted(live)]


def run(book: str, ep: int, chapters: str | None = None, limit: int | None = None, dry: bool = False,
        qa: bool = False) -> int:
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

    servers = live_servers(book)
    if not servers:
        print("no Qwen server is running: starting one on a free Kaggle account (about 7-10 minutes)")
        up = SimpleNamespace(book=book, chapter=todo[0], ep=ep, model=kcomfy.QWEN, account=None, tunnel="ngrok",
                             domain=None, idle_minutes=20, max_hours=6, gpu=1, comfy_arg=[], dry_run=False,
                             no_wait=False, ngrok_dataset=None)
        if KR.cmd_up(up, K.trial_dir(book, todo[0])) != 0:
            return 1
        servers = KR.load_servers(K.trial_dir(book, todo[0]))        # the second one joins when it answers

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
        for slot in (1, 2, 3):                              # every server goes to the chapter; a stale record goes
            mine = [x for x in servers if x["n"] == slot]
            if mine:
                KR.save_server(trial, mine[0], slot)
            else:
                KR.server_path(trial, slot).unlink(missing_ok=True)
        ch = SimpleNamespace(book=book, chapter=n, ep=ep, keys="", missing=True)
        KR.cmd_charsheet(ch, trial)                        # a new person gets a sheet before their first picture
        if K.cmd_prompts(ch) != 0:
            failed += 1
            continue
        if not qa:
            print(f"chapter {n}: no picture QA (look at the pictures; `--qa` checks them, e.g. for a night run)")
        # with qa: each picture is checked as soon as it is drawn, a failed one redrawn once (status list qa.json)
        rc = KR.cmd_run(SimpleNamespace(only=None, limit=limit, reroll=False, redo=False, plates_only=False,
                                        via="kaggle", resolution=None, budget=None, qa=qa, book=book,
                                        publish=ep_root / "images" / f"c{n:04d}"), trial)   # each picture lands at once
        img_dir = ep_root / "images" / f"c{n:04d}"
        adopt(trial, img_dir)                              # each picture was copied as it was drawn; this adds qa.json
        have = len([p for p in img_dir.glob("s*-2k.jpg")]) if img_dir.exists() else 0
        print(f"chapter {n}: {have} pictures in {img_dir}")
        failed += rc != 0
    print("next: novel hd (the 1920x1080 copies), then novel render / novel build")
    return 1 if failed else 0
