"""`novel import`: file Gemini's replies into the pipeline.

One run takes a reply (clipboard or --file) or the files saved by hand in <book>/inbox/ (--inbox):

1. Archive the raw input, append-only, as <book>/gemini/<batch>/raw/NNN-<UTC stamp>.txt (an identical input,
   same sha256, is reported and not archived again, but is processed again: importing is idempotent).
2. Split it into blocks (novel.gemini.contract) and handle each block in order. A block is accepted whole or
   REJECTED whole: nothing is written for a rejected block.
     BIBLE   novel.bible.apply_lines; saved; inbox copy filed in inbox/_done/.
     IMAGES  validated line by line, merged into the staging file inbox/cNNNN.images.jsonl; when every window
             has its image the chapter's scene file v2 is written and the staging file moves to inbox/_done/.
     PATCH   replaces image lines (by n) and group lines (by group) of an imported chapter; the scene file is
             rewritten and the image files of every changed picture move to images/cNNNN/old/<stamp>/.
     HINDI   recognised, saved to inbox/cNNNN.hindi.txt, not imported yet (a later task adds `do_hindi`).
3. Print the batch progress and the one line to say to Gemini next.

State: <book>/gemini/<batch>/progress.json (rewritten atomically). The by-hand path (--inbox) and the clipboard
path share every step after the block is read.

Later (full mode): `do_hindi` imports the Hindi with its {n} markers, and `chapter_windows_for` gains a second
way of deriving windows (from the markers instead of the paced text windows). Everything else stays.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from novel import bible as B
from novel.gemini import contract as C
from novel.gemini import prompt as P

MAX_SHOWN_TEXT = 3          # the first few errors of a block also show the offending text
MAX_FIX_ITEMS = 8           # errors listed in the "Say to Gemini" FIX list


class ImportFailure(Exception):
    """A problem with the run itself (no reply, no batch); the message says how to fix it."""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def _free(path: Path, stamp: str) -> Path:
    """path, or path with the stamp (and a counter) before its suffixes when it exists."""
    if not path.exists():
        return path
    stem, _, rest = path.name.partition(".")
    for k in range(1, 1000):
        cand = path.with_name(f"{stem}.{stamp}{'' if k == 1 else f'-{k}'}.{rest}" if rest
                              else f"{stem}.{stamp}{'' if k == 1 else f'-{k}'}")
        if not cand.exists():
            return cand
    raise ImportFailure(f"cannot find a free name for {path}")


# ------------------------------------------------------------------ batches

@dataclass
class Batch:
    id: str
    folder: Path
    meta: dict
    progress: dict

    @property
    def chapters(self) -> list[int]:
        return [int(c) for c in self.meta.get("chapters", [])]

    @property
    def mode(self) -> str:
        return self.meta.get("mode", "scenes")

    @property
    def first(self) -> int:
        return int(self.meta["first"])

    @property
    def last(self) -> int:
        return int(self.meta["last"])

    def window_count(self, n: int) -> int | None:
        v = (self.meta.get("windows") or {}).get(str(n))
        return int(v) if v is not None else None

    def chapter(self, n: int) -> dict:
        return self.progress.setdefault("chapters", {}).setdefault(str(n), {})


@dataclass
class Outcome:
    block: C.Block
    batch: Batch | None = None
    status: str = "ok"                       # ok | REJECTED | saved
    summary: str = ""
    errors: list[C.Issue] = field(default_factory=list)
    warnings: list = field(default_factory=list)        # Issue or str
    fix_first: str | None = None             # a command the user must run first (not Gemini's fault)
    fix_for_gemini: list[str] = field(default_factory=list)
    repeat: bool = True                      # a rejection Gemini fixes by repeating the block
    say_instead: str | None = None           # words for Gemini other than "repeat <this block>"

    @property
    def rejected(self) -> bool:
        return self.status == "REJECTED"


@dataclass
class Chapter:
    """What the code knows about one chapter of a scenes-mode batch before any line is read."""
    n: int
    ep_root: Path
    paras: list
    wins: list
    ctx: C.ChapterCtx


class Importer:
    def __init__(self, book: str, *, dry_run: bool = False, replace: bool = False, allow_revise: bool = False,
                 batch: str | None = None, out=print, source_args: str = ""):
        from novel.core import paths, settings
        from novel.stages import episode_scenes as S
        self.S = S
        self.book = book
        self.book_dir = paths.book_dir(book)
        self.gemini_dir = self.book_dir / "gemini"            # computed here on purpose (see the task notes)
        self.inbox = self.book_dir / "inbox"
        self.bible_dir = self.book_dir / "bible"
        self.dry_run, self.replace, self.allow_revise = dry_run, replace, allow_revise
        self.out = out
        self.source_args = source_args
        self.state = B.load_state(self.bible_dir)
        self.max_characters = int(settings.get("images.max_characters", book=book, default=S.MAX_CHARACTERS))
        self.stamp = utc_stamp()
        self.outcomes: list[Outcome] = []
        self.batches: dict[str, Batch] = {}
        self.staged: dict[int, tuple[dict, dict]] = {}        # in-memory staging (dry runs see their own work)
        self.dry_done: dict[int, int] = {}                     # chapters a dry run would have completed
        self.touched: list[str] = []
        self._eps = None
        self.forced = self._norm_batch(batch) if batch else None

    # -------------------------------------------------------------- batch lookup

    @staticmethod
    def _norm_batch(text: str) -> str:
        b = C.bible_id(text)
        if b:
            return b[0]
        try:
            return P.batch_id(*P.parse_chapters(text))
        except P.PromptError:
            raise ImportFailure(f"--batch {text!r} is not a batch id like B021-030")

    def load_batch(self, bid: str) -> Batch | None:
        if bid in self.batches:
            return self.batches[bid]
        folder = self.gemini_dir / bid
        f = folder / "batch.json"
        if not f.exists():
            return None
        meta = json.loads(f.read_text(encoding="utf-8"))
        pf = folder / "progress.json"
        progress = {}
        if pf.exists():
            try:
                progress = json.loads(pf.read_text(encoding="utf-8")) or {}
            except ValueError:
                progress = {}
        progress.setdefault("batch", bid)
        progress.setdefault("bible", {"done": False, "blocks": {}})
        progress.setdefault("chapters", {})
        b = Batch(bid, folder, meta, progress)
        self.batches[bid] = b
        return b

    def batch_for_chapter(self, n: int) -> Batch | None:
        found = []
        for f in self.gemini_dir.glob("*/batch.json"):
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if n in [int(c) for c in meta.get("chapters", [])]:
                found.append((f.stat().st_mtime, f.parent.name))
        if not found:
            return None
        return self.load_batch(max(found)[1])

    def prompt_fix(self, n: int | None = None, rng: tuple[int, int] | None = None) -> str:
        if rng:
            a, z = rng
        else:
            a = z = n
            ep = self.episodes().get(n)
            if ep is not None:
                try:
                    info = json.loads((ep / "episode.json").read_text(encoding="utf-8"))
                    a, z = int(info["start"]), int(info["end"])
                except (ValueError, KeyError, OSError):
                    pass
        return f"novel prompt --chapters {a}-{z}" if a != z else f"novel prompt --chapters {a}"

    def resolve(self, block: C.Block) -> tuple[Batch | None, str | None]:
        """The batch a block belongs to, or (None, message with the fix)."""
        if self.forced:
            b = self.load_batch(self.forced)
            if b is None:
                _, a, z, _ = C.bible_id(self.forced)
                return None, (f"batch {self.forced} has no {self.gemini_dir / self.forced / 'batch.json'}. "
                              f"Fix: novel prompt --chapters {a}-{z}")
            if block.kind == "BIBLE" and block.batch and block.batch != b.id:
                return None, f"the block names batch {block.batch}, but --batch says {b.id}"
            return b, None
        if block.kind == "BIBLE":
            if not block.batch:
                return None, "the BIBLE id names no batch"
            b = self.load_batch(block.batch)
            if b is None:
                a, z = block.batch_range
                return None, (f"no prompt batch {block.batch} (no {self.gemini_dir / block.batch / 'batch.json'}). "
                              f"Fix: novel prompt --chapters {a}-{z}")
            return b, None
        if block.chapter is None:
            return None, "the block names no chapter"
        b = self.batch_for_chapter(block.chapter)
        if b is None:
            return None, (f"chapter {block.chapter} is in no prompt batch (no batch.json under {self.gemini_dir} "
                          f"lists it). Fix: {self.prompt_fix(block.chapter)}")
        return b, None

    def episodes(self) -> dict:
        if self._eps is None:
            self._eps = P.episode_of(self.book_dir)
        return self._eps

    # -------------------------------------------------------------- raw archive

    def archive(self, text: str, batch: Batch | None) -> str:
        folder = (batch.folder if batch else self.gemini_dir / "_unmatched") / "raw"
        data = text.encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        existing = sorted(folder.glob("*.txt")) if folder.exists() else []
        for f in existing:
            if hashlib.sha256(f.read_bytes()).hexdigest() == sha:
                return f"same text as {f} (already archived; processed again)"
        if self.dry_run:
            return "not archived (--dry-run)"
        nums = [int(m.group(1)) for f in existing if (m := re.match(r"(\d+)-", f.name))]
        nxt = (max(nums) + 1) if nums else 1
        path = folder / f"{nxt:03d}-{self.stamp}.txt"
        folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"archived as {path}"

    # -------------------------------------------------------------- entry points

    def run_text(self, text: str, origin: str) -> int:
        if not text or not text.strip():
            raise ImportFailure(f"{origin} is empty. Copy Gemini's whole reply (its copy button) and run "
                                f"novel import again.")
        reply = C.split_reply(text)
        resolved = [(b, *self.resolve(b)) for b in reply.blocks]
        batches = []
        for _, bt, _ in resolved:
            if bt is not None and bt not in batches:
                batches.append(bt)
        if not batches and self.forced:
            bt = self.load_batch(self.forced)
            if bt is not None:
                batches.append(bt)
        if not batches:
            for ch, _ in reply.more:
                bt = self.batch_for_chapter(ch)
                if bt is not None and bt not in batches:
                    batches.append(bt)
        notes = [self.archive(text, bt) for bt in (batches or [None])]
        self.out(f"novel import: book '{self.book}', {origin}" + (" (dry run: nothing is written)" if self.dry_run else ""))
        for nt in notes:
            self.out(f"Raw reply: {nt}")
        if not reply.blocks and not reply.more:
            raise ImportFailure(
                f"no blocks found in {origin} ({reply.total_lines} lines). A Gemini reply holds lines like "
                f"'===== BEGIN IMAGES c0021 -> inbox/c0021.images.jsonl ====='. Copy the whole reply and try again.")
        if reply.ignored:
            self.out(f"Ignored {reply.ignored} line{'s' if reply.ignored != 1 else ''} outside blocks.")
        for nt in reply.notes:
            self.out(f"note: {nt}")
        for ch, kind in reply.more:
            bt = self.batch_for_chapter(ch) if not self.forced else self.load_batch(self.forced)
            if bt is not None:
                bt.chapter(ch)["more_owed"] = True
                bt.chapter(ch)["more_kind"] = kind
                self._touch(bt)
        more = {(ch, kind) for ch, kind in reply.more}
        self.out("")
        for block, bt, err in resolved:
            self.handle(block, bt, err, more_owed=(block.chapter, block.kind) in more)
        for ch, kind in reply.more:
            bt = self.batch_for_chapter(ch) if not self.forced else self.load_batch(self.forced)
            if bt is not None:
                self.out(f">>> MORE {C.block_name('IMAGES', None, ch)} {kind}: Gemini still owes the rest of this block.")
                self.save_progress(bt)
        return self.finish()

    def run_inbox(self) -> int:
        self.out(f"novel import: book '{self.book}', files in {self.inbox}"
                 + (" (dry run: nothing is written)" if self.dry_run else ""))
        files = sorted(p for p in self.inbox.glob("*") if p.is_file()) if self.inbox.exists() else []
        known, skipped = [], []
        for f in files:
            if f.name.endswith(".tmp"):
                continue
            k = C.parse_inbox_name(f.name)
            (known if k else skipped).append((f, k))
        for f, _ in skipped:
            self.out(f"note: {f.name} is not a contract file name (bible-B021-030.jsonl, c0021.images.jsonl, "
                     f"c0021.patch.jsonl, c0021.hindi.txt); left alone")
        if not known:
            self.out("The inbox has no files to import.")
            return 0

        order = {"BIBLE": 0, "HINDI": 1, "IMAGES": 2, "PATCH": 3}

        def sort_key(item):                      # the batch bible, then per chapter: extra bible, HINDI, IMAGES, PATCH
            f, (kind, rid) = item
            if kind == "BIBLE":
                b = C.bible_id(rid)
                return (b[3] if b and b[3] is not None else -1, 0, f.name)
            return (C.chapter_of(rid) or 0, order[kind], f.name)

        self.out("")
        for f, _ in sorted(known, key=sort_key):
            text = f.read_text(encoding="utf-8-sig")
            block = C.inbox_block(f.name, text)
            block.file_hint = str(f)
            bt, err = self.resolve(block)
            note = self.archive(text, bt)
            self.out(f"{f.name}: {note}")
            self.handle(block, bt, err, more_owed=False, path=f)
        return self.finish()

    # -------------------------------------------------------------- per block

    def handle(self, block: C.Block, bt: Batch | None, err: str | None, more_owed: bool, path: Path | None = None):
        o = Outcome(block, bt)
        self.outcomes.append(o)
        try:
            if block.errors:
                o.errors += block.errors
                o.warnings += block.warnings
                cut = any("cut off" in e.msg for e in block.errors)
                o.repeat = True
                if cut:
                    o.fix_for_gemini = []
                else:
                    o.fix_for_gemini = [e.msg for e in block.errors]
                return self.reject(o)
            o.warnings += block.warnings
            if bt is None:
                o.errors.append(C.Issue(None, "", err))
                o.repeat = False
                o.fix_first = err.split("Fix: ", 1)[1] if "Fix: " in err else None
                return self.reject(o)
            if block.chapter is not None and block.chapter not in bt.chapters:
                o.errors.append(C.Issue(None, "", f"chapter {block.chapter} is not in batch {bt.id} "
                                                  f"(chapters {bt.first}-{bt.last})"))
                o.repeat = False
                return self.reject(o)
            self._touch(bt)
            if block.kind == "BIBLE":
                self.do_bible(o, path)
            elif block.kind == "IMAGES":
                self.do_images(o, more_owed, path)
            elif block.kind == "PATCH":
                self.do_patch(o, path)
            elif block.kind == "HINDI":
                self.do_hindi(o, path)
        finally:
            self.report(o)
            if bt is not None and not o.rejected:
                self.save_progress(bt)

    def reject(self, o: Outcome) -> None:
        o.status = "REJECTED"

    def _touch(self, bt: Batch):
        if bt.id not in self.touched:
            self.touched.append(bt.id)

    # -------------------------------------------------------------- BIBLE

    def do_bible(self, o: Outcome, path: Path | None):
        b, bt = o.block, o.batch
        items, errs, warns = C.parse_jsonl(b.lines, b.quoted)
        o.warnings += warns
        canon = sha_text("\n".join(C.canonical(it.obj) for it in items))
        prev = bt.progress["bible"]["blocks"].get(b.name)
        if prev and prev.get("sha") == canon and not errs:
            o.summary = "already imported (identical); nothing to do"
            self._file_away(b, path, "\n".join(b.lines))
            return
        trial = copy.deepcopy(self.state)
        allow = self.allow_revise or bool(bt.meta.get("review"))
        aerrs, awarns = B.apply_lines(trial, [it.obj for it in items], bt.first, bt.last, allow)
        o.errors += errs
        for m in aerrs:
            iss = self._bible_issue(m, items)
            if "revise is not allowed" in m:
                iss.msg += " (this batch did not ask for revise lines; run with --allow-revise to accept them)"
            o.errors.append(iss)
        o.warnings += [self._bible_issue(m, items) for m in awarns]
        if o.errors:
            o.fix_for_gemini = [f"{e.what + ': ' if e.what else ''}{e.msg}" for e in o.errors]
            return self.reject(o)
        counts: dict[str, int] = {}
        for it in items:
            counts[it.obj.get("type")] = counts.get(it.obj.get("type"), 0) + 1
        self.state = trial
        o.summary = (", ".join(f"{v} {k}{'s' if v != 1 else ''}" for k, v in counts.items()) or "nothing new") + \
            (" applied to the bible" if counts else "")
        bt.progress["bible"]["blocks"][b.name] = {"sha": canon, "imported": utc_iso(), "lines": len(items)}
        if b.chapter is None:
            bt.progress["bible"]["done"] = True     # in memory; a dry run never saves progress
        if self.dry_run:
            return
        if counts:
            B.save_state(self.bible_dir, self.state)
        self._file_away(b, path, "\n".join(b.lines))

    @staticmethod
    def _bible_issue(msg: str, items: list) -> C.Issue:
        m = re.match(r"^line (\d+)(?: \((.*?)\))?: (.*)$", msg)
        if not m:
            return C.Issue(None, "", msg)
        k = int(m.group(1))
        it = items[k - 1] if 0 < k <= len(items) else None
        return C.Issue(it.line if it else None, (m.group(2) or "").strip(), m.group(3),
                       C.excerpt(json.dumps(it.obj, ensure_ascii=False)) if it else "")

    def _file_away(self, b: C.Block, path: Path | None, body: str):
        """Keep what arrived under its contract name, filed in inbox/_done/ (never deleted)."""
        if self.dry_run:
            return
        if path is not None:
            self._to_done(path)
            return
        dest = _free(self.inbox / "_done" / b.inbox_name, self.stamp)
        write_atomic(dest, body.strip("\n") + "\n")

    def _to_done(self, path: Path) -> Path:
        dest = _free(self.inbox / "_done" / path.name, self.stamp)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, dest)
        return dest

    # -------------------------------------------------------------- chapters

    def chapter_windows_for(self, bt: Batch, n: int) -> tuple[Chapter | None, str | None]:
        """The chapter's windows as `novel prompt` computed them, re-checked against the files now.
        Scenes mode only; full mode (windows from the HINDI markers) is the later HINDI task."""
        S = self.S
        from novel.stages import episode as E
        ep = self.episodes().get(n)
        fix = self.prompt_fix(rng=(bt.first, bt.last))
        if ep is None:
            return None, (f"chapter {n} belongs to no episode (no episodes/E*/episode.json covers it). "
                          f"Fix: add the episode, then {fix}")
        try:
            E.check_lock(ep, n)
        except SystemExit as e:
            return None, f"chapter {n}: {e}. Fix the Hindi or its lock, then: {fix}"
        hf = ep / "hindi" / f"{n:05d}.txt"
        want = (bt.meta.get("hindi_sha256") or {}).get(str(n))
        if P.sha256(hf) != want:
            return None, (f"the Hindi of chapter {n} changed after `novel prompt` made batch {bt.id}, so the windows "
                          f"Gemini numbered may have moved. Fix: {fix} (then paste the new prompt into a new Gemini chat)")
        params = bt.meta.get("params") or {}
        now = {"paced_seconds": S.PACED_SECONDS, "max_seconds": S.MAX_SECONDS,
               "chars_per_second": S.CHARS_PER_SECOND, "pacing": S.PACING}
        if params != now:
            return None, (f"the window settings changed after `novel prompt` made batch {bt.id} "
                          f"({params} then, {now} now). Fix: {fix}")
        paras = S.chapter_paras(ep, n)
        wins = S.chapter_windows(paras, S.PACING)
        if len(wins) != bt.window_count(n):
            return None, (f"chapter {n} now cuts into {len(wins)} windows, batch {bt.id} says {bt.window_count(n)}. "
                          f"Fix: {fix}")
        ctx = C.ChapterCtx(n, len(wins), self.state, self.max_characters, S.BANNED)
        return Chapter(n, ep, paras, wins, ctx), None

    def staging_path(self, n: int) -> Path:
        return self.inbox / f"c{n:04d}.images.jsonl"

    def read_lines(self, lines: list[str], ctx: C.ChapterCtx, quoted=False, where: str = ""):
        """Parse and validate image/group lines. -> (groups{id: (norm, line)}, images{n: (norm, line)},
        errors[Issue], warnings[Issue])."""
        items, errs, warns = C.parse_jsonl(lines, quoted)
        groups, images = {}, {}
        for it in items:
            label = C.line_label(it.obj)
            kind, norm, lerrs, lwarns = C.check_line(it.obj, ctx)
            text = C.excerpt(json.dumps(it.obj, ensure_ascii=False))
            for f, m in lerrs:
                what = f"{label} {f}".strip() if not f.startswith(label) else f
                errs.append(C.Issue(it.line, (where + what).strip(), m, text))
            for w in lwarns:
                warns.append(C.Issue(it.line, label, w))
            if norm is None:
                continue
            if norm.get("_extra"):
                warns.append(C.Issue(it.line, label, f"unknown fields ignored: {', '.join(norm['_extra'])}"))
            target, key = (groups, norm["group"]) if kind == "group" else (images, norm["n"])
            if key in target:
                if C.canonical(target[key][0]) == C.canonical(norm):
                    warns.append(C.Issue(it.line, label, f"the same line again (first at line {target[key][1]}); "
                                                         f"ignored"))
                else:
                    errs.append(C.Issue(it.line, label, f"{label} appears twice in this block with different "
                                                        f"content (first at line {target[key][1]})", text))
                continue
            target[key] = (norm, it.line)
        return groups, images, errs, warns

    def load_staging(self, n: int, ctx: C.ChapterCtx) -> tuple[dict, dict, list]:
        if n in self.staged:
            g, i = self.staged[n]
            return dict(g), dict(i), []
        p = self.staging_path(n)
        if not p.exists():
            return {}, {}, []
        lines = p.read_text(encoding="utf-8-sig").replace("\r\n", "\n").split("\n")
        groups, images, errs, _ = self.read_lines(lines, ctx)
        if errs:
            errs = [C.Issue(None, "", f"the staged file {p} has errors ({errs[0].render()}"
                                      f"{f' and {len(errs) - 1} more' if len(errs) > 1 else ''}); fix it by hand or "
                                      f"move it out of the inbox")]
        return {k: v[0] for k, v in groups.items()}, {k: v[0] for k, v in images.items()}, errs

    def write_staging(self, n: int, groups: dict, images: dict):
        self.staged[n] = (dict(groups), dict(images))
        if self.dry_run:
            return
        write_atomic(self.staging_path(n), self.staging_text(groups, images))

    @staticmethod
    def staging_text(groups: dict, images: dict) -> str:
        out, done = [], set()
        for n in sorted(images):
            g = images[n]["group"]
            if g in groups and g not in done:
                out.append(C.dump_line(groups[g]))
                done.add(g)
            out.append(C.dump_line(images[n]))
        out += [C.dump_line(groups[g]) for g in groups if g not in done]
        return "\n".join(out) + "\n"

    @staticmethod
    def doc_lines(doc: dict) -> tuple[dict, dict]:
        groups = {gid: {"type": "group", "group": gid, "loc": g.get("loc"), "time": g.get("time"),
                        "light": g.get("light"), "weather": g.get("weather", ""), "present": g.get("present", []),
                        "staging": g.get("staging")}
                  for gid, g in (doc.get("groups") or {}).items()}
        images = {s["id"]: {"type": "image", "n": s["id"], "group": s.get("group"), "shot": s.get("shot"),
                            "cast": s.get("cast", []), "others": s.get("others", ""), "action": s.get("action", ""),
                            "focus": [float(x) for x in s.get("focus", [0.5, 0.5])]}
                  for s in doc.get("scenes", [])}
        return groups, images

    def scenes_file(self, ch: Chapter) -> Path:
        return self.S.scenes_path(ch.ep_root, ch.n)

    def read_doc(self, ch: Chapter) -> dict | None:
        p = self.scenes_file(ch)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return {"version": None}

    @staticmethod
    def image_files(ep_root: Path, n: int) -> list[Path]:
        d = ep_root / "images" / f"c{n:04d}"
        return sorted(d.glob("s*-2k.jpg")) if d.exists() else []

    # -------------------------------------------------------------- IMAGES

    def do_images(self, o: Outcome, more_owed: bool, path: Path | None):
        b, bt = o.block, o.batch
        n = b.chapter
        if bt.mode != "scenes":
            o.status = "saved"
            o.summary = ("not imported: this is a full-mode batch, whose windows come from the HINDI markers, and the "
                         "HINDI import is not built yet")
            self._save_inbox(b, path)
            return
        ch, fatal = self.chapter_windows_for(bt, n)
        if fatal:
            o.errors.append(C.Issue(None, "", fatal))
            o.repeat = False
            o.fix_first = fatal.split("Fix: ", 1)[1] if "Fix: " in fatal else None
            return self.reject(o)
        groups_new, images_new, errs, warns = self.read_lines(b.lines, ch.ctx, b.quoted)
        o.errors += errs
        o.warnings += warns

        doc = self.read_doc(ch)
        imported = bool(doc and doc.get("version") == 2)
        new_g = {k: v[0] for k, v in groups_new.items()}
        new_i = {k: v[0] for k, v in images_new.items()}
        if imported and not o.errors:
            dg, di = self.doc_lines(doc)
            same = all(k in di and C.canonical(di[k]) == C.canonical(v) for k, v in new_i.items()) and \
                all(k in dg and C.canonical(dg[k]) == C.canonical(v) for k, v in new_g.items())
            if same:
                o.summary = f"already imported (identical to {self.scenes_file(ch)}); nothing to do"
                left = path if path is not None else self.staging_path(n)
                if left.exists() and not self.dry_run:    # e.g. a run stopped between the scene file and filing
                    self._to_done(left)
                return
        if doc is not None and not self.replace:
            imgs = self.image_files(ch.ep_root, n)
            if imported or imgs:
                why = (f"chapter {n} was already imported ({self.scenes_file(ch)})" if imported else
                       f"chapter {n} already has a scene file ({self.scenes_file(ch)})")
                if imgs:
                    why += f" and {len(imgs)} image{'s' if len(imgs) != 1 else ''}"
                fix = f"novel import --replace{(' ' + self.source_args) if self.source_args else ''}"
                o.errors.append(C.Issue(None, "", f"{why}. To replace them run: {fix} (the old files move to "
                                                  f"old/vN/, nothing is deleted). To change a few pictures, ask "
                                                  f"Gemini for a PATCH instead."))
                o.repeat = False
                o.fix_first = fix
                return self.reject(o)

        if path is not None or (imported and self.replace):
            sg, si, serrs = {}, {}, []                  # the inbox file IS the staging / --replace starts afresh
        else:
            sg, si, serrs = self.load_staging(n, ch.ctx)
        if serrs:
            o.errors += serrs
            o.repeat = False
            return self.reject(o)
        if 1 in new_i and 1 in si and C.canonical(si[1]) != C.canonical(new_i[1]) and not self.replace:
            o.warnings.append(f"this block starts again at image 1: the {len(si)} image lines received earlier for "
                              f"c{n:04d} are set aside (filed in inbox/_done/)")
            if not self.dry_run and self.staging_path(n).exists():
                dest = _free(self.inbox / "_done" / f"c{n:04d}.images.superseded.jsonl", self.stamp)
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(self.staging_path(n), dest)
            self.staged.pop(n, None)
            sg, si = {}, {}
        for key, line in new_g.items():
            if key in sg and C.canonical(sg[key]) != C.canonical(line) and not self.replace:
                o.errors.append(C.Issue(groups_new[key][1], f"group {key}",
                                        f"group {key} was already received in an earlier part with different "
                                        f"content (use --replace to take the new one)"))
        for key, line in new_i.items():
            if key in si and C.canonical(si[key]) != C.canonical(line) and not self.replace:
                o.errors.append(C.Issue(images_new[key][1], f"image {key}",
                                        f"image {key} was already received in an earlier part with different "
                                        f"content (after the import, change it with a PATCH; or use --replace)",
                                        C.excerpt(C.dump_line(line))))
        groups = {**sg, **new_g}
        images = {**si, **new_i}
        for key, line in new_i.items():
            if line["group"] not in groups:
                o.errors.append(C.Issue(images_new[key][1], f"image {key} group",
                                        f"unknown group '{line['group']}': no group line for it in this block or an "
                                        f"earlier part (a group line comes before its images)"))
        missing = C.missing_windows(images, ch.ctx.count)
        if o.errors:
            o.fix_for_gemini = [f"{e.what + ': ' if e.what else ''}{e.msg}" for e in o.errors]
            return self.reject(o)
        if missing and b.origin == "reply" and not more_owed:
            o.errors.append(C.Issue(None, "", f"too few images: windows {C.ranges_text(missing)} have no image line "
                                              f"({len(images)} of {ch.ctx.count} received) and the reply has no "
                                              f"'>>> MORE c{n:04d} IMAGES' line"))
            o.fix_for_gemini = [f"chapter {n} has {ch.ctx.count} windows; image lines are missing for windows "
                                f"{C.ranges_text(missing)}"]
            return self.reject(o)

        got = len(new_i)
        if missing:
            if path is None:
                self.write_staging(n, groups, images)
            else:
                self.staged[n] = (groups, images)    # the inbox file already holds them
            o.summary = (f"{got} image line{'s' if got != 1 else ''}, {len(new_g)} group{'s' if len(new_g) != 1 else ''}; "
                         f"chapter {n}: {len(images)} of {ch.ctx.count} images (waiting for "
                         f"{C.ranges_text(missing)})")
            bt.chapter(n)["more_owed"] = more_owed or bt.chapter(n).get("more_owed", False)
            return
        self.complete_chapter(o, ch, groups, images, path)

    def complete_chapter(self, o: Outcome, ch: Chapter, groups: dict, images: dict, path: Path | None):
        bt, n = o.batch, ch.n
        used = {im["group"] for im in images.values()}
        for gid in groups:
            if gid not in used:
                o.warnings.append(f"group {gid} is not used by any image")
        o.warnings += C.chapter_warnings(groups, images, ch.ctx)
        doc = self.build_doc(bt, ch, {g: v for g, v in groups.items() if g in used}, images)
        target = self.scenes_file(ch)
        o.summary = (f"chapter {n} complete: {len(images)} images in {len(used)} group{'s' if len(used) != 1 else ''} "
                     f"-> {target}")
        self.staged.pop(n, None)
        if self.dry_run:
            self.dry_done[n] = len(images)
            return
        moved = self.version_old(ch)
        if moved:
            o.warnings.append(f"the old files of chapter {n} moved to {moved}")
        write_atomic(target, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        problems = self.S.check_chapter(ch.ep_root, n)
        for p_ in problems:
            o.warnings.append(f"scene check: {p_}")
        if path is not None:
            self._to_done(path)
        elif self.staging_path(n).exists():
            self._to_done(self.staging_path(n))
        else:
            dest = _free(self.inbox / "_done" / f"c{n:04d}.images.jsonl", self.stamp)
            write_atomic(dest, self.staging_text(groups, images))
        c = bt.chapter(n)
        c.update({"status": "done", "imported": utc_iso(), "more_owed": False,
                  "scenes": str(target.relative_to(self.book_dir)).replace("\\", "/")})

    def build_doc(self, bt: Batch, ch: Chapter, groups: dict, images: dict) -> dict:
        from novel.stages import episode as E
        lock = json.loads(E.lock_path(ch.ep_root).read_text(encoding="utf-8"))
        params = {k: v for k, v in (bt.meta.get("params") or {}).items() if k != "pacing"}
        order = []
        for k in sorted(images):
            g = images[k]["group"]
            if g not in order:
                order.append(g)
        scenes = []
        for k in range(1, ch.ctx.count + 1):
            im = images[k]
            scenes.append({"id": k, "para": ch.wins[k - 1][0], "group": im["group"], "shot": im["shot"],
                           "cast": im["cast"], "others": im["others"], "action": im["action"],
                           "focus": im["focus"], "characters": [c["key"] for c in im["cast"]],
                           "scene": im["action"]})
        return {"version": 2, "chapter": ch.n, "pacing": self.S.PACING, "pacing_params": params,
                "hindi_sha256": lock[f"{ch.n:05d}.txt"],
                "source": {"batch": bt.id, "imported": utc_iso()},
                "groups": {g: {k: v for k, v in groups[g].items() if k not in ("type", "group") and not k.startswith("_")}
                           for g in order},
                "scenes": scenes}

    def version_old(self, ch: Chapter) -> Path | None:
        """Move the chapter's scene file, brief and image folder into old/vN/ (as cmd_draft does). Never deletes."""
        ep, n = ch.ep_root, ch.n
        name = f"{n:05d}"
        rels = [f"scenes/{name}.json", f"scenes/{name}.brief.md", f"images/c{n:04d}"]
        present = [r for r in rels if (ep / r).exists()]
        if not present:
            return None
        k = 1
        while any((ep / "old" / f"v{k}" / r).exists() for r in rels):
            k += 1
        old = ep / "old" / f"v{k}"
        for rel in present:
            src, dst = ep / rel, old / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(src, dst)
            except PermissionError:
                if src.is_file():
                    shutil.copy2(src, dst)
                else:
                    shutil.copytree(src, dst)
                self.out(f"  note: {rel} is open in another program - copied to {old}, not moved")
        return old

    # -------------------------------------------------------------- PATCH

    def do_patch(self, o: Outcome, path: Path | None):
        b, bt = o.block, o.batch
        n = b.chapter
        if bt.mode != "scenes":
            o.status = "saved"
            o.summary = "not imported: PATCH for a full-mode batch needs the HINDI import (not built yet)"
            self._save_inbox(b, path)
            return
        ch, fatal = self.chapter_windows_for(bt, n)
        if fatal:
            o.errors.append(C.Issue(None, "", fatal))
            o.repeat = False
            o.fix_first = fatal.split("Fix: ", 1)[1] if "Fix: " in fatal else None
            return self.reject(o)
        doc = self.read_doc(ch)
        if not doc or doc.get("version") != 2:
            o.errors.append(C.Issue(None, "", f"chapter {n} has not been imported yet (no scene file v2 at "
                                              f"{self.scenes_file(ch)}): a PATCH changes an imported chapter"))
            o.repeat = False
            o.say_instead = f"repeat c{n:04d} IMAGES"
            return self.reject(o)
        groups_new, images_new, errs, warns = self.read_lines(b.lines, ch.ctx, b.quoted)
        o.errors += errs
        o.warnings += warns
        dg, di = self.doc_lines(doc)
        changed_i = [k for k, v in images_new.items() if C.canonical(di.get(k, {})) != C.canonical(v[0])]
        changed_g = [k for k, v in groups_new.items() if C.canonical(dg.get(k, {})) != C.canonical(v[0])]
        for k in set(images_new) - set(changed_i):
            o.warnings.append(f"image {k}: the same as the imported line; no change")
        groups = {**dg, **{k: v[0] for k, v in groups_new.items()}}
        images = {**di, **{k: v[0] for k, v in images_new.items()}}
        for k, (line, no) in images_new.items():
            if line["group"] not in groups:
                o.errors.append(C.Issue(no, f"image {k} group", f"unknown group '{line['group']}': no group line "
                                                                f"for it in this PATCH or the imported chapter"))
        if o.errors:
            o.fix_for_gemini = [f"{e.what + ': ' if e.what else ''}{e.msg}" for e in o.errors]
            return self.reject(o)
        affected = sorted(set(changed_i) | {k for k, im in images.items() if im["group"] in changed_g})
        if not affected and not changed_g:
            o.summary = "no change (every line is the same as the imported chapter)"
            if path is not None and not self.dry_run:
                self._to_done(path)
            return
        o.warnings += C.chapter_warnings(groups, images, ch.ctx)
        used = {im["group"] for im in images.values()}
        new_doc = self.build_doc(bt, ch, {g: v for g, v in groups.items() if g in used}, images)
        new_doc["source"] = dict(doc.get("source") or {})
        new_doc["source"].setdefault("patched", []).append({"at": utc_iso(), "images": affected})
        if changed_g:
            o.warnings.append(f"group{'s' if len(changed_g) > 1 else ''} {', '.join(changed_g)} changed: every image "
                              f"in {'them' if len(changed_g) > 1 else 'it'} is regenerated")
        o.summary = (f"chapter {n}: {len(changed_i)} image line{'s' if len(changed_i) != 1 else ''} and "
                     f"{len(changed_g)} group line{'s' if len(changed_g) != 1 else ''} replaced; "
                     f"pictures to make again: {C.ranges_text(affected) or 'none'}")
        if self.dry_run:
            return
        moved = self.move_images(ch, affected)
        if moved:
            o.warnings.append(f"{moved[1]} existing image file{'s' if moved[1] != 1 else ''} moved to {moved[0]}")
        target = self.scenes_file(ch)
        hist = target.parent / "history" / f"{target.stem}-{self.stamp}.json"
        hist = _free(hist, self.stamp)
        hist.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, hist)
        write_atomic(target, json.dumps(new_doc, ensure_ascii=False, indent=1) + "\n")
        for p_ in self.S.check_chapter(ch.ep_root, n):
            o.warnings.append(f"scene check: {p_}")
        self._file_away(b, path, "\n".join(b.lines))
        c = bt.chapter(n)
        c.setdefault("patches", []).append({"at": utc_iso(), "images": affected})

    def move_images(self, ch: Chapter, ids: list[int]) -> tuple[Path, int] | None:
        d = ch.ep_root / "images" / f"c{ch.n:04d}"
        if not d.exists() or not ids:
            return None
        dest = d / "old" / self.stamp
        count = 0
        folders = [d] + [p for p in d.iterdir() if p.is_dir() and p.name != "old"]
        for k in ids:
            sid = f"s{k:02d}"
            for folder in folders:
                for f in list(folder.glob(f"{sid}.*")) + list(folder.glob(f"{sid}-*")):
                    if not f.is_file():
                        continue
                    rel = f.relative_to(d)
                    (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(f, dest / rel)
                    count += 1
        return (dest, count) if count else None

    # -------------------------------------------------------------- HINDI (not built yet)

    def do_hindi(self, o: Outcome, path: Path | None):
        """Recognised and kept; the import itself is a later task (it will validate the heading, the
        paragraphs and the {n} markers, then write hindi/NNNNN.txt and the marker windows)."""
        o.status = "saved"
        where = self._save_inbox(o.block, path)
        o.summary = f"HINDI import is not built yet: the text is kept in {where}"

    def _save_inbox(self, b: C.Block, path: Path | None) -> Path:
        if path is not None:
            return path
        dest = self.inbox / b.inbox_name
        if self.dry_run:
            return dest
        body = "\n".join(b.lines).strip("\n") + "\n"
        if dest.exists() and dest.read_text(encoding="utf-8") != body:
            self._to_done(dest)
        write_atomic(dest, body)
        return dest

    # -------------------------------------------------------------- progress and the final lines

    def chapter_status(self, bt: Batch, n: int) -> tuple[str, str]:
        """(images text, status) for the progress table, from the files."""
        m = bt.window_count(n)
        of = f"{m}" if m is not None else "?"
        if n in self.dry_done:
            return f"{self.dry_done[n]} of {of}", "done (dry run: not written)"
        ep = self.episodes().get(n)
        if ep is not None:
            p = self.S.scenes_path(ep, n)
            if p.exists():
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                except ValueError:
                    doc = {}
                if doc.get("version") == 2 and (doc.get("source") or {}).get("batch") == bt.id:
                    k = len(doc.get("scenes", []))
                    return f"{k} of {of}", "done"
        if n in self.staged:
            k = len(self.staged[n][1])
        elif self.staging_path(n).exists():
            k = sum(1 for line in self.staging_path(n).read_text(encoding="utf-8-sig").splitlines()
                    if '"type":"image"' in line.replace(" ", ""))
        else:
            k = 0
        if bt.mode != "scenes":
            if (self.inbox / f"c{n:04d}.hindi.txt").exists():
                return "-", "hindi saved (import not built yet)"
            return "-", "waiting"
        if k:
            owed = bt.chapter(n).get("more_owed")
            return f"{k} of {of}", "partial (Gemini owes the rest)" if owed else "partial"
        return f"0 of {of}", "waiting"

    def save_progress(self, bt: Batch):
        if self.dry_run:
            return
        prog = bt.progress
        prog["updated"] = utc_iso()
        prog["mode"] = bt.mode
        for n in bt.chapters:
            imgs, status = self.chapter_status(bt, n)
            c = bt.chapter(n)
            c["status"] = status.split(" ")[0]
            c["images"] = imgs
        write_atomic(bt.folder / "progress.json", json.dumps(prog, ensure_ascii=False, indent=2) + "\n")

    def report(self, o: Outcome):
        b = o.block
        head = b.label if b.kind in C.KINDS else f"{b.kind} {b.raw_id}"
        if o.rejected:
            self.out(f"{head}  REJECTED ({len(o.errors)} error{'s' if len(o.errors) != 1 else ''})")
            for k, e in enumerate(o.errors):
                self.out("  " + e.render(with_text=k < MAX_SHOWN_TEXT))
        else:
            word = "ok" if o.status == "ok" else o.status
            self.out(f"{head}  {word}" + (f": {o.summary}" if o.summary else ""))
        for w in o.warnings:
            self.out("  warning: " + (w.render() if isinstance(w, C.Issue) else w))

    def say(self, bt: Batch | None) -> str:
        firsts = [o.fix_first for o in self.outcomes if o.rejected and o.fix_first]
        if firsts:
            return f"nothing yet. First run: {firsts[0]}"
        rejected = [o for o in self.outcomes if o.rejected]
        instead = [o.say_instead for o in rejected if o.say_instead]
        repeats = []
        for o in rejected:
            if o.repeat and o.block.kind in C.KINDS and (o.block.batch or o.block.chapter is not None):
                if o.block.say_repeat not in repeats:
                    repeats.append(o.block.say_repeat)
        if repeats or instead:
            text = "; ".join(instead + (["repeat " + " and ".join(repeats)] if repeats else []))
            fixes = []
            for o in rejected:
                for f in o.fix_for_gemini:
                    if f not in fixes:
                        fixes.append(f)
            if fixes:
                shown = fixes[:MAX_FIX_ITEMS]
                more = f"; and {len(fixes) - len(shown)} more" if len(fixes) > len(shown) else ""
                text += ". FIX: " + "; ".join(shown) + more
            return text
        if rejected:
            return "nothing yet: fix the problems above, then run novel import again"
        if bt is None:
            return "nothing yet"
        if not bt.progress["bible"].get("done"):
            return f"repeat BIBLE {bt.id}"
        states = {n: self.chapter_status(bt, n)[1] for n in bt.chapters}
        if any(s.startswith("partial") for s in states.values()):
            return "next"
        pending = [n for n, s in states.items() if not s.startswith("done")]
        if not pending:
            return f"nothing: batch {bt.id} is complete. Next: novel qa --chapters {bt.first}-{bt.last}"
        started = [n for n, s in states.items() if s != "waiting"]
        if started and min(pending) < max(started) and states[min(pending)] == "waiting":
            return f"repeat c{min(pending):04d} {'IMAGES' if bt.mode == 'scenes' else 'HINDI'}"
        return "next"

    def finish(self) -> int:
        shown = [self.batches[b] for b in self.touched if b in self.batches]
        for bt in shown:
            self.out("")
            self.out(f"Batch progress {bt.id} ({bt.mode} mode)")
            self.out(f"  bible     {'done' if bt.progress['bible'].get('done') else 'not yet'}")
            self.out(f"  {'chapter':<9} {'images':<12} status")
            for n in bt.chapters:
                imgs, status = self.chapter_status(bt, n)
                self.out(f"  {n:<9} {imgs:<12} {status}")
        self.out("")
        self.out(f"Say to Gemini: {self.say(shown[-1] if shown else None)}")
        return 1 if any(o.rejected for o in self.outcomes) else 0
