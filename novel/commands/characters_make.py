"""novel characters make [--book B] [--ep N] [--keys k1,k2:variant,...] [--resend KEY] [--yes]
                       [--i-checked-flow] [--dry-run]

Create the saved Flow "Characters" (references) a plan needs, by driving flow/char.mjs in the signed-in, visible
Chrome. Same safety as `novel images`: the Flow stop gate, the Chrome check (offered a start at an interactive
terminal), the machine-wide Chrome lock, one keypress per stop, never a retry of a Flow limit or CAPTCHA.

A reference whose old text carried a held item (needs_new_reference) is made again under a NEW name ("<old> v2",
v3 ...) so the old Flow Character is never overwritten or ambiguous; the old name is kept under the variant's
`legacy`. Right after the rename, every episode plan of the book (flow/plan.json, old one kept as plan.prev.json)
gets the new name in `chars` of the shots not generated yet (and on the CAST line of their prompt file); shots whose
picture already exists keep the old name and are listed. The flag is cleared only after the new reference was made. Looks flagged by the prop detector that Gemini
has not cleaned up yet are only listed.

Exit codes: 0 everything asked for was made; 1 refused, declined, or some targets failed / were left; 5 Flow
stopped the run; 130 a second Ctrl+C.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from novel import bible as B
from novel import flowrun as F
from novel.core import chromelock, context, paths, settings

USAGE = ("usage: novel characters make [--book B] [--ep N] [--keys k1,k2:variant,...] [--resend KEY] [--yes] "
         "[--i-checked-flow] [--dry-run]")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def tid(key: str, var: str) -> str:
    """The id char.mjs uses (journal, arguments): key for base, key:variant otherwise."""
    return key if var == "base" else f"{key}:{var}"


def flagged_new(ch: dict, v: dict) -> bool:
    """The old reference of this variant carries a prop and its look has since been cleaned."""
    return bool(v.get("needs_new_reference") or (ch.get("needs_new_reference") and not v.get("look")))


def _all_names(doc: dict) -> set[str]:
    out = set()
    for ch in (doc.get("characters") or {}).values():
        for v in (ch.get("variants") or {}).values():
            if v.get("flow_name"):
                out.add(v["flow_name"])
            lg = v.get("legacy") or {}
            if lg.get("flow_name"):
                out.add(lg["flow_name"])
            out |= {e.get("flow_name") for e in lg.get("earlier") or [] if e.get("flow_name")}
    return out


def next_name(doc: dict, old: str) -> str:
    base = re.sub(r"\s+v\d+$", "", old)
    taken = _all_names(doc)
    n = 2
    while f"{base} v{n}" in taken:
        n += 1
    return f"{base} v{n}"


def rename_for_new_reference(doc: dict, key: str, var: str, now: datetime) -> tuple[str, str]:
    """Give a made variant a new Flow name and unmade state; returns (old, new). The old name is kept."""
    ch = doc["characters"][key]
    v = ch["variants"][var]
    old = v["flow_name"]
    new = next_name(doc, old)
    old_ver = int(v.get("ref_version") or 1)
    lg = dict(v.get("legacy") or {})
    earlier = list(lg.pop("earlier", []) or [])
    if lg.get("flow_name"):
        earlier.append(lg)
    v["legacy"] = {"flow_name": old, "ref_version": old_ver, "renamed_at": now.isoformat(), **(
        {"earlier": earlier} if earlier else {})}
    v["flow_name"] = new
    v["ref_version"] = old_ver + 1
    v["flow_made"] = False
    v["needs_new_reference"] = True          # tracked per variant until its new reference exists
    return old, new


CAST_PREFIX = "CAST (attached Flow characters, keep their look exactly): "


def _atomic_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def _rename_in_prompt(path: Path, renames: dict[str, str]) -> bool:
    """Rewrite the names on the CAST line of a not-yet-sent prompt file as `novel approve` would write them."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith(CAST_PREFIX) and line.endswith("."):
            names = line[len(CAST_PREFIX):-1].split(", ")
            new = [renames.get(n, n) for n in names]
            if new != names:
                lines[i] = CAST_PREFIX + ", ".join(new) + "."
                _atomic_text(path, "\n".join(lines))
                return True
    return False


def update_plans_after_rename(book: str, renames: dict[str, str], out=print) -> dict:
    """After a reference was renamed ("Tarot Lady" -> "Tarot Lady v2"): in every episode plan of the book, give
    the shots that are not generated yet the new name in `chars` (and on the CAST line of their prompt file).
    Shots whose picture already exists are left alone and listed. The old plan is kept as plan.prev.json.
    Returns {episode folder name: {"updated": [ids], "left": [ids]}} (only episodes that use an old name)."""
    result: dict[str, dict] = {}
    eps = paths.book_dir(book) / "episodes"
    for ep_root in sorted(eps.glob("E*")) if eps.is_dir() else []:
        pfile = F.plan_file(ep_root)
        plan = F.load_plan(pfile)
        if plan is None:
            continue
        done = {r["id"] for r in F.classify(plan, ep_root) if r["kind"] == "done"}
        updated, left = [], []
        for shot in plan["shots"]:
            chars = list(shot.get("chars") or [])
            if not any(c in renames for c in chars):
                continue
            if shot["id"] in done:
                left.append(shot["id"])
                continue
            new = []
            for c in chars:
                c = renames.get(c, c)
                if c not in new:
                    new.append(c)
            shot["chars"] = new
            try:
                _rename_in_prompt(F.resolve_plan_path(shot["prompt"], plan, ep_root), renames)
            except (KeyError, OSError):
                pass
            updated.append(shot["id"])
        if not updated and not left:
            continue
        if updated:
            _atomic_text(pfile.with_name("plan.prev.json"), pfile.read_bytes().decode("utf-8"))
            _atomic_text(pfile, json.dumps(plan, ensure_ascii=False, indent=1) + "\n")
        result[ep_root.name] = {"updated": updated, "left": left}
        pair = ", ".join(f"'{o}' -> '{n}'" for o, n in renames.items())
        if updated:
            out(f"{ep_root.name} plan: {pair} in {len(updated)} shot{'s' if len(updated) != 1 else ''} not generated yet "
                f"({', '.join(updated[:6])}{', ...' if len(updated) > 6 else ''}); previous plan kept as plan.prev.json")
        if left:
            out(f"{ep_root.name} plan: left alone, the picture already exists ({', '.join(left[:6])}"
                f"{', ...' if len(left) > 6 else ''}): they keep the old name '{next(iter(renames))}'")
    return result


def _push_flag_down(doc: dict, key: str) -> None:
    ch = doc["characters"][key]
    if ch.get("needs_new_reference"):
        for v in (ch.get("variants") or {}).values():
            if v.get("flow_name") and v.get("flow_made") and not v.get("look"):
                v["needs_new_reference"] = True


def _journal_last(bible_flow: Path, target_id: str):
    ev = [e for e in F.read_jsonl(bible_flow / "journal.jsonl") if e.get("id") == target_id]
    return ev[-1] if ev else None


def _parse_key(arg: str) -> tuple[str, str]:
    key, _, var = arg.strip().partition(":")
    return key, (var or "base")


def main(argv, inject=None, ask=input, interactive=None) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel characters make", usage=USAGE)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--keys", help="k1,k2:variant,... (default: what the plan needs and is not made yet)")
    ap.add_argument("--resend", metavar="KEY", help="make this one again (you looked in Flow first)")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--i-checked-flow", action="store_true", dest="checked")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if interactive is None:
        interactive = F.is_interactive()

    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    context.activate(book)
    print(context.header(book, ep))
    ep_root = paths.ep_root(book, ep)
    eff = settings.effective(book, ep)
    img = eff.get("images") or {}
    ep_flag = f" --ep {ep}"
    bdir = B.bible_dir(book)
    bible_flow = bdir / "flow"

    pfile = F.plan_file(ep_root)
    plan = F.load_plan(pfile)
    if plan is None:
        _err(f"Nothing approved yet: {pfile} does not exist (the plan holds the Flow project address).\n"
             f"Approve the chapters first: {F.episode_approve_hint(ep_root)}")
        return 1
    doc = B.load_characters(bdir)
    chars = doc.get("characters") or {}
    props = {(f["key"], f["var"]) for f in B.prop_report(doc)}

    # ---- targets
    targets: list[dict] = []          # {key, var, kind: create|recreate|resend, name}
    waiting: list[tuple[str, str]] = []
    notes: list[str] = []

    def variant(key, var):
        ch = chars.get(key)
        v = (ch or {}).get("variants", {}).get(var)
        return ch, v

    if a.resend:
        key, var = _parse_key(a.resend)
        ch, v = variant(key, var)
        if not v or not v.get("flow_name"):
            _err(f"--resend {a.resend}: not a character with a Flow name in the bible. See: novel characters list")
            return 1
        targets.append({"key": key, "var": var, "kind": "resend", "name": v["flow_name"]})
    elif a.keys:
        for arg in [x for x in a.keys.split(",") if x.strip()]:
            key, var = _parse_key(arg)
            ch, v = variant(key, var)
            if not v:
                _err(f"--keys {arg}: not in the bible. See: novel characters list")
                return 1
            if not v.get("flow_name"):
                _err(f"--keys {arg}: this variant has no Flow name (text-only), so it has no Flow Character")
                return 1
            _classify(targets, waiting, notes, key, var, ch, v, props, explicit=True)
    else:
        by_name = {}
        legacy_names = {}
        for key, ch in chars.items():
            for var, v in (ch.get("variants") or {}).items():
                if v.get("flow_name"):
                    by_name[v["flow_name"]] = (key, var)
                lg = v.get("legacy") or {}
                if lg.get("flow_name"):
                    legacy_names[lg["flow_name"]] = (key, var)
        seen = set()
        done_ids = {r["id"] for r in F.classify(plan, ep_root) if r["kind"] == "done"}
        for shot in plan["shots"]:
            for name in shot.get("chars") or []:
                if name in seen:
                    continue
                seen.add(name)
                if name in by_name:
                    key, var = by_name[name]
                    ch, v = variant(key, var)
                    _classify(targets, waiting, notes, key, var, ch, v, props, explicit=False)
                elif name in legacy_names:
                    k, vr = legacy_names[name]
                    users = [x["id"] for x in plan["shots"] if name in (x.get("chars") or [])]
                    open_ = [i for i in users if i not in done_ids]
                    if open_:        # pictures still to be drawn would use the old name: the plan needs refreshing
                        notes.append(f"the plan still uses the old name '{name}' ({tid(k, vr)} was renamed) in "
                                     f"{len(open_)} picture{'s' if len(open_) != 1 else ''} not made yet: "
                                     f"refresh the plan with {F.episode_approve_hint(ep_root)}{ep_flag}")
                    else:            # all of them are drawn already: left as they are on purpose
                        notes.append(f"'{name}' was renamed ({tid(k, vr)}); the {len(users)} picture"
                                     f"{'s' if len(users) != 1 else ''} already made with the old name stay as "
                                     f"they are. Nothing to do.")
                else:
                    notes.append(f"the plan uses '{name}', which is not in the bible: "
                                 f"{F.episode_approve_hint(ep_root)}{ep_flag}")

    # ---- the list
    create = [t for t in targets if t["kind"] == "create"]
    recreate = [t for t in targets if t["kind"] == "recreate"]
    resend = [t for t in targets if t["kind"] == "resend"]
    if waiting:
        print("Flagged; waiting for a cleaned-up look from Gemini (the prompt's REVIEW TASK); not created:")
        for k, v in waiting:
            print(f"  {tid(k, v)}")
    for n in notes:
        print(f"note: {n}")
    if not targets:
        print("Nothing to make: every Flow Character this plan needs is already made.")
        return 0
    if create:
        print(f"Create ({len(create)}):")
        for t in create:
            print(f"  {tid(t['key'], t['var'])}  '{t['name']}'")
    if recreate:
        print(f"Recreate under a new name ({len(recreate)}; the old reference has a baked-in prop):")
        for t in recreate:
            print(f"  {tid(t['key'], t['var'])}  '{t['name']}' -> '{next_name(doc, t['name'])}' (the old one stays in Flow)")
    if resend:
        print(f"Make again ({len(resend)}; you looked in Flow first):")
        for t in resend:
            print(f"  {tid(t['key'], t['var'])}  '{t['name']}'")
    print(f"Each one costs Flow credits for a portrait and a body image ({len(targets)} character(s)).")
    if a.dry_run:
        print("dry run: nothing was changed, no lock was taken.")
        return 0

    # ---- gate, chrome, confirmation
    cdp_url = F.cdp_url_of(eff)
    pend = F.pending_stops(book, img.get("wall_cooldown_hours", 12), bible_flow)
    if pend and not a.checked:
        _err(F.gate_message(pend[-1], f"novel characters make{ep_flag} --i-checked-flow",
                            img.get("wall_cooldown_hours", 12)))
        return 1
    if not F.ensure_chrome(book, cdp_url, inject, ask, interactive):
        return 1
    if not a.yes:
        try:
            reply = ask("Proceed? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            reply = ""
        if str(reply).strip().lower() not in ("y", "yes"):
            print("Cancelled: nothing was changed.")
            return 1
    if pend:
        f = F.record_ack(ep_root, pend)
        print(f"Recorded: you checked Flow after the stop of {pend[-1].when()} ({pend[-1].reason}) -> {f}")

    try:
        lock = chromelock.acquire(cmd=f"novel characters make{ep_flag}", book=book, episode=ep)
    except chromelock.ChromeBusy as e:
        _err(str(e.message))
        return 1
    with lock:
        return _run(a, book, ep, ep_root, plan, pfile, bdir, bible_flow, targets, eff, cdp_url, inject, ask,
                    interactive)


def _classify(targets, waiting, notes, key, var, ch, v, props, explicit) -> None:
    made = bool(v.get("flow_made"))
    flagged = flagged_new(ch, v)
    if made and flagged:
        targets.append({"key": key, "var": var, "kind": "recreate", "name": v["flow_name"]})
    elif not made:
        if (key, var) in props and not flagged:
            if explicit:
                notes.append(f"{tid(key, var)}: the look still carries a prop (flagged by `novel characters props`); "
                             "creating it anyway because you named it")
                targets.append({"key": key, "var": var, "kind": "create", "name": v["flow_name"]})
            else:
                waiting.append((key, var))
        else:
            targets.append({"key": key, "var": var, "kind": "create", "name": v["flow_name"]})
    else:
        if (key, var) in props and not flagged:
            waiting.append((key, var))
        if explicit:
            notes.append(f"{tid(key, var)} is already made; skipped (to make it again: "
                         f"novel characters make --resend {tid(key, var)})")


def _run(a, book, ep, ep_root, plan, pfile, bdir, bible_flow, targets, eff, cdp_url, inject, ask,
         interactive) -> int:
    now = datetime.now(timezone.utc)
    project = plan.get("project") or "(no project address in the plan)"
    print(f"\nFlow project: {project}")
    print("Keep that Chrome window visible: if Flow asks for a CAPTCHA or anything else, you step in there. "
          "The Flow tab is raised once at the start (and again if something stops).")

    # renames first, through the bible's own save (history snapshot)
    doc = B.load_characters(bdir)
    renamed_any = False
    renames: dict[str, str] = {}
    for t in targets:
        if t["kind"] == "recreate":
            _push_flag_down(doc, t["key"])
    for t in targets:
        if t["kind"] == "recreate":
            ch, v = doc["characters"][t["key"]], doc["characters"][t["key"]]["variants"][t["var"]]
            if v.get("flow_made"):
                old, new = rename_for_new_reference(doc, t["key"], t["var"], now)
                print(f"{tid(t['key'], t['var'])}: new reference name '{old}' -> '{new}' (old name kept as legacy)")
                renamed_any = True
                renames[old] = new
    if renamed_any:
        snap = B.save_characters(bdir, doc)
        print(f"bible saved (history copy: {snap})")
        # the plans carry Flow names per shot: move the shots not generated yet to the new name
        update_plans_after_rename(book, renames)

    # which invocations
    doc = B.load_characters(bdir)
    plain, solo, skipped = [], [], []
    for t in targets:
        v = doc["characters"][t["key"]]["variants"][t["var"]]
        t["name"] = v["flow_name"]
        last = _journal_last(bible_flow, tid(t["key"], t["var"]))
        renamed_at = F.parse_ts((v.get("legacy") or {}).get("renamed_at"))
        done_at = F.parse_ts((last or {}).get("at"))
        if t["kind"] == "resend":
            solo.append(t)
        elif last and last.get("state") == "done" and not v.get("flow_made"):
            if renamed_at and done_at and done_at < renamed_at:
                solo.append(t)                    # the journal's "done" belongs to the old reference
            else:
                skipped.append(t)
        else:
            plain.append(t)
    for t in skipped:
        print(f"{tid(t['key'], t['var'])}: the journal says done but the bible says not made. Look in Flow; if it "
              f"exists, set flow_made in the bible, else: novel characters make --resend {tid(t['key'], t['var'])}")

    script = os.environ.get("FLOW_CHAR") or F.repo_flow("char.mjs")
    node = F.node_exe()
    groups = []
    if plain:
        groups.append(([tid(t["key"], t["var"]) for t in plain], None))
    for t in solo:
        groups.append(([tid(t["key"], t["var"])], tid(t["key"], t["var"])))

    front = True
    code = 0
    try:
        for ids, resend_id in groups:
            while True:
                argv = [node, script, str(bdir.as_posix()), *ids]
                if resend_id:
                    argv += ["--resend", resend_id]
                argv += ["--plan", str(pfile)]
                env = F.runner_env(eff, ep_root)
                if front:
                    env["FLOW_FRONT"] = "1"
                else:
                    env.pop("FLOW_FRONT", None)
                front = False
                before = F.collect_stops(book, bible_flow)
                rc = F.spawn(argv, env, None)
                new = F.new_stops_since(before, book, bible_flow)
                if rc != 5 and not new:
                    code = rc if rc else code
                    break
                code = 5
                stop = F.pick_stop(new) if new else F.Stop(datetime.now(timezone.utc), "the character script reported a Flow stop",
                                                  "runner", "runner")
                if not interactive:
                    print(f"\nFlow stopped: {stop.reason}. Not interactive: ending. The next run is refused until "
                          f"you have looked at Flow and run: novel characters make --i-checked-flow")
                    raise _Done
                if not F.ask_continue(stop, ask, beep=(inject or {}).get("beep"), repeat_seconds=F.alert_seconds(eff)):
                    print("Stopping here at your request.")
                    raise _Done
                print(f"Recorded your confirmation in {F.record_ack(ep_root, new or [stop])}")
                if not F.ensure_chrome(book, cdp_url, inject, ask, interactive):
                    code = 1
                    raise _Done
                front = True
                if resend_id:                      # never resend blindly: only when nothing was sent before the stop
                    last = _journal_last(bible_flow, resend_id)
                    if not last or last.get("state") not in ("error-before-send", "resend-approved"):
                        resend_id = None
                code = 0
    except _Done:
        pass
    except F.Interrupted:
        print("Stopped by a second Ctrl+C.")
        return 130

    # report, then clear flags only for what was made
    doc = B.load_characters(bdir)
    made, failed = [], []
    changed = False
    for t in targets:
        ch = doc["characters"][t["key"]]
        v = ch["variants"][t["var"]]
        if v.get("flow_made") and t["kind"] != "resend":
            made.append(t)
            if v.pop("needs_new_reference", None) is not None:
                changed = True
            if ch.get("needs_new_reference") and not any(x.get("needs_new_reference") for x in ch["variants"].values()):
                ch.pop("needs_new_reference")
                changed = True
        elif v.get("flow_made") and t["kind"] == "resend":
            made.append(t)
        else:
            failed.append(t)
    if changed:
        B.save_characters(bdir, doc)
    print("\n--- summary ---")
    for t in made:
        stem = t["key"] if t["var"] == "base" else f"{t['key']}.{t['var']}"
        print(f"made: {tid(t['key'], t['var'])} '{t['name']}'")
        for kind in ("portrait", "body"):
            f = bible_flow / f"{stem}-{kind}.jpg"
            print(f"    {kind}: {f}" + ("" if f.exists() else "  (file not found)"))
    for t in failed:
        why = "left (not reached)" if code == 5 else "failed or left"
        print(f"{why}: {tid(t['key'], t['var'])} '{t['name']}'  (look at the output above; "
              "run `novel characters make` again, or --resend KEY after looking in Flow)")
    print(f"made {len(made)}, not made {len(failed)}")
    if code == 5:
        return 5
    if not failed and made:
        print(f"Next: novel images --ep {ep}   (or novel status)")
    return 0 if not failed else 1


class _Done(Exception):
    pass
