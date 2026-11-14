"""novel thumb [--book B] [--ep N] --hook "LINE ONE|*KEYWORD* LINE" [--hook ...] [--image NAME_OR_PATH]
              [--ch X-Y] [--focus x,y] [--zoom Z] [--face left|right] [--no-badge] [--no-tag] [--out DIR]

Writes thumb-a.jpg (b, c) and sheet.jpg to <episode folder>/publish/ unless --out is given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from PIL import Image

from novel.core import context, paths, settings
from novel.publish import thumb

NO_HOOKS = 'No hooks. Pass --hook "LINE|*KEYWORD*" or run novel meta first.'
DEFAULT_FONT = "assets/fonts/Anton-Regular.ttf"
DEFAULT_FOCUS = "0.53,0.40"
PORTRAIT_SUFFIX = "-portrait.jpg"


def _fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


def _repo_file(value) -> Path:
    """Relative paths (e.g. assets/fonts/...) resolve against the repo root, never the working directory."""
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else paths.repo_root() / p


def _flow_dir(book: str) -> Path:
    return paths.book_dir(book) / "bible" / "flow"


def flow_keys(book: str) -> list[str]:
    flow = _flow_dir(book)
    if not flow.is_dir():
        return []
    return sorted(p.name[: -len(PORTRAIT_SUFFIX)] for p in flow.glob("*" + PORTRAIT_SUFFIX))


def find_art(spec: str, book: str) -> Path | None:
    """An existing file path wins; otherwise the character key <book>/bible/flow/<key>-portrait.jpg."""
    p = Path(spec).expanduser()
    if p.is_file():
        return p
    cand = _flow_dir(book) / (spec + PORTRAIT_SUFFIX)
    return cand if cand.is_file() else None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def art_from_meta(book: str, ep_dir: Path):
    """(portrait file, name) for the `thumbnail_character` that `novel meta` stored in publish/publish.json,
    when that character's portrait exists; else None. The name may be a bible key, a display name or a Flow name."""
    f = ep_dir / "publish" / "publish.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = data.get("thumbnail_character") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    cands = [_slug(name), name.lower(), name]
    try:
        from novel import bible as B
        for key, ch in (B.load_characters(B.bible_dir(book)).get("characters") or {}).items():
            names = [ch.get("display")] + [v.get("flow_name") for v in (ch.get("variants") or {}).values()]
            if any(isinstance(n, str) and n.strip().lower() == name.lower() for n in names):
                cands.append(key)
    except (OSError, ValueError, KeyError, SystemExit, AttributeError):
        pass
    for c in cands:
        p = _flow_dir(book) / (c + PORTRAIT_SUFFIX)
        if p.is_file():
            return p, name
    return None


def _hooks_from_file(ep_dir: Path):
    f = ep_dir / "publish" / "publish.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        raise thumb.ThumbError(f"{f} is not valid JSON: {e}")
    return data.get("thumbnail_hooks") if isinstance(data, dict) else None


def _chapter_tag(ep_dir: Path, ch: str | None) -> str:
    if ch is not None:
        if not re.fullmatch(r"\d+-\d+", ch):
            raise thumb.ThumbError(f"--ch must look like 11-20, got '{ch}'")
        return f"CH {ch}"
    f = ep_dir / "episode.json"
    if not f.is_file():
        raise thumb.ThumbError(f"no chapter range: {f} is missing. Pass --ch X-Y or --no-tag")
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        raise thumb.ThumbError(f"{f} is not valid JSON: {e}")
    if not isinstance(info, dict) or "start" not in info or "end" not in info:
        raise thumb.ThumbError(f"{f} needs keys start and end. Pass --ch X-Y or --no-tag")
    return f"CH {info['start']}-{info['end']}"


def _run(a) -> int:
    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        return _fail("No episode. Pass --ep N, or pick one with: novel use <book> --ep N")
    ep_dir = paths.ep_root(book, ep)
    if not ep_dir.is_dir():
        return _fail(f"episode folder not found: {ep_dir}")

    hooks = list(a.hook or [])
    if not hooks:
        hooks = _hooks_from_file(ep_dir) or []
        if not hooks:
            if (ep_dir / "publish" / "publish.json").is_file():
                return _fail('publish.json has no thumbnail_hooks (novel meta --ai none writes none). '
                             'Pass --hook "LINE ONE|*KEYWORD* LINE" (up to three times), or run novel meta with an AI.')
            return _fail(NO_HOOKS)
    if not isinstance(hooks, list) or not all(isinstance(h, str) and h.strip() for h in hooks):
        return _fail("thumbnail_hooks must be a list of non-empty strings")
    if len(hooks) > 3:
        return _fail("at most 3 hooks (YouTube Test & compare takes 3)")

    raw_focus = a.focus
    if raw_focus is None:
        conf = settings.get("thumbnail.focus", book=book, ep=ep, default=None)
        raw_focus = ",".join(str(v) for v in conf) if isinstance(conf, (list, tuple)) else conf
        raw_focus = str(raw_focus) if raw_focus not in (None, "") else DEFAULT_FOCUS
    try:
        focus = tuple(float(v) for v in raw_focus.split(","))
        if len(focus) != 2:
            raise ValueError
    except ValueError:
        return _fail(f"--focus must be x,y (two numbers), got '{raw_focus}'"
                     + ("" if a.focus is not None else " (it comes from the setting thumbnail.focus)"))

    badge = None if a.no_badge else f"EP {ep}"
    tag = None if a.no_tag else _chapter_tag(ep_dir, a.ch)

    font = _repo_file(settings.get("thumbnail.font", book=book, ep=ep, default=DEFAULT_FONT))
    if not font.is_file():
        return _fail(f"thumbnail font not found: {font} (setting thumbnail.font)")

    spec = a.image or settings.get("thumbnail.art", book=book, ep=ep, default=None)
    art_path = None
    if not spec:
        meta_art = art_from_meta(book, ep_dir)           # the character `novel meta` chose for this episode
        if meta_art:
            art_path = meta_art[0]
            print(f"art: {meta_art[1]}'s portrait ({art_path.name}), the thumbnail_character that novel meta chose "
                  f"(--image or thumbnail.art would override it)")
        else:
            keys = flow_keys(book)
            return _fail("No art. Pass --image NAME_OR_PATH, or set thumbnail.art for this episode."
                         + (f" Characters with a portrait: {', '.join(keys)}." if keys else
                            f" No portraits yet in {_flow_dir(book)} (novel characters make)."))
    else:
        art_path = find_art(str(spec), book)
    if art_path is None:
        keys = flow_keys(book)
        return _fail(f"image not found: {spec} (not a file, and no {spec}{PORTRAIT_SUFFIX} in "
                     f"{_flow_dir(book)}). Keys available there: {', '.join(keys) or '(none)'}")

    out = Path(a.out).expanduser() if a.out else ep_dir / "publish"
    art = Image.open(art_path).convert("RGB")
    written = thumb.make_thumbs(art, hooks, out, font=font, badge=badge, tag=tag,
                                focus=focus, zoom=a.zoom, face=a.face)
    for i, p in enumerate(written[:-1]):
        print(f"wrote {p}  ({p.stat().st_size // 1024} KB)  hook: {hooks[i]}")
    print(f"wrote {written[-1]}")
    return 0


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel thumb")
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--ch", help="chapter range for the CH tag, e.g. 11-20 (default: start and end in episode.json)")
    ap.add_argument("--hook", action="append", help='"LINE ONE|*KEYWORD* LINE" (repeat for a, b, c)')
    ap.add_argument("--image", help="a file path, or a character key: <book>/bible/flow/<key>-portrait.jpg")
    ap.add_argument("--focus", default=None,
                    help="x,y of the face between the eyes, 0-1 (default: setting thumbnail.focus, else 0.53,0.40)")
    ap.add_argument("--zoom", type=float, default=1.5)
    ap.add_argument("--face", choices=["right", "left"], default="right")
    ap.add_argument("--no-badge", action="store_true", help="leave out the red EP badge")
    ap.add_argument("--no-tag", action="store_true", help="leave out the yellow CH tag")
    ap.add_argument("--out", help="folder (default: <episode folder>/publish)")
    a = ap.parse_args(argv)
    try:
        return _run(a)
    except thumb.ThumbError as e:
        return _fail(str(e))
    except SystemExit as e:  # context and paths stop with a message
        if isinstance(e.code, str):
            return _fail(e.code)
        raise
