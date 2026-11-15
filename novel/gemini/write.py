"""`novel write`: the Hindi, the picture lines and the bible by the Gemini API, filed by the real importer.

What it does, per chapter of the current episode that still needs work (up to gemini.chapters_per_run):

  1. Translation, only for a chapter with NO Hindi: one call returns the Hinglish narration as plain text; it is
     checked (paragraph_problem, heading, names, word count against translate.target_words), stored the way `episode_translate draft` stores a
     chapter, and locked with the lock code the other commands use.
  2. Scenes, for a chapter whose Hindi is locked and that has no valid scene file: the batch is made by
     `novel.gemini.prompt.generate` (the same files as `novel prompt --mode scenes`), then the conversation of
     the hand-paste path is replaced by stateless calls: one for the BIBLE block of the range, then one per chapter
     for its IMAGES block. Every reply goes through `novel.gemini.importer.Importer` (archive, staging, progress,
     scene file): the importer's decision (`Importer.decide`) says whether to go on, repeat with a FIX list, or
     ask for the missing windows.

Each call carries the rules (`prompt.build_rules`), the CURRENT bible dump (read again before every call), the
chapter's English text and its windows only, and one REQUEST section naming the block to return.

Concurrency (--parallel N, default 1): the API calls of different chapters run side by side; every import, every
bible read and every progress write is serialised by one lock, and each import validates against the bible as it
is at that moment. Two chapters that add the same location in parallel cannot both win: the second BIBLE block is
rejected by the importer (the key exists), that chapter's IMAGES block is rejected with it, and the chapter is
asked again with a fresh bible dump that already holds the location (the loser is retried).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from novel import bible as B
from novel.gemini import contract as C
from novel.gemini import importer as I
from novel.gemini import prompt as P

SLEEP = time.sleep                  # tests replace these two
CLOCK = time.monotonic
MAX_CONTINUATIONS = 4               # "continue from the last complete paragraph" rounds per translation
MIN_KEPT_RATIO = 0.0                # the length ratio is a warning only (qa.omission_ratio)
HEADING = re.compile(r"^\**\s*चैप्टर\s+[^\n:]{1,40}:\s*\S[^\n]*$")


class RunStop(Exception):
    """Stop the run cleanly (Ctrl+C asked for it, or the quota is gone)."""


class Stop(Exception):
    """A chapter cannot go on (a problem that asking again does not fix); the message says how to fix it."""


# ------------------------------------------------------------------ settings

def load_settings(book: str, ep: int | None, model=None, parallel=None, max_chapters=None) -> SimpleNamespace:
    from novel.core import settings

    def get(key, default, cast):
        try:
            v = settings.get(key, book=book, ep=ep, default=default)
            return cast(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    fb = settings.get("gemini.fallback_models", book=book, ep=ep, default=[]) or []
    if isinstance(fb, str):
        fb = [fb]
    s = SimpleNamespace(
        model=model or str(get("gemini.api_model", "gemini-3.5-flash-lite", str)),
        fallbacks=[str(m) for m in fb if m],
        chapters=int(max_chapters if max_chapters is not None else get("gemini.chapters_per_run", 5, int)),
        parallel=int(parallel if parallel is not None else get("gemini.parallel", 1, int)),
        attempts=get("gemini.max_attempts_per_block", 3, int),
        min_gap=get("gemini.min_seconds_between_calls", 4, float),
        target=get("translate.target_words", 800, int),
        tolerance=get("translate.words_tolerance", 0.20, float),
        t_attempts=get("translate.max_attempts", 4, int))
    for name in ("chapters", "parallel", "attempts", "t_attempts", "target"):
        if getattr(s, name) < 1:
            raise SystemExit(f"novel write: {name} must be 1 or more (settings gemini.* and translate.*)")
    s.low, s.high = P.word_band(s.target, s.tolerance)
    s.min_gap = max(0.0, s.min_gap)
    return s


# ------------------------------------------------------------------ small helpers

def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def kb(n: int) -> str:
    return f"{n:,}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def spans(needing: list[int], locked: set[int]) -> list[tuple[int, int]]:
    """Batches for the chapters that need scenes: runs of consecutive LOCKED chapters (a locked chapter between two
    that need work is included, so one batch covers the range), each holding at least one chapter that needs work."""
    out, run = [], []
    for n in range(min(needing), max(needing) + 1) if needing else []:
        if n in locked:
            run.append(n)
        else:
            if run:
                out.append(run)
            run = []
    if run:
        out.append(run)
    need = set(needing)
    return [(r[0], r[-1]) for r in out if need & set(r)]


def bible_dump(state: dict, with_review: bool) -> str:
    text, _ = P.bible_text(state)
    return text if with_review else text.split("\nTO REVIEW")[0].rstrip("\n") + "\n"


def window_lines(wins: list[str]) -> str:
    return "\n".join(f"[w {k}] {t}" for k, t in enumerate(wins, 1))


# ------------------------------------------------------------------ requests

NO_OTHER_BLOCKS = ("Return only the block(s) named here, each with its BEGIN and END lines, and nothing else: no "
                   "greeting, no notes, no text outside the blocks.")


def images_instruction(n: int, count: int, batch: str) -> str:
    return (
        f"Return the IMAGES block of chapter {n}: one image line for each of its {count} windows (n = 1 to {count}), "
        f"in story order, and a group line before the first image of every group. {NO_OTHER_BLOCKS}\n"
        f"===== BEGIN IMAGES c{n:04d} -> inbox/c{n:04d}.images.jsonl =====\n```jsonl\n<group and image lines>\n```\n"
        f"===== END IMAGES c{n:04d} =====\n"
        f"Every character, outfit variant and location you use must already be in the CURRENT BIBLE above. If one is "
        f"missing and the chapter really needs it, write a BIBLE block with its line(s) BEFORE the IMAGES block "
        f"(BEGIN BIBLE {batch}-c{n:04d}, END BIBLE {batch}-c{n:04d}, same JSON line shapes as in the rules). Never use "
        f"a key that is neither in the bible nor in that block. Every group line has \"where\" (indoor or outdoor), "
        f"\"time\" and a \"present\" list of EVERY named character in that place (list them first; everyone in a "
        f"cast must be in it); every cast member has a \"position\" and no two share one; fewer people is better.")


def images_rest_instruction(n: int, count: int, missing: list[int], groups: dict, batch: str,
                            bad: str = "") -> str:
    """The request for the missing windows only. bad: the raw lines of the last answer that were not valid JSON
    and were dropped (the only part of that answer that is sent again)."""
    glines = "\n".join(C.dump_line(g) for g in groups.values()) or "(none)"
    last_group = list(groups)[-1] if groups else None
    fixes = (f"These lines of your last answer were not valid JSON, so the tool dropped them. Send each one again, "
             f"corrected (one JSON object per line, straight double quotes, the same content):\n{bad}\n"
             if bad.strip() else "")
    return (
        f"Your answer for chapter {n} stopped early. The tool already holds the image lines for windows "
        f"{C.ranges_text([k for k in range(1, count + 1) if k not in set(missing)]) or 'none'} and these group "
        f"lines:\n{glines}\n"
        f"{fixes}"
        f"Return an IMAGES block with ONLY the image lines for the missing windows {C.ranges_text(missing)} "
        f"(n = {', '.join(map(str, missing[:6]))}{', ...' if len(missing) > 6 else ''})"
        f"{' and the corrected lines above' if fixes else ''}, and a group line for every "
        f"NEW group they start (continue the group numbering after the last one above"
        f"{f', {last_group}' if last_group else ''}; reuse an existing group id when the moment goes on in the same "
        f"place). Do not repeat earlier lines. {NO_OTHER_BLOCKS}\n"
        f"===== BEGIN IMAGES c{n:04d} -> inbox/c{n:04d}.images.jsonl =====\n```jsonl\n<lines>\n```\n"
        f"===== END IMAGES c{n:04d} =====")


def retry_instruction(first: str, say: str, previous: str) -> str:
    return (f"{first}\n\nYour previous answer was rejected by the tool that reads it. What it says to you:\n"
            f"{say}\n\nYOUR PREVIOUS ANSWER (rejected):\n{previous.strip()}\n\nReturn the corrected, COMPLETE block "
            f"now, in the same format, fixing every point of the FIX list and changing nothing else.")


def repair_cut(text: str, n: int, more: bool = True) -> str:
    """A reply cut off by the output limit: keep every complete line, close the fence and the block, and (for IMAGES)
    say `>>> MORE`, so the importer files the part and the rest is asked for."""
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and not lines[-1].strip().endswith("}"):
        lines.pop()
    body = "\n".join(lines)
    if body.count("```") % 2:
        body += "\n```"
    body += f"\n===== END IMAGES c{n:04d} =====\n"
    if more:
        body += f">>> MORE c{n:04d} IMAGES\n"
    return body


def map_images_blocks(text: str, n: int, edit) -> str:
    """The reply with the body of every IMAGES block of chapter n replaced by edit(body_lines, quoted) -> lines.
    The BEGIN and END lines, the other blocks and any '>>> MORE' line stay as they are."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = [b for b in C.split_reply(text).blocks if b.kind == "IMAGES" and b.chapter == n]
    for b in sorted(blocks, key=lambda b: b.begin_idx, reverse=True):      # from the end, so the indexes hold
        end = b.end_idx if b.end_idx >= 0 else len(lines)
        more = [l for l in lines[b.begin_idx + 1:end]
                if C.MORE_RX.search(l.translate(C.NBSP).replace("**", "").replace("`", ""))]
        lines[b.begin_idx + 1:end] = list(edit(b.lines, b.quoted)) + more
    return "\n".join(lines)


def prepend_images_lines(text: str, n: int, lines: list[str]) -> str:
    """The reply with `lines` put first in one IMAGES block of chapter n."""
    done: list[bool] = []

    def edit(body: list[str], quoted: bool) -> list[str]:
        if done:
            return body
        done.append(True)
        return lines + body

    return map_images_blocks(text, n, edit)


# ------------------------------------------------------------------ the Hindi: checks

def names_map(state: dict) -> dict:
    names = dict(state.get("names") or {})
    for ch in (state["characters"].get("characters") or {}).values():
        if ch.get("display") and ch.get("hi"):
            names.setdefault(ch["display"], ch["hi"])
    return names


def clean_hindi_reply(text: str) -> list[str]:
    """The paragraphs of a reply: fences and markdown marks removed, white space collapsed; one paragraph per blank
    line (one per line when the reply has no blank lines at all)."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    text = "\n".join(l for l in text.split("\n") if not re.match(r"^\s*`{3,}\s*\w*\s*$", l))
    text = text.replace("**", "").replace("__", "")
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) <= 2 and text.count("\n") >= 3:
        paras = [" ".join(l.split()) for l in text.split("\n") if l.strip()]
    return [p.strip("* ") for p in paras if p.strip("* ")]


@dataclass
class Hindi:
    paras: list
    errors: list = field(default_factory=list)          # block saving
    name_errors: list = field(default_factory=list)     # block saving, except on the last attempt
    warnings: list = field(default_factory=list)
    ratio: float = 0.0                                  # Hindi characters over English characters (information)
    words: int = 0                                      # words of the narration, heading line not counted
    length_problem: str = ""                            # outside the word band: asked again (see closest_attempt)


def validate_hindi(paras: list[str], english: str, names: dict, band: tuple[int, int]) -> Hindi:
    """The checks of the existing translate/draft path and of `novel qa`: paragraph_problem on every paragraph, the
    chapter heading, the names of the NAMES list, and the word count of the narration against the band (low, high)."""
    from novel import qa as Q
    from novel.stages import episode_translate as T
    paras = [T.apply_variants(p) for p in paras]
    h = Hindi(paras)
    if not paras:
        h.errors.append("the reply has no text")
        return h
    if not HEADING.match(paras[0]) or len(paras[0]) > 150:
        h.errors.append("the first line must be the heading 'चैप्टर <number in Hindi words>: <title in Hinglish>' "
                        "and nothing else")
    for i, p in enumerate(paras):
        why = T.paragraph_problem(p)
        if why:
            h.errors.append(f"paragraph {i + 1} {Q.PARA_PROBLEM.get(why, why)}: {C.excerpt(p, width=60)}")
    if len(h.errors) > 8:
        h.errors = h.errors[:8] + [f"and {len(h.errors) - 8} more problems"]
    hindi_all = "\n".join(paras)
    for en, hi in names.items():
        if (isinstance(en, str) and isinstance(hi, str) and hi not in hindi_all
                and re.search(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])", english, re.I)):
            h.name_errors.append(f"the name '{en}' is in the English chapter but '{hi}' never occurs: write it "
                                 f"exactly as '{hi}'")
    e_len = len(re.sub(r"\s", "", english))
    h_len = len(re.sub(r"\s", "", hindi_all))
    h.ratio = h_len / e_len if e_len else 1.0
    h.words = sum(len(p.split()) for p in paras[1:])     # the heading line is not part of the narration
    low, high = band
    if h.words > high:
        h.length_problem = (f"too long: {h.words} words; write between {low} and {high} words. Cut descriptions and "
                            f"side information first; keep the events and the action.")
    elif h.words < low:
        h.length_problem = (f"too short: {h.words} words; write between {low} and {high} words. Put back the events, "
                            f"decisions and dialogue you cut; keep action scenes move by move.")
    return h


def closest_attempt(tries: list[Hindi], target: int, band: tuple[int, int]) -> Hindi | None:
    """When no attempt landed in the band: the one closest to it, but only if it is within 0.5x to 1.5x of the
    target (for 800: 400 to 1200 words). Attempts with a name problem come after the others."""
    low, high = band
    fit = [h for h in tries if target * 0.5 <= h.words <= target * 1.5]
    if not fit:
        return None

    def distance(h: Hindi) -> int:
        return 0 if low <= h.words <= high else min(abs(h.words - low), abs(h.words - high))
    return min(fit, key=lambda h: (bool(h.name_errors), distance(h)))


def translation_request(n: int, english: str, names_text: str) -> str:
    return (f"{names_text}\n\nCHAPTER {n} (English)\n{english.strip()}\n\nREQUEST\nWrite the Hinglish narration of "
            f"chapter {n} now, following every narration rule. Return only the text: the heading line, then the "
            f"paragraphs separated by one blank line.")


def translation_retry(first: str, problems: list[str], previous: str) -> str:
    return (f"{first}\n\nYOUR PREVIOUS ANSWER:\n{previous.strip()}\n\nThe tool that reads it found these problems:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\n\nWrite the whole narration again with every problem fixed. Return only the text.")


def translation_continue(first: str, written: list[str]) -> str:
    return (f"{first}\n\nYOUR ANSWER SO FAR (it was cut off by the length limit; the last paragraph shown is the last "
            f"COMPLETE one):\n" + "\n\n".join(written)
            + "\n\nContinue from the paragraph after the last one above. Return ONLY the remaining paragraphs, in the "
              "same style, one blank line between them; do not repeat anything above and do not write the heading "
              "again.")


# ------------------------------------------------------------------ the run

@dataclass
class Ch:
    """One chapter of the run and what happened to it."""
    n: int
    hindi: str = "existed"        # existed | written | failed | refused
    need_translate: bool = False
    need_lock: bool = False
    need_scenes: bool = False
    redo: bool = False
    bible: str = "-"              # ok | failed | -   (the bible block of the batch)
    images: str = "-"             # "65 of 65"
    status: str = "pending"       # complete | partial | failed | refused | stopped
    calls: int = 0
    models: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    warnings: int = 0
    ratio: float = 0.0            # the Hindi length over the English (same function as the omission ratio)
    note: str = ""


@dataclass
class BatchInfo:
    id: str
    first: int
    last: int
    meta: dict
    review: bool
    wins: dict                    # {chapter: [window texts]}
    english: dict                 # {chapter: text}


@dataclass
class Imported:
    """What one import said (the importer's own objects, read back)."""
    imp: object = None
    bt: object = None
    lines: list = field(default_factory=list)
    decision: object = None
    failure: str | None = None    # the run itself failed (no blocks found, ...)

    def outcomes(self, kind: str, chapter: int | None):
        if self.imp is None:
            return []
        return [o for o in self.imp.outcomes if o.block.kind == kind and o.block.chapter == chapter]


class RunLog:
    """run-<timestamp>.log plus the request and reply bodies (api/NNN-<tag>.request.txt / .reply.txt)."""

    def __init__(self, folder: Path, stamp: str):
        self.folder, self.stamp = Path(folder), stamp
        self.path = self.folder / f"run-{stamp}.log"
        self.api = self.folder / "api"
        self.lock = threading.Lock()
        self.k = len(list(self.api.glob("*.request.txt"))) if self.api.exists() else 0

    def line(self, text: str) -> None:
        with self.lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now().isoformat(timespec='seconds')}  {text}\n")

    def request(self, tag: str, system: str, user: str) -> tuple[int, Path]:
        with self.lock:
            self.k += 1
            k = self.k
            self.api.mkdir(parents=True, exist_ok=True)
            p = self.api / f"{k:03d}-{tag}.request.txt"
            p.write_text(f"[system]\n{system}\n\n[user]\n{user}\n", encoding="utf-8")
        return k, p

    def reply(self, k: int, tag: str, text: str) -> Path:
        with self.lock:
            p = self.api / f"{k:03d}-{tag}.reply.txt"
            p.write_text(text, encoding="utf-8")
        return p


def scenes_state(ep_root: Path, n: int) -> tuple[str, str]:
    """('none' | 'done' | 'bad', first problem): the same test `novel status` uses for a scene file."""
    from novel.stages import episode_scenes as S
    sf = S.scenes_path(ep_root, n)
    if not sf.exists():
        return "none", ""
    try:
        doc = json.loads(sf.read_text(encoding="utf-8"))
        ids = [int(s["id"]) for s in doc["scenes"]]
        problems = S.check_chapter(ep_root, n)
    except SystemExit as e:
        return "bad", str(e.code)
    except Exception as e:
        return "bad", f"{type(e).__name__}: {str(e)[:80]}"
    if doc.get("version") != 2 or not ids:
        return "bad", "not a version 2 scene file"
    return ("bad", problems[0]) if problems else ("done", "")


class Runner:
    def __init__(self, book: str, ep: int, args, *, out=lambda text: print(text, flush=True), client=None):
        from novel.core import context, paths
        self.book, self.ep, self.a = book, ep, args
        self.out = out
        context.activate(book)
        self.book_dir = paths.book_dir(book)
        self.ep_root = paths.ep_root(book, ep)
        self.bible_dir = B.bible_dir(book)
        self.gemini_dir = self.book_dir / "gemini"
        self.cfg = load_settings(book, ep, getattr(args, "model", None), getattr(args, "parallel", None),
                                 getattr(args, "max_chapters", None))
        self.client = client
        self.log: RunLog | None = None
        self.stop = threading.Event()
        self.ilock = threading.RLock()          # imports, bible reads, progress writes
        self.olock = threading.Lock()           # console
        self.batches: dict[str, BatchInfo] = {}
        self.chs: dict[int, Ch] = {}
        self.batch_bible: dict[str, str] = {}
        self.fatal: str | None = None           # why the run stopped early (quota, refusal)
        self.interrupted = False
        self.model_calls: dict[str, int] = {}
        self.t0 = CLOCK()
        self.dry = bool(getattr(args, "dry_run", False))

    # -------------------------------------------------------------- console and log

    def say(self, text: str) -> None:
        with self.olock:
            self.out(text)
        if self.log is not None:
            self.log.line(text.strip())

    def check_stop(self) -> None:
        if self.stop.is_set():
            raise RunStop(self.fatal or "stopped by Ctrl+C")

    # -------------------------------------------------------------- what to do

    def episode_chapters(self) -> tuple[int, int]:
        f = self.ep_root / "episode.json"
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
            return int(info["start"]), int(info["end"])
        except (OSError, ValueError, KeyError):
            raise SystemExit(f"novel write: no usable {f}. Pick the episode with: novel use {self.book} --ep N")

    def english_ok(self, n: int) -> bool:
        return any((self.book_dir / "chapters").glob(f"{n:04d}_*.txt"))

    def select(self) -> tuple[list[Ch], list[tuple[int, str]]]:
        """([chapters of this run in order], [(chapter, why it is left out)])."""
        lo, hi = self.episode_chapters()
        a = self.a
        if getattr(a, "redo", None):
            first, last = P.parse_chapters(str(a.redo))
            wanted = list(range(first, last + 1))
        elif getattr(a, "chapters", None):
            first, last = P.parse_chapters(a.chapters)
            wanted = list(range(first, last + 1))
        else:
            wanted = list(range(lo, hi + 1))
        outside = [n for n in wanted if not lo <= n <= hi]
        if outside:
            raise SystemExit(f"novel write: chapters {outside} are not in episode {self.ep} (chapters {lo}-{hi})")
        chosen, left = [], []
        for n in wanted:
            ch = Ch(n)
            state = P.hindi_state(self.ep_root, n)
            kind, why = scenes_state(self.ep_root, n)
            if getattr(a, "redo", None):
                ch.redo = True
            if state == "stale":
                ch.hindi, ch.status = "refused", "refused"
                ch.problems.append("the Hindi changed after it was locked: restore it (its hash is in hindi.lock.json)")
            elif state == "none":
                if not self.english_ok(n):
                    ch.hindi, ch.status = "refused", "refused"
                    ch.problems.append(f"no English chapter file for chapter {n}. Fix: novel extract")
                else:
                    ch.need_translate = ch.need_scenes = True
            else:
                ch.need_lock = state == "unlocked"
                if ch.redo:
                    ch.need_scenes = True
                elif kind == "none":
                    ch.need_scenes = True
                elif kind == "bad":
                    left.append((n, f"it has a scene file that is not usable ({why}); run: novel write --redo {n}"))
                    continue
                else:
                    left.append((n, "already complete (scenes written)"))
                    continue
            if ch.hindi == "refused" or ch.need_translate or ch.need_scenes:
                chosen.append(ch)
        limit = 10**6 if getattr(a, "redo", None) else self.cfg.chapters
        if len(chosen) > limit:
            for ch in chosen[limit:]:
                left.append((ch.n, f"over the limit of {limit} chapters per run (gemini.chapters_per_run or "
                                   f"--max-chapters); run the command again"))
            chosen = chosen[:limit]
        return chosen, sorted(left)

    # -------------------------------------------------------------- shared pieces

    def rules_args(self, first: int, last: int) -> dict:
        from novel.core import settings
        from novel.stages import episode_scenes as S
        get = lambda k, d=None: settings.get(k, book=self.book, ep=self.ep, default=d)
        target = int(get("translate.target_words", 800))
        low, high = P.word_band(target, float(get("translate.words_tolerance", 0.20)))
        return dict(channel=get("channel.name", "") or "", title=get("book.title") or self.book, first=first,
                    last=last, paced=float(get("images.paced_seconds", S.PACED_SECONDS)),
                    target=target, low=low, high=high,
                    chars_per_second=float(get("images.chars_per_second", S.CHARS_PER_SECOND)),
                    max_characters=int(get("images.max_characters", S.MAX_CHARACTERS)))

    def fresh_state(self) -> dict:
        with self.ilock:
            return B.load_state(self.bible_dir)

    def make_client(self):
        from novel.stages import episode_translate as T
        folder = self.log.folder
        return T.Gemini(self.cfg.model, folder / f"run-{self.log.stamp}.client.jsonl",
                        fallback_models=self.cfg.fallbacks, min_gap=self.cfg.min_gap, sleep=SLEEP, clock=CLOCK,
                        say=self.say)

    def call(self, tag: str, step: str, system: str, user: str):
        """One API call: logged, saved, counted. -> (GenResult | None, request number). None: the reply was blocked
        (a failed attempt, not a stop). Quota or a refusal stop the whole run (RunStop)."""
        from novel.stages import episode_translate as T
        self.check_stop()
        k, _ = self.log.request(tag, system, user)
        size = len(system) + len(user)
        self.say(f"{step}: request {k} ... {kb(size)} characters sent")
        try:
            res = self.client.generate(system, user, temperature=0.3)
        except T.QuotaExhausted as e:
            self.fatal = f"the Gemini quota is used up: {e}"
            self.stop.set()
            raise RunStop(self.fatal)
        except SystemExit as e:                                # a refusal, or a request that kept failing
            self.fatal = f"Gemini stopped the run: {e.code}"
            self.stop.set()
            raise RunStop(self.fatal)
        except ValueError as e:
            self.say(f"{step}: blocked: {e}")
            return None, k
        self.log.reply(k, tag, res.text)
        u = res.usage or {}
        self.model_calls[res.model] = self.model_calls.get(res.model, 0) + 1
        self.say(f"{step}: reply {len(res.text):,} characters, finish {res.finish}, key {res.key}, {res.seconds:g}s, "
                 f"tokens in {int(u.get('promptTokenCount') or 0):,} out {int(u.get('candidatesTokenCount') or 0):,}"
                 + (f", model {res.model}" if res.model != self.cfg.model else ""))
        return res, k

    # -------------------------------------------------------------- translation

    def translate(self, ch: Ch) -> None:
        n = ch.n
        step = f"chapter {n}: hindi"
        try:
            english = P.chapters_text(self.book_dir, n, n)
        except P.PromptError as e:
            ch.hindi, ch.status = "failed", "failed"
            ch.problems.append(str(e))
            return
        state = self.fresh_state()
        names = names_map(state)
        system = P.build_rules("translate", **self.rules_args(n, n))
        first = translation_request(n, english, "\n".join(P.names_lines(state)))
        band = (self.cfg.low, self.cfg.high)
        problems, previous = None, ""
        tries: list[Hindi] = []                          # every attempt without a format error, for the choice
        chosen = None
        for attempt in range(1, self.cfg.t_attempts + 1):
            user = first if problems is None else translation_retry(first, problems, previous)
            text, cut = self.translate_call(ch, f"{step}: call {attempt}", f"c{n:04d}-hindi", system, user, first)
            if text is None:
                problems, previous = ["the reply was blocked; write the narration again"], ""
                continue
            h = validate_hindi(clean_hindi_reply(text), english, names, band)
            if cut:
                h.errors.append(f"the narration is still cut off after {MAX_CONTINUATIONS} continuations")
            if not h.errors:
                tries.append(h)
            if not h.errors and not h.length_problem and not h.name_errors:
                chosen = h
                break
            problems = h.errors + h.name_errors + ([h.length_problem] if h.length_problem else [])
            previous = "\n\n".join(h.paras)
            self.say(f"{step}: rejected ({plural(len(problems), 'problem')}): {problems[0]}")
        if chosen is None:
            chosen = closest_attempt(tries, self.cfg.target, band)
            if chosen is not None:
                chosen.warnings.append(f"accepted outside the band {band[0]}-{band[1]} words: {chosen.length_problem}")
        if chosen is None:
            ch.hindi, ch.status = "failed", "failed"
            ch.problems.append(f"the Hindi did not pass the checks after {self.cfg.t_attempts} attempts: "
                               + "; ".join(problems or ["no usable reply"])[:300])
            return
        chosen.warnings += [f"accepted with a name problem: {m}" for m in chosen.name_errors]
        self.store_hindi(ch, chosen)
        ch.ratio = chosen.ratio
        ch.warnings += len(chosen.warnings)
        self.say(f"{step}: written, {len(chosen.paras)} paragraphs, {chosen.words} words (band {band[0]}-{band[1]}), "
                 f"ratio {chosen.ratio:.2f} of the English"
                 + (f" ({plural(len(chosen.warnings), 'warning')}: {chosen.warnings[0]})" if chosen.warnings else "")
                 + "; locked")

    def translate_call(self, ch: Ch, step: str, tag: str, system: str, user: str, first: str):
        """One translation attempt, with continuations while the reply is cut off by the output limit.
        -> (text or None, still_cut_off)."""
        res, _ = self.call(tag, step, system, user)
        if res is None:
            return None, False
        ch.calls += 1
        ch.models.append(res.model)
        text = res.text
        cont = 0
        while res.finish == "MAX_TOKENS" and cont < MAX_CONTINUATIONS:
            cont += 1
            done = clean_hindi_reply(text)[:-1]              # the last paragraph may be cut in the middle
            self.say(f"{step}: the reply was cut off by the output limit after {len(done)} full paragraphs; "
                     f"asking it to continue (round {cont})")
            res, _ = self.call(f"{tag}-cont{cont}", f"{step} (continuation {cont})", system,
                               translation_continue(first, done))
            if res is None:
                return None, False
            ch.calls += 1
            ch.models.append(res.model)
            text = "\n\n".join(done) + ("\n\n" if done else "") + res.text.strip()
        return text, res.finish == "MAX_TOKENS"

    def store_hindi(self, ch: Ch, h: Hindi) -> None:
        """Store the chapter the way `episode_translate draft` does (hindi file, QA note, manifest), then lock it."""
        from novel.stages import episode as E
        from novel.stages import episode_translate as T
        n, name = ch.n, f"{ch.n:05d}"
        (self.ep_root / "hindi").mkdir(parents=True, exist_ok=True)
        (self.ep_root / "qa").mkdir(parents=True, exist_ok=True)
        text = "\n\n".join(h.paras) + "\n"
        tmp = self.ep_root / "hindi" / f"{name}.tmp"
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.ep_root / "hindi" / f"{name}.txt")
        english = " ".join(T.clean_paragraphs(next((self.book_dir / "chapters").glob(f"{n:04d}_*.txt"))
                                              .read_text(encoding="utf-8")))
        (self.ep_root / "qa" / f"{name}.json").write_text(json.dumps({
            "chapter": n, "source": f"api {ch.models[-1] if ch.models else self.cfg.model}",
            "hindi_paragraphs": len(h.paras), "words": len(text.split()), "ratio": round(len(text) / len(english), 3),
            "english_numbers": T.english_numbers(text, names_map(self.fresh_state())),
            "warnings": h.warnings}, ensure_ascii=False, indent=1), encoding="utf-8")
        mf = self.ep_root / "manifest.json"
        if mf.exists():
            try:
                from novel.stages.manifest import Manifest, TRANSLATED
                man = Manifest.load(mf)
                for c in man.chapters:
                    if c.idx == n:
                        c.hindi, c.status, c.audio, c.timing, c.seconds = f"{name}.txt", TRANSLATED, None, None, None
                man.save()
            except Exception as e:                                  # the manifest is bookkeeping, not the text
                self.say(f"chapter {n}: note: manifest not updated ({type(e).__name__}: {e})")
        with self.ilock:
            T.cmd_lock(SimpleNamespace(episode=self.ep, chapters=str(n)))
        ch.hindi = "written"
        ch.need_lock = False

    def lock_existing(self, ch: Ch) -> None:
        from novel.stages import episode_translate as T
        with self.ilock:
            T.cmd_lock(SimpleNamespace(episode=self.ep, chapters=str(ch.n)))
        ch.need_lock = False
        self.say(f"chapter {ch.n}: the Hindi exists but was not locked: locked it (nothing was translated)")

    # -------------------------------------------------------------- batches

    def batch_info(self, first: int, last: int, meta: dict) -> BatchInfo:
        wins = {n: P.chapter_windows(self.ep_root, n) for n in range(first, last + 1)}
        english = {n: P.chapters_text(self.book_dir, n, n) for n in range(first, last + 1)}
        return BatchInfo(meta["batch"], first, last, meta, bool(meta.get("review")), wins, english)

    def reusable(self, first: int, last: int) -> dict | None:
        """The batch.json of exactly this range when it still matches the Hindi and the window parameters."""
        from novel.stages import episode_scenes as S
        f = self.gemini_dir / P.batch_id(first, last) / "batch.json"
        if not f.exists():
            return None
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            return None
        now = {"paced_seconds": S.PACED_SECONDS, "max_seconds": S.MAX_SECONDS,
               "chars_per_second": S.CHARS_PER_SECOND, "pacing": S.PACING}
        shas = {str(n): P.sha256(self.ep_root / "hindi" / f"{n:05d}.txt") for n in range(first, last + 1)}
        if (meta.get("mode") != "scenes" or meta.get("first") != first or meta.get("last") != last
                or meta.get("params") != now or meta.get("hindi_sha256") != shas):
            return None
        return meta

    def covering(self, first: int, last: int) -> tuple[int, int] | None:
        """The smallest existing batch that holds all of chapters first-last and still matches the Hindi: a chapter
        that failed in an earlier run is asked again inside the batch it began in (its bible is already imported)."""
        best = None
        for f in self.gemini_dir.glob("B*/batch.json"):
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                a, z = int(meta["first"]), int(meta["last"])
            except (OSError, ValueError, KeyError):
                continue
            if a <= first and last <= z and self.reusable(a, z) is not None:
                key = (not self.bible_done(P.batch_id(a, z)), z - a)       # a batch whose bible is done comes first
                if best is None or key < best[0]:
                    best = (key, (a, z))
        return best[1] if best else None

    def ensure_batch(self, first: int, last: int) -> BatchInfo:
        cover = self.covering(first, last)
        if cover is not None and cover != (first, last):
            self.say(f"batch {P.batch_id(*cover)}: reusing the batch that already holds chapters {first}-{last}")
            first, last = cover
        bid = P.batch_id(first, last)
        meta = self.reusable(first, last)
        if meta is not None:
            if cover is None or cover == (first, last):
                self.say(f"batch {bid}: reusing the batch that exists for chapters {first}-{last}")
        else:
            r = P.generate(self.book, first, last, "scenes")
            meta = r["meta"]
            self.say(f"batch {bid}: written ({r['mode']} mode, chapters {first}-{last}): {r['folder']}")
        bi = self.batch_info(first, last, meta)
        self.batches[bid] = bi
        return bi

    # -------------------------------------------------------------- importing

    def import_reply(self, bi: BatchInfo, text: str, *, replace: bool, model: str, what: str) -> Imported:
        with self.ilock:
            lines: list[str] = []
            imp = I.Importer(self.book, replace=replace, batch=bi.id, out=lines.append,
                             api={"via": "api", "model": model})
            try:
                imp.run_text(text, f"the API reply ({what})")
            except I.ImportFailure as e:
                self.log.line(f"import {what}: {e}")
                return Imported(failure=str(e), lines=lines)
            bt = imp.load_batch(bi.id)
            self.log.line(f"import {what}:\n" + "\n".join("    " + l for l in lines))
            return Imported(imp, bt, lines, imp.decide(bt))

    def annotate(self, bid: str, n: int | None, **kw) -> None:
        """Add the API's own facts (calls, models, attempts) to the batch's progress.json."""
        with self.ilock:
            f = self.gemini_dir / bid / "progress.json"
            try:
                prog = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            target = prog["bible"] if n is None else prog.setdefault("chapters", {}).setdefault(str(n), {})
            target.setdefault("api", {}).update(kw)
            I.write_atomic(f, json.dumps(prog, ensure_ascii=False, indent=2) + "\n")

    def unexpected(self, text: str, bi: BatchInfo, kinds: dict) -> str | None:
        """A sentence naming blocks the request did not ask for (or the missing block), or None when the reply holds
        only what was asked. kinds: {"IMAGES": n} / {"PATCH": n} / {"BIBLE": None} (+ chapter-level BIBLE for n)."""
        reply = C.split_reply(text)
        want_n = next((v for v in kinds.values() if v is not None), None)
        extra, got = [], set()
        for b in reply.blocks:
            if b.kind == "BIBLE" and b.batch == bi.id and b.chapter is None and "BIBLE" in kinds:
                got.add("BIBLE")
            elif b.kind == "BIBLE" and b.batch == bi.id and want_n is not None and b.chapter == want_n \
                    and "BIBLE" not in kinds:
                continue                                              # extra lines for this chapter: allowed
            elif b.kind in kinds and b.kind != "BIBLE" and b.chapter == kinds[b.kind]:
                got.add(b.kind)
            else:
                extra.append(b.label)
        if extra:
            return ("the reply holds block(s) that were not asked for: " + ", ".join(extra)
                    + ". Return only " + " and ".join(
                        (f"{k} {bi.id}" if k == "BIBLE" else f"{k} c{v:04d}") for k, v in kinds.items()) + ".")
        missing = [k for k in kinds if k not in got]
        if missing and reply.blocks:
            return ("the reply has no " + " or ".join(
                (f"{k} {bi.id}" if k == "BIBLE" else f"{k} c{kinds[k]:04d}") for k in missing) + " block")
        return None

    # -------------------------------------------------------------- the bible block

    def bible(self, bi: BatchInfo) -> str:
        """'ok' | 'failed' | 'done' (an earlier run did it)."""
        with self.ilock:
            prog = {}
            f = self.gemini_dir / bi.id / "progress.json"
            if f.exists():
                try:
                    prog = json.loads(f.read_text(encoding="utf-8"))
                except ValueError:
                    prog = {}
        if (prog.get("bible") or {}).get("done"):
            self.say(f"batch {bi.id}: bible: already imported in an earlier run, skipped")
            return "done"
        step = f"batch {bi.id}: bible"
        system = P.build_rules("bible", **self.rules_args(bi.first, bi.last), review=bi.review)
        english = "\n".join(bi.english[n] for n in range(bi.first, bi.last + 1))
        ask = (f"Return the BIBLE block of chapters {bi.first}-{bi.last}: only what is NEW (part C of the rules)"
               + (", plus the revise lines of the REVIEW TASK" if bi.review else "")
               + f". If nothing is new, return the block with no lines. {NO_OTHER_BLOCKS}\n"
               f"===== BEGIN BIBLE {bi.id} -> inbox/bible-{bi.id}.jsonl =====\n```jsonl\n<lines>\n```\n"
               f"===== END BIBLE {bi.id} =====")
        feedback = None
        for attempt in range(1, self.cfg.attempts + 1):
            state = self.fresh_state()
            first = (f"CURRENT BIBLE (what is decided so far; never repeat or re-describe it)\n"
                     f"{bible_dump(state, bi.review)}\nCHAPTERS {bi.first}-{bi.last} (English)\n{english}\n\n"
                     f"REQUEST\n")
            user = first + (ask if feedback is None else retry_instruction(ask, *feedback))
            res, _ = self.call(f"{bi.id}-bible", f"{step}: call {attempt}", system, user)
            if res is None:
                continue
            text = res.text
            if res.finish == "MAX_TOKENS":
                feedback = ("the answer was cut off by the length limit: keep the block short, only characters, "
                            "locations and variants the chapters really need", text[-3000:])
                self.say(f"{step}: the reply was cut off by the output limit; asking again for a shorter block")
                continue
            bad = self.unexpected(text, bi, {"BIBLE": None})
            if bad:
                feedback = (bad, text)
                self.say(f"{step}: rejected: {bad}")
                continue
            ir = self.import_reply(bi, text, replace=False, model=res.model, what="bible")
            if ir.failure:
                feedback = (ir.failure, text)
                self.say(f"{step}: not imported: {ir.failure}")
                continue
            outs = [o for o in ir.outcomes("BIBLE", None)]
            if outs and not outs[0].rejected:
                o = outs[0]
                self.say(f"{step}: imported ({o.summary}; {plural(len(o.warnings), 'warning')})")
                self.annotate(bi.id, None, calls=attempt, models=[res.model])
                return "ok"
            d = ir.decision
            if d.kind == "fix_first":
                self.say(f"{step}: cannot go on: {d.text}")
                return "failed"
            self.say(f"{step}: rejected ({plural(len(outs[0].errors) if outs else 0, 'error')}): "
                     f"{(d.fixes or [d.text])[0]}")
            feedback = (d.text, text)
        self.say(f"{step}: still rejected after {self.cfg.attempts} attempts; left as it is (the chapters go on "
                 f"with the bible as it is)")
        return "failed"

    # -------------------------------------------------------------- the IMAGES block of one chapter

    def images_user(self, bi: BatchInfo, n: int, instruction: str) -> str:
        state = self.fresh_state()
        wins = bi.wins[n]
        return (f"CURRENT BIBLE (what is decided so far, including everything added earlier in this run; reuse these "
                f"keys and spellings exactly)\n{bible_dump(state, False)}\n"
                f"CHAPTER {n} (English)\n{bi.english[n].strip()}\n\n"
                f"WINDOWS OF CHAPTER {n} (the Hindi narration, final; one image line per window)\n"
                f"##### CHAPTER {n}: {len(wins)} WINDOWS #####\n{window_lines(wins)}\n\nREQUEST\n{instruction}")

    def drop_unreadable_lines(self, text: str, n: int, step: str) -> tuple[str, C.Dropped | None]:
        """Take the lines that are not valid JSON out of the IMAGES block(s) of chapter n (C.drop_unreadable), with a
        warning that names how many and which windows. The rest of the block is imported as it is, so the dropped
        windows count as missing. A block with no valid line left is passed on unchanged (rejected as before).
        -> (text, what was dropped, or None when nothing was)."""
        seen: list[C.Dropped] = []

        def edit(body: list[str], quoted: bool) -> list[str]:
            d = C.drop_unreadable(body, quoted)
            if not d.bad or d.objects == 0:
                return body
            seen.append(d)
            return d.kept + [f">>> MORE c{n:04d} IMAGES"]           # the block is still owed: the part is filed

        text = map_images_blocks(text, n, edit)
        if not seen:
            return text, None
        bad = [line for d in seen for line in d.bad]
        windows = sorted({w for d in seen for w in d.windows})
        groups = [g for d in seen for g in d.groups]
        held = sorted(h for d in seen for h in d.held)
        where = (f"window{'s' if len(windows) != 1 else ''} {C.ranges_text(windows)}" if windows
                 else "the window numbers cannot be read")
        extra = (f"; group {', '.join(groups)} dropped too" if groups else "") + \
                (f"; {plural(len(held), 'image line')} of that group held back" if held else "")
        self.say(f"{step}: warning: dropped {plural(len(bad), 'line')} that {'is' if len(bad) == 1 else 'are'} "
                 f"not valid JSON ({where}){extra}; "
                 f"asking again for those windows only")
        return text, C.Dropped(bad=bad, windows=windows, groups=groups, held=held)

    def scenes(self, ch: Ch, bi: BatchInfo) -> None:
        """The IMAGES block of one chapter: ask, import, read the importer's decision, repeat (bounded)."""
        n, count = ch.n, len(bi.wins[ch.n])
        step = f"chapter {n}: scenes"
        system = P.build_rules("images", **self.rules_args(bi.first, bi.last), chapter=n, window_count=count)
        base = images_instruction(n, count, bi.id)
        feedback = None                                    # (say, previous text) after a rejection
        rest = None                                        # (missing windows, known groups, dropped lines) after a partial
        held = ""                                          # --redo: the lines the last partial import staged
        dropped_any = False                                # some line of this chapter was dropped
        for attempt in range(1, self.cfg.attempts + 1):
            if rest:
                instruction = images_rest_instruction(n, count, rest[0], rest[1], bi.id, rest[2])
            elif feedback:
                instruction = retry_instruction(base, *feedback)
            else:
                instruction = base
            user = self.images_user(bi, n, instruction)
            res, _ = self.call(f"c{n:04d}-images", f"{step}: call {attempt}", system, user)
            if res is None:
                continue
            ch.calls += 1
            ch.models.append(res.model)
            text = res.text
            if res.finish == "MAX_TOKENS":
                text = repair_cut(text, n, more=not ch.redo)
                self.say(f"{step}: the reply was cut off by the output limit; keeping its complete lines")
            text, dropped = self.drop_unreadable_lines(text, n, f"{step}: call {attempt}")
            dropped_any = dropped_any or dropped is not None
            bad = self.unexpected(text, bi, {"IMAGES": n})
            if bad:
                feedback, rest = (bad, text), None
                self.say(f"{step}: rejected: {bad}")
                continue
            if held:                                       # --redo starts the import afresh: send the held lines too
                text = prepend_images_lines(text, n, held.splitlines())
            ir = self.import_reply(bi, text, replace=ch.redo, model=res.model, what=f"chapter {n}")
            if ir.failure:
                feedback, rest = (ir.failure, text), None
                self.say(f"{step}: not imported: {ir.failure}")
                continue
            outs = ir.outcomes("IMAGES", n)
            bible_outs = ir.outcomes("BIBLE", n)
            for o in bible_outs:
                if not o.rejected and o.summary:
                    self.say(f"{step}: bible lines for this chapter imported ({o.summary})")
            if not outs:
                feedback, rest = (f"the reply has no IMAGES block for chapter {n}", text), None
                self.say(f"{step}: rejected: no IMAGES block for this chapter")
                continue
            o = outs[0]
            imgs, status = ir.imp.chapter_status(ir.bt, n)
            if not o.rejected and status.startswith("done"):
                ch.images = imgs
                ch.status = "complete"
                ch.warnings = len(o.warnings)
                self.say(f"{step}: call {attempt} ... imported {imgs} ({plural(len(o.warnings), 'warning')})")
                self.annotate(bi.id, n, calls=ch.calls, models=sorted(set(ch.models)), attempts=attempt)
                return
            if not o.rejected:                             # partial: ask for the missing windows only
                staged = ir.imp.staged.get(n)
                missing = C.missing_windows(staged[1], count) if staged else []
                ch.images = imgs
                if ch.redo:
                    held = I.Importer.staging_text(*staged) if staged else ""
                bad_text = "\n".join(dropped.bad) if dropped else ""
                if (ch.redo and not dropped_any) or not missing:
                    feedback, rest = (f"the block covered {imgs} windows; chapter {n} needs all {count}", text), None
                    self.say(f"{step}: partial ({imgs}); asking for the whole chapter again")
                else:
                    rest, feedback = (missing, staged[0], bad_text), None
                    self.say(f"{step}: partial ({imgs}); asking for windows {C.ranges_text(missing)} only"
                             + (" and the dropped lines again" if bad_text else ""))
                continue
            d = ir.decision
            if d.kind == "fix_first" or (not o.repeat and not d.fixes):
                raise Stop(f"{(o.errors[0].msg if o.errors else d.text)[:300]}"
                           + (f" (first run: {d.first})" if d.first else ""))
            only_short = (len(o.errors) == 1 and o.errors[0].msg.startswith("too few images") and not ch.redo)
            if only_short:                                 # a valid but short block: file it as a part
                ir2 = self.import_reply(bi, text + f"\n>>> MORE c{n:04d} IMAGES\n", replace=False, model=res.model,
                                        what=f"chapter {n} (short block filed as a part)")
                o2 = ir2.outcomes("IMAGES", n)
                staged = ir2.imp.staged.get(n) if ir2.imp else None
                if o2 and not o2[0].rejected and staged:
                    missing = C.missing_windows(staged[1], count)
                    ch.images = ir2.imp.chapter_status(ir2.bt, n)[0]
                    rest, feedback = (missing, staged[0], ""), None
                    self.say(f"{step}: the block stops short ({ch.images}); asking for windows "
                             f"{C.ranges_text(missing)} only")
                    continue
            feedback, rest = (d.text, text), None
            self.say(f"{step}: rejected ({plural(len(o.errors), 'error')}): {(d.fixes or [d.text])[0][:160]}")
        ch.status = "partial" if ch.images not in ("-", "") else "failed"
        ch.problems.append(f"the IMAGES block was still not accepted after {self.cfg.attempts} attempts; left as it "
                           f"is. Run novel write again, or use the hand-paste path: novel prompt --chapters "
                           f"{bi.first}-{bi.last}")
        self.say(f"{step}: not complete after {self.cfg.attempts} attempts; reported, going on")

    def chapter_task(self, ch: Ch, bi: BatchInfo) -> None:
        try:
            self.scenes(ch, bi)
        except Stop as e:
            ch.status = "failed"
            ch.problems.append(str(e))
            self.say(f"chapter {ch.n}: scenes: cannot go on: {e}")
        except RunStop:
            ch.status = "stopped"
        except Exception as e:                             # one chapter must not end the run
            ch.status = "failed"
            ch.problems.append(f"{type(e).__name__}: {e}")
            self.say(f"chapter {ch.n}: scenes: unexpected {type(e).__name__}: {e}")

    # -------------------------------------------------------------- phases

    def run_translations(self, chs: list[Ch]) -> None:
        for ch in chs:
            if not ch.need_translate or ch.status == "refused":
                continue
            if getattr(self.a, "no_translate", False):
                ch.hindi, ch.status = "refused", "refused"
                ch.problems.append("no Hindi yet and --no-translate was given: translate it, or run without "
                                   "--no-translate")
                self.say(f"chapter {ch.n}: hindi: refused (--no-translate)")
                continue
            try:
                self.translate(ch)
            except RunStop:
                ch.hindi, ch.status = "failed", "stopped"
                return
            if ch.hindi == "written":
                ch.need_translate = False

    def run_scenes(self, chs: list[Ch]) -> None:
        by_n = {c.n: c for c in chs}
        lo, hi = self.episode_chapters()
        locked = {n for n in range(lo, hi + 1) if P.hindi_state(self.ep_root, n) == "locked"}
        for ch in chs:
            if ch.need_lock and ch.status != "refused" and P.hindi_state(self.ep_root, ch.n) == "unlocked":
                self.lock_existing(ch)
                locked.add(ch.n)
        todo = [c.n for c in chs if c.need_scenes and not c.redo and c.status not in ("refused", "failed", "stopped")
                and c.n in locked]
        for c in chs:
            if c.need_scenes and c.n not in locked and c.status == "pending":
                c.status = "failed"
                c.problems.append("its Hindi is not locked, so the scenes step cannot run")
        for ch in chs:                                       # --redo: the chapter's own batch, no bible call
            if ch.redo and ch.status not in ("refused", "failed", "stopped") and ch.n in locked:
                try:
                    self.check_stop()
                    a, z = self.redo_range(ch.n)
                    bi = self.ensure_batch(a, z)
                except RunStop:
                    ch.status = "stopped"
                    return
                ch.bible = "-"
                self.run_chapters([ch], bi)
        for first, last in spans(todo, locked):
            run = [by_n[n] for n in range(first, last + 1) if n in by_n and by_n[n].n in todo]
            try:
                self.check_stop()
                bi = self.ensure_batch(first, last)
                b = self.bible(bi)
            except RunStop:
                for c in run:
                    c.status = "stopped"
                return
            for c in run:
                c.bible = "ok" if b in ("ok", "done") else "failed"
            self.run_chapters(run, bi)
            if self.stop.is_set():
                return

    def redo_range(self, n: int) -> tuple[int, int]:
        """The batch a redo uses: the one named in the chapter's scene file when it still matches, else a batch of
        the chapter alone."""
        from novel.stages import episode_scenes as S
        try:
            doc = json.loads(S.scenes_path(self.ep_root, n).read_text(encoding="utf-8"))
            b = C.bible_id((doc.get("source") or {}).get("batch") or "")
        except (OSError, ValueError):
            b = None
        if b and b[1] <= n <= b[2] and self.reusable(b[1], b[2]) is not None:
            return b[1], b[2]
        return n, n

    def run_chapters(self, run: list[Ch], bi: BatchInfo) -> None:
        if self.cfg.parallel <= 1 or len(run) <= 1:
            for ch in run:
                if self.stop.is_set():
                    ch.status = "stopped"
                    continue
                self.chapter_task(ch, bi)
            return
        with ThreadPoolExecutor(max_workers=self.cfg.parallel) as pool:
            list(pool.map(lambda c: self.chapter_task(c, bi), run))

    # -------------------------------------------------------------- dry run

    def run_dry(self, chs: list[Ch], left: list[tuple[int, str]]) -> int:
        self.say = lambda text: self.out(text)               # nothing is logged or written
        self.out(f"Dry run: nothing is sent and nothing is written. Model {self.cfg.model}, "
                 f"{self.cfg.chapters} chapters per run, {self.cfg.attempts} attempts per block, "
                 f"{self.cfg.parallel} at a time, {self.cfg.min_gap:g}s between calls.")
        state = self.fresh_state()
        total = 0
        for ch in chs:
            if ch.status == "refused":
                self.out(f"chapter {ch.n}: refused: {ch.problems[0]}")
        for ch in chs:
            if ch.need_translate and ch.status != "refused":
                if getattr(self.a, "no_translate", False):
                    self.out(f"chapter {ch.n}: hindi: would be refused (--no-translate)")
                    continue
                english = P.chapters_text(self.book_dir, ch.n, ch.n)
                system = P.build_rules("translate", **self.rules_args(ch.n, ch.n))
                user = translation_request(ch.n, english, "\n".join(P.names_lines(state)))
                total += len(system) + len(user)
                self.out(f"chapter {ch.n}: hindi: 1 call, request {kb(len(system) + len(user))} characters "
                         f"(rules {kb(len(system))}, text {kb(len(user))}); then locked. Scenes follow once the "
                         f"Hindi exists (not sized here).")
        needing = [c.n for c in chs if c.need_scenes and c.status != "refused" and not c.redo
                   and P.hindi_state(self.ep_root, c.n) in ("locked", "unlocked")]
        for ch in chs:
            if ch.need_lock and ch.n in needing:
                self.out(f"chapter {ch.n}: the Hindi exists but is not locked: would lock it (no translation)")
        lo, hi = self.episode_chapters()
        locked = {n for n in range(lo, hi + 1) if P.hindi_state(self.ep_root, n) == "locked"} | set(needing)
        todo_ranges = list(spans(needing, locked))
        for c in chs:
            if c.redo and c.status != "refused":
                todo_ranges.append(self.redo_range(c.n))
        for first, last in todo_ranges:
            try:
                r = P.generate(self.book, first, last, "scenes", write=False)
            except P.PromptError as e:
                self.out(f"batch {P.batch_id(first, last)}: {e}")
                continue
            bi = self.batch_info(first, last, r["meta"])
            reuse = self.reusable(first, last) is not None
            self.out(f"batch {bi.id}: chapters {first}-{last}, "
                     f"{'would be reused (it exists and still matches)' if reuse else 'would be written'}; "
                     f"windows " + ", ".join(f"{n}: {len(w)}" for n, w in bi.wins.items()))
            done = self.bible_done(bi.id)
            sysb = P.build_rules("bible", **self.rules_args(first, last), review=bi.review)
            userb = (f"CURRENT BIBLE\n{bible_dump(state, bi.review)}\nCHAPTERS\n"
                     + "\n".join(bi.english.values()) + "\n\nREQUEST\n" + "x" * 700)
            if done or any(c.redo for c in chs if first <= c.n <= last):
                self.out(f"  bible: no call ({'redo: the bible is not asked again' if not done else 'already imported'})")
            else:
                total += len(sysb) + len(userb)
                self.out(f"  bible: 1 call, request about {kb(len(sysb) + len(userb))} characters "
                         f"(rules {kb(len(sysb))}, bible dump + English {kb(len(userb))})")
            for n in range(first, last + 1):
                mine = next((c for c in chs if c.n == n), None)
                if mine is None or not mine.need_scenes:
                    self.out(f"  chapter {n}: no call (nothing to do)")
                    continue
                count = len(bi.wins[n])
                sysi = P.build_rules("images", **self.rules_args(first, last), chapter=n, window_count=count)
                useri = self.images_user(bi, n, images_instruction(n, count, bi.id))
                total += len(sysi) + len(useri)
                self.out(f"  chapter {n}: scenes: 1 call, request {kb(len(sysi) + len(useri))} characters "
                         f"(rules {kb(len(sysi))}, bible + English + {count} windows {kb(len(useri))}); "
                         f"expects {count} image lines")
        for n, why in left:
            self.out(f"chapter {n}: left out: {why}")
        self.out(f"Total of the first requests: {kb(total)} characters (about {kb(total // 3)} tokens; the real "
                 f"count comes from the API). Retries are extra, at most {self.cfg.attempts} calls per block.")
        return 0

    def bible_done(self, bid: str) -> bool:
        f = self.gemini_dir / bid / "progress.json"
        try:
            return bool((json.loads(f.read_text(encoding="utf-8")).get("bible") or {}).get("done"))
        except (OSError, ValueError):
            return False

    # -------------------------------------------------------------- the whole run

    def run(self) -> int:
        chs, left = self.select()
        if not chs:
            for n, why in left:
                self.out(f"chapter {n}: {why}")
            self.out("Nothing to do: every chapter asked for is complete (no call was made).")
            return 0
        if self.dry:
            return self.run_dry(chs, left)
        lo, hi = chs[0].n, chs[-1].n
        folder = self.gemini_dir / P.batch_id(lo, hi)
        self.log = RunLog(folder, utc_stamp())
        self.log.line(f"novel write: book {self.book}, episode {self.ep}, chapters {[c.n for c in chs]}, "
                      f"model {self.cfg.model}, fallbacks {self.cfg.fallbacks}, parallel {self.cfg.parallel}, "
                      f"attempts {self.cfg.attempts}, pacing {self.cfg.min_gap:g}s")
        self.out(f"novel write: book '{self.book}', episode {self.ep}, chapters {', '.join(str(c.n) for c in chs)} "
                 f"(model {self.cfg.model})")
        for n, why in left:
            self.out(f"chapter {n}: left out: {why}")
        for c in chs:
            if c.status == "refused":
                self.say(f"chapter {c.n}: refused: {c.problems[0]}")
        if self.client is None:
            from novel.stages import episode_translate as T
            try:
                self.client = self.make_client()
            except SystemExit as e:
                self.out(f"novel write: {e.code}")
                self.out("Add your Gemini keys to secrets.env (GEMINI_API_KEY, GEMINI_API_KEY2, ...); see "
                         "`novel doctor`.")
                return 1
        old = None
        try:
            def on_sigint(signum, frame):
                if self.stop.is_set():
                    raise KeyboardInterrupt
                self.interrupted = True
                self.stop.set()
                self.out("Ctrl+C: finishing the current call, then stopping (press again to stop at once).")
            if threading.current_thread() is threading.main_thread():
                old = signal.signal(signal.SIGINT, on_sigint)
        except (ValueError, OSError):
            old = None
        try:
            try:
                self.run_translations(chs)
                self.run_scenes(chs)
            except RunStop:
                pass
        finally:
            if old is not None:
                signal.signal(signal.SIGINT, old)
        return self.summary(chs)

    # -------------------------------------------------------------- summary

    def summary(self, chs: list[Ch]) -> int:
        self.out("")
        head = f"{'chapter':<8} {'hindi':<8} {'bible':<7} {'images':<10} {'calls':<6} {'model':<26} result"
        self.out(head)
        for c in chs:
            if c.status == "pending":
                c.status = "stopped" if self.stop.is_set() else "failed"
            models = ", ".join(sorted(set(c.models))) or "-"
            self.out(f"{c.n:<8} {c.hindi:<8} {c.bible:<7} {c.images:<10} {c.calls:<6} {models:<26} {c.status}"
                     + (f" ({plural(c.warnings, 'warning')})" if c.warnings and c.status == "complete" else "")
                     + (f" [ratio {c.ratio:.2f}]" if c.ratio else ""))
            for p in c.problems:
                self.out(f"           problem: {p}")
        u = self.client.usage if self.client else {}
        calls = self.client.calls if self.client else 0
        sw = self.client.rate_switches if self.client else 0
        secs = CLOCK() - self.t0
        self.out("")
        self.out(f"Gemini calls (requests sent, retries included): {calls}; tokens in {u.get('prompt', 0):,}, "
                 f"out {u.get('output', 0):,}, thoughts {u.get('thoughts', 0):,}, total {u.get('total', 0):,}; "
                 f"rate-limit switches: {sw}; time {int(secs // 60)} min {int(secs % 60)} s")
        used = {m: k for m, k in self.model_calls.items()}
        if len(used) > 1 or (used and self.cfg.model not in used):
            self.out("Replies by model: " + ", ".join(f"{m} {k}" for m, k in used.items()))
        self.out(f"Run log: {self.log.path}")
        self.out(f"Requests and replies saved in: {self.log.api}")
        if self.fatal:
            self.out(f"STOPPED: {self.fatal}")
            self.out("Run the same command again later: finished chapters are skipped, the rest continues.")
        elif self.interrupted:
            self.out("Stopped by Ctrl+C after the current call. Run the same command again to continue.")
        ok = all(c.status == "complete" for c in chs)
        scene_chs = [c for c in chs if c.status == "complete" and c.need_scenes]
        if scene_chs:
            a, z = scene_chs[0].n, scene_chs[-1].n
            self.out(f"Next: novel qa --chapters {a}-{z}" if a != z else f"Next: novel qa --chapters {a}")
        if not ok:
            self.out("Not every chapter is complete (see the table). The same command continues where it stopped.")
        return 0 if ok else 1

    # -------------------------------------------------------------- novel write --retranslate / --restore

    # Everything made from the old Hindi of one chapter, moved together into one old/vK/ folder. The same
    # "set the old version aside, never delete" idea as cmd_draft and the importer's version_old, but wider:
    # it also moves the Hindi, audio, timing, captions and the two QA files, drops the chapter's lock entry
    # (saved) and resets the manifest row (saved). A RESTORE.json lists every move so --restore can put it back.

    ASIDE_RELS = ("hindi/{name}.txt", "audio/{name}.mp3", "timing/{name}.json", "captions/{name}.json",
                  "scenes/{name}.json", "scenes/{name}.brief.md", "images/c{n:04d}", "qa/{name}.json",
                  "qa/{name}.review.json")

    def _parse_chapter_list(self, spec: str) -> list[int]:
        out = []
        for part in str(spec).split(","):
            a, _, b = part.partition("-")
            try:
                first, last = int(a), int(b or a)
            except ValueError:
                raise SystemExit(f"novel write: --translate {spec!r}: each part is N or N-M")
            if last < first:
                raise SystemExit(f"novel write: --translate {part!r}: need first <= last")
            out += list(range(first, last + 1))
        # de-duplicate, keep order
        seen = set()
        uniq = [n for n in out if not (n in seen or seen.add(n))]
        return uniq

    def _episode_published(self) -> bool:
        f = self.ep_root / "episode.yaml"
        if not f.exists():
            return False
        try:
            import yaml
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(isinstance(doc, dict) and doc.get("published"))

    def _has_hindi(self, n: int) -> bool:
        return (self.ep_root / "hindi" / f"{n:05d}.txt").exists()

    def _load_lock(self) -> dict:
        f = self.ep_root / "hindi.lock.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    def _save_lock(self, lock: dict) -> None:
        I.write_atomic(self.ep_root / "hindi.lock.json",
                       json.dumps(lock, indent=1, sort_keys=True, ensure_ascii=False) + "\n")

    def _next_old(self) -> Path:
        """The smallest old/vK/ that does not exist yet, made."""
        k = 1
        while (self.ep_root / "old" / f"v{k}").exists():
            k += 1
        old = self.ep_root / "old" / f"v{k}"
        old.mkdir(parents=True, exist_ok=True)
        return old

    def _reset_manifest_row(self, n: int) -> dict | None:
        """Reset chapter n's manifest row to the pre-narration state. Returns the old row as a dict (None if
        there is no manifest or no such chapter)."""
        from novel.stages.manifest import Manifest, PENDING
        mf = self.ep_root / "manifest.json"
        if not mf.exists():
            return None
        man = Manifest.load(mf)
        row = None
        for c in man.chapters:
            if c.idx == n:
                row = asdict(c)
                c.hindi = None
                c.status = PENDING
                c.audio = None
                c.timing = None
                c.seconds = None
                c.error = None
                c.attempts = 0
                break
        man.save()
        return row

    def _restore_manifest_row(self, n: int, row: dict) -> None:
        from novel.stages.manifest import Manifest, Chapter
        mf = self.ep_root / "manifest.json"
        man = Manifest.load(mf)
        for c in man.chapters:
            if c.idx == n:
                for k, v in row.items():
                    setattr(c, k, v)
                break
        else:
            man.chapters.append(Chapter(**row))
        man.save()

    def _set_aside(self, n: int) -> Path:
        """Move chapter n's files into a fresh old/vK/ and write RESTORE.json. Returns the old/vK/ path.
        Nothing is deleted; a file that does not exist is simply not listed."""
        name = f"{n:05d}"
        old = self._next_old()
        moved: dict[str, str] = {}
        for rel in self.ASIDE_RELS:
            src = self.ep_root / rel.format(name=name, n=n)
            if not src.exists():
                continue
            dst = old / rel.format(name=name, n=n)
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(src, dst)
            except (PermissionError, OSError):
                if src.is_file():
                    shutil.copy2(src, dst)
                else:
                    shutil.copytree(src, dst)
                self.say(f"  note: {rel} is open in another program - copied to {old}, not moved")
            moved[rel.format(name=name, n=n)] = rel.format(name=name, n=n)
        # lock entry: remove it from hindi.lock.json and save it in RESTORE.json
        lock = self._load_lock()
        lock_entry = {}
        key = f"{name}.txt"
        if key in lock:
            lock_entry = {key: lock.pop(key)}
            self._save_lock(lock)
        # manifest row: reset and save the old row
        manifest_row = self._reset_manifest_row(n)
        restore = {"chapter": n, "timestamp": utc_stamp(), "files": moved,
                   "lock": lock_entry, "manifest": manifest_row}
        I.write_atomic(old / "RESTORE.json", json.dumps(restore, ensure_ascii=False, indent=1) + "\n")
        return old

    def _restore_from(self, target: Path) -> None:
        """Move every file listed in target/RESTORE.json back to its original path, put the saved lock entry
        and manifest row back, and verify the lock check passes. Makes no API call."""
        from novel.stages import episode as E
        restore = json.loads((target / "RESTORE.json").read_text(encoding="utf-8"))
        n = int(restore["chapter"])
        for orig_rel, _stored_rel in restore["files"].items():
            src = target / orig_rel
            if not src.exists():
                continue
            dst = self.ep_root / orig_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and dst.is_dir() and not src.is_dir():
                shutil.rmtree(dst)
            try:
                os.replace(src, dst)
            except (PermissionError, OSError):
                if src.is_file():
                    shutil.copy2(src, dst)
                else:
                    shutil.copytree(src, dst)
        lock = self._load_lock()
        lock.update(restore["lock"] or {})
        self._save_lock(lock)
        if restore.get("manifest"):
            self._restore_manifest_row(n, restore["manifest"])
        E.check_lock(self.ep_root, n)

    def _restore_candidates(self, n: int) -> list[tuple[int, Path]]:
        """[(k, old/vK/)] for every old/vK/RESTORE.json whose chapter is n, sorted by k."""
        out = []
        old_root = self.ep_root / "old"
        if not old_root.exists():
            return out
        for d in sorted(old_root.glob("v*")):
            rf = d / "RESTORE.json"
            if not rf.exists() or not d.is_dir():
                continue
            try:
                r = json.loads(rf.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if int(r.get("chapter", -1)) == n:
                try:
                    k = int(d.name[1:])
                except ValueError:
                    continue
                out.append((k, d))
        return out

    def run_retranslate(self, spec: str) -> int:
        chapters = self._parse_chapter_list(spec)
        lo, hi = self.episode_chapters()
        outside = [n for n in chapters if not lo <= n <= hi]
        if outside:
            self.out(f"novel write: chapters {outside} are not in episode {self.ep} (chapters {lo}-{hi})")
            return 2
        if self._episode_published():
            self.out(f"novel write: episode {self.ep} is marked published: true in its episode.yaml; "
                     f"re-translate is refused.")
            return 2
        if self.dry:
            self.out("Dry run: nothing is set aside, sent or written.")
            for n in chapters:
                name = f"{n:05d}"
                if not self._has_hindi(n):
                    self.out(f"chapter {n}: has no translation yet; would translate it (nothing to set aside).")
                    continue
                rels = [rel.format(name=name, n=n) for rel in self.ASIDE_RELS
                        if (self.ep_root / rel.format(name=name, n=n)).exists()]
                self.out(f"chapter {n}: a translation already exists; would set aside ({', '.join(rels) or 'no files'}) "
                         f"to a new old/vK/, drop its lock entry and reset its manifest row, then translate and write scenes.")
                self.out(f"chapter {n}: WARNING the narrated audio would be set aside; new audio is needed: "
                         f"novel tts push --chapters {n}")
                self.out(f"chapter {n}: to go back: novel write --restore {n}")
            return 0
        # set up the log and the client once for the whole run
        self.log = RunLog(self.gemini_dir / f"retranslate-{utc_stamp()}", utc_stamp())
        self.log.line(f"novel write --translate {spec}: book {self.book}, episode {self.ep}, chapters {chapters}, "
                       f"model {self.cfg.model}")
        if self.client is None:
            try:
                self.client = self.make_client()
            except SystemExit as e:
                self.out(f"novel write: {e.code}")
                return 1
        self.out(f"novel write: re-translate chapters {', '.join(str(n) for n in chapters)} of episode {self.ep} "
                 f"(model {self.cfg.model})")
        any_failed = False
        for n in chapters:
            had_hindi = self._has_hindi(n)
            old = self._set_aside(n) if had_hindi else None
            if old is not None:
                self.out(f"chapter {n}: a translation already exists. It was moved to {old} together with the audio, "
                         f"scenes and pictures made from it. To go back: novel write --restore {n}")
            ch = Ch(n)
            ch.need_translate = True
            ch.need_scenes = True
            try:
                self.run_translations([ch])
                if ch.hindi == "written":
                    self.run_scenes([ch])
            except RunStop:
                ch.status = "stopped"
            if ch.status == "complete":
                if old is not None:
                    self.out(f"chapter {n}: WARNING the narrated audio of this chapter was set aside and new audio is "
                             f"needed: novel tts push --chapters {n}")
                    self.out(f"chapter {n}: to go back: novel write --restore {n}")
            elif ch.hindi == "written":
                # the new Hindi passed its checks and is locked: it stays. Only the scenes are missing, and a plain
                # `novel write --chapters N` writes them without translating again. The old version stays in old/vK/.
                why = ch.problems[0] if ch.problems else ch.status
                self.out(f"chapter {n}: translated; scenes are not complete ({why}). Run: novel write --chapters {n}")
                if old is not None:
                    self.out(f"chapter {n}: WARNING the narrated audio of this chapter was set aside and new audio is "
                             f"needed: novel tts push --chapters {n}")
                    self.out(f"chapter {n}: the old version is kept in {old} (to go back: novel write --restore {n})")
                any_failed = True
            elif old is None:
                why = ch.problems[0] if ch.problems else ch.status
                self.out(f"chapter {n}: translation failed ({why}). Fix the problem and run again.")
                any_failed = True
            else:
                # never leave the chapter without Hindi: put the old version back exactly as it was
                why = ch.problems[0] if ch.problems else ch.status
                self.say(f"chapter {n}: re-translate failed ({why}); restoring the old version from {old}")
                self._restore_from(old)
                # the old version is back in place: drop the set-aside record so --restore does not offer it.
                # Only when nothing else is left in that folder; anything still there is kept.
                if not any(p.is_file() and p.name != "RESTORE.json" for p in old.rglob("*")):
                    (old / "RESTORE.json").unlink(missing_ok=True)
                    shutil.rmtree(old, ignore_errors=True)
                self.out(f"chapter {n}: re-translate failed; the old version is back. Fix the problem and run again.")
                any_failed = True
        self.out(f"Run log: {self.log.path}")
        return 1 if any_failed else 0

    def run_restore(self, n: int) -> int:
        candidates = self._restore_candidates(n)
        if not candidates:
            self.out(f"novel write: chapter {n} has nothing to restore (no old/vK/RESTORE.json for it).")
            return 2
        lo, hi = self.episode_chapters()
        if not lo <= n <= hi:
            self.out(f"novel write: chapter {n} is not in episode {self.ep} (chapters {lo}-{hi})")
            return 2
        target_k, target = max(candidates)
        # set the CURRENT version aside first (into a newer old/vM/), so restoring is itself reversible
        current = self._set_aside(n)
        self.out(f"chapter {n}: set the current version aside to {current} (restoring is reversible: "
                  f"novel write --restore {n})")
        self._restore_from(target)
        self.out(f"chapter {n}: restored the old version from old/v{target_k}/ "
                 f"(Hindi, lock entry, audio, timing, captions, scenes, images, QA, manifest row).")
        return 0

    # -------------------------------------------------------------- novel write --fix N

    def run_fix(self, n: int) -> int:
        from novel import qa as Q
        from novel.gemini import fix as F
        run = Q.Run(self.book, self.ep)
        ep_root = run.ep_root_for(n)
        if ep_root is None:
            self.out(f"novel write: chapter {n} belongs to no episode")
            return 1
        try:
            fx = F.build(ep_root, n, with_warnings=False)
        except F.FixError as e:
            self.out(f"novel write: {e}")
            return 1
        for note in fx.notes:
            self.out(f"note: {note}")
        from novel.stages import episode_scenes as S
        doc = json.loads(S.scenes_path(ep_root, n).read_text(encoding="utf-8"))
        bid = (doc.get("source") or {}).get("batch")
        imp = I.Importer(self.book)
        bt = imp.load_batch(bid) if bid else None
        bt = bt or imp.batch_for_chapter(n)
        if bt is None:
            self.out(f"novel write: chapter {n} is in no prompt batch. Fix: {imp.prompt_fix(n)}")
            return 1
        meta = bt.meta
        self.ep_root = ep_root
        bi = self.batch_info(bt.first, bt.last, meta)
        count = len(bi.wins[n])
        system = P.build_rules("images", **self.rules_args(bt.first, bt.last), chapter=n, window_count=count)
        first = (f"CURRENT BIBLE (what is decided so far)\n{bible_dump(self.fresh_state(), False)}\n"
                 f"CHAPTER {n} (English)\n{bi.english[n].strip()}\n\nWINDOWS OF CHAPTER {n} (the Hindi narration, "
                 f"final)\n{window_lines(bi.wins[n])}\n\nREQUEST\n")
        if self.dry:
            self.out(f"Dry run: chapter {n}: 1 call, request {kb(len(system) + len(first) + len(fx.text))} characters "
                     f"(pictures {C.ranges_text(fx.images)}); the PATCH block it returns is imported. Nothing sent.")
            return 0
        self.log = RunLog(bt.folder, utc_stamp())
        self.log.line(f"novel write --fix {n}: book {self.book}, batch {bt.id}, model {self.cfg.model}")
        if self.client is None:
            try:
                self.client = self.make_client()
            except SystemExit as e:
                self.out(f"novel write: {e.code}")
                return 1
        step = f"chapter {n}: fix"
        feedback, done = None, False
        try:
            for attempt in range(1, self.cfg.attempts + 1):
                user = first + (fx.text if feedback is None else retry_instruction(fx.text, *feedback))
                res, _ = self.call(f"c{n:04d}-fix", f"{step}: call {attempt}", system, user)
                if res is None:
                    continue
                bad = self.unexpected(res.text, bi, {"PATCH": n})
                if bad:
                    feedback = (bad, res.text)
                    self.say(f"{step}: rejected: {bad}")
                    continue
                ir = self.import_reply(bi, res.text, replace=False, model=res.model, what=f"fix chapter {n}")
                if ir.failure:
                    feedback = (ir.failure, res.text)
                    self.say(f"{step}: not imported: {ir.failure}")
                    continue
                outs = ir.outcomes("PATCH", n)
                if outs and not outs[0].rejected:
                    self.say(f"{step}: call {attempt} ... imported ({outs[0].summary}; "
                             f"{plural(len(outs[0].warnings), 'warning')})")
                    done = True
                    break
                d = ir.decision
                if d.kind == "fix_first":
                    self.say(f"{step}: cannot go on: {d.text}")
                    break
                feedback = (d.text, res.text)
                self.say(f"{step}: rejected: {(d.fixes or [d.text])[0][:160]}")
        except RunStop:
            pass
        u = self.client.usage
        self.out(f"Gemini calls: {self.client.calls}; tokens in {u['prompt']:,}, out {u['output']:,}; "
                 f"rate-limit switches: {self.client.rate_switches}")
        self.out(f"Run log: {self.log.path}")
        if self.fatal:
            self.out(f"STOPPED: {self.fatal}")
        if done:
            self.out(f"Next: novel qa --chapters {n}")
            return 0
        self.out(f"The PATCH was not accepted. Nothing was changed. Try again, or paste it by hand: "
                 f"novel qa --fix-prompt {n}")
        return 1


def first_of(s) -> int:
    return min(s) if s else 0


def last_of(s) -> int:
    return max(s) if s else 0
