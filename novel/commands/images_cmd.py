"""novel images [--book B] [--ep N] [--chapters A-B] [--max N] [--batch K] [--resend ID] [--upgrade]
               [--i-checked-flow] [--dry-run]

Generate today's pictures in Google Flow by driving flow/run.mjs, which attaches over the DevTools port to the
signed-in, visible Chrome that `novel chrome` starts. This command never starts a browser of its own kind, never
closes Chrome or a tab, and never retries, waits out or works around a Flow limit, CAPTCHA, "unusual activity" or
sign-in stop: after a stop it refuses to run again until the human says they looked (--i-checked-flow), or, at an
interactive terminal, asks right there and waits for one keypress per stop.

Refusals (exit 1), in this order: nothing approved; the Flow stop gate; missing Flow Characters; Chrome not answering
(an interactive terminal is offered to start it); the Chrome lock held by another job.

Exit codes
  0    finished its work (or nothing to do), including NEEDS CHECK / REFUSED pictures, which are listed
  1    refused before running, or the runner stopped on a problem that is not a Flow wall (preflight failed,
       three failures in a row, Flow shortened a prompt)
  5    Flow stopped the run (limit, CAPTCHA, unusual activity, sign-in wall, wrong tab/aspect/mode). flow/run.mjs
       itself exits 1 for these; this command maps it to 5 using the journal. With a person at the terminal the
       command asks first; 5 is returned when they type s, or when no one is there (background job).
  6    stopped on request (flow/STOP, `novel jobs --stop images`, or Ctrl+C once): orderly, nothing is wrong
  130  a second Ctrl+C ended the runner at once (a picture may be in an unknown state)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from novel import bible as B
from novel import flowrun as F
from novel.core import chromelock, context, paths, settings

USAGE = ("usage: novel images [--book B] [--ep N] [--chapters A-B] [--max N] [--batch K] [--resend ID] "
         "[--upgrade] [--i-checked-flow] [--dry-run]")

# runner option -> settings key (a missing key leaves the runner's own default in place)
OPTION_KEYS = [
    ("--pause", "images.pause"),
    ("--chapter-pause", "images.chapter_pause"),
    ("--rest-every", "images.rest_every"),
    ("--rest", "images.rest"),
    ("--batch", "images.batch"),
    ("--batch-pause", "images.batch_pause"),      # config/defaults.yaml: 45 s, the runner's own default
]


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _flat(eff: dict, dotted: str):
    node = eff
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def runner_argv(plan_path: Path, only: str | None, max_n: int, eff: dict, batch=None, resend=None,
                upgrade=False) -> list[str]:
    argv = [F.node_exe(), F.repo_flow("run.mjs"), str(plan_path)]
    if only:
        argv += ["--only", only]
    argv += ["--max", str(max_n)]
    for opt, key in OPTION_KEYS:
        val = batch if (opt == "--batch" and batch is not None) else _flat(eff, key)
        if val is not None:
            argv += [opt, str(val)]
    if resend:
        argv += ["--resend", resend]
    if upgrade:
        argv += ["--upgrade"]
    return argv


def _counts(rows, chapters) -> dict:
    sel = [r for r in rows if r["chapter"] in chapters]
    out = {"total": len(sel)}
    for k in ("done", "todo", "recover", "check", "refused"):
        out[k] = sum(1 for r in sel if r["kind"] == k)
    return out


def _reference_problems(book: str, rows, chapters) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """(missing pairs to make, names the bible does not know, names renamed away) for pictures still to be sent."""
    names: list[str] = []
    for r in rows:
        if r["chapter"] in chapters and r["kind"] == "todo":
            for n in r["chars"]:
                if n not in names:
                    names.append(n)
    if not names:
        return [], [], []
    doc = B.load_characters(B.bible_dir(book))
    by_name, legacy = {}, {}
    for key, c in (doc.get("characters") or {}).items():
        for var, v in (c.get("variants") or {}).items():
            if v.get("flow_name"):
                by_name[v["flow_name"]] = (key, var, bool(v.get("flow_made")))
            lg = v.get("legacy") or {}
            for old in [lg.get("flow_name")] + [e.get("flow_name") for e in lg.get("earlier") or []]:
                if old:
                    legacy[old] = (key, var, v.get("flow_name"))
    make, unknown, renamed = [], [], []
    for n in names:
        if n in by_name:
            key, var, made = by_name[n]
            if not made and (key, var) not in make:
                make.append((key, var))
        elif n in legacy:
            renamed.append(f"{n} (now {legacy[n][2]})")
        else:
            unknown.append(n)
    return make, unknown, renamed


def make_keys(pairs) -> str:
    return ",".join(k if v == "base" else f"{k}:{v}" for k, v in pairs)


def _pending_stops_line(stops) -> str:
    return ", ".join(f"{s.when()} {s.reason}" for s in stops)


def main(argv, inject=None, ask=input, interactive=None) -> int:
    """inject: test hooks for chrome_cmd.start / the port probe (keys answers, do_launch, sleep, clock).
    ask: the input function; interactive: None = look at the terminal, else a bool."""
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel images", usage=USAGE)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--chapters", help="e.g. 3 or 2-5 (default: the next images.chapters_per_day chapters "
                                       "that still have pictures to make)")
    ap.add_argument("--max", type=int, dest="max_n")
    ap.add_argument("--batch", type=int)
    ap.add_argument("--resend", metavar="ID")
    ap.add_argument("--upgrade", action="store_true")
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

    # 1. the plan
    pfile = F.plan_file(ep_root)
    plan = F.load_plan(pfile)
    if plan is None:
        _err(f"Nothing approved yet: {pfile} does not exist (or is not a plan).\n"
             f"Approve the chapters first: {F.episode_approve_hint(ep_root)}")
        return 1

    # 2. count pictures (same validity rule as the runner)
    rows = F.classify(plan, ep_root)
    plan_chapters = F.chapter_order(rows)
    n_done = sum(1 for r in rows if r["kind"] == "done")
    print(f"Plan: {len(rows)} pictures in {len(plan_chapters)} chapters; {n_done} done, "
          f"{len(rows) - n_done} to make or check.")

    # 3. choose chapters
    resend_row = None
    if a.resend:
        resend_row = next((r for r in rows if r["id"] == a.resend), None)
        if resend_row is None:
            _err(f"--resend {a.resend}: no such picture in the plan. Pictures look like {rows[0]['id'] if rows else 'c0001-s01'}.")
            return 1
        chosen = [resend_row["chapter"]]
    elif a.chapters:
        wanted = [f"c{n:04d}" for n in F.parse_chapters(a.chapters)]
        absent = [w for w in wanted if w not in plan_chapters]
        if absent:
            _err(f"--chapters {a.chapters}: {', '.join(absent)} {'is' if len(absent) == 1 else 'are'} not in the plan.\n"
                 f"Approve them first: novel approve --chapters {a.chapters}{ep_flag}")
            return 1
        chosen = wanted
    elif a.upgrade:
        chosen = plan_chapters
    else:
        per_day = int(img.get("chapters_per_day") or 3)
        have_work = [c for c in plan_chapters if any(r["chapter"] == c and r["kind"] in ("todo", "recover") for r in rows)]
        chosen = have_work[:per_day]
        if not any(r["kind"] != "done" for r in rows):
            print(f"all {len(rows)} pictures are done")
            print("Next: novel status")
            return 0
        if not chosen:
            blocked = [r for r in rows if r["kind"] in ("check", "refused")]
            _blocked_help(plan, pfile, blocked)
            return 0
    cnt = _counts(rows, chosen)
    if not a.resend and not a.upgrade and cnt["todo"] + cnt["recover"] == 0 and a.chapters:
        print(f"nothing to send in chapters {', '.join(chosen)}: {cnt['done']} of {cnt['total']} pictures are done")
        _blocked_help(plan, pfile, [r for r in rows if r["chapter"] in chosen and r["kind"] in ("check", "refused")])
        return 0
    nums = ", ".join(str(int(c[1:])) for c in chosen)
    print(f"Chapters: {nums} ({cnt['total']} pictures: {cnt['done']} done, {cnt['todo']} to send, "
          f"{cnt['recover']} to download, {cnt['check']} need a check, {cnt['refused']} refused)")

    # 4. the stop gate
    cdp_url = F.cdp_url_of(eff)
    hours = img.get("wall_cooldown_hours", 12)
    ok, _acked = F.gate(book, ep_root, hours, B.bible_dir(book) / "flow", a.checked,
                       f"novel images{ep_flag} --i-checked-flow", a.dry_run)
    if not ok:
        return 1

    # 5. Flow Characters
    if not a.upgrade:
        refs_chapters = set(chosen)
        ref_rows = rows if not a.resend else [dict(r, kind="todo") for r in rows if r["id"] == a.resend]
        make, unknown, renamed = _reference_problems(book, ref_rows, refs_chapters)
        if make or unknown or renamed:
            lines = ["These pictures use Flow Characters that are not ready:"]
            if make:
                lines.append("  not created yet: " + ", ".join(f"{k}" if v == "base" else f"{k}:{v}" for k, v in make))
            if renamed:
                lines.append("  renamed since the plan was written: " + "; ".join(renamed))
            if unknown:
                lines.append("  not in the bible: " + ", ".join(unknown))
            if make:
                lines.append(f"Create them: novel characters make --keys {make_keys(make)}{ep_flag}")
            if renamed or unknown:
                lines.append(f"Then refresh the plan: {F.episode_approve_hint(ep_root)}{ep_flag}")
            _err("\n".join(lines))
            return 1
        # preflight (flow/preflight.mjs) checks every name in the plan, not only today's chapters
        other, _, _ = _reference_problems(book, rows, set(plan_chapters) - refs_chapters)
        if other and not a.dry_run:
            print("note: the preflight also checks the Flow Characters of the other approved chapters; "
                  f"not created yet: {make_keys(other)} (novel characters make --keys {make_keys(other)})")

    max_n = int(a.max_n if a.max_n is not None else (img.get("max_per_run") or 200))
    plan_arg = pfile
    only = a.resend if a.resend else (",".join(chosen) if (a.chapters or not a.upgrade) else None)
    cmd = runner_argv(plan_arg, only, max_n, eff, batch=a.batch, resend=a.resend, upgrade=a.upgrade)

    # 6. Chrome
    if a.dry_run:
        if not F.chrome_answers(cdp_url, (inject or {}).get("answers")):
            _err(f"Chrome is not answering on {cdp_url}. Start it with: novel chrome")
            return 1
        print("\ndry run: nothing was run, no lock was taken. The command would be:")
        print("  " + F.cmdline(cmd))
        print(f"  with FLOW_CDP={cdp_url} FLOW_MODEL={img.get('flow_model')}")
        return 0
    if not F.ensure_chrome(book, cdp_url, inject, ask, interactive):
        return 1

    # 7. the Chrome lock
    try:
        lock = chromelock.acquire(cmd=f"novel images{ep_flag}", book=book, episode=ep)
    except chromelock.ChromeBusy as e:
        _err(str(e.message))
        return 1
    with lock:
        return _run_locked(a, book, ep, ep_root, plan, pfile, rows, chosen, cmd, eff, max_n, cdp_url, inject, ask,
                           interactive)


def _blocked_help(plan, pfile, blocked) -> None:
    if not blocked:
        return
    chk = [r["id"] for r in blocked if r["kind"] == "check"]
    ref = [r["id"] for r in blocked if r["kind"] == "refused"]
    print("Nothing can be sent without a person looking first:")
    _print_blocked(pfile, chk, ref)


def _print_blocked(pfile, check, refused) -> None:
    if check:
        print("NEEDS CHECK (it may already have been sent; never resent automatically): " + " ".join(check))
        print("  Open Flow and look for the picture. If it is there:")
        print(f"    node {F.repo_flow('adopt.mjs')} {pfile} {check[0]}    (then `novel images` downloads it)")
        print("  If it is not there, send it again on purpose: novel images --resend " + check[0])
    if refused:
        print("REFUSED (Flow would not draw these; the scene text must change): " + " ".join(refused))
        for r in refused:
            n = F.chapter_no(r)
            print(f"  {r}: novel qa --fix-prompt {n}   then   novel approve --chapters {n}   then   "
                  f"novel images --resend {r}")


def _run_locked(a, book, ep, ep_root, plan, pfile, rows, chosen, cmd, eff, max_n, cdp_url, inject, ask,
                interactive) -> int:
    stop_file = F.stop_file_path(plan, ep_root)
    if stop_file.exists():
        stop_file.unlink()
        print(f"Removed a stale stop file from an earlier request: {stop_file}")
    bible_flow = B.bible_dir(book) / "flow"
    project = plan.get("project") or "(no project address in the plan)"
    print(f"\nFlow project: {project}")
    print("Keep that Chrome window visible on screen: if Flow asks for a CAPTCHA or anything else, you step in "
          "there. The Flow tab is raised once at the start (and again if something stops). Ctrl+C once = stop "
          "after the current picture; twice = stop now.")

    start_counts = _counts(rows, chosen)
    total_chosen = start_counts["total"]
    started = datetime.now(timezone.utc)
    remaining = max_n
    front = True
    code = 0
    try:
        while True:
            env = F.runner_env(eff, ep_root)
            if front:
                env["FLOW_FRONT"] = "1"
            else:
                env.pop("FLOW_FRONT", None)
            before = F.collect_stops(book, bible_flow)
            now_rows = F.classify(plan, ep_root)
            progress = F.Progress(_counts(now_rows, chosen)["done"], total_chosen)
            argv = list(cmd)
            if "--max" in argv:
                argv[argv.index("--max") + 1] = str(remaining)
            code = F.spawn(argv, env, stop_file, on_line=None if a.upgrade else progress)
            if code in (0, 6):
                break
            new = F.new_stops_since(before, book, bible_flow)
            if code != 5 and not new:
                break                                      # a problem, not a Flow wall
            code = 5
            stop = F.pick_stop(new) if new else F.Stop(datetime.now(timezone.utc), "the runner reported a Flow stop",
                                              "runner", "runner")
            if not interactive:
                print(f"\nFlow stopped: {stop.reason}. Not interactive: ending. The next run is refused until you "
                      f"have looked at Flow and run: novel images --ep {ep} --i-checked-flow")
                break
            if not F.ask_continue(stop, ask, beep=(inject or {}).get("beep"), repeat_seconds=F.alert_seconds(eff)):
                print("Stopping here at your request. Look at Flow before the next run; then "
                      f"novel images --ep {ep} --i-checked-flow")
                break
            f = F.record_ack(ep_root, new or [stop])
            print(f"Recorded your confirmation in {f}")
            if not F.ensure_chrome(book, cdp_url, inject, ask, interactive):
                code = 1
                break
            if not a.upgrade and not a.resend:
                remaining = max_n - (_counts(F.classify(plan, ep_root), chosen)["done"] - start_counts["done"])
                if remaining <= 0:
                    code = 0
                    break
            front = True
            if a.resend:                                   # the resend was approved once; never repeat it
                i = cmd.index("--resend")
                del cmd[i:i + 2]
    except F.Interrupted:
        print("Stopped by a second Ctrl+C.")
        return 130
    _summary(a, plan, pfile, ep_root, chosen, start_counts, rows, code)
    return code


def _summary(a, plan, pfile, ep_root, chosen, start, rows_before, code) -> None:
    now = F.classify(plan, ep_root)
    c = _counts(now, chosen)
    whole = sum(1 for r in now if r["kind"] != "done")
    print("\n--- summary ---")
    if a.upgrade:
        print("upgrade finished (see the runner's own lines above)")
    else:
        print(f"saved this run: {c['done'] - start['done']}   already done before: {start['done']}   "
              f"left in these chapters: {c['total'] - c['done']}   left in the episode: {whole}")
    status = None
    try:
        import json
        status = json.loads((F.flow_dir(ep_root) / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if status and status.get("stopped") and status["stopped"] != "stop file":
        print(f"stopped: {status['stopped']}")
    _print_blocked(pfile, [r["id"] for r in now if r["chapter"] in chosen and r["kind"] == "check"],
                   [r["id"] for r in now if r["chapter"] in chosen and r["kind"] == "refused"])
    if code == 5:
        print("Flow stopped the run. Open Flow, check the account, and when it is clear: novel images --i-checked-flow")
    elif code == 6:
        print("Stopped on request. Run `novel images` again to continue with the rest (nothing is resent).")
    elif whole == 0:
        print("All pictures are done. Next: novel status")
    elif code == 0:
        print("Next: novel images again tomorrow (it picks the next chapters), or novel status")
