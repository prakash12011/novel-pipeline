"""novel characters list | props | sheet | convert | make"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from novel import bible as B

USAGE = ("usage: novel characters list [--book B] [--json]\n"
         "       novel characters props [--book B]\n"
         "       novel characters sheet [--book B]\n"
         "       novel characters convert --from OLD.json [--places k1,k2] [--book B] [--force]\n"
         "       novel characters make [--book B] [--ep N] [--keys k1,k2:variant,...] [--resend KEY] [--yes]\n"
         "                             [--i-checked-flow] [--dry-run]")


def make_main(argv, inject=None, ask=input, interactive=None) -> int:
    from novel.commands import characters_make
    return characters_make.main(argv, inject=inject, ask=ask, interactive=interactive)


def _book_dir(book) -> tuple[str, Path]:
    from novel.core import context
    name, _ = context.resolve(book)
    return name, B.bible_dir(name)


def _parser(sub):
    ap = argparse.ArgumentParser(prog=f"novel characters {sub}")
    ap.add_argument("--book")
    return ap


def _list(argv) -> int:
    ap = _parser("list")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    _, d = _book_dir(a.book)
    doc = B.load_characters(d)
    rows = []
    for key, c in doc.get("characters", {}).items():
        rows.append({"key": key, "display": c.get("display"), "tier": c.get("tier"),
                     "first_chapter": c.get("first_chapter"),
                     "variants": [{"var": v, "chapters": x.get("chapters"), "flow_name": x.get("flow_name"),
                                   "flow_made": bool(x.get("flow_made"))}
                                  for v, x in (c.get("variants") or {}).items()]})
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no characters")
        return 0
    print(f"{'key':<22} {'display':<24} tier first  variants")
    for r in rows:
        first = "-" if r["first_chapter"] is None else r["first_chapter"]
        print(f"{r['key']:<22} {str(r['display']):<24} {r['tier']!s:<4} {first!s:<5}", end="")
        parts = []
        for v in r["variants"]:
            flow = v["flow_name"] or "text-only"
            made = "made" if v["flow_made"] else "not made"
            parts.append(f"{v['var']} [{v['chapters']}] {flow} ({made})")
        print("  " + "; ".join(parts))
    return 0


def _props(argv) -> int:
    a = _parser("props").parse_args(argv)
    _, d = _book_dir(a.book)
    found = B.prop_report(B.load_characters(d))
    if not found:
        print("no character reference carries a prop")
        return 0
    for f in found:
        made = ", ".join(f["flow_made_variants"]) or "none"
        print(f"{f['key']} [{f['var']}] ({f['source']}): " + " | ".join(f["phrases"]))
        print(f"    Flow Characters made from this text: {made}")
    print(f"{len(found)} flagged")
    return 1


def build_sheet(chars_doc: dict, locs_doc: dict) -> str:
    style = chars_doc.get("style", "")
    out = ["# Cast sheet", "", f"Style: {style}", ""]
    for key, c in chars_doc.get("characters", {}).items():
        out += [f"## {c.get('display')} (`{key}`) - {c.get('hi', '')}", "",
                f"- Role: {c.get('role', '')}", f"- Tier: {c.get('tier')}, first chapter {c.get('first_chapter')}",
                f"- Props: {', '.join(c.get('props') or []) or 'none'}"]
        if c.get("needs_new_reference"):
            out.append("- NEEDS A NEW REFERENCE (old one carries a prop)")
        out.append("")
        for var, v in (c.get("variants") or {}).items():
            look = v.get("look") or c.get("look", "")
            flow = v.get("flow_name") or "text-only (no Flow Character)"
            out += [f"### {c.get('display')}: {var}", "",
                    f"- Chapters: {v.get('chapters')}", f"- Flow name: {flow}",
                    f"- Made: {'yes' if v.get('flow_made') else 'no'} (ref v{v.get('ref_version')})",
                    f"- Portrait prompt: {B.portrait_prompt(style, look)}",
                    f"- Body prompt: {B.body_prompt(style)}", ""]
    out += ["# Locations", ""]
    locs = locs_doc.get("locations", {})
    if not locs:
        out += ["none", ""]
    for key, l in locs.items():
        out += [f"## {l.get('name')} (`{key}`)", "", f"- Kind: {l.get('kind')}",
                f"- Chapters: {l.get('chapters')}", f"- Default light: {l.get('default_light')}",
                f"- Flow name: {l.get('flow_name') or 'none'}", "", l.get("block") or "", ""]
    return "\n".join(out)


def _sheet(argv) -> int:
    a = _parser("sheet").parse_args(argv)
    _, d = _book_dir(a.book)
    text = build_sheet(B.load_characters(d), B.load_locations(d))
    f = d / "cast-sheet.md"
    f.write_text(text, encoding="utf-8")
    print(f"wrote {f}")
    return 0


def _convert(argv) -> int:
    ap = _parser("convert")
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--places", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    _, d = _book_dir(a.book)
    target = d / B.CHARACTERS_FILE
    if target.exists() and not a.force:
        print(f"{target} already exists. Use --force to replace it (a history copy is kept).", file=sys.stderr)
        return 1
    old = json.loads(Path(a.src).read_text(encoding="utf-8"))
    places = [p for p in a.places.split(",") if p]
    unknown = [p for p in places if p not in old.get("characters", {})]
    if unknown:
        print(f"--places not in the old file: {', '.join(unknown)}", file=sys.stderr)
        return 2
    chars, locs = B.convert_v1(old, places)
    B.save_characters(d, chars)
    B.save_locations(d, locs)
    if not (d / B.NAMES_FILE).exists():
        B.save_names(d, {})
    print(f"converted {len(chars['characters'])} characters and {len(locs['locations'])} locations into {d}")
    return 0


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "make":
        return make_main(rest)
    table = {"list": _list, "props": _props, "sheet": _sheet, "convert": _convert}
    if sub not in table:
        print(f"novel characters: unknown subcommand '{sub}' (list | props | sheet | convert | make)",
              file=sys.stderr)
        return 2
    return table[sub](rest)
