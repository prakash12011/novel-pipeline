"""novel sheet --chapter N [--book B] [--ep E] [--open]

A contact sheet (HTML) for one chapter: the pictures in order, grouped by moment and place, each with its
cast and what the hands do, the shot, the action text, the Hindi of its window and what Flow reports it used.
A v1 chapter gets the old sheet. Image paths are relative to the HTML file, so the folder can be moved.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import webbrowser
from pathlib import Path
from types import SimpleNamespace

from novel import bible as B
from novel.core import context

USAGE = "usage: novel sheet --chapter N [--book B] [--ep E] [--open]"

CSS = """body{font:14px system-ui;background:#111;color:#ddd;margin:16px}
h2{margin:28px 0 4px;padding-top:14px;border-top:3px solid #c9a227;color:#f0d070;font-size:18px}
.stage{color:#aab;margin:0 0 12px}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:16px}
figure{margin:0;background:#1c1c1c;border-radius:6px;overflow:hidden}
img{width:100%;display:block;aspect-ratio:16/9;object-fit:cover}
.missing{aspect-ratio:16/9;display:grid;place-items:center;color:#f88;font-size:28px;background:#241818}
figcaption{padding:8px 10px} p{margin:6px 0} .hi{color:#9a9} .cast{color:#cdb} .used{color:#8bd}"""


def _rel(path: Path, start: Path) -> str:
    return os.path.relpath(path, start).replace("\\", "/")


def _used_text(stem: Path) -> str:
    meta_f = Path(f"{stem}.flow.json")
    meta = {}
    if meta_f.exists():
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
    used = meta.get("flow_reported_ingredients") or []
    parts = []
    if used:
        parts.append("Flow used: " + ", ".join(map(str, used)))
    share = meta.get("prompt_used_share")
    if share is not None:
        parts.append(f"prompt used {share * 100:.0f}%" + (" (SHORTENED)" if meta.get("prompt_shortened") else ""))
    return " · ".join(parts)


def _setting(g: dict, loc: dict) -> str:
    """`indoor · night` for a group heading (an older group without `where` shows the location's kind)."""
    where = g.get("where") or {"interior": "indoor", "exterior": "outdoor"}.get(str(loc.get("kind") or ""), "")
    return " · ".join(x for x in (where, str(g.get("time", ""))) if x)


def build_v2(ep_root: Path, n: int, chars_doc: dict, locs_doc: dict, out_dir: Path) -> str:
    from novel.stages import episode_scenes as S
    doc = json.loads(S.scenes_path(ep_root, n).read_text(encoding="utf-8"))
    paras = S.chapter_paras(ep_root, n)
    wins = S.chapter_windows(paras, doc.get("pacing"))
    img_dir = ep_root / "images" / f"c{n:04d}"
    locs = locs_doc.get("locations", {})
    chars = chars_doc.get("characters", {})
    body, current = [], None
    for s in doc["scenes"]:
        gid = s.get("group")
        g = (doc.get("groups") or {}).get(gid, {})
        if gid != current:
            if current is not None:
                body.append("</main>")
            loc = locs.get(g.get("loc"), {})
            body.append(f'<h2>Group {html.escape(str(gid))} · {html.escape(str(loc.get("name") or g.get("loc")))}'
                        f' · {html.escape(_setting(g, loc))}</h2>'
                        f'<p class="stage">{html.escape(str(g.get("light", "")))} — staging: '
                        f'{html.escape(str(g.get("staging", "")))}</p><main>')
            current = gid
        stem = img_dir / f"s{s['id']:02d}"
        clean = img_dir / "clean" / f"s{s['id']:02d}.jpg"
        raw = Path(f"{stem}-2k.jpg")
        shown = clean if clean.exists() else raw
        pic = (f'<img src="{html.escape(_rel(shown, out_dir))}" alt="s{s["id"]:02d}">' if shown.exists()
               else f'<div class="missing">s{s["id"]:02d} not generated</div>')
        cast = "<br>".join(
            f'{html.escape(str((chars.get(c["key"]) or {}).get("display") or c["key"]))}'
            f'{"" if (c.get("var") or "base") == "base" else " [" + html.escape(c["var"]) + "]"}: '
            f'{html.escape(str(c.get("position", "")) + "; " if c.get("position") else "")}'
            f'{html.escape(str(c.get("pose", "")))}; hands: {html.escape(str(c.get("hands", "")))}'
            for c in s.get("cast", [])) or "no named characters"
        others = f'<br>others: {html.escape(str(s["others"]))}' if s.get("others") else ""
        a, sa, b, eb = wins[s["id"] - 1]
        hindi = " ".join(t for _, t in S.window_text(paras, wins[s["id"] - 1]))
        used = _used_text(stem)
        body.append(
            f'<figure>{pic}<figcaption><b>s{s["id"]:02d}</b> · {html.escape(str(s["shot"]))} · '
            f'{html.escape(str(gid))}'
            f'<p class="cast">{cast}{others}</p>'
            f'<p>{html.escape(str(s.get("action") or s.get("scene")))}</p>'
            f'<p class="hi">[{a}{"-" + str(b) if b != a else ""}] {html.escape(hindi)}</p>'
            f'{f"<p class=used>{html.escape(used)}</p>" if used else ""}'
            f'</figcaption></figure>')
    if current is not None:
        body.append("</main>")
    return (f'<!doctype html><meta charset="utf-8"><title>Chapter {n} contact sheet</title><style>{CSS}</style>'
            f'<h1>Chapter {n}: {html.escape(paras[0])}</h1>' + "".join(body))


def main(argv) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel sheet", usage=USAGE)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--open", action="store_true", help="open the sheet in the default browser")
    a = ap.parse_args(argv)
    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    print(context.header(book, ep))
    from novel.stages import episode as E
    from novel.stages import episode_scenes as S
    ep_root = E.root(ep).resolve()
    sf = S.scenes_path(ep_root, a.chapter)
    if not sf.exists():
        raise SystemExit(f"no scene file for chapter {a.chapter} at {sf}")
    out = ep_root / "images" / f"c{a.chapter:04d}" / "contact.html"
    if json.loads(sf.read_text(encoding="utf-8")).get("version") == 2:
        d = B.bible_dir(book)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_v2(ep_root, a.chapter, B.load_characters(d), B.load_locations(d), out.parent),
                       encoding="utf-8")
        print(f"open {out}")
    else:
        S.cmd_sheet(SimpleNamespace(episode=ep, chapter=a.chapter))
    if a.open:
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
