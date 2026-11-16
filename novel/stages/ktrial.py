"""`novel ktrial scenes | prompts`: the local half of the FLUX.3 klein trial (pictures from an open model on Kaggle).

Trial in one line: write new picture lines for one chapter with Gemini using the "tight" rules, build prompts for the
open image model, make the pictures through ComfyUI on Kaggle (novel.stages.ktrial_run), compare with Google Flow.
NOTHING in production is written: not <book>/episodes, not <book>/bible, not <book>/gemini, not <book>/inbox. Everything
goes to one folder, and every production file is only read:

    <trial> = <book>/_work/ktrial/c<chapter:04d>
      scenes/<chapter:05d>.json        the trial scene file (the shape `novel import` writes)
      gemini/001.request.txt, 001.reply.txt, ...     every call of `scenes`, kept
      refs/<key>.jpg | refs/<key>.<var>.jpg          one square reference per person (from bible/flow/*-portrait.jpg)
      plates/g01.prompt.txt                          g01.jpg and g01.meta.json come later, from `run`
      images/s01.prompt.txt                          s01.jpg and s01.meta.json come later, from `run`
      plan.json                                      what `run` and `report` read

`scenes` does what `novel write` does for one chapter's IMAGES block (the same windows, the same request layout, the
same contract checks) but never calls the importer's file-writing parts: the importer is used only to READ lines
(`Importer.read_lines`) and to make the document (`Importer.build_doc`), both of which touch nothing. The bible is
read-only here: a BIBLE block in the reply is ignored with a warning, so a key that is not in the bible is an error
(and the chapter is asked again).
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from novel import bible as B
from novel import kcomfy, kprompts
from novel.core import context, paths, settings
from novel.gemini import contract as C
from novel.gemini import importer as I
from novel.gemini import prompt as P
from novel.gemini import write as W
from novel.prompts import PromptError, _locations
from novel.stages import episode_scenes as S

WIDTH, HEIGHT = 1376, 768          # 1K pictures: the FHD panels video only uses cutouts
FRAMING = "tight"
DEFAULT_MAX_CHARACTERS = 2
REF_JPEG_QUALITY = 92
assert WIDTH % kcomfy.SIZE_STEP == 0 and HEIGHT % kcomfy.SIZE_STEP == 0

# Added to the IMAGES request: write.images_instruction lets Gemini add a BIBLE block, which this trial cannot keep.
TRIAL_NOTE = ("\nThis is a trial and the bible is read-only: do not write a BIBLE block, and use only the characters, "
              "variants and locations that are in the CURRENT BIBLE above.")
MAX_FIX_ITEMS = I.MAX_FIX_ITEMS


class TrialError(Exception):
    """A problem the message explains (and usually how to fix); the command prints it and returns 1."""


# ------------------------------------------------------------------ layout

def trial_dir(book: str, chapter: int) -> Path:
    """<book root>/_work/ktrial/c<chapter:04d>: the only place the trial writes."""
    return paths.book_dir(book) / "_work" / "ktrial" / f"c{int(chapter):04d}"


def scenes_file(trial: Path, chapter: int) -> Path:
    return Path(trial) / "scenes" / f"{int(chapter):05d}.json"


def ref_stem(key: str, var: str) -> str:
    """`lumian_lee` for the base variant, `lumian_lee.storm_coat` otherwise (the names `novel characters make` uses)."""
    return key if var in (None, "", "base") else f"{key}.{var}"


def episode_of(book: str, chapter: int) -> int:
    """The episode whose range (episode.json start-end) holds the chapter, as `novel import` finds it."""
    folder = P.episode_of(paths.book_dir(book)).get(int(chapter))
    m = re.fullmatch(r"E(\d+)", folder.name) if folder is not None else None
    if m is None:
        raise TrialError(f"chapter {chapter} is in no episode of book '{book}' (no episodes/E*/episode.json covers "
                         f"it). Pass --ep N")
    return int(m.group(1))


def _setup(args) -> tuple[str, int, int, Path, Path]:
    """(book, chapter, episode, episode folder, trial folder) from the arguments; `--ep` overrides the lookup."""
    book, chapter = args.book, int(args.chapter)
    if not book:
        raise TrialError("no book: pass --book or run: novel use <book>")
    context.activate(book)
    ep = int(args.ep) if getattr(args, "ep", None) else episode_of(book, chapter)
    ep_root = paths.ep_root(book, ep)
    if not (ep_root / "episode.json").exists():
        raise TrialError(f"episode {ep} of book '{book}' does not exist ({ep_root / 'episode.json'} not found)")
    return book, chapter, ep, ep_root, trial_dir(book, chapter)


def window_texts(ep_root: Path, chapter: int, *, need_lock: bool) -> list[str]:
    """The Hindi windows of the chapter exactly as `novel write` computes them (prompt.chapter_windows), so the count
    equals production's. need_lock: the Hindi must be locked (what the scene writer demands)."""
    state = P.hindi_state(ep_root, chapter)
    if state == "none":
        raise TrialError(f"chapter {chapter} has no Hindi file in {ep_root / 'hindi'}")
    if need_lock and state != "locked":
        raise TrialError(f"the Hindi of chapter {chapter} is {state}: lock it first (novel write, or episode_translate "
                         f"lock); the windows must be final")
    wins = P.chapter_windows(ep_root, chapter)
    if not wins:
        raise TrialError(f"chapter {chapter}: the Hindi has no narration after its title line")
    return wins


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))            # LF on every platform


def _load_scenes(trial: Path, chapter: int) -> dict:
    f = scenes_file(trial, chapter)
    if not f.exists():
        raise TrialError(f"no trial scene file {f}. Run first: novel ktrial scenes --chapter {chapter}")
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        raise TrialError(f"{f} is not valid JSON: {e}")
    if doc.get("version") != 2 or not doc.get("scenes"):
        raise TrialError(f"{f} is not a version 2 scene file with scenes")
    return doc


# ------------------------------------------------------------------ scenes: the request

def rules_args(book: str, ep: int, chapter: int, max_characters: int) -> dict:
    """The arguments `Runner.rules_args` gives build_rules (the settings it reads, the same defaults), for a batch of
    this one chapter, except max_characters."""
    get = lambda k, d=None: settings.get(k, book=book, ep=ep, default=d)
    target = int(get("translate.target_words", 800))
    low, high = P.word_band(target, float(get("translate.words_tolerance", 0.20)))
    return dict(channel=get("channel.name", "") or "", title=get("book.title") or book, first=chapter, last=chapter,
                paced=float(get("images.paced_seconds", S.PACED_SECONDS)), target=target, low=low, high=high,
                chars_per_second=float(get("images.chars_per_second", S.CHARS_PER_SECOND)),
                max_characters=int(max_characters))


def images_user(state: dict, chapter: int, english: str, wins: list[str], instruction: str) -> str:
    """The user text of one IMAGES request: the layout of `Runner.images_user` (bible, English chapter, windows,
    REQUEST). The bible is read-only here, so its heading does not promise "everything added earlier in this run"."""
    n = chapter
    return (f"CURRENT BIBLE (what is decided so far; reuse these keys and spellings exactly)\n"
            f"{W.bible_dump(state, False)}\n"
            f"CHAPTER {n} (English)\n{english.strip()}\n\n"
            f"WINDOWS OF CHAPTER {n} (the Hindi narration, final; one image line per window)\n"
            f"##### CHAPTER {n}: {len(wins)} WINDOWS #####\n{W.window_lines(wins)}\n\nREQUEST\n{instruction}")


def make_client(cfg, folder: Path, say=print):
    """The Gemini client `Runner.make_client` builds (model, fallbacks, minimum gap from the settings), logging into
    the trial's gemini folder."""
    from novel.stages import episode_translate as T
    folder.mkdir(parents=True, exist_ok=True)
    return T.Gemini(cfg.model, folder / f"run-{W.utc_stamp()}.client.jsonl", fallback_models=cfg.fallbacks,
                    min_gap=cfg.min_gap, sleep=W.SLEEP, clock=W.CLOCK, say=say)


def _call(client, folder: Path, step: str, system: str, user: str, out=print):
    """One API call, request and reply saved as <folder>/NNN.request.txt and .reply.txt. -> GenResult, or None when
    the reply was blocked (a failed attempt). Quota or a refusal end the run (TrialError)."""
    folder.mkdir(parents=True, exist_ok=True)
    k = len(list(folder.glob("*.request.txt"))) + 1
    _write_text(folder / f"{k:03d}.request.txt", f"[system]\n{system}\n\n[user]\n{user}\n")
    out(f"{step}: request {k} ... {W.kb(len(system) + len(user))} characters sent")
    try:
        res = client.generate(system, user, temperature=0.3)
    except SystemExit as e:                                   # quota used up, or a refusal
        raise TrialError(f"Gemini stopped the run: {e.code}")
    except ValueError as e:
        out(f"{step}: blocked: {e}")
        return None
    _write_text(folder / f"{k:03d}.reply.txt", res.text)
    u = res.usage or {}
    out(f"{step}: reply {len(res.text):,} characters, finish {res.finish}, {res.seconds:g}s, tokens in "
        f"{int(u.get('promptTokenCount') or 0):,} out {int(u.get('candidatesTokenCount') or 0):,}, model {res.model}")
    return res


# ------------------------------------------------------------------ scenes: the reply

def _fix_text(problems: list[str]) -> str:
    """The FIX list that goes back to Gemini (deduplicated, the first few, like the importer's)."""
    items = list(dict.fromkeys(problems))
    text = "The IMAGES block has these problems; fix every one of them:\n" + "\n".join(
        f"- {p}" for p in items[:MAX_FIX_ITEMS])
    if len(items) > MAX_FIX_ITEMS:
        text += f"\n- ... and {len(items) - MAX_FIX_ITEMS} more of the same kind"
    return text


def check_reply(text: str, imp: I.Importer, ctx: C.ChapterCtx, max_people: int, out=print):
    """Parse and validate the IMAGES block of chapter ctx.chapter with the importer's own reading and the contract's
    checks (groups, images, the whole-chapter checks, exactly one image per window), but nothing is staged, archived
    or written. BIBLE blocks are ignored with a warning.

    -> (groups, images, warnings, problems): groups {id: normalised line}, images {n: normalised line}; problems is a
    list of texts for Gemini (empty = accepted)."""
    n = ctx.chapter
    reply = C.split_reply(text)
    blocks = [b for b in reply.blocks if b.kind == "IMAGES" and b.chapter == n]
    problems: list[str] = []
    for b in reply.blocks:
        if b.kind == "BIBLE":
            out(f"warning: the reply holds a BIBLE block ({b.label}); ignored (the bible is read-only in the trial)")
        elif b not in blocks:
            problems.append(f"the reply holds a block that was not asked for: {b.label}. Return only IMAGES c{n:04d}")
    if not blocks:
        return {}, {}, [], problems + [f"the reply has no IMAGES block for chapter {n}"]
    for b in blocks:
        problems += [f"{e.what + ': ' if e.what else ''}{e.msg}" for e in b.errors]
    lines = [line for b in blocks for line in b.lines]
    groups_new, images_new, errs, warns = imp.read_lines(lines, ctx, any(b.quoted for b in blocks))
    problems += [f"{e.what + ': ' if e.what else ''}{e.msg}" + (f" (line {e.line})" if e.line else "") for e in errs]
    groups = {k: v[0] for k, v in groups_new.items()}
    images = {k: v[0] for k, v in images_new.items()}
    for k, im in images.items():
        if im["group"] not in groups:
            problems.append(f"image {k} group: unknown group '{im['group']}': no group line for it in this block "
                            f"(a group line comes before its images)")
    problems += [f"image {k} cast: {m}" for k, m in C.cast_present_errors(groups, images)]
    missing = C.missing_windows(images, ctx.count)
    if missing:
        problems.append(f"too few images: windows {C.ranges_text(missing)} have no image line ({len(images)} of "
                        f"{ctx.count} received); chapter {n} needs exactly one image line for each of its "
                        f"{ctx.count} windows")
    warnings = [w if isinstance(w, str) else w.render() for w in warns]
    if not problems:
        used = {im["group"] for im in images.values()}
        warnings += [f"group {g} is not used by any image" for g in groups if g not in used]
        # the tight rules allow many 'medium' pictures: the contract's "medium more than a third" warning is dropped
        warnings += [w for w in C.chapter_warnings(groups, images, ctx) if "(at most a third)" not in w]
        warnings += [f"image {k}: {len(im['cast'])} people in the cast; the tight framing wants at most {max_people}"
                     for k, im in sorted(images.items()) if len(im["cast"]) > max_people]
    return groups, images, warnings, problems


def build_trial_doc(imp: I.Importer, ep_root: Path, chapter: int, ctx: C.ChapterCtx, groups: dict, images: dict,
                    batch: str, model: str) -> dict:
    """The scene document in the shape `Importer.build_doc` writes (called as it is: it only reads the lock file),
    with the source of the trial."""
    paras = S.chapter_paras(ep_root, chapter)
    wins = S.chapter_windows(paras, S.PACING)
    if len(wins) != ctx.count:
        raise TrialError(f"chapter {chapter} cuts into {len(wins)} windows, not {ctx.count}")
    used = {im["group"] for im in images.values()}
    params = {"paced_seconds": S.PACED_SECONDS, "max_seconds": S.MAX_SECONDS, "chars_per_second": S.CHARS_PER_SECOND}
    bt = SimpleNamespace(id=batch, meta={"params": params})
    ch = SimpleNamespace(n=chapter, ep_root=ep_root, ctx=ctx, wins=wins)
    doc = imp.build_doc(bt, ch, {g: v for g, v in groups.items() if g in used}, images)
    doc["source"] = {"via": "ktrial", "framing": FRAMING, "model": model, "written": I.utc_iso()}
    return doc


def summary_lines(doc: dict, warnings: list[str] | None = None) -> list[str]:
    scenes = doc.get("scenes") or []
    shots = Counter(s.get("shot") for s in scenes)
    people = Counter(len(s.get("cast") or []) for s in scenes)
    order = [s for s in S.SHOTS_V2 if s in shots] + sorted(s for s in shots if s not in S.SHOTS_V2)
    out = [f"windows: {len(scenes)}",
           f"groups: {len(doc.get('groups') or {})}",
           "shots: " + ", ".join(f"{s} {shots[s]}" for s in order),
           "people per picture: " + ", ".join(f"{k}: {people[k]}" for k in sorted(people))]
    if warnings is not None:
        out.append(f"warnings: {len(warnings)}")
        out += [f"  - {w}" for w in warnings]
    return out


# ------------------------------------------------------------------ scenes: the command

def cmd_scenes(args, client=None) -> int:
    """Write <trial>/scenes/<chapter:05d>.json. args: book, chapter, ep, max_characters, from_production, force.
    client: an injected Gemini client (tests); default: the real one from the settings."""
    try:
        return _scenes(args, client)
    except TrialError as e:
        print(f"novel ktrial scenes: {e}")
        return 1


def _scenes(args, client) -> int:
    book, chapter, ep, ep_root, trial = _setup(args)
    target = scenes_file(trial, chapter)
    if target.exists() and not getattr(args, "force", False):
        raise TrialError(f"{target} already exists. Pass --force to replace it")

    if getattr(args, "from_production", False):
        src = S.scenes_path(ep_root, chapter)
        if not src.exists():
            raise TrialError(f"no production scene file {src} to copy")
        text = src.read_text(encoding="utf-8")
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise TrialError(f"{src} is not valid JSON: {e}")
        if doc.get("version") != 2 or not doc.get("scenes"):
            raise TrialError(f"{src} is not a version 2 scene file")
        I.write_atomic(target, text)
        print(f"copied the production scene file {src} -> {target} (no Gemini call)")
        print("\n".join(summary_lines(doc)))
        return 0

    max_people = int(getattr(args, "max_characters", None) or DEFAULT_MAX_CHARACTERS)
    cfg = W.load_settings(book, ep)
    wins = window_texts(ep_root, chapter, need_lock=True)
    count = len(wins)
    english = P.chapters_text(paths.book_dir(book), chapter, chapter)
    imp = I.Importer(book, dry_run=True, out=lambda t: None)         # read-only use: read_lines and build_doc
    state = imp.state
    ctx = C.ChapterCtx(chapter, count, state, max_people, S.BANNED)
    batch = P.batch_id(chapter, chapter)
    system = P.build_rules("images", **rules_args(book, ep, chapter, max_people), chapter=chapter,
                           window_count=count, framing=FRAMING)
    base = W.images_instruction(chapter, count, batch) + TRIAL_NOTE
    gem = trial / "gemini"
    if client is None:
        client = make_client(cfg, gem)
    print(f"chapter {chapter}: {count} windows; tight rules, at most {max_people} people; model {cfg.model}")

    feedback = None                                    # (what the tool said, the rejected answer)
    for attempt in range(1, cfg.attempts + 1):
        step = f"chapter {chapter}: scenes: call {attempt}"
        user = images_user(state, chapter, english, wins, base if feedback is None
                           else W.retry_instruction(base, *feedback))
        res = _call(client, gem, step, system, user)
        if res is None:
            continue
        if res.finish == "MAX_TOKENS":
            feedback = ("the answer was cut off by the length limit: return the whole block again, every line "
                        "complete, with shorter action texts", res.text)
            print(f"{step}: the reply was cut off by the output limit; asking again for the whole chapter")
            continue
        groups, images, warnings, problems = check_reply(res.text, imp, ctx, max_people)
        if problems:
            feedback = (_fix_text(problems), res.text)
            print(f"{step}: rejected ({len(problems)} problem{'s' if len(problems) != 1 else ''}): "
                  f"{problems[0][:200]}")
            continue
        doc = build_trial_doc(imp, ep_root, chapter, ctx, groups, images, batch, res.model)
        I.write_atomic(target, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        print(f"{step}: accepted -> {target}")
        print("\n".join(summary_lines(doc, warnings)))
        return 0
    raise TrialError(f"the IMAGES block was still not accepted after {cfg.attempts} attempts; the requests and "
                     f"replies are in {gem}")


# ------------------------------------------------------------------ prompts

def square_crop(src: Path, dest: Path) -> None:
    """The CENTRAL SQUARE of the picture (the whole short side, centred on the long one), JPEG quality 92. A Flow
    portrait is a head-and-shoulders picture centred on a plain background and the Gemini mark sits in a bottom
    corner, so the central square keeps the person and drops the mark."""
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.crop((left, top, left + side, top + side)).save(dest, "JPEG", quality=REF_JPEG_QUALITY)


def cmd_prompts(args) -> int:
    """Write images/sNN.prompt.txt, plates/<gid>.prompt.txt, refs/ and plan.json. No GPU, no network."""
    try:
        return _prompts(args)
    except TrialError as e:
        print(f"novel ktrial prompts: {e}")
        return 1


def _prompts(args) -> int:
    book, chapter, ep, ep_root, trial = _setup(args)
    doc = _load_scenes(trial, chapter)
    bdir = B.bible_dir(book)
    characters, locations = B.load_characters(bdir), B.load_locations(bdir)
    try:
        pl = kprompts.plan(doc, chapter, characters, locations)
    except PromptError as e:
        raise TrialError(str(e))
    wins = window_texts(ep_root, chapter, need_lock=False)
    if len(wins) != len(doc["scenes"]):
        raise TrialError(f"chapter {chapter} has {len(wins)} Hindi windows but the trial scene file has "
                         f"{len(doc['scenes'])} scenes: the scene file does not belong to this Hindi")

    # references: one portrait per (person, variant)
    people: dict[tuple, str] = {}
    for sh in pl["shots"]:
        if len(sh["refs"]) > kcomfy.MAX_REFS:
            raise TrialError(f"shot {sh['id']} has {len(sh['refs'])} references; the limit is {kcomfy.MAX_REFS}")
        for r in sh["refs"]:
            if r["role"] == "character":
                people[(r["key"], r["var"])] = r["display"]
    flow = bdir / "flow"
    sources = {kv: flow / f"{ref_stem(*kv)}-portrait.jpg" for kv in people}
    missing = [str(f) for f in sources.values() if not f.exists()]
    if missing:
        raise TrialError("portrait files are missing (make them with: novel characters make):\n  "
                         + "\n  ".join(missing))

    for kv, src in sources.items():
        square_crop(src, trial / "refs" / f"{ref_stem(*kv)}.jpg")

    scene_by_id = {s["id"]: s for s in doc["scenes"]}
    locs = _locations(locations)
    plates = []
    for p in pl["plates"]:
        gid = p["group"]
        g = doc["groups"][gid]
        loc = locs.get(g.get("loc")) or {}
        _write_text(trial / "plates" / f"{gid}.prompt.txt", p["text"])
        plates.append({"group": gid, "loc": g.get("loc"), "name": loc.get("name") or g.get("loc"),
                       "time": g.get("time"), "light": g.get("light") or loc.get("default_light") or "",
                       "prompt": f"plates/{gid}.prompt.txt", "out": f"plates/{gid}.jpg", "seed": p["seed"]})
    shots = []
    for sh in pl["shots"]:
        tag = f"s{sh['n']:02d}"
        _write_text(trial / "images" / f"{tag}.prompt.txt", sh["text"])
        refs = [{"role": "character", "key": r["key"], "var": r["var"], "display": r["display"],
                 "file": f"refs/{ref_stem(r['key'], r['var'])}.jpg"} if r["role"] == "character" else
                {"role": "place", "group": r["group"], "file": f"plates/{r['group']}.jpg"} for r in sh["refs"]]
        shots.append({"id": sh["id"], "n": sh["n"], "group": sh["group"], "shot": scene_by_id[sh["n"]]["shot"],
                      "prompt": f"images/{tag}.prompt.txt", "out": f"images/{tag}.jpg", "seed": sh["seed"],
                      "refs": refs})
    plan = {"version": 1, "book": book, "episode": ep, "chapter": chapter, "width": WIDTH, "height": HEIGHT,
            "flow_images": str(ep_root / "images" / f"c{chapter:04d}"),
            "production_scenes": str(S.scenes_path(ep_root, chapter)),
            "trial_scenes": f"scenes/{chapter:05d}.json",
            "windows": {str(k): t for k, t in enumerate(wins, 1)},
            "plates": plates, "shots": shots}
    I.write_atomic(trial / "plan.json", json.dumps(plan, ensure_ascii=False, indent=1) + "\n")

    by_refs = Counter(len(s["refs"]) for s in shots)
    sizes = {sh["id"]: len(sh["text"]) for sh in pl["shots"]}
    longest = sorted(sizes, key=lambda i: (-sizes[i], i))[:3]
    print(f"{len(shots)} shots, {len(plates)} plates, {len(sources)} reference picture(s) in {trial}")
    print("shots by number of references: " + ", ".join(f"{k}: {by_refs[k]}" for k in sorted(by_refs)))
    print(f"prompt length (characters): min {min(sizes.values())}, mean {statistics.mean(sizes.values()):.0f}, "
          f"max {max(sizes.values())}")
    print("longest prompts: " + ", ".join(f"{i} ({sizes[i]})" for i in longest))
    print(f"plan: {trial / 'plan.json'}")
    return 0
