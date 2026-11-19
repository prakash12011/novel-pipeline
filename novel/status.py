"""What is ready and what is missing for an episode: pure functions that read files and return data.

`collect(ep_root, book, ep, deps=...)` returns one dict (chapter matrix, episode lines, verdict, the
missing list with the command that fixes each gap). `format_text` turns it into the terminal report.
Nothing here writes to the book's data folder.

Every rule of "valid" or "current" comes from the stage modules (check_lock, check_chapter, why_render,
captions.stale, tts state files) or from flow/run.mjs (the image file test, the journal states); the
functions that need settings, ffprobe or a Gemini key are passed in through `Deps`, so tests can swap them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

MIN_JPEG_BYTES = 50000          # flow/run.mjs jpegOk: x.length > 50000 plus FFD8 ... FFD9
AUDIO_TOLERANCE = 0.25          # episode_video.narration: mp3 versus timing.total_seconds
RECENT_FAILURE_DAYS = 3

# Stages in the order the user runs the fixes. Everything up to "captions" must be done before a build.
STAGES = ["text", "hindi", "lock", "scenes", "qa", "chars", "plan", "images", "audio", "captions",
          "video", "join", "meta", "thumb"]
PRE_BUILD = set(STAGES[:STAGES.index("captions") + 1])
BUILD = {"video", "join"}

BY_HAND = "(or by hand: novel prompt --chapters {ch} --copy, paste into Gemini, then novel import)"
REDO_BY_HAND = "(or by hand: novel prompt --chapters {ch} --mode scenes --copy, paste into Gemini, then novel import --replace)"


# ------------------------------------------------------------------ small helpers

def ranges(nums) -> str:
    """[21, 22, 23, 25] -> '21-23, 25'."""
    nums = sorted(set(nums))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def runs(nums) -> list[list[int]]:
    nums = sorted(set(nums))
    out: list[list[int]] = []
    for n in nums:
        if out and n == out[-1][-1] + 1:
            out[-1].append(n)
        else:
            out.append([n])
    return out


def run_arg(nums) -> str:
    """The value of --chapters: '21-23,25'."""
    return ranges(nums).replace(" ", "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def jpeg_ok(path: Path) -> bool:
    """flow/run.mjs jpegOk, reading only the size and the first and last two bytes."""
    try:
        size = os.stat(path).st_size
        if size <= MIN_JPEG_BYTES:
            return False
        with open(path, "rb") as fh:
            head = fh.read(2)
            fh.seek(-2, os.SEEK_END)
            tail = fh.read(2)
        return head == b"\xff\xd8" and tail == b"\xff\xd9"
    except OSError:
        return False


def journal_states(ep_root: Path) -> dict[str, str]:
    """{shot id: 'needs_check' | 'refused'} from flow/journal.jsonl, by run.mjs's maybeSent: only the events
    after the last clean end (saved, error-before-send, resend-approved) count. A 'wall' alone is neither."""
    f = Path(ep_root) / "flow" / "journal.jsonl"
    events: dict[str, list[str]] = {}
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and "id" in e:
            events.setdefault(str(e["id"]), []).append(str(e.get("state")))
    out = {}
    for sid, states in events.items():
        state = None
        for s in states:
            if s in ("saved", "error-before-send", "resend-approved"):
                state = None
            elif s in ("sending", "sent", "tile", "error-after-send", "refused", "wall"):
                state = s
        if state == "refused":
            out[sid] = "refused"
        elif state and state != "wall":
            out[sid] = "needs_check"
    return out


def _local_hhmm(stamp: str) -> str:
    """'20261110T174906' (UTC, from the tts push marker) or an ISO time -> local 'HH:MM'."""
    try:
        if re.fullmatch(r"\d{8}T\d{6}", stamp):
            dt = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.strftime("%H:%M")
        return dt.astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return str(stamp)


# ------------------------------------------------------------------ what comes from outside the files

@dataclass
class Deps:
    why_render: Callable[[Path, int], str | None]
    media_seconds: Callable[[Path], float | None]
    gemini_keys: Callable[[], list]
    captions_needed: Callable[[int, int], bool]
    cards_state: Callable[[], str]
    chrome_lock_files: Callable[[], list[Path]]
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))


def default_deps(book: str) -> Deps:
    """The real thing: why_render (needs settings, so the book is activated), ffprobe through
    episode_video.probe, Gemini keys from secrets.env, the chrome lock folder under %LOCALAPPDATA%."""
    from novel.core import context
    context.activate(book)

    def why_render(ep_root, n):
        from novel.stages import episode_build
        try:
            return episode_build.why_render(ep_root, n)
        except (SystemExit, Exception) as e:                    # a settings or report problem: say so
            return f"cannot tell ({str(e)[:60]})"

    def media_seconds(path):
        try:
            from novel.stages.episode_video import probe
            return float(probe(path)["format"]["duration"])
        except Exception:
            return None

    def gemini_keys():
        from novel.core import secrets
        return secrets.gemini_keys()

    def captions_needed(ep, n):
        from novel.stages import episode_build
        try:
            return episode_build.captions_on(ep, n)
        except (SystemExit, Exception):
            return True

    def cards_state():
        return cards_state_readonly(book)

    def chrome_files():
        base = os.environ.get("LOCALAPPDATA")
        d = Path(base) / "novel-pipeline" if base else None
        return sorted(d.glob("chrome-cdp-*.lock")) if d and d.is_dir() else []

    return Deps(why_render, media_seconds, gemini_keys, captions_needed, cards_state, chrome_files)


def cards_state_readonly(book: str) -> str:
    """'ready' | 'stale' | 'missing'. episode_extras.cards_ready makes the cards when they are out of date,
    which writes into the book's data, so this repeats its test without the make step."""
    try:
        from novel.core import paths
        from novel.stages import episode as E
        from novel.stages import episode_extras as X
        root = paths.extras_dir(book) / f"E{X.EP:03d}"
        mp3 = root / "audio" / f"{X.CH:05d}.mp3"
        voiced = root / "video" / "subscribe-voiced.mp4"
        silent = root / "video" / "subscribe-silent.mp4"
        if not mp3.exists() or not voiced.exists() or not silent.exists():
            return "missing"
        pics = list((root / "image").glob("subscribe*.jpg"))
        newest = max([mp3.stat().st_mtime, Path(X.__file__).stat().st_mtime] + [q.stat().st_mtime for q in pics])
        stamp = _read_json(root / "video" / "cards.json", {})
        current = stamp.get("key") == X._signature(E.cfg())
        return "ready" if voiced.stat().st_mtime >= newest and current else "stale"
    except (SystemExit, Exception):
        return "missing"


# ------------------------------------------------------------------ per-chapter facts

def _lock_status(ep_root: Path, n: int) -> str:
    from novel.stages import episode as E
    try:
        E.check_lock(ep_root, n)
        return "ok"
    except FileNotFoundError:
        return "-"
    except SystemExit as e:
        msg = str(e.code)
        if "changed after" in msg:
            return "CHANGED"
        return "-"
    except (OSError, ValueError):
        return "-"


def _sha(path: Path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _qa_file_is_stale(doc, ep_root: Path, n: int) -> bool:
    """A v2 QA file (check or review) whose stored hashes differ from the current Hindi / scene file.
    A file without a stored hash cannot be compared and is not called stale."""
    if not isinstance(doc, dict) or doc.get("version") != 2:
        return False
    for key, path in (("hindi_sha256", Path(ep_root) / "hindi" / f"{n:05d}.txt"),
                      ("scenes_sha256", Path(ep_root) / "scenes" / f"{n:05d}.json")):
        if key in doc and doc[key] != _sha(path):
            return True
    return False


def qa_state(ep_root: Path, n: int) -> tuple[str, int]:
    """('ok' | 'FIX' | 'forced' | 'old' | 'stale' | '-', number of errors). 'stale': a v2 check or review
    was made from a Hindi or scene file that has changed since; it does not count."""
    f = Path(ep_root) / "qa" / f"{n:05d}.json"
    rv = Path(ep_root) / "qa" / f"{n:05d}.review.json"
    doc = _read_json(f) if f.exists() else None
    review = _read_json(rv) if rv.exists() else None
    v2 = isinstance(doc, dict) and doc.get("version") == 2
    if not v2 and not isinstance(review, dict):
        return ("old", 0) if f.exists() else ("-", 0)
    if _qa_file_is_stale(doc, ep_root, n) or _qa_file_is_stale(review, ep_root, n):
        return "stale", 0
    errors = len(doc.get("errors") or []) if v2 else 0
    fix = False
    if isinstance(review, dict):
        if str(review.get("verdict", "")).upper() == "FIX":
            fix = True
            errors += sum(1 for x in (review.get("findings") or [])
                          if isinstance(x, dict) and x.get("severity") == "error")
    if errors or fix:
        return ("forced" if v2 and doc.get("forced") else "FIX"), errors
    return "ok", 0


def _variant(roster: dict, key: str, var: str) -> tuple[str | None, bool]:
    """(flow_name, made) of a cast member, as episode_scenes._attached reads the roster."""
    ch = roster.get(key) or {}
    if isinstance(ch.get("variants"), dict):
        v = ch["variants"].get(var or "base") or {}
        return v.get("flow_name"), bool(v.get("flow_made"))
    return ch.get("flow_name"), bool(ch.get("flow_made"))


def _scene_file(ep_root: Path, n: int):
    return _read_json(Path(ep_root) / "scenes" / f"{n:05d}.json")


def _chapter_row(ep_root: Path, ep: int, c, lock_by: dict, roster, plan_ids, journal, deps: Deps,
                 tts_run: dict | None, keys: bool, text_present: bool) -> dict:
    from novel.stages import episode_captions as C
    from novel.stages import episode_scenes as S
    n = c.idx
    name = f"{n:05d}"
    row: dict = {"chapter": n, "title": c.title}
    row["text"] = text_present
    hindi = ep_root / "hindi" / f"{name}.txt"
    row["hindi"] = hindi.exists()
    row["lock"] = _lock_status(ep_root, n) if row["hindi"] else "-"
    row["qa"], row["qa_errors"] = qa_state(ep_root, n)

    # scenes
    doc = _scene_file(ep_root, n)
    sf = S.scenes_path(ep_root, n)
    row.update(scenes=None, scenes_state="-", scenes_problem=None, scene_ids=[], version=None)
    if sf.exists():
        ids = []
        try:
            ids = [int(s["id"]) for s in doc["scenes"]]
        except (TypeError, KeyError, ValueError):
            doc = None
        row["scene_ids"] = ids
        row["version"] = doc.get("version") if isinstance(doc, dict) else None
        try:
            problems = S.check_chapter(ep_root, n)
        except SystemExit as e:
            problems = [str(e.code)]
        except Exception as e:                                  # unreadable file, missing roster, ...
            problems = [f"{type(e).__name__}: {str(e)[:80]}"]
        if doc is None and not problems:
            problems = ["not a valid scene file"]
        if problems:
            row["scenes_state"], row["scenes_problem"] = "BAD", problems[0]
        else:
            row["scenes_state"], row["scenes"] = "ok", len(ids)

    # chars (v2 scene files only)
    row.update(chars_made=None, chars_needed=None, chars_missing=[])
    if isinstance(doc, dict) and doc.get("version") == 2 and roster is not None:
        need: dict[tuple, str] = {}
        for s in doc.get("scenes") or []:
            for m in s.get("cast") or []:
                if not isinstance(m, dict):
                    continue
                var = m.get("var") or "base"
                flow, made = _variant(roster, m.get("key"), var)
                if flow:
                    need[(m.get("key"), var)] = (flow, made)
        row["chars_needed"] = len(need)
        row["chars_made"] = sum(1 for _, made in need.values() if made)
        row["chars_missing"] = [{"key": k, "var": v, "flow_name": f} for (k, v), (f, made) in need.items() if not made]

    # images
    img_dir = ep_root / "images" / f"c{n:04d}"
    ids = row["scene_ids"]
    good, invalid = [], []
    for i in ids:
        p = img_dir / f"s{i:02d}-2k.jpg"
        if jpeg_ok(p):
            good.append(i)
        elif p.exists() and p.stat().st_size >= 1:
            invalid.append(f"c{n:04d}-s{i:02d}")
    row["images_ok"] = len(good) if ids else None
    row["images_expected"] = len(ids) if ids else None
    row["images_invalid"] = invalid
    shots = {f"c{n:04d}-s{i:02d}" for i in ids if i not in good}
    row["needs_check"] = sorted(s for s in shots if journal.get(s) == "needs_check")
    row["refused"] = sorted(s for s in shots if journal.get(s) == "refused")
    row["planned"] = None if not ids else all(f"c{n:04d}-s{i:02d}" in (plan_ids or ()) for i in ids)

    # audio
    mp3, tfile = ep_root / "audio" / f"{name}.mp3", ep_root / "timing" / f"{name}.json"
    row.update(audio="-", audio_detail=None)
    if c.status == "done" and mp3.exists() and tfile.exists():
        want = (_read_json(tfile) or {}).get("total_seconds")
        got = deps.media_seconds(mp3)
        if want is None:
            row["audio"], row["audio_detail"] = "BAD", "timing file has no total_seconds"
        elif got is not None and abs(got - float(want)) > AUDIO_TOLERANCE:
            row["audio"], row["audio_detail"] = "BAD", f"mp3 is {got:.1f}s, timing says {float(want):.1f}s"
        else:
            row["audio"] = "ok"
    elif mp3.exists() and tfile.exists():
        row["audio"], row["audio_detail"] = "BAD", f"manifest says '{c.status}' although mp3 and timing exist"
    elif mp3.exists() or tfile.exists():
        row["audio"], row["audio_detail"] = "BAD", ("timing map missing" if mp3.exists() else "mp3 missing")
    if row["audio"] != "ok" and tts_run and n in (tts_run.get("chapters") or []):
        if tts_run.get("last_status") in ("ERROR", "CANCEL_ACKNOWLEDGED", "CANCEL_REQUESTED"):
            row["audio"], row["audio_detail"] = "FAILED", f"the Kaggle run ended with {tts_run['last_status']}"
        else:
            row["audio"], row["audio_detail"] = "RUNNING", _local_hhmm(str(tts_run.get("pushed")))

    # captions
    cap = C.caption_path(ep_root, n)
    if not cap.exists():
        row["capt"] = "-"
    else:
        try:
            row["capt"] = "stale" if C.stale(ep_root, n) else "ok"
        except (OSError, ValueError, KeyError):
            row["capt"] = "stale"
    row["capt_needed"] = bool(deps.captions_needed(ep, n))

    # video
    mp4 = ep_root / "video" / f"c{n:04d}.mp4"
    row.update(video="-", video_reason=None)
    if mp4.exists():
        why = deps.why_render(ep_root, n)
        row["video"] = "ok" if why is None else f"stale: {why}"
        row["video_reason"] = why
    return row


# ------------------------------------------------------------------ episode lines

def _yaml(path: Path) -> dict:
    try:
        d = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return d if isinstance(d, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def episode_lines(ep_root: Path, ep: int, start, end, rows: list[dict], deps: Deps) -> dict:
    out: dict = {}
    full = [f"E{ep:03d}.mp4"] + ([f"E{ep:03d}-c{start}-{end}.mp4"] if start is not None else [])
    final = next((ep_root / f for f in full if (ep_root / f).exists()), None)
    vids = [ep_root / "video" / f"c{r['chapter']:04d}.mp4" for r in rows]
    vids = [v for v in vids if v.exists()]
    joined: dict = {"name": full[0], "exists": final is not None, "current": False, "seconds": None, "reason": None}
    if final is not None:
        joined["name"] = final.name
        joined["seconds"] = deps.media_seconds(final)
        newer = [v.name for v in vids if v.stat().st_mtime > final.stat().st_mtime]
        stale = [r["chapter"] for r in rows if r["video"].startswith("stale")]
        joined["chapter_videos_found"] = len(vids)
        if newer:
            joined["reason"] = f"{', '.join(newer[:3])} rendered after it"
        elif stale:
            joined["reason"] = f"chapter video(s) {ranges(stale)} out of date"
        else:
            joined["current"] = True
    out["joined"] = joined
    out["partial_videos"] = sorted(p.name for p in ep_root.glob(f"E{ep:03d}-c*.mp4") if p != final)
    out["timestamps"] = sorted(p.name for p in ep_root.glob(f"E{ep:03d}*.timestamps.*"))
    out["cards"] = deps.cards_state()
    pj = ep_root / "publish" / "publish.json"
    pub = _read_json(pj) if pj.exists() else None
    if pub is None:
        out["publish"] = {"state": "missing", "errors": 0}
    elif isinstance(pub, dict) and pub.get("valid") is True:
        out["publish"] = {"state": "valid", "errors": 0}
    else:
        errs = pub.get("errors") if isinstance(pub, dict) else None
        out["publish"] = {"state": "invalid", "errors": len(errs) if isinstance(errs, list) else 0}
    out["thumbnails"] = sorted(p.name for p in (ep_root / "publish").glob("thumb-*.jpg"))
    y = _yaml(ep_root / "episode.yaml")
    out["published"] = bool(y.get("published"))
    out["published_url"] = y.get("published_url") or None
    return out


def jobs_info(ep_root: Path, deps: Deps) -> list[dict]:
    """Running jobs, and jobs that failed, crashed or were stopped in the last few days."""
    out = []
    now = deps.now()
    for f in sorted((Path(ep_root) / "jobs").glob("*.json")):
        j = _read_json(f)
        if not isinstance(j, dict):
            continue
        state = str(j.get("state"))
        if state == "running":
            keep = True
        elif state in ("failed", "crashed", "stopped"):
            keep = True
            fin = j.get("finished")
            try:
                dt = datetime.fromisoformat(str(fin).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=now.tzinfo or timezone.utc)
                keep = now - dt <= timedelta(days=RECENT_FAILURE_DAYS)
            except (ValueError, TypeError):
                pass
        else:
            keep = False
        if keep:
            since = j.get("started") if state == "running" else (j.get("finished") or j.get("started"))
            out.append({"name": j.get("name") or f.stem, "state": state, "since": _local_hhmm(str(since)) if since else None,
                        "log": j.get("log"), "note": j.get("note"), "exit_code": j.get("exit_code")})
    return out


def progress_text(prog) -> str:
    """One short line for qa/importer progress.json ({"bible": {"done"}, "chapters": {"1": {"status", "images"}}}).
    Another shape is shown as compact JSON."""
    if prog is None:
        return "unreadable"
    chs = prog.get("chapters") if isinstance(prog, dict) else None
    if isinstance(chs, dict) and chs and all(isinstance(v, dict) for v in chs.values()):
        parts = []
        bible = prog.get("bible")
        if isinstance(bible, dict):
            parts.append("bible done" if bible.get("done") else "bible waiting")
        groups: dict[str, list] = {}
        for k, v in chs.items():
            if str(k).isdigit():
                label = str(v.get("status") or "waiting")
                groups.setdefault(label, []).append((int(k), v.get("images")))
        for label in ("done", "partial", "waiting"):
            rows = sorted(groups.pop(label, []))
            if not rows:
                continue
            if label == "partial":
                parts.append("partial " + ", ".join(f"{n} ({img})" if img else str(n) for n, img in rows))
            else:
                parts.append(f"{label} {ranges([n for n, _ in rows])}")
        for label, rows in groups.items():
            parts.append(f"{label} {ranges([n for n, _ in sorted(rows)])}")
        text = "; ".join(parts)
    else:
        text = json.dumps(prog, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= 80 else text[:77] + "..."


def gemini_batches(book: str, chapters: list[int]) -> list[dict]:
    from novel.core import paths
    out = []
    d = paths.gemini_dir(book)
    if not d.is_dir():
        return out
    want = set(chapters)
    for bj in sorted(d.glob("*/batch.json")):
        b = _read_json(bj)
        pj = bj.parent / "progress.json"
        if not isinstance(b, dict) or not pj.exists() or not want & set(b.get("chapters") or []):
            continue
        prog = _read_json(pj)
        out.append({"batch": b.get("batch") or bj.parent.name, "mode": b.get("mode"),
                    "chapters": b.get("chapters") and f"{min(b['chapters'])}-{max(b['chapters'])}",
                    "progress": progress_text(prog)})
    return out


def chrome_info(deps: Deps) -> list[str]:
    out = []
    for f in deps.chrome_lock_files():
        j = _read_json(f)
        if isinstance(j, dict):
            ep = j.get("episode")
            who = f"{j.get('book', '?')} E{int(ep):03d}" if isinstance(ep, int) else str(j.get("book", "?"))
            since = f" since {_local_hhmm(str(j['started']))}" if j.get("started") else ""
            out.append(f"Chrome is in use by {who}{since} (pid {j.get('pid', '?')})")
    return out


# ------------------------------------------------------------------ the missing list

class _Gaps:
    """Collects (stage, text, command, kind) per chapter and groups chapters that share a gap."""

    def __init__(self, prefix: Callable[[str, bool], str]):
        self.items: dict[tuple, dict] = {}
        self.order = 0
        self.prefix = prefix

    def add(self, stage: str, n: int | None, text: str, cmd: str, *, kind: str = "todo",
            single_range: bool = False, ep_flag: bool = True, after: str = ""):
        key = (stage, text, cmd, kind, single_range, ep_flag, after)
        it = self.items.get(key)
        if it is None:
            it = self.items[key] = {"chapters": [], "order": self.order}
            self.order += 1
        if n is not None:
            it["chapters"].append(n)

    def build(self) -> list[dict]:
        out = []
        for (stage, text, cmd, kind, single, ep_flag, after), it in self.items.items():
            chs = sorted(set(it["chapters"]))
            groups = runs(chs) if single and chs else [chs]
            for g in groups:
                label = ""
                if chs:
                    label = ("chapter " if len(g) == 1 else "chapters ") + ranges(g) + ": "
                spec = run_arg(g)
                line_cmd = self.prefix(cmd.replace("{ch}", spec), ep_flag)
                tail = after.replace("{ch}", spec)
                full = f"{label}{text.replace('{ch}', ranges(g))}"
                out.append({"stage": stage, "kind": kind, "chapters": g, "text": full, "command": line_cmd + (f"   {tail}" if tail else ""),
                            "line": f"{full} -> {line_cmd}" + (f"   {tail}" if tail else ""),
                            "order": (STAGES.index(stage), it["order"], g[0] if g else 0)})
        out.sort(key=lambda x: x["order"])
        for x in out:
            x.pop("order")
        return out


def missing_list(rows: list[dict], info: dict, ep: int, book: str, state_book, state_ep, deps: Deps,
                 *, episode_level: bool, tts_run: dict | None) -> tuple[list[dict], list[str]]:
    """([{stage, kind, chapters, text, command, line}], notes). kind: 'todo' or 'waiting' (a blocker that
    needs time, not an action) or 'later' (a step after the build)."""
    def prefix(cmd: str, ep_flag: bool) -> str:
        """Name the book and episode when they are not the current ones (state.json)."""
        flags = ([f"--book {book}"] if book != state_book else []) + ([f"--ep {ep}"] if ep_flag and ep != state_ep else [])
        return " ".join([cmd] + flags)

    g = _Gaps(prefix)
    notes: list[str] = []
    keys = bool(deps.gemini_keys())
    pre_ok = True
    for r in rows:
        n = r["chapter"]
        if not r["hindi"]:
            if not r["text"]:
                g.add("text", n, "English text missing", "novel extract", ep_flag=False, after="(then: novel episode add)")
            else:
                g.add("hindi", n, "no Hindi yet", "novel write", single_range=True, after=BY_HAND)
            continue
        if r["lock"] == "-":
            g.add("lock", n, "not approved (Hindi not locked or prompts not written)", "novel approve --chapters {ch}")
        elif r["lock"] == "CHANGED":
            g.add("lock", n, "Hindi changed after it was locked", "novel approve --chapters {ch}",
                  after="(or restore the file; audio, scenes and captions made from the old text go stale)")
        locked = r["lock"] == "ok"
        if locked and r["scenes_state"] == "-":
            g.add("scenes", n, "no scenes yet", "novel write", single_range=True, after=BY_HAND)
        elif locked and r["scenes_state"] == "BAD":
            g.add("scenes", n, f"scenes are not usable ({r['scenes_problem']})",
                  "novel write --redo {ch}", single_range=True, after=REDO_BY_HAND)
        # QA is optional and run by hand: an out-of-date QA no longer blocks the build (the table still shows "stale").
        # if r["qa"] == "stale":
        #     g.add("qa", n, "QA is out of date (the Hindi or the scenes changed after it ran)",
        #           "novel qa --chapters {ch}", single_range=True, ep_flag=False)
        if r["qa"] == "FIX":
            g.add("qa", n, f"QA says FIX ({_plural(r['qa_errors'], 'error')})", "novel qa --fix-prompt {ch}", ep_flag=False)
        for m in r["chars_missing"]:
            g.add("chars", n, f'Flow Character "{m["flow_name"]}" not created (needed by chapters {{ch}})',
                  f"novel characters make --keys {m['key']}", ep_flag=False)
        exp = r["images_expected"]
        if r["scenes_state"] == "ok" and exp:
            short = r["images_ok"] < exp
            if short and r["planned"] is False:
                g.add("plan", n, "image prompts and Flow plan not written", "novel approve --chapters {ch}")
            if short:
                inv = r["images_invalid"]
                extra = f" ({_plural(len(inv), 'invalid file')}: {', '.join(i.split('-')[1] for i in inv[:4])})" if inv else ""
                gone = len(r["needs_check"]) + len(r["refused"])
                if exp - r["images_ok"] - gone > 0:
                    g.add("images", n, f"{r['images_ok']} of {exp} images{extra}", "novel images --chapters {ch}")
            if r["needs_check"]:
                ids = r["needs_check"]
                g.add("images", n, f"{_plural(len(ids), 'image')} need a manual look in Flow ({', '.join(ids[:4])}"
                      f"{', ...' if len(ids) > 4 else ''})", f"novel images --resend {ids[0]}", ep_flag=False,
                      after="(look in flow/journal.jsonl first; resend each id)")
            if r["refused"]:
                ids = r["refused"]
                g.add("images", n, f"Flow refused {_plural(len(ids), 'prompt')} ({', '.join(ids[:4])}"
                      f"{', ...' if len(ids) > 4 else ''}): the scene needs rewriting", f"novel images --resend {ids[0]}",
                      ep_flag=False, after="(after rewriting the scene)")
        if locked:
            if r["audio"] == "RUNNING":
                g.add("audio", n, f"narration RUNNING on Kaggle since {r['audio_detail']}", "novel jobs", kind="waiting", ep_flag=False)
            elif r["audio"] == "FAILED":
                g.add("audio", n, f"narration failed on Kaggle ({r['audio_detail']})", "novel tts reset",
                      after="(clears the failed run; then: novel tts push --chapters {ch})")
            elif r["audio"] == "-":
                g.add("audio", n, "no narration", "novel run", ep_flag=False, after="(or: novel tts push --chapters {ch})")
            elif r["audio"] == "BAD":
                cmd = "novel tts pull" if "timing map missing" in (r["audio_detail"] or "") else "novel tts push --chapters {ch}"
                g.add("audio", n, f"narration is not usable ({r['audio_detail']})", cmd)
        if r["capt_needed"] and r["capt"] in ("-", "stale"):
            if keys:
                notes.append(("captions", n, r["capt"]))
            else:
                g.add("captions", n, f"captions {'out of date' if r['capt'] == 'stale' else 'missing'} and no Gemini key to make them",
                      "novel captions --chapters {ch}", after="(needs GEMINI_API_KEY in secrets.env)")
    out_notes = []
    cap = [n for (_, n, _) in notes]
    if cap:
        out_notes.append(f"captions missing or out of date for chapters {ranges(cap)}: the render makes them itself "
                         f"(Gemini key found), not a blocker")
    items = g.build()
    inputs_ok = not [i for i in items if i["stage"] in PRE_BUILD and i["kind"] != "waiting"] and \
        not [i for i in items if i["kind"] == "waiting"]
    if inputs_ok:
        vids = {"-": [], "stale": []}
        for r in rows:
            if r["video"] == "-":
                vids["-"].append(r["chapter"])
            elif r["video"].startswith("stale"):
                vids["stale"].append(r)
        for n in vids["-"]:
            g.add("video", n, "video not rendered", "novel build", ep_flag=True)
        for r in vids["stale"]:
            g.add("video", r["chapter"], f"video is out of date ({r['video_reason']})", "novel build")
        if episode_level:
            if not any(i[0] == "video" for i in g.items) and not info["joined"]["current"]:
                g.add("join", None, "episode video " + ("is out of date: " + info["joined"]["reason"] if info["joined"]["exists"]
                                                      else "not built"), "novel build")
            if info["cards"] == "missing":
                out_notes.append("subscribe cards are not made: the joined video goes without them "
                                 "(novel extras voice, then novel extras cards)")
            if info["publish"]["state"] == "missing":
                g.add("meta", None, "publish text missing", "novel meta", kind="later")
            elif info["publish"]["state"] == "invalid":
                g.add("meta", None, f"publish text invalid ({_plural(info['publish']['errors'], 'error')})", "novel meta", kind="later")
            if not info["thumbnails"]:
                g.add("thumb", None, "thumbnails missing", "novel thumb", kind="later")
        items = g.build()
    return items, out_notes


# ------------------------------------------------------------------ the whole episode

def chapter_numbers(ep_root: Path):
    """(chapter rows from the manifest, start, end). Falls back to episode.json when there is no manifest."""
    from novel.stages.manifest import Chapter, Manifest
    info = _read_json(Path(ep_root) / "episode.json", {}) or {}
    man = Manifest.load(Path(ep_root) / "manifest.json")
    chapters = list(man.chapters)
    if not chapters and "start" in info and "end" in info:
        chapters = [Chapter(idx=i, title="", source="") for i in range(int(info["start"]), int(info["end"]) + 1)]
    start = info.get("start", chapters[0].idx if chapters else None)
    end = info.get("end", chapters[-1].idx if chapters else None)
    return chapters, start, end


def collect(ep_root: Path, book: str, ep: int, deps: Deps, chapters: list[int] | None = None,
            state: tuple | None = None) -> dict:
    """Everything `novel status` knows about one episode, as plain data."""
    ep_root = Path(ep_root)
    state = state or (book, ep)
    all_ch, start, end = chapter_numbers(ep_root)
    sel = [c for c in all_ch if chapters is None or c.idx in chapters]
    lock_doc = _read_json(ep_root / "hindi.lock.json", {})
    from novel.stages import episode_scenes as S
    roster = None
    try:
        roster = S.load_roster(ep_root)["characters"]
    except (SystemExit, Exception):
        roster = None
    plan = _read_json(ep_root / "flow" / "plan.json")
    plan_ids = {s.get("id") for s in plan.get("shots", [])} if isinstance(plan, dict) else None
    journal = journal_states(ep_root)
    ts = _read_json(ep_root / "tts-state.json", {}) or {}
    tts_run = ts if ts.get("pushed") and not ts.get("pulled") and not ts.get("abandoned") else None
    keys = bool(deps.gemini_keys())
    rows = []
    for c in sel:
        text_ok = bool(c.source) and (ep_root / "chapters" / c.source).exists()
        if not text_ok and not c.source:
            text_ok = any((ep_root / "chapters").glob(f"{c.idx:04d}_*.txt"))
        rows.append(_chapter_row(ep_root, ep, c, lock_doc, roster, plan_ids, journal, deps, tts_run, keys, text_ok))
    whole = chapters is None
    info = episode_lines(ep_root, ep, start, end, rows if whole else [], deps) if whole else None
    result: dict = {"book": book, "episode": ep, "start": start, "end": end, "scope": None if whole else ranges(chapters),
                    "chapters": rows}
    if whole:
        result["info"] = info
        result["jobs"] = jobs_info(ep_root, deps)
        result["gemini"] = gemini_batches(book, [c.idx for c in all_ch])
        result["chrome"] = chrome_info(deps)
        st = _read_json(ep_root / "flow" / "status.json")
        result["flow_status"] = st if isinstance(st, dict) else None
    if whole and info["published"]:
        items, notes = [], ["published: the gaps in the matrix are history (migrated episodes keep no chapter videos); "
                            "nothing is left to do"]
        if not info["joined"]["exists"]:
            notes.append(f"the final video {info['joined']['name']} is not in this folder")
    else:
        stub = info or {"joined": {"current": True, "exists": True, "reason": None}, "cards": "ready",
                        "publish": {"state": "valid", "errors": 0}, "thumbnails": ["x"]}
        items, notes = missing_list(rows, stub, ep, book, state[0], state[1], deps, episode_level=whole, tts_run=tts_run)
    result["missing"], result["notes"] = items, notes
    verdict, blockers = verdict_of(result)
    result["verdict"], result["blockers"] = verdict, blockers
    scope = f" for chapters {result['scope']}" if result["scope"] else ""
    result["verdict_text"] = (f"NOT READY ({_plural(blockers, 'blocker')}){scope}" if verdict == "NOT READY"
                              else verdict + scope)
    result["exit_code"] = 0 if verdict in ("READY TO BUILD", "READY TO PUBLISH", "PUBLISHED") else 1
    return result


def verdict_of(res: dict) -> tuple[str, int]:
    items = res["missing"]
    info = res.get("info")
    if info and info["published"]:
        return "PUBLISHED", 0
    pre = [i for i in items if i["stage"] in PRE_BUILD]
    if pre:
        return "NOT READY", len(pre)
    if not items:
        return ("READY TO PUBLISH" if info else "READY TO BUILD"), 0
    if any(i["stage"] in BUILD for i in items):
        return "READY TO BUILD", 0
    return "NOT READY", len(items)                      # built, but publish text or thumbnails are missing


def next_command(res: dict) -> str:
    if res["verdict"] == "PUBLISHED":
        return "-"
    if res["verdict"] == "READY TO PUBLISH":
        return "upload it, then: novel published --url <URL>"
    return res["missing"][0]["command"] if res["missing"] else "-"


# ------------------------------------------------------------------ all episodes of a book

def list_episodes(book: str) -> list[tuple[int, int | None, int | None]]:
    from novel.core import paths
    out = []
    d = paths.episodes_dir(book)
    if not d.is_dir():
        return out
    for p in d.iterdir():
        m = re.fullmatch(r"E(\d+)", p.name)
        if not (m and p.is_dir()) or int(m.group(1)) >= 900:
            continue
        info = _read_json(p / "episode.json", {}) or {}
        out.append((int(m.group(1)), info.get("start"), info.get("end")))
    return sorted(out, reverse=True)


def collect_all(book: str, deps: Deps, state: tuple | None = None) -> list[dict]:
    from novel.core import paths
    rows = []
    for ep, start, end in list_episodes(book):
        try:
            r = collect(paths.ep_root(book, ep), book, ep, deps, state=state)
            rows.append({"episode": ep, "start": start, "end": end, "verdict": r["verdict_text"],
                         "next": next_command(r), "exit_code": r["exit_code"]})
        except (SystemExit, Exception) as e:
            rows.append({"episode": ep, "start": start, "end": end, "verdict": "UNREADABLE",
                         "next": f"{type(e).__name__}: {str(e)[:60]}", "exit_code": 1})
    return rows


# ------------------------------------------------------------------ the text report

WIDTH = 110


def _wrap(text: str, indent: str, width: int = WIDTH, hang: int = 3) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=" " * (len(indent) + hang),
                         break_long_words=False, break_on_hyphens=False) or [indent]


def _cells(r: dict) -> list[str]:
    scenes = "-" if r["scenes_state"] == "-" else ("BAD" if r["scenes_state"] == "BAD" else str(r["scenes"]))
    if r["chars_needed"] is None:
        chars = "-"
    else:
        chars = f"{r['chars_made']}/{r['chars_needed']}"
    if r["images_expected"] is None:
        images = "-"
    else:
        images = f"{r['images_ok']}/{r['images_expected']}"
        if r["needs_check"]:
            images += f" chk{len(r['needs_check'])}"
        if r["refused"]:
            images += f" ref{len(r['refused'])}"
    audio = r["audio"] if r["audio"] != "RUNNING" else "RUNNING"
    video = r["video"] if len(r["video"]) <= 22 else r["video"][:21] + "~"
    return [str(r["chapter"]), "yes" if r["text"] else "-", "yes" if r["hindi"] else "-", r["lock"], r["qa"], scenes, chars,
            images, audio, r["capt"], video]


def _legend(rows: list[dict]) -> list[str]:
    """The one-line key under the table: only the entries whose values occur in it."""
    out = []
    if any(r["needs_check"] for r in rows):
        out.append("chkN = N pictures need a manual look in Flow")
    if any(r["refused"] for r in rows):
        out.append("refN = N pictures Flow refused")
    if any(r["qa"] == "old" for r in rows):
        out.append("qa old = checked by an older version of the tool (a migrated episode): that check does not "
                   "count, run novel qa to check it again")
    out.append("chars = Flow Characters made/needed")
    return out


def format_text(res: dict) -> str:
    lines = []
    head = f"[{res['book']} - E{res['episode']:03d}" + (f" - ch {res['start']}-{res['end']}" if res["start"] is not None else "") + "]"
    lines.append(head + (f"  (chapters {res['scope']} only)" if res["scope"] else ""))
    cols = ["ch", "text", "hindi", "lock", "qa", "scenes", "chars", "images", "audio", "capt", "video"]
    table = [cols] + [_cells(r) for r in res["chapters"]]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    for row in table:
        lines.append("  ".join(c.ljust(w) if i else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths))).rstrip())
    lines.append("  " + "; ".join(_legend(res["chapters"])))
    info = res.get("info")
    if info:
        lines.append("")
        j = info["joined"]
        if j["exists"]:
            dur = f", {j['seconds'] / 60:.1f} min" if j["seconds"] else ""
            lines.append(f"  episode video : {j['name']}{dur} - " + ("current" if j["current"] else f"OUT OF DATE ({j['reason']})"))
        else:
            lines.append(f"  episode video : {j['name']} not built")
        if info["partial_videos"]:
            lines.append(f"  partial videos: {', '.join(info['partial_videos'])}")
        lines.append(f"  timestamps    : {', '.join(info['timestamps']) or 'none'}")
        lines.append(f"  subscribe cards: {info['cards']}")
        p = info["publish"]
        lines.append(f"  publish text  : {p['state']}" + (f" ({_plural(p['errors'], 'error')})" if p["errors"] else ""))
        lines.append(f"  thumbnails    : {', '.join(info['thumbnails']) or 'none'}")
        lines.append(("  published     : yes " + str(info["published_url"] or "")).rstrip() if info["published"]
                     else "  published     : no")
        for j2 in res["jobs"]:
            lines.append(f"  job {j2['name']}: {j2['state']}" + (f" since {j2['since']}" if j2["since"] else "")
                         + (f", log {j2['log']}" if j2.get("log") else "") + (f" ({j2['note']})" if j2.get("note") else ""))
        for b in res["gemini"]:
            lines.append(f"  gemini batch {b['batch']} ({b['mode']}, ch {b['chapters']}): {b['progress']}")
        fs = res.get("flow_status")
        if fs:
            lines.append(f"  last image run: saved {len(fs.get('saved') or [])}, left {fs.get('left')}"
                         + (f", stopped: {fs['stopped']}" if fs.get("stopped") else "") + f" ({fs.get('at', '')[:16]})")
        for c in res["chrome"]:
            lines.append(f"  {c}")
    for n in res["notes"]:
        lines += _wrap(n, "  note: ", hang=0)
    lines += ["", f"VERDICT: {res['verdict_text']}"]
    if res["missing"]:
        lines += ["", "Missing, in the order to run it:"]
        for i, m in enumerate(res["missing"], 1):
            tag = "" if m["kind"] == "todo" else (" [waiting]" if m["kind"] == "waiting" else " [after the build]")
            lines += _wrap(m["line"] + tag, f"{i:>3}. ")
    elif res["verdict"] == "READY TO PUBLISH":
        lines += ["", f"Next: {next_command(res)}"]
    return "\n".join(lines)


def format_all(book: str, rows: list[dict]) -> str:
    lines = [f"[{book}] episodes, newest first"]
    for r in rows:
        rng = f"{r['start']}-{r['end']}" if r["start"] is not None else "?"
        head = f"E{r['episode']:03d}  ch {rng:<8} {r['verdict']:<24} "
        lines += _wrap(head + r["next"], "", WIDTH, hang=6)
    return "\n".join(lines)
