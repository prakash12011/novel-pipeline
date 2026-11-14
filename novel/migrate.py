"""Copy the old COI Audiobook project into the new per-book layout (copy only, idempotent).

The old project is only ever read. Every file written lands in <book>/ (see paths.book_dir) and is
recorded in <book>/migration.json (source, target, size, sha256). A target file that already exists
with different content stops the run before anything is written.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from novel import bible as B
from novel.core import paths
from novel.stages import extract as ex

RESERVE_BYTES = 5 * 1024 ** 3          # free space kept on top of what is copied
CHUNK = 1 << 20
FINISHED = (1, 2)                      # published: kept as they are
REDO = (3, 4, 5, 6)                    # new image flow: old scenes/images archived
EXTRA_EPISODE = "E900"
ARCHIVE = Path("_archive") / "v1-old-flow"
PLACES = ["sefirah_castle"]            # old "characters" that are really places
VOLUMES = [[1, 500, 1], [501, 1000, 2], [1001, 1180, 3]]
TEXT_SUFFIXES = {".txt", ".json", ".ass", ".srt", ".log", ".md", ".yaml", ".yml"}
SMALL_TEXT_MAX = 1024 * 1024
OLD_CHANNEL, NEW_CHANNEL = "Manga Aur Novel", "Manga and Novel"
FLOW_MODEL = "Nano Banana Pro"
KAGGLE_PROFILE = "prakash"

REDO_COPY = ["episode.json", "manifest.json", "chapters", "hindi", "hindi.lock.json", "qa", "audio",
             "timing", "captions", "tts-state.json", "glossary.json", "names.json"]
REDO_ARCHIVE = ["scenes", "images", "flow", "video", "bible", "cast-sheet.md", "gemini-prompt.txt"]

BOOK_FOLDERS = ["source", "chapters", "bible", "bible/flow", "inbox", "gemini", "research", "extras",
                "episodes", "_work/kaggle"]


class MigrateError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def fmt_bytes(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def posix(p) -> str:
    return str(p).replace("\\", "/")


def rename_channel(value):
    if isinstance(value, str):
        return value.replace(OLD_CHANNEL, NEW_CHANNEL)
    if isinstance(value, list):
        return [rename_channel(v) for v in value]
    if isinstance(value, dict):
        return {k: rename_channel(v) for k, v in value.items()}
    return value


def expected_chapter_bytes(ch: ex.Chapter) -> bytes:
    """What write_chapters leaves on disk on this OS (text mode translates newlines)."""
    return ch.text().replace("\n", os.linesep).encode("utf-8")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".migrating")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class _Dumper(yaml.SafeDumper):
    pass


def _str_rep(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


def _list_rep(dumper, data):
    flow = all(isinstance(x, (int, float)) for x in data) or \
        (data and all(isinstance(x, list) and all(isinstance(y, int) for y in x) for x in data))
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=bool(flow))


_Dumper.add_representer(str, _str_rep)
_Dumper.add_representer(list, _list_rep)


def dump_yaml(data: dict) -> bytes:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                     default_flow_style=False, width=1000).encode("utf-8")


def dump_json(data) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------- plan model

@dataclass
class Copy:
    src: Path
    dst: Path
    size: int
    status: str = "new"            # new | same | conflict
    sha: str | None = None         # source hash once known


@dataclass
class Gen:
    dst: Path
    data: bytes
    label: str
    status: str = "new"


@dataclass
class Step:
    key: str
    title: str
    copies: list[Copy] = field(default_factory=list)
    gens: list[Gen] = field(default_factory=list)
    skipped: list[tuple[str, int, int]] = field(default_factory=list)   # (what, files, bytes)
    notes: list[str] = field(default_factory=list)
    dirs: list[Path] = field(default_factory=list)       # empty source folders to create
    chapters: list[ex.Chapter] = field(default_factory=list)
    chapters_dir: Path | None = None
    chapters_new: int = 0
    chapters_same: int = 0

    def new_bytes(self) -> int:
        n = sum(c.size for c in self.copies if c.status == "new")
        n += sum(len(g.data) for g in self.gens if g.status == "new")
        n += sum(len(expected_chapter_bytes(c)) for c in self.chapters if self._chapter_new(c))
        return n

    def _chapter_new(self, c) -> bool:
        d = self.chapters_dir / c.filename
        return (not d.exists()) or d.read_text(encoding="utf-8") != c.text()

    def new_files(self) -> int:
        return (sum(1 for c in self.copies if c.status == "new")
                + sum(1 for g in self.gens if g.status == "new") + self.chapters_new)

    def same_files(self) -> int:
        return (sum(1 for c in self.copies if c.status == "same")
                + sum(1 for g in self.gens if g.status == "same") + self.chapters_same)


@dataclass
class Plan:
    old: Path
    book: Path
    with_media: bool
    steps: list[Step] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)        # findings about the old data
    unclassified: list[tuple[str, int]] = field(default_factory=list)
    chapter_mismatches: list[str] = field(default_factory=list)
    chapters_verified: int = 0


def tree_size(p: Path) -> tuple[int, int]:
    if p.is_file():
        return 1, p.stat().st_size
    n = s = 0
    for r, _, fs in os.walk(p):
        for f in fs:
            n += 1
            s += os.path.getsize(os.path.join(r, f))
    return n, s


def add_tree(step: Step, src: Path, dst: Path, keep=None) -> None:
    """Add every file under src to the step (dst mirrors it). keep(rel_posix, size) -> bool filters."""
    if src.is_file():
        step.copies.append(Copy(src, dst, src.stat().st_size))
        return
    for root, dirs, files in os.walk(src):
        dirs.sort()
        rel_root = Path(root).relative_to(src)
        if not files and not dirs:
            step.dirs.append(dst / rel_root)
        for f in sorted(files):
            sp = Path(root) / f
            size = sp.stat().st_size
            if keep is not None and not keep(posix(rel_root / f), size):
                continue
            step.copies.append(Copy(sp, dst / rel_root / f, size))


# ---------------------------------------------------------------- reading the old project

def ep_name(n: int) -> str:
    return f"E{n:03d}"


def old_episodes(old: Path) -> Path:
    return old / "workspace" / "episodes"


def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def old_voice(old: Path) -> str | None:
    cfg = old / "config.yaml"
    if not cfg.exists():
        return None
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    node = (data.get("tts") or {}).get("maya2") or {}
    return node.get("voice")


def author_from_disclaimer(paragraphs) -> str | None:
    m = re.search(r"original author,\s*(.+?),\s*and the respective", " ".join(paragraphs))
    return m.group(1).strip() if m else None


def parse_publish(skill_md: str) -> dict:
    """title / description / tags / hashtags templates from the coi-episode-publish SKILL.md."""
    def block(label: str) -> str | None:
        m = re.search(r"\*\*" + label + r"[^*\n]*\*\*[^\n]*\n```[^\n]*\n(.*?)\n```", skill_md, re.S)
        return m.group(1) if m else None

    out: dict = {}
    title = block("Title")
    desc = block("Description")
    tags = block("Tags")
    if title:
        out["title_template"] = title.strip()
    if desc:
        out["description_template"] = desc.rstrip()
        last = [ln for ln in desc.splitlines() if ln.strip()][-1].strip()
        if last.startswith("#"):
            out["hashtags"] = last.split()
    if tags:
        out["tags"] = [t.strip() for t in tags.replace("\n", " ").split(",") if t.strip()]
    return out


def old_disclaimer(old: Path) -> tuple[str, list[str]] | None:
    """The literal (heading, paragraphs) returned by the OLD abpipe/episode_join.disclaimer_text.
    Read from the old source with ast (nothing imported or run). The new repo's disclaimer_text now
    builds its text from settings, so it cannot supply the old wording."""
    f = old / "abpipe" / "episode_join.py"
    if not f.exists():
        return None
    for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == "disclaimer_text":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and sub.value is not None:
                    heading, paras = ast.literal_eval(sub.value)
                    return heading, list(paras)
    return None


def old_extras(old: Path) -> dict:
    """From the old abpipe/episode_extras.py: SPOKEN (module-level list) and the four literal lines
    card_image() draws on the subscribe card (heading, button, reactions, tagline; the channel name is
    a variable there and is drawn separately). The old CARD_TEXT constant is only the narrator's text
    in English, not what the card shows, so it is not used."""
    f = old / "abpipe" / "episode_extras.py"
    out: dict = {}
    if not f.exists():
        return out
    tree = ast.parse(f.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)                 and node.targets[0].id == "SPOKEN":
            out["SPOKEN"] = ast.literal_eval(node.value)
        if isinstance(node, ast.FunctionDef) and node.name == "card_image":
            lines = []
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "text"                         and len(sub.args) >= 2 and isinstance(sub.args[1], ast.Constant)                         and isinstance(sub.args[1].value, str):
                    lines.append((sub.lineno, sub.args[1].value))
            out["CARD_LINES"] = [t for _, t in sorted(lines)]
    return out


def build_book_yaml(old: Path, missing: list[str]) -> dict:
    disc = old_disclaimer(old)
    if disc is None:
        raise MigrateError(f"disclaimer_text() not found in {old / 'abpipe' / 'episode_join.py'}")
    heading, paras = disc
    extras = old_extras(old)
    book: dict = {"title": "Circle of Inevitability"}
    author = author_from_disclaimer(paras)
    if author:
        book["author"] = author
    else:
        missing.append("book.author (not found in disclaimer_text())")
    book.update({"slug": "coi", "series_label": "Circle of Inevitability",
                 "chapters_per_episode": 10, "volumes": copy.deepcopy(VOLUMES)})
    doc: dict = {"book": book}

    tts: dict = {}
    voice = old_voice(old)
    if voice:
        tts["voice"] = voice
    else:
        missing.append("tts.voice (tts.maya2.voice not found in old config.yaml)")
    tts["kaggle_profile"] = KAGGLE_PROFILE
    doc["tts"] = tts

    images: dict = {}
    e3 = old_episodes(old) / "E003" / "episode.json"
    flow_project = _load_json(e3).get("flow_project") if e3.exists() else None
    if flow_project:
        images["flow_project"] = flow_project
    else:
        missing.append("images.flow_project (not in old E003/episode.json)")
    images["flow_model"] = FLOW_MODEL
    doc["images"] = images

    sub: dict = {}
    card = extras.get("CARD_LINES") or []
    if len(card) == 4:
        sub["card_text"] = chr(10).join(card)
    else:
        missing.append(f"video.subscribe.card_text (expected 4 text lines in old card_image(), found {len(card)})")
    if "SPOKEN" in extras:
        sub["spoken_lines"] = list(extras["SPOKEN"])
    else:
        missing.append("video.subscribe.spoken_lines (SPOKEN not found in old episode_extras.py)")
    doc["video"] = {"disclaimer": {"text": "\n\n".join(paras)}, "subscribe": sub}

    skill = old / ".claude" / "skills" / "coi-episode-publish" / "SKILL.md"
    pub: dict = {}
    if skill.exists():
        pub = parse_publish(skill.read_text(encoding="utf-8"))
    for k in ("title_template", "description_template", "tags", "hashtags"):
        if k not in pub:
            missing.append(f"publish.{k} (not found in coi-episode-publish SKILL.md)")
    missing.append("publish.playlist_url (SKILL.md only has the placeholder <playlist link>; no real URL in the old project)")
    doc["publish"] = pub
    return rename_channel(doc)


# ---------------------------------------------------------------- bible

def merge_old_characters(old_eps: Path, report: list[str]) -> dict:
    """E006's characters.json plus any key found only in another episode's file."""
    docs = {}
    for n in (*FINISHED, *REDO):
        f = old_eps / ep_name(n) / "bible" / "characters.json"
        if f.exists():
            docs[n] = _load_json(f)
    if 6 not in docs:
        raise MigrateError(f"{old_eps / 'E006' / 'bible' / 'characters.json'} not found")
    base = copy.deepcopy(docs[6])
    base.setdefault("characters", {})
    added = []
    for n, d in sorted(docs.items()):
        if n == 6:
            continue
        for k, v in (d.get("characters") or {}).items():
            if k not in base["characters"]:
                base["characters"][k] = copy.deepcopy(v)
                added.append(f"{k} (from {ep_name(n)})")
        for k, v in (d.get("content_flags") or {}).items():
            base.setdefault("content_flags", {})
            if k not in base["content_flags"]:
                base["content_flags"][k] = copy.deepcopy(v)
                added.append(f"content_flags.{k} (from {ep_name(n)})")
    eps = ", ".join(ep_name(n) for n in sorted(docs))
    if added:
        report.append(f"bible/characters: E006 was NOT a superset; merged in: {', '.join(added)}")
    else:
        report.append(f"bible/characters: E006's {len(base['characters'])} characters are a superset of "
                      f"every key in {eps} (nothing to merge)")
    return base


def pick_names(old_eps: Path, report: list[str]) -> Path:
    cands = {}
    for n in (*FINISHED, *REDO):
        f = old_eps / ep_name(n) / "names.json"
        if f.exists():
            cands[n] = (f, _load_json(f))
    if not cands:
        raise MigrateError("no names.json in any old episode")
    best = max(cands, key=lambda n: (len(cands[n][1]), cands[n][0].stat().st_size, n))
    base = cands[best][1]
    diffs = []
    for n, (_, d) in sorted(cands.items()):
        for k in sorted(set(base) | set(d)):
            if base.get(k) != d.get(k):
                diffs.append(f"{k!r}: {ep_name(best)}={base.get(k)!r} vs {ep_name(n)}={d.get(k)!r}")
    sizes = ", ".join(f"{ep_name(n)}={len(c[1])} keys" for n, c in sorted(cands.items()))
    report.append(f"bible/names: used {ep_name(best)}/names.json (largest, then newest; {sizes})")
    report.append("bible/names: keys with a different spelling between episodes: "
                  + ("none" if not diffs else f"{len(diffs)}: " + "; ".join(diffs[:20])))
    return cands[best][0]


def union_flow(old_eps: Path, report: list[str]) -> list[tuple[str, Path]]:
    """[(file name, source path)] over every episode's bible/flow; newest episode wins a name clash,
    and every clash is reported."""
    versions: dict[str, dict[str, list[int]]] = {}
    chosen: dict[str, Path] = {}
    for n in (*FINISHED, *REDO):
        d = old_eps / ep_name(n) / "bible" / "flow"
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if not f.is_file():
                continue
            versions.setdefault(f.name, {}).setdefault(sha256_file(f), []).append(n)
            chosen[f.name] = f                    # later (newer) episode overwrites
    clashes = {k: v for k, v in versions.items() if len(v) > 1}
    report.append(f"bible/flow: union of {len(chosen)} files; names held with DIFFERENT content by two "
                  f"episodes: " + ("none" if not clashes else
                                   "; ".join(f"{k} -> " + " vs ".join(",".join(ep_name(n) for n in eps)
                                                                      for eps in v.values())
                                             for k, v in clashes.items())
                                   + " (newest episode's copy used)"))
    return sorted(chosen.items())


# ---------------------------------------------------------------- building the plan

def _final_mp4(name: str) -> bool:
    return bool(re.match(r"^E\d{3}.*\.mp4$", name))


def build_plan(old: Path, book: Path, with_media: bool) -> Plan:
    old = Path(old)
    book = Path(book)
    eps = old_episodes(old)
    if not eps.is_dir():
        raise MigrateError(f"{eps} not found. Is --from the old COI Audiobook folder?")
    plan = Plan(old=old, book=book, with_media=with_media)

    # 1. book.yaml
    s1 = Step("book", "book.yaml")
    s1.gens.append(Gen(book / "book.yaml", dump_yaml(build_book_yaml(old, plan.missing)), "book.yaml"))
    plan.steps.append(s1)

    # 2. source + chapters
    s2 = Step("source", "source/ (EPUBs) and chapters/")
    ebook = old / "ebook"
    epubs = sorted(ebook.glob("*.epub"))
    if not epubs:
        raise MigrateError(f"no EPUB files in {ebook}")
    for p in epubs:
        s2.copies.append(Copy(p, book / "source" / p.name, p.stat().st_size))
    sc = ex.scan(ebook, None, VOLUMES)
    if sc.gaps or sc.duplicates:
        plan.reports.append(f"extract: gaps {sc.gaps}, duplicates {sc.duplicates}")
    s2.chapters = sc.chapters
    s2.chapters_dir = book / "chapters"
    s2.notes.append(f"{len(sc.chapters)} chapters from {len(epubs)} EPUBs (same extractor as `novel extract`)")
    plan.steps.append(s2)

    # 3. bible
    s3 = Step("bible", "bible/ (characters, locations, names, Flow reference images)")
    merged = merge_old_characters(eps, plan.reports)
    chars, locs = B.convert_v1(merged, places=PLACES)
    plan.reports.append(f"bible/convert_v1: {len(chars['characters'])} characters, {len(locs['locations'])} "
                        f"location(s) ({', '.join(PLACES)})")
    s3.gens.append(Gen(book / "bible" / "characters.json", dump_json(chars), "bible/characters.json (convert_v1)"))
    s3.gens.append(Gen(book / "bible" / "locations.json", dump_json(locs), "bible/locations.json (convert_v1)"))
    names_src = pick_names(eps, plan.reports)
    s3.copies.append(Copy(names_src, book / "bible" / "names.json", names_src.stat().st_size))
    for name, src in union_flow(eps, plan.reports):
        s3.copies.append(Copy(src, book / "bible" / "flow" / name, src.stat().st_size))
    plan.steps.append(s3)

    # 4. finished episodes
    for n in FINISHED:
        src = eps / ep_name(n)
        if not src.is_dir():
            raise MigrateError(f"{src} not found")
        st = Step(f"e{n}", f"episodes/{ep_name(n)} (published; whole folder)")
        dst = book / "episodes" / ep_name(n)
        skipped = {"final video": [0, 0], "thumbnail images": [0, 0], "video intermediates": [0, 0]}

        def keep(rel, size, skipped=skipped):
            top, _, rest = rel.partition("/")
            suffix = Path(rel).suffix.lower()
            if not rest and _final_mp4(top):
                if with_media:
                    return True
                skipped["final video"][0] += 1
                skipped["final video"][1] += size
                return False
            if top == "thumbnail" and rest:
                if with_media:
                    return True
                skipped["thumbnail images"][0] += 1
                skipped["thumbnail images"][1] += size
                return False
            if top == "video" and rest:
                if suffix in TEXT_SUFFIXES and size <= SMALL_TEXT_MAX:
                    return True
                skipped["video intermediates"][0] += 1
                skipped["video intermediates"][1] += size
                return False
            return True

        add_tree(st, src, dst, keep)
        for what, (c, b) in skipped.items():
            if c:
                st.skipped.append((what, c, b))
        st.gens.append(Gen(dst / "episode.yaml", dump_yaml({"published": True}), "episode.yaml"))
        plan.steps.append(st)

    # 5. episodes to redo
    for n in REDO:
        src = eps / ep_name(n)
        if not src.is_dir():
            raise MigrateError(f"{src} not found")
        st = Step(f"e{n}", f"episodes/{ep_name(n)} (kept data; old flow archived)")
        dst = book / "episodes" / ep_name(n)
        for name in REDO_COPY:
            if (src / name).exists():
                add_tree(st, src / name, dst / name)
        for name in REDO_ARCHIVE:
            if (src / name).exists():
                add_tree(st, src / name, dst / ARCHIVE / name)
        st.gens.append(Gen(dst / "episode.yaml", dump_yaml({"published": False}), "episode.yaml"))
        known = set(REDO_COPY) | set(REDO_ARCHIVE)
        for p in sorted(src.iterdir()):
            if p.name not in known:
                plan.unclassified.append((posix(p.relative_to(old)), tree_size(p)[1]))
        plan.steps.append(st)

    # 6. E900
    s6 = Step("e900", "extras/E900 (subscribe message)")
    e900 = eps / EXTRA_EPISODE
    if e900.is_dir():
        add_tree(s6, e900, book / "extras" / EXTRA_EPISODE)
    else:
        plan.missing.append(f"extras/{EXTRA_EPISODE} (folder not found)")
    plan.steps.append(s6)

    # 7. what no rule covers
    known_eps = {ep_name(n) for n in (*FINISHED, *REDO)} | {EXTRA_EPISODE}
    for p in sorted(eps.iterdir()):
        if p.name not in known_eps:
            plan.unclassified.append((posix(p.relative_to(old)), tree_size(p)[1]))

    classify(plan)
    verify_chapters_dry(plan)
    return plan


# ---------------------------------------------------------------- classify (existing targets)

def classify(plan: Plan) -> None:
    for st in plan.steps:
        for c in st.copies:
            if not c.dst.exists():
                continue
            if not c.dst.is_file():
                c.status = "conflict"
            elif c.dst.stat().st_size != c.size:
                c.status = "conflict"
            else:
                c.sha = sha256_file(c.src)
                c.status = "same" if sha256_file(c.dst) == c.sha else "conflict"
            if c.status == "conflict":
                plan.conflicts.append(posix(c.dst))
        for g in st.gens:
            if not g.dst.exists():
                continue
            if g.dst.is_file() and g.dst.read_bytes() == g.data:
                g.status = "same"
            else:
                g.status = "conflict"
                plan.conflicts.append(posix(g.dst))
        if st.chapters:
            diff = {p.name for p in ex.differing(st.chapters, st.chapters_dir)}
            for name in sorted(diff):
                plan.conflicts.append(posix(st.chapters_dir / name))
            st.chapters_same = sum(1 for c in st.chapters if (st.chapters_dir / c.filename).exists()
                                   and c.filename not in diff)
            st.chapters_new = len(st.chapters) - st.chapters_same - len(diff)


def verify_chapters_dry(plan: Plan) -> None:
    """Chapters the extractor produces vs the old per-episode chapters/ folders."""
    eps = old_episodes(plan.old)
    new = {c.filename: c for st in plan.steps for c in st.chapters}
    for n in (*FINISHED, *REDO):
        d = eps / ep_name(n) / "chapters"
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.txt")):
            c = new.get(f.name)
            if c is None:
                plan.chapter_mismatches.append(f"{f.name}: not produced by the extractor")
            elif f.read_bytes() != expected_chapter_bytes(c):
                plan.chapter_mismatches.append(f"{f.name}: content differs from the old file")
            else:
                plan.chapters_verified += 1


def verify_chapters_real(plan: Plan) -> None:
    plan.chapter_mismatches = []
    plan.chapters_verified = 0
    eps = old_episodes(plan.old)
    out = plan.book / "chapters"
    for n in (*FINISHED, *REDO):
        d = eps / ep_name(n) / "chapters"
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.txt")):
            t = out / f.name
            if not t.exists():
                plan.chapter_mismatches.append(f"{f.name}: not written")
            elif t.read_bytes() != f.read_bytes():
                plan.chapter_mismatches.append(f"{f.name}: not byte-identical to the old file")
            else:
                plan.chapters_verified += 1


# ---------------------------------------------------------------- free space

def free_space(book: Path) -> tuple[Path, int]:
    p = book
    while not p.exists() and p != p.parent:
        p = p.parent
    return p, shutil.disk_usage(p).free


# ---------------------------------------------------------------- executing

def copy_file(src: Path, dst: Path) -> tuple[str, int]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".migrating")
    h = hashlib.sha256()
    n = 0
    st = src.stat()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                chunk = fi.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                fo.write(chunk)
                n += len(chunk)
        if n != st.st_size:
            raise MigrateError(f"{src} changed while copying ({n} bytes read, {st.st_size} expected)")
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, dst)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    return h.hexdigest(), n


class Record:
    """<book>/migration.json: one entry per file, keyed by target path."""

    def __init__(self, book: Path, old: Path, with_media: bool):
        self.book = book
        self.path = book / "migration.json"
        self.old = old
        self.with_media = with_media
        self.entries: dict[str, dict] = {}
        self.dirty = not self.path.exists()
        self.created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if self.path.exists():
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.created = d.get("created", self.created)
            self.entries = {e["target"]: e for e in d.get("files", [])}
            self.with_media = bool(d.get("with_media")) or with_media
            self.dirty = self.with_media != bool(d.get("with_media"))

    def rel(self, dst: Path) -> str:
        return posix(Path(dst).relative_to(self.book))

    def add(self, source: str, dst: Path, size: int, sha: str) -> None:
        t = self.rel(dst)
        entry = {"source": source, "target": t, "size": size, "sha256": sha}
        if self.entries.get(t) != entry:
            self.entries[t] = entry
            self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        self.dirty = False
        doc = {"version": 1, "created": self.created,
               "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "source_root": posix(self.old), "target_root": posix(self.book),
               "with_media": self.with_media,
               "files": [self.entries[k] for k in sorted(self.entries)]}
        atomic_write(self.path, dump_json(doc))


@dataclass
class StepResult:
    copied: int = 0
    copied_bytes: int = 0
    same: int = 0
    generated: int = 0


def execute_step(plan: Plan, st: Step, rec: Record) -> StepResult:
    r = StepResult()
    for d in st.dirs:
        d.mkdir(parents=True, exist_ok=True)
    for c in st.copies:
        if c.status == "same":
            r.same += 1
            rec.add(posix(c.src), c.dst, c.size, c.sha or sha256_file(c.dst))
            continue
        sha, n = copy_file(c.src, c.dst)
        rec.add(posix(c.src), c.dst, n, sha)
        r.copied += 1
        r.copied_bytes += n
    for g in st.gens:
        if g.status == "new":
            atomic_write(g.dst, g.data)
            r.generated += 1
            r.copied_bytes += len(g.data)
        else:
            r.same += 1
        rec.add(f"generated: {g.label}", g.dst, len(g.data), sha256_bytes(g.data))
    if st.chapters:
        written = ex.write_chapters(st.chapters, st.chapters_dir)
        r.generated += written
        r.same += len(st.chapters) - written
        for c in st.chapters:
            f = st.chapters_dir / c.filename
            rec.add("generated: novel.stages.extract from source/*.epub", f, f.stat().st_size, sha256_file(f))
        r.copied_bytes += sum(len(expected_chapter_bytes(c)) for c in st.chapters)
    rec.save()
    return r


# ---------------------------------------------------------------- the command body

def run(old: Path, book_name: str, dry_run: bool, with_media: bool, out=None) -> int:
    out = out or sys.stdout

    def say(msg=""):
        print(msg, file=out)

    old = Path(old)
    book = paths.book_dir(book_name)
    mode = "DRY RUN (nothing is written)" if dry_run else "REAL RUN"
    say(f"novel migrate {book_name}: {old} -> {book}   [{mode}{', with media' if with_media else ''}]")
    try:
        plan = build_plan(old, book, with_media)
    except MigrateError as e:
        print(f"novel migrate: {e}", file=sys.stderr)
        return 1

    if plan.conflicts:
        print(f"novel migrate: {len(plan.conflicts)} target file(s) already exist with different content. "
              "Nothing was written.", file=sys.stderr)
        for p in plan.conflicts[:20]:
            print(f"  {p}", file=sys.stderr)
        if len(plan.conflicts) > 20:
            print(f"  ... and {len(plan.conflicts) - 20} more", file=sys.stderr)
        print("Move or remove them yourself (the migration never overwrites), then run again.", file=sys.stderr)
        return 1

    total_new = sum(s.new_bytes() for s in plan.steps)
    where, free = free_space(book)
    need = total_new + RESERVE_BYTES
    drive = where.anchor or str(where)
    say(f"free space on {drive}: {fmt_bytes(free)}; to copy {fmt_bytes(total_new)} + "
        f"{fmt_bytes(RESERVE_BYTES)} reserve = {fmt_bytes(need)}: " + ("ok" if free >= need else "NOT ENOUGH"))
    if free < need:
        print(f"novel migrate: not enough free space on {drive}: {fmt_bytes(free)} free, "
              f"{fmt_bytes(need)} needed (copy + 5 GB). Free some space and run again.", file=sys.stderr)
        return 1

    say()
    results: dict[str, StepResult] = {}
    rec = None
    if not dry_run:
        for f in BOOK_FOLDERS:
            (book / f).mkdir(parents=True, exist_ok=True)
        rec = Record(book, old, with_media)

    total_files = total_same = 0
    for i, st in enumerate(plan.steps, 1):
        nf, nb, ns = st.new_files(), st.new_bytes(), st.same_files()
        total_files += nf
        total_same += ns
        if dry_run:
            line = f"[{i:>2}/{len(plan.steps)}] {st.title}: would copy {nf} files, {fmt_bytes(nb)}"
            if ns:
                line += f"; {ns} already migrated"
            say(line)
        else:
            r = execute_step(plan, st, rec)
            results[st.key] = r
            if r.copied + r.generated == 0:
                say(f"[{i:>2}/{len(plan.steps)}] {st.title}: already migrated ({r.same} files), nothing to copy")
            else:
                line = (f"[{i:>2}/{len(plan.steps)}] {st.title}: copied/written {r.copied + r.generated} files, "
                        f"{fmt_bytes(r.copied_bytes)}")
                if r.same:
                    line += f"; {r.same} already migrated"
                say(line)
        for n in st.notes:
            say(f"       {n}")
        for what, c, b in st.skipped:
            say(f"       skipped {what}: {c} files, {fmt_bytes(b)}" + ("" if with_media or what == "video intermediates"
                                                                      else " (use --with-media)"))

    say()
    for r in plan.reports:
        say(f"check: {r}")
    if not dry_run:
        verify_chapters_real(plan)
    say(f"check: chapters byte-identical to the old episodes' chapters/ files: {plan.chapters_verified} "
        f"identical, {len(plan.chapter_mismatches)} different")
    for m in plan.chapter_mismatches[:20]:
        say(f"  MISMATCH {m}")
    if plan.missing:
        say("values not found (left out):")
        for m in plan.missing:
            say(f"  {m}")
    if plan.unclassified:
        say("not covered by any rule (not copied):")
        for p, size in plan.unclassified:
            say(f"  {p}  {fmt_bytes(size)}")
    say()
    if dry_run:
        say(f"dry run: {total_files} files / {fmt_bytes(total_new)} would be copied or written; "
            f"{total_same} already migrated. Nothing was written.")
    elif total_files == 0 and not any(r.copied + r.generated for r in results.values()):
        say(f"nothing to copy: everything is already migrated ({total_same} files). Log: {rec.path}")
    else:
        copied = sum(r.copied + r.generated for r in results.values())
        size = sum(r.copied_bytes for r in results.values())
        say(f"done: {copied} files copied or written, {fmt_bytes(size)}; "
            f"{sum(r.same for r in results.values())} already migrated. Log: {rec.path}")
    return 1 if plan.chapter_mismatches else 0
