"""Shared helpers for `novel images` and `novel characters make`: plan and picture bookkeeping that mirrors
flow/run.mjs, the Flow stop gate (a stop is never retried, waited out or worked around: the human looks, then
says so with --i-checked-flow), the Chrome checks, and a streaming child runner that survives Ctrl+C.

Nothing here starts or stops Chrome, closes a tab, or talks to Flow. The Node scripts do that.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novel.core import paths

# ---------------------------------------------------------------- plan and pictures (mirrors flow/run.mjs)

MIN_JPEG_BYTES = 50000      # run.mjs jpegOk: length > 50000, starts FF D8, ends FF D9


def jpeg_ok(path) -> bool:
    """The same test as jpegOk() in flow/run.mjs."""
    try:
        x = Path(path).read_bytes()
    except OSError:
        return False
    return len(x) > MIN_JPEG_BYTES and x[0] == 0xFF and x[1] == 0xD8 and x[-2] == 0xFF and x[-1] == 0xD9


def read_jsonl(path) -> list[dict]:
    out = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def flow_dir(ep_root: Path) -> Path:
    return Path(ep_root) / "flow"


def plan_file(ep_root: Path) -> Path:
    return flow_dir(ep_root) / "plan.json"


def load_plan(path: Path) -> dict | None:
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return plan if isinstance(plan, dict) and isinstance(plan.get("shots"), list) else None


def plan_base(plan: dict, ep_root=None) -> Path:
    """Where relative plan paths start. The episode folder when it is known (the runner gets the same folder
    in FLOW_PLAN_BASE, so a data folder that was moved to another PC or path still works), else the plan's own
    "base", else the current directory (very old plans)."""
    if ep_root is not None:
        return Path(os.path.abspath(ep_root))
    return Path(plan["base"]) if plan.get("base") else Path.cwd()


def resolve_plan_path(p, plan: dict, ep_root=None) -> Path:
    """The one place a path from flow/plan.json becomes a real path (as flow/plan.mjs resolvePlanPath):
    absolute paths (old plans) stay as they are; relative ones start at plan_base()."""
    p = Path(str(p))
    if p.is_absolute():
        return p
    return (plan_base(plan, ep_root) / p).resolve()


def relative_plan_path(p, ep_root) -> str:
    """What `novel approve` writes into the plan: the path relative to the episode folder, with forward
    slashes. An old absolute path from another PC or folder (it still contains this episode's folder name,
    e.g. E003) is cut at that name; any other path outside the episode folder is left as it is."""
    q = Path(str(p))
    if not q.is_absolute():
        return q.as_posix()
    try:
        return q.resolve().relative_to(Path(ep_root).resolve()).as_posix()
    except ValueError:
        pass
    parts = q.parts
    name = Path(ep_root).name
    if name in parts[1:-1]:
        i = len(parts) - 1 - parts[::-1].index(name)
        return Path(*parts[i + 1:]).as_posix()
    return q.as_posix()


def journal_path(plan: dict, ep_root: Path) -> Path:
    return resolve_plan_path(plan["journal"], plan, ep_root) if plan.get("journal") else flow_dir(ep_root) / "journal.jsonl"


def stop_file_path(plan: dict, ep_root: Path) -> Path:
    """Where the runner looks for the stop file: the plan's stop_file, else STOP next to the journal."""
    if plan.get("stop_file"):
        return resolve_plan_path(plan["stop_file"], plan, ep_root)
    return journal_path(plan, ep_root).parent / "STOP"


def chapter_id(shot_id: str) -> str:
    return shot_id.split("-")[0]


def chapter_no(shot_id: str) -> int:
    return int(chapter_id(shot_id)[1:])


def maybe_sent(events) -> tuple[str | None, bool]:
    """(state, tile found) as run.mjs maybeSent: only events after the last clean end count."""
    state, tile = None, False
    for e in events or []:
        s = e.get("state")
        if s in ("saved", "error-before-send", "resend-approved"):
            state, tile = None, False
        elif s in ("sending", "sent", "tile", "error-after-send", "refused", "wall"):
            state = s
        if e.get("tile") and s != "saved":
            tile = True
    return state, tile


def classify(plan: dict, ep_root: Path) -> list[dict]:
    """One row per shot: id, chapter (c0001), out path, and a kind:
    done | todo (will be sent) | recover (sent earlier, tile known: downloaded without resending)
    | check (maybe sent: needs a human look) | refused (Flow would not draw it)."""
    events: dict[str, list] = {}
    for e in read_jsonl(journal_path(plan, ep_root)):
        events.setdefault(e.get("id"), []).append(e)
    rows = []
    for s in plan["shots"]:
        out = resolve_plan_path(s["out"], plan, ep_root)
        row = {"id": s["id"], "chapter": chapter_id(s["id"]), "out": out, "chars": list(s.get("chars") or []),
               "shot": s}
        if jpeg_ok(out):
            row["kind"] = "done"
        else:
            state, tile = maybe_sent(events.get(s["id"]))
            if state == "refused":
                row["kind"] = "refused"
            elif state and state != "wall":
                row["kind"] = "recover" if tile else "check"
            else:
                row["kind"] = "todo"
        rows.append(row)
    return rows


def chapter_order(rows) -> list[str]:
    seen: list[str] = []
    for r in rows:
        if r["chapter"] not in seen:
            seen.append(r["chapter"])
    return seen


def parse_chapters(text: str) -> list[int]:
    """"3", "2-5", "1,3-4" -> [1, 3, 4]"""
    out: list[int] = []
    for part in str(text).replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise SystemExit(f"cannot read --chapters {text!r}: use 3 or 2-5 or 1,3-4")
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        if hi < lo:
            raise SystemExit(f"--chapters {text!r}: {part} ends before it starts")
        out.extend(range(lo, hi + 1))
    return out


def episode_approve_hint(ep_root: Path) -> str:
    try:
        info = json.loads((Path(ep_root) / "episode.json").read_text(encoding="utf-8"))
        return f"novel approve --chapters {info['start']}-{info['end']}"
    except (OSError, ValueError, KeyError):
        return "novel approve --chapters A-B"


# ---------------------------------------------------------------- the Flow stop gate
#
# What counts as "Flow stopped" (the human must look before anything is sent again):
#   images journal   state "wall"   gen.mjs journals it in its catch block when a Stop is thrown (the page
#                    shows captcha / unusual traffic or activity / not a robot / sign in to continue / out of
#                    credits / not enough credits / daily limit / reached the limit / limit reached / quota /
#                    too many requests / try again later; or Flow asks to sign in; or the tab is not the
#                    project; or the image is not 16:9; or the prompt bar is not on Image). Exit code 5.
#   status.json      "stopped" containing "Flow put up a limit" (run.mjs, on gen exit 5, single or batch).
#   character journal  an error-* event whose "error" matches /limit|CAPTCHA|sign in/i (char.mjs, which then
#                    exits 5).
# Not a Flow stop: "stop file" (the user's own request, exit 6), "three failures in a row", "three batches in
# a row saved nothing", "Flow shortened the prompt ...", a failed preflight (nothing was sent).

STATUS_WALL = re.compile(r"Flow put up a limit", re.I)
CHAR_WALL = re.compile(r"limit|CAPTCHA|sign in", re.I)
ACKS = "acks.jsonl"


@dataclass(frozen=True)
class Stop:
    at: datetime
    reason: str
    source: str
    key: str

    def when(self) -> str:
        return self.at.astimezone().strftime("%Y-%m-%d %H:%M")


def parse_ts(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _mtime(p: Path) -> datetime:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


def _stops_in_journal(p: Path, want, source: str) -> list[Stop]:
    out = []
    for i, e in enumerate(read_jsonl(p)):
        reason = want(e)
        if reason:
            out.append(Stop(parse_ts(e.get("at")) or _mtime(p), reason, source, f"{p}:{i}"))
    return out


def collect_stops(book: str, bible_flow: Path | None = None) -> list[Stop]:
    """Every Flow stop on record for this book: all episodes' image journals and status files, plus the
    character journal. (Flow is one account for the whole book, so an episode's stop blocks its sister episodes.)"""
    stops: list[Stop] = []
    for fd in sorted(paths.episodes_dir(book).glob("E*/flow")):
        stops += _stops_in_journal(
            fd / "journal.jsonl",
            lambda e: (e.get("error") or "Flow put up a limit, CAPTCHA or sign-in check") if e.get("state") == "wall" else None,
            "images")
        st = None
        try:
            st = json.loads((fd / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if isinstance(st, dict) and STATUS_WALL.search(str(st.get("stopped") or "")):
            stops.append(Stop(parse_ts(st.get("at")) or _mtime(fd / "status.json"), str(st["stopped"]),
                              "status", f"{fd / 'status.json'}:{st.get('at')}"))
    if bible_flow is not None:
        stops += _stops_in_journal(
            Path(bible_flow) / "journal.jsonl",
            lambda e: (e.get("error") if str(e.get("state", "")).startswith("error") and CHAR_WALL.search(str(e.get("error") or "")) else None),
            "characters")
    return sorted(stops, key=lambda s: s.at)


def last_ack(book: str) -> datetime | None:
    best = None
    for f in paths.episodes_dir(book).glob(f"E*/flow/{ACKS}"):
        for e in read_jsonl(f):
            t = parse_ts(e.get("at"))
            if t and (best is None or t > best):
                best = t
    return best


def pending_stops(book: str, hours: float, bible_flow: Path | None = None, now: datetime | None = None) -> list[Stop]:
    """Stops inside the cool-down window that no acknowledgement covers (an ack covers stops older than itself)."""
    now = now or datetime.now(timezone.utc)
    ack = last_ack(book)
    limit = timedelta(hours=float(hours))
    return [s for s in collect_stops(book, bible_flow)
            if now - s.at < limit and (ack is None or s.at > ack)]


def record_ack(ep_root: Path, stops: list[Stop], now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    f = flow_dir(ep_root) / ACKS
    f.parent.mkdir(parents=True, exist_ok=True)
    line = {"at": now.isoformat(), "by": "--i-checked-flow",
            "acknowledged": [{"at": s.at.isoformat(), "reason": s.reason, "source": s.source} for s in stops]}
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return f


def gate_message(stop: Stop, rerun: str, hours) -> str:
    return (f"Flow stopped at {stop.when()}: {stop.reason}. Open Flow yourself and check the account. "
            f"When it is clear, run again with --i-checked-flow:\n  {rerun}\n"
            f"(The tool never retries after a Flow limit, CAPTCHA or sign-in stop; this block lasts "
            f"{hours} h unless you acknowledge it.)")


def gate(book: str, ep_root: Path, hours, bible_flow, acknowledged: bool, rerun: str, dry_run: bool,
         say=print, err=None) -> tuple[bool, list[Stop]]:
    """(ok, stops to acknowledge). Refuses (False) when an unacknowledged stop is inside the cool-down.
    With --i-checked-flow the acknowledgement is written now (not in a dry run) and the run goes on."""
    err = err or (lambda m: print(m, file=sys.stderr))
    pend = pending_stops(book, hours, bible_flow)
    if not pend:
        return True, []
    newest = pend[-1]
    if not acknowledged:
        err(gate_message(newest, rerun, hours))
        if len(pend) > 1:
            err(f"({len(pend)} stops are on record; the newest is shown.)")
        return False, pend
    if dry_run:
        say(f"dry run: would record that you checked Flow after the stop of {newest.when()} ({newest.reason}).")
    else:
        f = record_ack(ep_root, pend)
        say(f"Recorded: you checked Flow after the stop of {newest.when()} ({newest.reason}) -> {f}")
    return True, pend


def pick_stop(stops: list[Stop]) -> Stop:
    """The stop to show a person: the newest one from a journal (it carries Flow's own words), else the newest."""
    return next((s for s in reversed(stops) if s.source != "status"), stops[-1])


def new_stops_since(before: list[Stop], book: str, bible_flow: Path | None = None) -> list[Stop]:
    keys = {s.key for s in before}
    return [s for s in collect_stops(book, bible_flow) if s.key not in keys]


# ---------------------------------------------------------------- chrome checks

def chrome_answers(cdp_url: str, answers=None) -> str | None:
    """The browser version if the DevTools address answers (never starts anything)."""
    if answers is None:
        from novel.commands import chrome_cmd
        answers = chrome_cmd.probe
    return answers(cdp_url)


def cdp_url_of(eff: dict) -> str:
    from novel.core import chromelock
    return str((eff.get("chrome") or {}).get("cdp_url") or chromelock.DEFAULT_URL)


def runner_env(eff: dict, ep_root=None) -> dict:
    """The child's environment: ours (FLOW_GEN / FLOW_CHAR pass through untouched) plus the settings.
    With ep_root, FLOW_PLAN_BASE tells the runner where the plan's relative paths start."""
    env = dict(os.environ)
    if ep_root is not None:
        env["FLOW_PLAN_BASE"] = os.path.abspath(ep_root)
    img = eff.get("images") or {}
    env["FLOW_CDP"] = cdp_url_of(eff)
    if img.get("flow_model"):
        env["FLOW_MODEL"] = str(img["flow_model"])
    if str(img.get("quality") or "").upper() == "1K" and "FLOW_QUALITY" not in env:
        env["FLOW_QUALITY"] = "1K"
    if img.get("allow_agent") and "FLOW_ALLOW_AGENT" not in env:
        env["FLOW_ALLOW_AGENT"] = "1"
    return env


def node_exe() -> str:
    exe = shutil.which("node")
    if not exe:
        raise SystemExit("node was not found on PATH. Install Node.js, then run: novel doctor")
    return exe


def repo_flow(name: str) -> str:
    return str(paths.repo_root() / "flow" / name)


def cmdline(argv) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


# ---------------------------------------------------------------- streaming child, Ctrl+C aware

class Interrupted(Exception):
    """A second Ctrl+C: the child was ended at once."""


def safe_write(out, text: str) -> None:
    try:
        out.write(text)
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "ascii"
        out.write(text.encode(enc, "replace").decode(enc))
    try:
        out.flush()
    except OSError:
        pass


def kill_tree(proc) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except OSError:
            proc.kill()


def pump(proc, stop_file: Path | None, say=print, out=None, on_line=None) -> int:
    """Copy the child's output line by line. First Ctrl+C: ask for a stop (stop file) and keep waiting.
    Second: end the child at once and raise Interrupted. Returns the child's exit code."""
    out = out or sys.stdout
    hits = 0
    while True:
        try:
            for line in proc.stdout:
                if on_line:
                    pre = on_line(line)
                    if pre:
                        safe_write(out, pre + "\n")
                safe_write(out, line if line.endswith("\n") else line + "\n")
            break
        except KeyboardInterrupt:
            hits += 1
            if hits == 1:
                if stop_file is not None:
                    try:
                        Path(stop_file).parent.mkdir(parents=True, exist_ok=True)
                        Path(stop_file).write_text("", encoding="utf-8")
                    except OSError:
                        pass
                    say("\nCtrl+C: asked the runner to stop. It will finish the picture it is on and then end "
                        "(exit code 6). Waiting for it. Press Ctrl+C again to stop it immediately.")
                else:
                    say("\nCtrl+C: this script has no stop file; it will go on to the end of the character it "
                        "is on. Press Ctrl+C again to stop it immediately.")
            else:
                kill_tree(proc)
                say("\nSecond Ctrl+C: the child was ended at once. A picture (or a character) may be in an "
                    "unknown state: look in Flow before running again (the journal never resends on its own).")
                raise Interrupted()
    return proc.wait()


def spawn(argv, env, stop_file: Path | None, say=print, out=None, cwd=None, on_line=None) -> int:
    """Start argv in its own process group (Ctrl+C must not reach it) and stream it. Returns the exit code."""
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            bufsize=1, **kw)
    return pump(proc, stop_file, say=say, out=out, on_line=on_line)


# ---------------------------------------------------------------- human-assisted stops

# gen.mjs WALL = /captcha|unusual (traffic|activity)|not a robot|sign in to continue|out of credits|
# not enough credits|daily limit|reached (your|the) limit|limit reached|quota|too many requests|try again later/
# plus the other Stop messages (sign-in, wrong tab/project, aspect not 16:9, prompt bar not on Image).
# LIMIT: no click in the browser clears it; only time (or credits) does.
LIMIT_RX = re.compile(r"out of credits|not enough credits|daily limit|reached (your|the) limit|limit reached|"
                      r"quota|too many requests|try again later", re.I)
# CLEARABLE: a person at the browser can fix it now.
CLEARABLE_RX = re.compile(r"captcha|unusual (traffic|activity)|not a robot|sign in|asking you to sign|"
                          r"did not open project|prompt bar is set to|not 16:9|aspect|limit or CAPTCHA", re.I)


def classify_stop(reason: str) -> tuple[str, str]:
    """("limit" | "clearable", plain advice). A reason we do not recognise is shown to the person as clearable."""
    r = str(reason or "")
    if LIMIT_RX.search(r) and not re.search(r"captcha|not a robot|unusual", r, re.I):
        return "limit", ("This is a usage limit, not something you can clear in the browser. Stop here and "
                         "wait until the limit resets (or the account has credits again); pressing Enter "
                         "anyway only runs into it again.")
    if CLEARABLE_RX.search(r):
        return "clearable", "Solve it in the Chrome window."
    return "clearable", "Look at the Chrome window and fix what Flow shows."


def is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


ALERT_REPEAT_SECONDS = 30


def system_sound() -> None:
    """The Windows system sound (MessageBeep). Never fails: not Windows, no sound device, no winsound."""
    try:
        import winsound
        winsound.MessageBeep()
    except Exception:
        pass


def alert_beep() -> None:
    """One audible alert: the BEL character on the console and, on Windows, the system sound. It must never
    fail (no console, no sound card, no winsound)."""
    try:
        sys.stdout.write("\x07")
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
    system_sound()


class Alerter:
    """Beeps now and then every `every` seconds until stop(). It only beeps: it never answers the prompt, never
    touches the run. every <= 0 means one beep and no repeat."""

    def __init__(self, beep=None, every: float = ALERT_REPEAT_SECONDS):
        import threading
        self.beep = beep or alert_beep
        self.every = every
        self._done = threading.Event()
        self._thread = None
        self._threading = threading

    def _safe_beep(self) -> None:
        try:
            self.beep()
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._done.wait(self.every):
            self._safe_beep()

    def start(self) -> "Alerter":
        self._safe_beep()
        if self.every and self.every > 0:
            self._thread = self._threading.Thread(target=self._loop, daemon=True, name="flow-alert")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


def alert_seconds(eff: dict) -> float:
    """images.alert_repeat_seconds (default 30; 0 = beep once)."""
    v = (eff.get("images") or {}).get("alert_repeat_seconds", ALERT_REPEAT_SECONDS)
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return float(ALERT_REPEAT_SECONDS)


def ask_continue(stop: Stop, ask, say=print, beep=None, repeat_seconds: float = ALERT_REPEAT_SECONDS) -> bool:
    """Ring the bell, say what happened, wait for ONE keypress. True = continue (Enter), False = stop (s).
    The bell sounds again every `repeat_seconds` while the person has not answered (0 = once). The alert only
    beeps; the run continues only when the keypress arrives."""
    kind, advice = classify_stop(stop.reason)
    say("")
    say("=" * 70)
    say(f"FLOW STOPPED at {stop.when()}: {stop.reason}")
    say(advice)
    if kind == "limit":
        prompt = "Press Enter to try anyway, or type s then Enter to stop here: "
    else:
        prompt = ("When Flow works again, press Enter to continue. Type s then Enter to stop here: ")
    say("=" * 70)
    alerter = Alerter(beep, repeat_seconds).start()
    try:
        answer = ask(prompt)
    except (EOFError, KeyboardInterrupt):
        return False
    finally:
        alerter.stop()
    return str(answer).strip().lower() not in ("s", "stop", "q")


def ensure_chrome(book: str, cdp_url: str, inject: dict | None, ask, interactive: bool,
                  say=print, err=None) -> bool:
    """True when the DevTools port answers. If not and a person is at the terminal, offer to start Chrome with
    the signed-in profile through chrome_cmd (the one place that knows how). Never closes anything."""
    err = err or (lambda m: print(m, file=sys.stderr))
    inject = dict(inject or {})
    answers = inject.get("answers")
    if chrome_answers(cdp_url, answers):
        return True
    if not interactive:
        err(f"Chrome is not answering on {cdp_url}. Start it with: novel chrome")
        return False
    try:
        reply = ask("Chrome is not running. Start it now with your signed-in profile? [Y/n] ")
    except (EOFError, KeyboardInterrupt):
        reply = "n"
    if str(reply).strip().lower() in ("n", "no"):
        err(f"Chrome is not answering on {cdp_url}. Start it with: novel chrome")
        return False
    from novel.commands import chrome_cmd
    code = chrome_cmd.main(["--book", book], **inject)
    if code != 0 or not chrome_answers(cdp_url, answers):
        err(f"Chrome did not come up on {cdp_url}. Try: novel chrome")
        return False
    return True


# Progress header: the first line a picture's id appears in (gen.mjs logs "HH:MM:SS <id> ...").
ID_LINE = re.compile(r"^[0-9:]{8} (c[0-9]{4}-s[0-9]+)")


class Progress:
    """Counts pictures from the runner's own output and returns a one-line header the first time an id shows."""

    def __init__(self, done_before: int, total: int):
        self.n, self.total, self.seen = done_before, total, set()

    def __call__(self, line: str):
        m = ID_LINE.match(line)
        if m and m.group(1) not in self.seen:
            self.seen.add(m.group(1))
            self.n += 1
            return f"[{self.n}/{self.total}] {m.group(1)} ..."
        return None
