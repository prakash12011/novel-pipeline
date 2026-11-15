"""`novel qa`: a second look at translations and picture lists before images and TTS are paid for.

Two layers, both written per chapter into <episode>/qa/:

  1. Code checks (cheap, always, from the saved files; never from the inbox). They reuse the validation that
     already exists: `paragraph_problem` and the lock (Hindi), `episode_scenes.check_chapter` and the
     importer's `chapter_warnings` (scene file), `bible.resolve` against the CURRENT bible; and add the checks
     that need the whole chapter: names, length ratio, continuity of groups, people named but not in the cast.
     -> qa/NNNNN.json   {"version": 2, "chapter", "mode", "errors", "warnings", "forced", "checked_at",
                         "hindi_sha256", "scenes_sha256"}
  2. AI review (one call per chapter): Claude CLI (novel.publish.meta.call_claude), Gemini as the fallback.
     -> qa/NNNNN.review.json  {"version": 2, "chapter", "verdict", "reviewer", "model", "findings", ...}

and one report for the run: qa/report.md (the previous one is kept as report.prev.md).
Old-format files (no "version": 2) are moved to qa/legacy/, never deleted.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from novel import bible as B
from novel.gemini import contract as C
from novel.gemini import importer as I
from novel.gemini import prompt as P

VERSION = 2
GROUP_WARN_AT = 25          # a group of this many pictures or more in one place gets a warning
OMISSION_RATIO = 0.45       # default of settings qa.omission_ratio: Hindi / English characters (spaces excluded)
                            # below this: "possible omission" (real chapters sit near 0.5)
PARALLEL = 3                # default of settings qa.parallel: AI reviews running at the same time
MAX_FINDINGS = 20
CLAUDE_TIMEOUT = 420
REVIEW_KINDS = ("missing_plot", "image_mismatch", "continuity", "prop_imprint", "wrong_character", "tone")
TEXT_KINDS = ("missing_plot", "tone")                   # findings about the Hindi, not the pictures
SEVERITIES = ("error", "warn")
REVIEWERS = ("claude", "gemini")
PARA_PROBLEM = {"note": "is a bracketed note, not story text", "digits": "contains digits (numbers are written in "
                "Hindi words)", "english": "has untranslated English or a remark about the text",
                "latin": "contains Latin letters"}
NAME_STOP = {"the", "old", "young", "lady", "lord", "sir", "miss", "mister", "mr", "mrs", "dr", "little", "big",
             "master", "madam", "captain", "father", "mother", "brother", "sister", "uncle", "aunt", "king",
             "queen", "prince", "princess", "saint", "grand", "mad", "and", "of"}
PLACE_STOP = {"the", "of", "and", "house", "room", "hall", "street", "road", "village", "town", "city", "old",
              "great", "grand", "inner", "outer", "north", "south", "east", "west", "main", "upper", "lower",
              "small", "large", "new", "gate", "square", "yard", "front", "back", "side", "area", "place",
              "interior", "exterior", "outside", "inside"}


# ------------------------------------------------------------------ small helpers

def _setting_number(book: str, key: str, default, cast):
    from novel.core import settings
    try:
        v = cast(settings.get(key, book=book, default=default))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def omission_ratio(book: str) -> float:
    """settings qa.omission_ratio (default OMISSION_RATIO)."""
    return _setting_number(book, "qa.omission_ratio", OMISSION_RATIO, float)


def parallel_reviews(book: str) -> int:
    """settings qa.parallel (default PARALLEL)."""
    return _setting_number(book, "qa.parallel", PARALLEL, int)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, doc) -> None:
    I.write_atomic(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")


def sha_file(path: Path) -> str | None:
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def qa_dir(ep_root: Path) -> Path:
    return Path(ep_root) / "qa"


def code_path(ep_root: Path, n: int) -> Path:
    return qa_dir(ep_root) / f"{n:05d}.json"


def review_path(ep_root: Path, n: int) -> Path:
    return qa_dir(ep_root) / f"{n:05d}.review.json"


def is_v2(doc) -> bool:
    return isinstance(doc, dict) and doc.get("version") == VERSION


def item(kind: str, ref: str, text: str) -> dict:
    return {"kind": kind, "ref": ref, "text": text}


def ranges_of(spec: str) -> list[int]:
    """'3, 5-7' -> [3, 5, 6, 7]."""
    out: list[int] = []
    for part in re.split(r"\s*[,&]\s*", spec.strip()):
        m = re.fullmatch(r"(\d+)(?:\s*[-–]\s*(\d+))?", part)
        if m:
            a = int(m.group(1))
            b = int(m.group(2) or a)
            out += list(range(a, max(a, b) + 1))
    return out


_NUMS = r"(\d+(?:\s*[-–]\s*\d+)?(?:\s*[,&]\s*\d+(?:\s*[-–]\s*\d+)?)*)"
IMG_REF = re.compile(r"(?i)\b(?:images?|imgs?|pictures?|scenes?)\s+" + _NUMS)
PARA_REF = re.compile(r"(?i)\b(?:paras?|paragraphs?)\s+" + _NUMS)
GROUP_REF = re.compile(r"(?i)\bgroups?\s+([A-Za-z0-9_]+)")


def refs_of(ref: str) -> tuple[list[int], list[int], list[str]]:
    """(picture numbers, paragraph numbers, group ids) named by a finding's ref."""
    imgs = [n for m in IMG_REF.finditer(ref or "") for n in ranges_of(m.group(1))]
    paras = [n for m in PARA_REF.finditer(ref or "") for n in ranges_of(m.group(1))]
    groups = [m.group(1) for m in GROUP_REF.finditer(ref or "")]
    return imgs, paras, groups


# ------------------------------------------------------------------ the run's context

class Run:
    """The book, its CURRENT bible and episode folders for one `novel qa` run."""

    def __init__(self, book: str, ep: int | None):
        from novel.core import paths
        self.book = book
        self.book_dir = paths.book_dir(book)
        self.imp = I.Importer(book, out=lambda *a, **k: None)
        self.state = self.imp.state
        self.eps = self.imp.episodes()
        self.run_ep = paths.ep_root(book, ep) if ep is not None else None
        self.S = self.imp.S

    def ep_root_for(self, n: int) -> Path | None:
        if self.run_ep is not None and (self.run_ep / "episode.json").exists():
            info = read_json(self.run_ep / "episode.json") or {}
            try:
                if int(info["start"]) <= n <= int(info["end"]):
                    return self.run_ep
            except (KeyError, ValueError, TypeError):
                pass
        return self.eps.get(n)


@dataclass
class Result:
    n: int
    ep_root: Path | None = None
    mode: str = "scenes"
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    info: dict = field(default_factory=dict)
    hindi_sha: str | None = None
    scenes_sha: str | None = None
    doc: dict | None = None             # the scene file when it is a usable v2
    paras: list = field(default_factory=list)
    reviewable: bool = False
    # AI review
    review: dict | None = None          # the saved review (current hashes), if any
    review_state: str = "none"          # none | skipped | ok | failed | stale | impossible
    review_notes: list = field(default_factory=list)
    review_chars: int = 0
    review_seconds: float = 0.0

    def err(self, kind: str, ref: str, text: str) -> None:
        self.errors.append(item(kind, ref, text))

    def warn(self, kind: str, ref: str, text: str) -> None:
        self.warnings.append(item(kind, ref, text))

    @property
    def code_status(self) -> str:
        return "FIX" if self.errors else "PASS"

    def review_counts(self) -> tuple[int, int]:
        f = (self.review or {}).get("findings") or []
        return (sum(1 for x in f if x.get("severity") == "error"), sum(1 for x in f if x.get("severity") == "warn"))

    @property
    def overall(self) -> str:
        if self.errors or (self.review and self.review.get("verdict") == "FIX"):
            return "FIX"
        if self.review_state == "failed":
            return "review failed"
        if self.review is None:
            return "not reviewed"
        return "PASS"


# ------------------------------------------------------------------ code checks

@contextlib.contextmanager
def assumed_lock(ep_root: Path, n: int):
    """Run episode_scenes.check_chapter on a chapter whose Hindi is not locked yet (full mode): the lock is
    taken to be the file's own hash for the duration of the call. Nothing is written to the episode."""
    from novel.stages import episode as E
    hf = Path(ep_root) / "hindi" / f"{n:05d}.txt"
    with tempfile.TemporaryDirectory() as tmp:
        lockf = Path(tmp) / "lock.json"
        lockf.write_text(json.dumps({f"{n:05d}.txt": E.sha256(hf)}), encoding="utf-8")
        old = (E.check_lock, E.lock_path)
        E.check_lock = lambda *a, **k: None
        E.lock_path = lambda ep: lockf
        try:
            yield
        finally:
            E.check_lock, E.lock_path = old


def english_text(run: Run, ep_root: Path, n: int) -> str | None:
    for folder in (Path(ep_root) / "chapters", run.book_dir / "chapters"):
        found = sorted(folder.glob(f"{n:04d}_*.txt")) if folder.is_dir() else []
        if found:
            return found[0].read_text(encoding="utf-8", errors="replace")
    return None


def _scene_problem_ref(msg: str) -> tuple[str, str]:
    m = re.match(r"^scene (\d+): (.*)$", msg)
    return (f"image {m.group(1)}", m.group(2)) if m else ("chapter", msg)


def classify_importer_warning(msg: str) -> tuple[str, str]:
    """(kind, ref) of a message from contract.chapter_warnings."""
    nums = re.search(r"\(([\d, .]+)\)", msg)
    if "is in the cast but not in group" in msg:
        return "cast_not_present", "image " + re.match(r"image (\d+)", msg).group(1)
    if "make a variant" in msg:
        return "wearing", f"images {nums.group(1).strip()}" if nums else "chapter"
    if "in a row" in msg:
        m = re.search(r"ending at image (\d+)", msg)
        return "shots", f"image {m.group(1)}" if m else "chapter"
    if "'medium' on" in msg:
        m = re.match(r"group (\S+):", msg)
        return "shots", f"group {m.group(1)}" if m else "chapter"
    if "involved but never drawn" in msg:
        m = re.match(r"group (\S+?):", msg)
        return "present_unused", f"group {m.group(1)}" if m else "chapter"
    if "signature prop" in msg:
        return "props", f"images {nums.group(1).strip()}" if nums else "chapter"
    m = re.match(r"(?:note: )?group (\S+?):? ", msg)
    if m and ("where is '" in msg or "the light mentions" in msg or "the only light outdoors" in msg
              or "follows group" in msg):
        return "setting", f"group {m.group(1)}"
    return "scene_warning", "chapter"


def name_forms(display: str, token_count: dict) -> list[str]:
    forms = [display]
    for tok in re.findall(r"[A-Za-z][A-Za-z'\-]+", display or ""):
        if len(tok) >= 3 and tok.lower() not in NAME_STOP and token_count.get(tok, 0) == 1:
            forms.append(tok)
    return forms


def place_words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (name or "").lower()) if len(w) >= 4 and w not in PLACE_STOP}


def _norm(text) -> str:
    return " ".join(str(text or "").lower().split())


def code_check(run: Run, n: int) -> Result:
    from novel.stages import episode_translate as T
    S = run.S
    ep_root = run.ep_root_for(n)
    r = Result(n, ep_root)
    if ep_root is None:
        r.err("episode", "chapter", f"chapter {n} belongs to no episode (no episodes/E*/episode.json covers it)")
        return r
    bt = run.imp.batch_for_chapter(n)
    state = P.hindi_state(ep_root, n)
    r.mode = bt.mode if bt is not None else ("scenes" if state == "locked" else "full")

    # ---- the Hindi file
    hf = ep_root / "hindi" / f"{n:05d}.txt"
    english = english_text(run, ep_root, n)
    if not hf.is_file():
        r.err("hindi_missing", "chapter", f"hindi/{n:05d}.txt does not exist")
    else:
        r.hindi_sha = sha_file(hf)
        if state == "stale":
            r.err("lock", "chapter", f"hindi/{n:05d}.txt changed after it was locked (hindi.lock.json no longer "
                                     f"matches); restore the locked text")
        elif state == "unlocked":
            msg = f"hindi/{n:05d}.txt is not locked yet (novel approve locks it)"
            (r.err if r.mode == "scenes" else r.warn)("lock", "chapter", msg)
        r.paras = S.chapter_paras(ep_root, n)
        for i, para in enumerate(r.paras):
            why = T.paragraph_problem(para)
            if why:
                r.err("hindi_paragraph", f"para {i}", f"{PARA_PROBLEM.get(why, why)}: {C.excerpt(para, width=70)}")
        names = B.load_names(run.book_dir / "bible")
        hindi_all = "\n".join(r.paras)
        if english:
            missing = []
            for en, hi in names.items():
                if (isinstance(en, str) and isinstance(hi, str) and
                        re.search(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])", english, re.I)
                        and hi not in hindi_all):
                    missing.append((en, hi))
            for en, hi in missing:
                r.warn("name_missing", "chapter",
                       f"'{en}' is in the English chapter but its Devanagari form '{hi}' never occurs in the Hindi")
            e_len = len(re.sub(r"\s", "", english))
            h_len = len(re.sub(r"\s", "", hindi_all))
            ratio = h_len / e_len if e_len else 1.0
            r.info["ratio"] = round(ratio, 2)
            limit = omission_ratio(run.book)
            if ratio < limit:
                r.warn("omission", "chapter", f"possible omission: the Hindi is {ratio:.2f} of the English length "
                                              f"(under {limit:g}); information only")
        else:
            r.warn("english_missing", "chapter", "no English chapter file found; names and length not checked")

    # ---- the scene file
    sp = S.scenes_path(ep_root, n)
    if not sp.is_file():
        r.err("scenes_missing", "chapter", f"no scene file {sp} (novel import fills it)")
        return r
    r.scenes_sha = sha_file(sp)
    doc = read_json(sp)
    if not isinstance(doc, dict):
        r.err("scenes_invalid", "chapter", f"{sp} is not valid JSON")
        return r
    if doc.get("version") != 2:
        r.err("scenes_version", "chapter", f"the scene file is version {doc.get('version')!r}, not 2 "
                                           f"(the old format); import the chapter again with novel import --replace")
        return r
    r.doc = doc
    scenes = doc.get("scenes") or []

    if hf.is_file() and state != "stale":
        try:
            if state == "locked":
                problems = S.check_chapter(ep_root, n)
            else:
                with assumed_lock(ep_root, n):
                    problems = S.check_chapter(ep_root, n)
        except SystemExit as e:
            problems = [str(e.code)]
        except (OSError, ValueError, KeyError, TypeError) as e:
            problems = [f"cannot check the scene file ({type(e).__name__}: {e})"]
        for p_ in problems:
            ref, text = _scene_problem_ref(p_)
            r.err("hash" if "different Hindi" in p_ else "scene_check", ref, text)
    try:
        wins = S.chapter_windows(r.paras, doc.get("pacing")) if r.paras else []
    except SystemExit:
        wins = []
    r.reviewable = bool(r.paras) and len(scenes) == len(wins) and not any(e["kind"] in ("hash", "scenes_missing")
                                                                           for e in r.errors)

    # ---- the bible as it is now
    chars, locs = run.state["characters"], run.state["locations"].get("locations") or {}
    groups_doc = doc.get("groups") or {}
    for gid, g in groups_doc.items():
        if g.get("loc") not in locs:
            r.err("bible_location", f"group {gid}", f"the location '{g.get('loc')}' is no longer in the "
                                                    f"location registry")
        else:
            cover = locs[g["loc"]].get("chapters")
            try:
                if cover and not B.chapters_cover(cover, n):
                    r.warn("bible_location", f"group {gid}", f"location '{g['loc']}' covers chapters {cover}, "
                                                             f"not chapter {n}")
            except B.BibleError:
                pass
        for key in g.get("present") or []:
            if key not in (chars.get("characters") or {}):
                r.err("bible_character", f"group {gid}", f"present: character '{key}' is no longer in the bible")
    for s in scenes:
        for c in s.get("cast") or []:
            try:
                B.resolve(chars, c.get("key"), c.get("var"), n)
            except B.BibleError as e:
                r.err("bible_variant", f"image {s.get('id')}", str(e))

    # ---- warnings of the importer (cast outside present, wearing habit, shot variety, signature props)
    try:
        gl, il = run.imp.doc_lines(doc)
        for gid, g in gl.items():                       # an older scene file has no `where`: the location's kind
            if not g.get("where"):
                by_kind = C.kind_where(C.ChapterCtx(n, 0, run.state), g.get("loc"))
                if by_kind:
                    g["where"] = by_kind
                r.warn("setting", f"group {gid}", f"group {gid} has no `where` (indoor or outdoor)"
                       + (f"; the location's kind says {by_kind}" if by_kind else "; the location's kind does not say"))
        cctx = C.ChapterCtx(n, len(scenes), run.state, run.imp.max_characters, S.BANNED)
        for msg in C.chapter_warnings(gl, il, cctx):
            kind, ref = classify_importer_warning(msg)
            r.warn(kind, ref, msg)
    except (KeyError, TypeError, ValueError) as e:
        r.warn("scene_warning", "chapter", f"importer warnings not computed ({type(e).__name__}: {e})")

    continuity_checks(run, r, doc, n)
    return r


def continuity_checks(run: Run, r: Result, doc: dict, n: int) -> None:
    """Structural continuity, people named but not in the cast, and group sizes."""
    scenes = sorted(doc.get("scenes") or [], key=lambda s: s.get("id", 0))
    groups = doc.get("groups") or {}
    locs = run.state["locations"].get("locations") or {}
    chars = run.state["characters"].get("characters") or {}

    # pictures per chapter and per group (information; a very large group is a warning)
    per_group: dict[str, list[int]] = {}
    for s in scenes:
        per_group.setdefault(s.get("group"), []).append(s.get("id"))
    r.info["pictures"] = len(scenes)
    r.info["groups"] = len(per_group)
    if per_group:
        big = max(per_group, key=lambda g: len(per_group[g]))
        r.info["largest_group"] = (big, len(per_group[big]))
    for gid, ids in per_group.items():
        if len(ids) >= GROUP_WARN_AT:
            r.warn("group_size", f"group {gid}", f"{len(ids)} pictures in one place (warning from {GROUP_WARN_AT}): "
                                                 f"worth a human look that nothing is repeated")

    # runs of consecutive pictures in the same group
    runs: list[list] = []                    # [gid, first n, last n, count]
    for s in scenes:
        if runs and runs[-1][0] == s.get("group"):
            runs[-1][2] = s.get("id")
            runs[-1][3] += 1
        else:
            runs.append([s.get("group"), s.get("id"), s.get("id"), 1])
    for a, b in zip(runs, runs[1:]):
        ga, gb = groups.get(a[0]) or {}, groups.get(b[0]) or {}
        if (ga.get("loc") and ga.get("loc") == gb.get("loc") and ga.get("time") == gb.get("time")
                and _norm(ga.get("light")) != _norm(gb.get("light"))):
            r.warn("continuity", f"group {b[0]}",
                   f"group {b[0]} follows group {a[0]} in the same place ({ga.get('loc')}) at the same time "
                   f"({ga.get('time')}) but with a different light ('{ga.get('light')}' then '{gb.get('light')}')")
    for a, mid, c in zip(runs, runs[1:], runs[2:]):
        if a[0] == c[0] and mid[3] <= 2:
            span = f"{mid[1]}" if mid[1] == mid[2] else f"{mid[1]}-{mid[2]}"
            r.warn("continuity", f"image {span}",
                   f"group {a[0]} is interrupted by {plural(mid[3], 'picture')} of group {mid[0]} and resumes at "
                   f"image {c[1]}: check that the place really changes")

    # an action that names another place
    words = {k: place_words(v.get("name")) for k, v in locs.items()}
    for s in scenes:
        g = groups.get(s.get("group")) or {}
        own = g.get("loc")
        own_text = _norm((locs.get(own) or {}).get("name")) + " " + _norm((locs.get(own) or {}).get("block"))
        act = s.get("action") or ""
        for key, ws in words.items():
            if key == own:
                continue
            for w in sorted(ws):
                if re.search(r"(?<![a-z])" + re.escape(w) + r"s?(?![a-z])", act.lower()) and \
                        not re.search(r"(?<![a-z])" + re.escape(w), own_text):
                    r.warn("continuity", f"image {s.get('id')}",
                           f"the action names '{w}' ({locs[key].get('name')}), but the group's place is "
                           f"{(locs.get(own) or {}).get('name') or own}")
                    break
            else:
                continue
            break

    # people named in the action but not in the cast
    token_count: dict[str, int] = {}
    for ch in chars.values():
        for tok in set(re.findall(r"[A-Za-z][A-Za-z'\-]+", ch.get("display") or "")):
            token_count[tok] = token_count.get(tok, 0) + 1
    forms = {k: name_forms(ch.get("display"), token_count) for k, ch in chars.items() if ch.get("display")}
    for s in scenes:
        cast_keys = {c.get("key") for c in s.get("cast") or []}
        act = s.get("action") or ""
        for key, fs in forms.items():
            if key in cast_keys:
                continue
            hit = next((f for f in fs if re.search(r"(?<![A-Za-z])" + re.escape(f) + r"(?![A-Za-z])", act)), None)
            if hit:
                r.warn("people", f"image {s.get('id')}",
                       f"{chars[key].get('display')} is named in the action ('{hit}') but is not in the cast")


# ------------------------------------------------------------------ saved files

def archive_legacy(ep_root: Path, n: int) -> list[Path]:
    """Move old-format QA files of chapter n into qa/legacy/ (never delete). Returns the moved files."""
    moved = []
    q = qa_dir(ep_root)
    for name, always in ((f"{n:05d}.json", False), (f"{n:05d}.review.json", False), (f"{n:05d}.attach.json", True)):
        f = q / name
        if not f.is_file():
            continue
        if not always and is_v2(read_json(f)):
            continue
        dest = q / "legacy" / name
        if dest.exists():
            dest = q / "legacy" / f"{f.stem}.{utc_stamp()}{f.suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
        moved.append(dest)
    return moved


def save_code(r: Result) -> None:
    f = code_path(r.ep_root, r.n)
    old = read_json(f) if f.is_file() else None
    forced = bool(is_v2(old) and old.get("forced") and old.get("hindi_sha256") == r.hindi_sha
                  and old.get("scenes_sha256") == r.scenes_sha)
    write_json(f, {"version": VERSION, "chapter": r.n, "mode": r.mode, "errors": r.errors, "warnings": r.warnings,
                   "forced": forced, "checked_at": utc_iso(), "hindi_sha256": r.hindi_sha,
                   "scenes_sha256": r.scenes_sha})


def saved_review(r: Result) -> dict | None:
    """The review on file when it is v2 and made from the current Hindi and scene file."""
    f = review_path(r.ep_root, r.n)
    doc = read_json(f) if f.is_file() else None
    if is_v2(doc) and doc.get("hindi_sha256") == r.hindi_sha and doc.get("scenes_sha256") == r.scenes_sha:
        return doc
    return None


# ------------------------------------------------------------------ AI review: the prompt

REVIEW_RULES = """You are the second reader for a Hindi/Hinglish "novel explained" video. The video narrates ONE chapter in Hindi while a numbered list of pictures is shown; each picture stays on screen while its window of narration is read. A first AI wrote the Hindi and the picture list. You compare them with the English chapter and report real problems. You answer with JSON only.

WHAT YOU ARE GIVEN
1. CHAPTER: the English source.
2. NARRATION: the Hindi cut into picture windows. "[img 12]" starts the window of picture 12; "[para 7]" is the number of the Hindi paragraph.
3. PICTURES: the groups (one continuous moment in one place: place, time, light, who is present, staging) and, for each picture, its group, shot, the people in it (display name, outfit variant, what their hands hold) and a description of what is visible.
4. BIBLE: the characters and places of this chapter: look, signature props, outfit variants with the chapters they cover, and the fixed description of each place.

WHAT TO LOOK FOR (the "kind" of a finding)
- missing_plot: an important event, reveal, clue, decision or line of dialogue in the English that the Hindi leaves out; or an action scene that is summarised instead of told move by move. Ignore dropped filler, description and repetition: the Hindi is deliberately shorter.
- image_mismatch: a picture that shows something its window of narration does not tell, or the wrong moment of the story.
- wrong_character: a person in a picture's cast who is not present at that moment; a person who is present and visible but missing from the cast; or a wrong outfit variant for what the text says they wear or look like. Everyone the narration of that window shows acting or speaking must be in the cast.
- continuity: inside one group, a change of place, time of day or light that the text does not support; or a place that changes between two pictures without a group boundary. A picture's action must also not contradict its group's indoor/outdoor ("where") and time of day: for example people in the open sky at night under "indoor", a window with daylight in a "night" group, or a "day" group whose action lights lamps for the sky.
- prop_imprint: a person holding their signature prop (see "props" in the bible) when the text of that moment does not have them using it. Habit, not story.
- tone: Hindi that is stiff, bookish or literal translation instead of natural spoken Hinglish. Severity is always warn.

SEVERITY
- "error": a picture or passage that is wrong and should be redone before pictures are paid for.
- "warn": worth a look, but acceptable as it is.
Do not report matters of taste, small differences of wording, or pictures that simply show less than the window says. A picture shows one visible moment; thoughts and backstory in the narration need no picture. Be concrete and be right: no guesses.

{mode_note}

ANSWER FORMAT: ONE JSON object, nothing before or after it:
{{"verdict": "PASS" or "FIX",
 "findings": [{{"severity": "error" or "warn", "kind": "missing_plot|image_mismatch|continuity|prop_imprint|wrong_character|tone", "ref": "para 14-16" or "image 22" or "image 22-24" or "group g02", "text": "what is wrong, one or two sentences", "suggest": "one concrete line: what to change"}}]}}
Give at most {max_findings} findings, the most serious first. If nothing is wrong return {{"verdict": "PASS", "findings": []}}. The verdict is FIX when any finding has severity "error", else PASS. Refer to pictures by their number [img n] and to paragraphs by [para p]."""

MODE_NOTE = {
    "scenes": ("MODE: the Hindi is already final and locked; it cannot be changed. Still report missing_plot and "
               "tone, but always with severity \"warn\" (the user decides what to do). Pictures can be redone."),
    "full": ("MODE: the Hindi is a draft and can still be rewritten. A missing important event or an action scene "
             "that is summarised is an \"error\"; tone is always \"warn\"."),
}


def _variant_label(chars: dict, key: str, var: str) -> str:
    v = ((chars.get(key) or {}).get("variants") or {}).get(var) or {}
    return v.get("label") or var


def build_review_prompt(run: Run, r: Result, english: str) -> tuple[str, str]:
    """(system text, user text) for one chapter. The model sees only what it needs to judge."""
    S = run.S
    doc = r.doc
    chars = run.state["characters"].get("characters") or {}
    locs = run.state["locations"].get("locations") or {}
    groups = doc.get("groups") or {}
    scenes = sorted(doc.get("scenes") or [], key=lambda s: s.get("id", 0))
    wins = S.chapter_windows(r.paras, doc.get("pacing"))

    narr = []
    for k, w in enumerate(wins, 1):
        narr.append(f"[img {k}]")
        narr += [f"  [para {i}] {t}" for i, t in S.window_text(r.paras, w)]
    narration = f"[para 0] (title) {r.paras[0]}\n" + "\n".join(narr)

    order: list[str] = []
    for s in scenes:
        if s.get("group") not in order:
            order.append(s.get("group"))
    pics = []
    for gid in order:
        g = groups.get(gid) or {}
        place = (locs.get(g.get("loc")) or {}).get("name") or g.get("loc")
        present = ", ".join((chars.get(k) or {}).get("display") or k for k in g.get("present") or []) or "nobody named"
        pics.append(f"GROUP {gid}: place {place}; {g.get('where') or 'indoor/outdoor not given'}; time {g.get('time')}; light {g.get('light')}; weather "
                    f"{g.get('weather') or '-'}; present: {present}; staging: {g.get('staging')}")
        for s in scenes:
            if s.get("group") != gid:
                continue
            people = []
            for c in s.get("cast") or []:
                disp = (chars.get(c.get("key")) or {}).get("display") or c.get("key")
                bits = []
                if c.get("position"):
                    bits.append(f"position: {c.get('position')}")
                if c.get("var") and c.get("var") != "base":
                    bits.append(f"variant {_variant_label(chars, c.get('key'), c.get('var'))}")
                bits.append(f"hands: {c.get('hands')}")
                if c.get("wearing"):
                    bits.append(f"wearing: {c.get('wearing')}")
                people.append(f"{disp} ({'; '.join(bits)})")
            others = f" | others: {s.get('others')}" if s.get("others") else ""
            pics.append(f"  [img {s.get('id')}] {s.get('shot')} | " + (" + ".join(people) or "no named person")
                        + f"{others} | {s.get('action')}")

    keys = []
    for s in scenes:
        for c in s.get("cast") or []:
            if c.get("key") not in keys:
                keys.append(c.get("key"))
    for g in groups.values():
        for k in g.get("present") or []:
            if k not in keys:
                keys.append(k)
    bible = []
    for k in keys:
        ch = chars.get(k)
        if not ch:
            continue
        vs = "; ".join(f"{(v.get('label') or name)} (chapters {v.get('chapters')})"
                       + (f": {v.get('look')}" if v.get("look") else "")
                       for name, v in (ch.get("variants") or {}).items())
        bible.append(f"- {ch.get('display')} [{k}]: {ch.get('look')}\n  signature props (held only when the "
                     f"moment needs them): {', '.join(ch.get('props') or []) or 'none'}\n  variants: {vs}")
    lkeys = []
    for g in groups.values():
        if g.get("loc") not in lkeys:
            lkeys.append(g.get("loc"))
    places = [f"- {(locs.get(k) or {}).get('name') or k} [{k}]: {(locs.get(k) or {}).get('block')} "
              f"(usual light: {(locs.get(k) or {}).get('default_light')})" for k in lkeys if k in locs]

    system = REVIEW_RULES.format(mode_note=MODE_NOTE[r.mode], max_findings=MAX_FINDINGS)
    user = (f"CHAPTER {r.n} (English)\n{english.strip()}\n\nNARRATION (Hindi, cut into picture windows)\n{narration}\n\n"
            f"PICTURES ({len(scenes)})\n" + "\n".join(pics) + "\n\nBIBLE: CHARACTERS\n" + "\n".join(bible)
            + "\n\nBIBLE: PLACES\n" + "\n".join(places) + "\n")
    return system, user


# ------------------------------------------------------------------ AI review: calling and checking

class ReviewFailure(Exception):
    pass


def parse_review_json(text: str) -> dict:
    s = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S | re.I)
    if m:
        s = m.group(1).strip()
    if not s.startswith("{"):
        a, b = s.find("{"), s.rfind("}")
        if a < 0 or b <= a:
            raise ReviewFailure("no JSON object in the answer")
        s = s[a:b + 1]
    try:
        obj = json.loads(s)
    except ValueError as e:
        raise ReviewFailure(f"the JSON does not parse: {e}")
    if not isinstance(obj, dict):
        raise ReviewFailure("the JSON is not an object")
    if not isinstance(obj.get("findings"), list):
        raise ReviewFailure('the JSON has no "findings" list')
    return obj


def validate_findings(obj: dict, mode: str) -> tuple[list[dict], list[str]]:
    """(clean findings, notes about what was dropped or changed). Raises ReviewFailure when findings were given
    and every one of them is malformed."""
    raw = obj["findings"]
    good: list[dict] = []
    notes: list[str] = []
    for i, f in enumerate(raw, 1):
        why = None
        if not isinstance(f, dict):
            why = "not an object"
        elif f.get("severity") not in SEVERITIES:
            why = f"severity {f.get('severity')!r} is not error|warn"
        elif f.get("kind") not in REVIEW_KINDS:
            why = f"kind {f.get('kind')!r} is not one of {'|'.join(REVIEW_KINDS)}"
        elif not (isinstance(f.get("ref"), str) and f["ref"].strip()):
            why = "no ref"
        elif not (isinstance(f.get("text"), str) and f["text"].strip()):
            why = "no text"
        elif f.get("suggest") is not None and not isinstance(f.get("suggest"), str):
            why = "suggest is not text"
        if why:
            notes.append(f"dropped finding {i}: {why}")
            continue
        sev = f["severity"]
        if f["kind"] in TEXT_KINDS and (mode == "scenes" or f["kind"] == "tone") and sev == "error":
            sev = "warn"
            notes.append(f"finding {i} ({f['kind']}): severity lowered to warn "
                         f"({'the Hindi is locked' if mode == 'scenes' and f['kind'] != 'tone' else 'tone is warn only'})")
        good.append({"severity": sev, "kind": f["kind"], "ref": " ".join(f["ref"].split()),
                     "text": " ".join(f["text"].split()), "suggest": " ".join((f.get("suggest") or "").split())})
    if raw and not good:
        raise ReviewFailure(f"all {len(raw)} findings are malformed ({notes[0] if notes else ''})")
    good.sort(key=lambda f: f["severity"] != "error")                 # errors first, stable
    if len(good) > MAX_FINDINGS:
        notes.append(f"{len(good) - MAX_FINDINGS} findings beyond the {MAX_FINDINGS} limit were left out")
        good = good[:MAX_FINDINGS]
    return good, notes


def other_engine(primary: str, fallback: str | None) -> str | None:
    if fallback in (None, "", "none"):
        return None
    if fallback in REVIEWERS and fallback != primary:
        return fallback
    return next(e for e in REVIEWERS if e != primary)


def claude_model_name() -> str:
    from novel.publish import meta as M
    try:
        cmd = M.claude_command()
    except M.AIFailure:
        return "claude"
    if "--model" in cmd and cmd.index("--model") + 1 < len(cmd):
        return cmd[cmd.index("--model") + 1]
    return Path(cmd[0]).stem if cmd else "claude"


def ask_engine(engine: str, system: str, user: str, mode: str, model_gemini: str, log: Path) -> tuple[dict, list[str], str]:
    """One engine, up to two attempts (the second tells it what was wrong). Returns (obj, notes, model name);
    raises ReviewFailure("<reason>") when the engine cannot give a usable reply."""
    from novel.publish import meta as M
    errors: list[str] = []
    prompt = user
    for attempt in (1, 2):
        try:
            if engine == "claude":
                text = M.call_claude(system + "\n\n" + prompt, timeout=CLAUDE_TIMEOUT)
            else:
                text = M.call_gemini(system, prompt, model=model_gemini, log=log)
            obj = parse_review_json(text)
            findings, notes = validate_findings(obj, mode)
            obj["findings"] = findings
            return obj, notes, (claude_model_name() if engine == "claude" else model_gemini)
        except M.ClaudeUnavailable as e:
            raise ReviewFailure(str(e))
        except (M.AIFailure, ReviewFailure) as e:
            errors.append(str(e))
            prompt = (user + "\n\nYOUR PREVIOUS ANSWER FAILED THESE CHECKS. Fix every one and return the full "
                             f"JSON again:\n- {errors[-1]}")
    raise ReviewFailure("; ".join(errors))


def review_chapter(run: Run, r: Result, reviewer: str, fallback: str | None, force: bool) -> None:
    """Fill r.review / r.review_state / r.review_notes, and write qa/NNNNN.review.json on success."""
    english = english_text(run, r.ep_root, r.n)
    have = saved_review(r)
    if have is not None and not force:
        r.review, r.review_state = have, "skipped"
        r.review_notes.append("review kept: the Hindi and the scene file are unchanged since it was made "
                              "(--force-review makes a new one)")
        return
    if r.doc is None or not r.reviewable or not english:
        r.review_state = "impossible"
        r.review_notes.append("not reviewed: " + (
            "the English chapter is missing" if r.doc is not None and r.reviewable else
            "the scene file does not match the Hindi (fix the code errors first)"))
        return
    system, user = build_review_prompt(run, r, english)
    r.review_chars = len(system) + len(user)
    from novel.core import settings
    model_gemini = str(settings.get("gemini.api_model", book=run.book, default="gemini-3.5-flash-lite"))
    log = qa_dir(r.ep_root) / "qa-gemini.log.jsonl"
    engines = [reviewer] + ([e] if (e := other_engine(reviewer, fallback)) else [])
    reasons = []
    t0 = time.time()
    for k, engine in enumerate(engines):
        try:
            obj, notes, model = ask_engine(engine, system, user, r.mode, model_gemini, log)
        except ReviewFailure as e:
            reasons.append(f"{engine} failed: {e}")
            continue
        r.review_seconds = time.time() - t0
        if reasons:
            r.review_notes.append(f"fallback: {'; '.join(reasons)}; the answer is from {engine}")
        r.review_notes += notes
        findings = obj["findings"]
        verdict = "FIX" if any(f["severity"] == "error" for f in findings) else "PASS"
        if str(obj.get("verdict", "")).upper() not in ("", verdict):
            r.review_notes.append(f"the reviewer said {obj.get('verdict')}, but its findings make it {verdict}")
        doc = {"version": VERSION, "chapter": r.n, "verdict": verdict, "reviewer": engine, "model": model,
               "findings": findings, "reviewed_at": utc_iso(), "hindi_sha256": r.hindi_sha,
               "scenes_sha256": r.scenes_sha}
        old = review_path(r.ep_root, r.n)
        if old.is_file() and not is_v2(read_json(old)):
            archive_legacy(r.ep_root, r.n)
        write_json(old, doc)
        r.review, r.review_state = doc, "ok"
        return
    r.review_seconds = time.time() - t0
    r.review_state = "failed"
    r.review_notes.append("review failed: " + "; ".join(reasons))
    stale = review_path(r.ep_root, r.n)
    if stale.is_file():
        r.review_notes.append("an older review of different text is on file; it is not used")


# ------------------------------------------------------------------ the report

def table_rows(results: list[Result]) -> list[tuple]:
    rows = []
    for r in results:
        code = f"{r.code_status} ({plural(len(r.errors), 'error')}, {plural(len(r.warnings), 'warning')})"
        if r.review is not None:
            e, w = r.review_counts()
            ai = f"{r.review['verdict']} ({plural(e, 'error')}, {plural(w, 'warning')}; {r.review['reviewer']})"
        elif r.review_state == "failed":
            ai = "review failed"
        else:
            ai = "not reviewed"
        rows.append((str(r.n), str(r.info.get("pictures", "-")), code, ai, r.overall))
    return rows


def table_text(results: list[Result]) -> str:
    head = ("Chapter", "Pictures", "Code checks", "AI review", "Overall")
    lines = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in table_rows(results)]
    return "\n".join(lines)


def chapter_lines(r: Result) -> list[str]:
    """The findings of one chapter as plain sentences, errors first."""
    out = []
    for e in r.errors:
        out.append(f"- [code] {e['ref']}: {e['text']}")
    if r.review:
        fs = r.review.get("findings") or []
        for f in sorted(fs, key=lambda f: f["severity"] != "error"):
            if f["severity"] == "error":
                out.append(f"- [AI error] {f['ref']}: {f['text']}" + (f" Suggest: {f['suggest']}" if f.get("suggest") else ""))
    for w in r.warnings:
        out.append(f"- [code warning] {w['ref']}: {w['text']}")
    if r.review:
        for f in r.review.get("findings") or []:
            if f["severity"] == "warn":
                out.append(f"- [AI warn] {f['ref']}: {f['text']}" + (f" Suggest: {f['suggest']}" if f.get("suggest") else ""))
    return out


def next_commands(results: list[Result]) -> list[str]:
    fix = [r.n for r in results if r.overall == "FIX"]
    failed = [r.n for r in results if r.overall == "review failed"]
    lines = []
    if fix:
        lines.append("Chapters to fix: " + ", ".join(map(str, fix)) + ". "
                     + " ".join(f"novel qa --fix-prompt {n}" for n in fix))
    if failed:
        lines.append("The AI review failed for chapter" + ("s " if len(failed) > 1 else " ")
                     + ", ".join(map(str, failed)) + ": run novel qa --chapters "
                     + ",".join(map(str, failed)) + " again (or --reviewer gemini).")
    if not fix and not failed:
        ns = sorted(r.n for r in results)
        runs, start = [], ns[0]
        for a, b in zip(ns, ns[1:] + [None]):
            if b is None or b != a + 1:
                runs.append(f"{start}-{a}" if start != a else f"{a}")
                start = b
        lines.append("Next: " + "; ".join(f"novel approve --chapters {x}" for x in runs))
        nr = [r.n for r in results if r.overall == "not reviewed"]
        if nr:
            lines.append(("(chapter " + ", ".join(map(str, nr)) + " has" if len(nr) == 1 else
                          "(chapters " + ", ".join(map(str, nr)) + " have") + " no AI review yet)")
    return lines


def render_report(run: Run, results: list[Result], header: str, reviewer_text: str) -> str:
    out = [f"# QA report: {header}", "", f"Run at {utc_iso()}. {reviewer_text}", "", table_text(results), ""]
    for r in results:
        out.append(f"## Chapter {r.n}: {r.overall}")
        bits = []
        if r.info.get("pictures") is not None:
            lg = r.info.get("largest_group")
            bits.append(f"{plural(r.info['pictures'], 'picture')} in {plural(r.info.get('groups', 0), 'group')}"
                        + (f", the largest is {lg[0]} with {lg[1]}" if lg else ""))
        if r.info.get("ratio") is not None:
            bits.append(f"Hindi is {r.info['ratio']} of the English length")
        bits.append(f"mode: {r.mode}")
        out.append("Info: " + "; ".join(bits) + ".")
        for note in r.review_notes:
            out.append(f"Review note: {note}")
        if r.review_chars:
            out.append(f"Review input: {r.review_chars} characters; {r.review_seconds:.0f} s.")
        out.append("")
        lines = chapter_lines(r)
        out += lines or ["No findings."]
        if r.overall == "FIX":
            out += ["", f"Fix: novel qa --fix-prompt {r.n}"]
        out.append("")
    out += ["## Next", ""] + next_commands(results) + [""]
    return "\n".join(out)


def write_report(ep_root: Path, text: str) -> Path:
    d = qa_dir(ep_root)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "report.md"
    if f.exists():
        shutil.copyfile(f, d / "report.prev.md")
    I.write_atomic(f, text)
    return f


# ------------------------------------------------------------------ the run

def run_reviews(run: Run, results: list[Result], reviewer: str, fallback: str | None, force: bool,
                parallel: int, out=print) -> None:
    """AI-review every result that has an episode, `parallel` at a time. Each review is written by itself
    (qa/NNNNN.review.json); one failing does not stop the others; `results` keeps its order."""
    todo = [r for r in results if r.ep_root is not None]
    lock = threading.Lock()

    def say(text: str) -> None:
        with lock:
            out(text)

    def one(r: Result) -> None:
        say(f"AI review of chapter {r.n} ({reviewer}) started ...")
        try:
            review_chapter(run, r, reviewer, fallback, force)
        except Exception as e:      # a bug or a crash in one review must not cancel the others
            r.review_state = "failed"
            r.review_notes.append(f"review failed: {type(e).__name__}: {e}")
        if r.review_chars:
            say(f"AI review of chapter {r.n} ended: input {r.review_chars} characters, "
                f"{r.review_seconds:.0f} s, {r.review_state}")
        else:
            say(f"AI review of chapter {r.n} ended: {r.review_state}")

    workers = max(1, min(parallel, len(todo)))
    if len(todo) > 1:
        say(f"{len(todo)} chapters, {workers} review{'s' if workers != 1 else ''} at a time")
    if workers == 1:
        for r in todo:
            one(r)
        return
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qa-review") as pool:
        for f in [pool.submit(one, r) for r in todo]:
            f.result()


def run_qa(book: str, ep: int | None, chapters: list[int] | None, reviewer: str, fallback: str | None,
           only_code: bool, force_review: bool, out=print,
           parallel: int | None = None) -> tuple[int, list[Result], Path | None]:
    run = Run(book, ep)
    if chapters is None:
        ep_root = run.run_ep
        if ep_root is None or not (ep_root / "episode.json").exists():
            raise SystemExit("no episode: pass --ep N or --chapters A-B")
        info = read_json(ep_root / "episode.json") or {}
        every = list(range(int(info["start"]), int(info["end"]) + 1))
        chapters = [n for n in every if (ep_root / "scenes" / f"{n:05d}.json").is_file()] or every
        skipped = [n for n in every if n not in chapters]
        if skipped:
            out(f"note: no scene file yet for chapters {C.ranges_text(skipped)}; not checked "
                f"(name them with --chapters to check them anyway)")
    if run.run_ep is None:
        first = run.eps.get(chapters[0])
        run.run_ep = first
    results: list[Result] = []
    for n in chapters:
        r = code_check(run, n)
        if r.ep_root is not None:
            archive_legacy(r.ep_root, n)
            qa_dir(r.ep_root).mkdir(parents=True, exist_ok=True)
            save_code(r)
        results.append(r)
    if not only_code and reviewer != "none":
        run_reviews(run, results, reviewer, fallback, force_review,
                    parallel if parallel else parallel_reviews(book), out)
    else:
        for r in results:
            if r.ep_root is not None:
                have = saved_review(r)
                if have is not None:
                    r.review, r.review_state = have, "skipped"
                elif review_path(r.ep_root, r.n).is_file():
                    r.review_notes.append("an older review on file is out of date and is not used")
    if run.run_ep is None:
        run.run_ep = next((r.ep_root for r in results if r.ep_root), None)
    path = None
    if run.run_ep is not None:
        header = f"{book}, " + ("E%03d, " % int(run.run_ep.name[1:]) if run.run_ep.name[1:].isdigit() else "") + \
            f"chapters {C.ranges_text(sorted(r.n for r in results))}"
        rtext = ("AI review: none (code checks only)." if only_code or reviewer == "none"
                 else f"AI review: {reviewer}, fallback {other_engine(reviewer, fallback) or 'none'}.")
        path = write_report(run.run_ep, render_report(run, results, header, rtext))
    bad = any(r.overall in ("FIX", "review failed") for r in results)
    return (1 if bad else 0), results, path
