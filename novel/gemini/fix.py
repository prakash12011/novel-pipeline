"""`novel qa --fix-prompt N`: the message to paste into the same Gemini conversation to correct chapter N.

It is built from the saved QA files of the chapter (qa/NNNNN.json and qa/NNNNN.review.json), never from a new
run. Scenes mode (the Hindi is locked): a PATCH block with replacement `image` lines (and `group` lines when a
group changes) for the affected pictures only. Full mode: the same, plus a complete replacement HINDI block when
a text finding is an error (the HINDI import is not built yet; the request is still generated).

The delimiters come from novel.gemini.contract (BEGIN / END lines, file names), so the importer's splitter reads
the block exactly as written here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from novel import qa as Q
from novel.gemini import contract as C
from novel.gemini import importer as I


class FixError(Exception):
    """Nothing to build (no QA yet, out of date, nothing wrong); the message says what to do."""


@dataclass
class Fix:
    text: str
    mode: str
    images: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    hindi: bool = False
    notes: list = field(default_factory=list)


def begin_line(kind: str, n: int) -> str:
    return f"===== BEGIN {kind} {C.block_name(kind, None, n)} -> inbox/{C.inbox_file(kind, None, n)} ====="


def end_line(kind: str, n: int) -> str:
    return f"===== END {kind} {C.block_name(kind, None, n)} ====="


CODE_TEXT_KINDS = ("hindi_paragraph", "name_missing", "omission", "hindi_missing", "lock")


def _is_text_finding(source: str, kind: str) -> bool:
    return kind in (Q.TEXT_KINDS if source == "ai" else CODE_TEXT_KINDS)


def collect(ep_root: Path, n: int, with_warnings: bool) -> tuple[list[dict], str, dict | None]:
    """(findings, mode, code doc). Each finding: {source: code|ai, severity, kind, ref, text, suggest}."""
    code = Q.read_json(Q.code_path(ep_root, n)) if Q.code_path(ep_root, n).is_file() else None
    if not Q.is_v2(code):
        raise FixError(f"chapter {n} has no QA result yet. Run: novel qa --chapters {n}")
    hf = Path(ep_root) / "hindi" / f"{n:05d}.txt"
    from novel.stages import episode_scenes as S
    if code.get("hindi_sha256") != Q.sha_file(hf) or code.get("scenes_sha256") != Q.sha_file(S.scenes_path(ep_root, n)):
        raise FixError(f"the QA result of chapter {n} is out of date (the Hindi or the scene file changed since). "
                       f"Run: novel qa --chapters {n}")
    out = []
    for e in code.get("errors") or []:
        out.append({"source": "code", "severity": "error", **e, "suggest": ""})
    if with_warnings:
        for w in code.get("warnings") or []:
            out.append({"source": "code", "severity": "warn", **w, "suggest": ""})
    rv = Q.read_json(Q.review_path(ep_root, n)) if Q.review_path(ep_root, n).is_file() else None
    if Q.is_v2(rv) and rv.get("hindi_sha256") == code.get("hindi_sha256") and \
            rv.get("scenes_sha256") == code.get("scenes_sha256"):
        for f in rv.get("findings") or []:
            if f.get("severity") == "error" or with_warnings:
                out.append({"source": "ai", **f})
    out.sort(key=lambda f: f["severity"] != "error")
    return out, code.get("mode", "scenes"), code


def window_images(wins: list, paras: list[int]) -> list[int]:
    """Picture numbers whose window covers any of the paragraphs."""
    hit = []
    for k, (a, _, b, _) in enumerate(wins, 1):
        if any(a <= p <= b for p in paras):
            hit.append(k)
    return hit


def build(ep_root: Path, n: int, with_warnings: bool = False) -> Fix:
    from novel.stages import episode_scenes as S
    findings, mode, code = collect(ep_root, n, with_warnings)
    if not findings:
        raise FixError(f"nothing to fix in chapter {n}: no errors in its QA result"
                       + ("" if with_warnings else " (--with-warnings adds the warnings)"))
    doc = Q.read_json(S.scenes_path(ep_root, n))
    fx = Fix("", mode)
    if not Q.is_v2(doc):
        raise FixError(f"chapter {n} has no usable scene file (v2); import it first: novel import")
    paras = S.chapter_paras(ep_root, n)
    wins = S.chapter_windows(paras, doc.get("pacing"))
    gl, il = I.Importer.doc_lines(doc)

    images: set[int] = set()
    groups: set[str] = set()
    numbered = []                      # (number, finding) shown to Gemini
    text_errors = False
    for f in findings:
        imgs, pars, grps = Q.refs_of(f["ref"])
        text_kind = _is_text_finding(f["source"], f["kind"])
        if text_kind:
            if f["severity"] == "error" and mode == "full":
                text_errors = True
            if mode == "scenes":
                fx.notes.append(f"not sent to Gemini (the Hindi is locked and cannot change): [{f['source']}] "
                                f"{f['ref']}: {f['text']}")
                continue
            if f["severity"] != "error" and mode == "full" and not with_warnings:
                continue
            numbered.append(f)
            imgs = imgs or window_images(wins, pars)
            images.update(i for i in imgs if i in il)
            continue
        imgs = imgs or window_images(wins, pars)
        if not imgs and not grps:
            fx.notes.append(f"cannot be patched (it is about the whole chapter): [{f['source']}] {f['text']}")
            continue
        numbered.append(f)
        images.update(i for i in imgs if i in il)
        groups.update(g for g in grps if g in gl)
    for g in groups:
        images.update(k for k, im in il.items() if im.get("group") == g)
    fx.images, fx.groups, fx.hindi = sorted(images), sorted(groups), text_errors
    if not numbered:
        raise FixError("nothing Gemini can fix: " + (" ".join(fx.notes) or f"no usable findings in chapter {n}"))

    name = C.block_name("PATCH", None, n)
    lines = [f"Chapter {n} ({name}): the second check found problems in what you wrote. Change ONLY what is listed "
             f"below and keep everything else exactly as it is: the same keys, group ids, picture numbers and "
             f"the same JSON shape as before.", "", "PROBLEMS"]
    for k, f in enumerate(numbered, 1):
        who = "check" if f["source"] == "code" else "reviewer"
        lines.append(f"{k}. [{f['ref']}] ({who}, {f['kind']}) {f['text']}"
                     + (f" Suggestion: {f['suggest']}" if f.get("suggest") else ""))
    if fx.images:
        lines += ["", "PICTURES TO REPLACE (the narration of each window is shown so you can see what it tells)"]
        for i in fx.images:
            narr = " ".join(t for _, t in S.window_text(paras, wins[i - 1]))
            lines.append(f"- picture {i}: {narr[:400]}{'...' if len(narr) > 400 else ''}")
        lines += ["", "Send back ONE block, exactly this format and nothing outside it. It holds the CURRENT lines of "
                      "the pictures above; change what the problems require. Keep `n` and `group` ids of the pictures; "
                      "a group line is included only for a group flagged above (changing it redraws every picture of the group); every `cast` member needs `key`, `var`, `pose`, "
                      "`hands`, `mood`, `wearing`; `hands` says what the hands hold (empty unless the moment needs "
                      "the item).", "", begin_line("PATCH", n), "```jsonl"]
        lines += [C.dump_line(gl[g]) for g in fx.groups]
        lines += [C.dump_line(il[i]) for i in fx.images]
        lines += ["```", end_line("PATCH", n)]
    if fx.hindi:
        lines += ["", "TEXT: the narration itself also needs changes (see the problems about paragraphs). Send a "
                      "COMPLETE replacement of the Hindi of this chapter in one more block: the chapter heading, "
                      "then the paragraphs with the picture markers {1}, {2}... as before.", "",
                  begin_line("HINDI", n), "<the full Hindi text of the chapter, with the markers>", end_line("HINDI", n)]
        fx.notes.append("the HINDI import is not built yet: the request is generated, but novel import only keeps "
                        "the HINDI block in the inbox for now")
    fx.text = "\n".join(lines) + "\n"
    return fx


def next_steps(n: int) -> list[str]:
    return ["Paste the message into the SAME Gemini conversation.",
            "Copy Gemini's reply and run: novel import",
            f"Then check again: novel qa --chapters {n}"]
