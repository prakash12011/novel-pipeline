"""Build the YouTube publish pack for one episode.

Code gathers the facts and fills the templates; the AI writes only the free-text slots
(teaser, comment question, three thumbnail hooks). Everything is validated by
novel.publish.validate before it is written.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from novel.publish import validate

MIN_GAP = validate.MIN_GAP_SECONDS
CLAUDE_TIMEOUT = 180
CLAUDE_ATTEMPTS = 2
HEAD_CHARS = 1500
TAIL_CHARS = 500
RESEARCH_MAX_AGE_DAYS = 7
RESEARCH_PHRASES_FOR_AI = 20
GENERIC_QUESTION = "Comment mein batao: aapko kya lagta hai, aage kya hoga?"

# The command line used when NOVEL_CLAUDE_CMD is not set (the CLI is found on PATH).
CLAUDE_ARGS = ["-p", "--model", "claude-opus-5-5", "--output-format", "json", "--tools", "",
               "--no-session-persistence", "--disable-slash-commands"]

CHAPTER_FILE = re.compile(r"^(\d+)_")
FIRST_LINE_TITLE = re.compile(r"^\s*Chapter\s+\d+\s*[:\-.]\s*(.+?)\s*$", re.I)
STAMP_WORD = "Phase"     # YouTube timestamp label: "Phase 12" (the video never says "chapter")
TXT_STAMP = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s+(?:Chapter|Phase)\s+(\d+)", re.I)
ARITH = re.compile(r"\[([NXYV])([+-]\d+)?\]")
NOTE = re.compile(r"\s*<-.*$")


class MetaError(Exception):
    """A clear, fixable problem (printed, exit 1)."""


# ------------------------------------------------------------------ facts

def volume_for(volumes, chapter: int):
    """(volume, found). volumes = [[first, last, volume], ...]."""
    for row in volumes or []:
        try:
            lo, hi, vol = row
        except (TypeError, ValueError):
            continue
        if int(lo) <= chapter <= int(hi):
            return int(vol), True
    return 1, False


def read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def neighbour(ep_dir: Path, n: int):
    """published_url of episode n (None when unknown)."""
    url = read_yaml(ep_dir.parent / f"E{n:03d}" / "episode.yaml").get("published_url")
    return str(url).strip() if url else None


def fmt_stamp(sec: int, hour_form: bool) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if hour_form else f"{h * 60 + m}:{s:02d}"


def find_timestamp_files(ep_dir: Path, n: int, start: int, end: int):
    """(kind, path) of the best timestamp file: json before txt; the plain name before
    the E00N-cA-B variant of this exact range. (None, None) when there is none."""
    stem = f"E{n:03d}"
    for kind in ("json", "txt"):
        for name in (f"{stem}.timestamps.{kind}", f"{stem}-c{start}-{end}.timestamps.{kind}"):
            if (ep_dir / name).is_file():
                return kind, ep_dir / name
    return None, None


def load_chapter_starts(ep_dir: Path, n: int, start: int, end: int):
    """({'rows': [(chapter, start_seconds)], 'duration', 'kind', 'file'}, warnings)."""
    warns: list[str] = []
    kind, f = find_timestamp_files(ep_dir, n, start, end)
    if f is None:
        warns.append(f"no E{n:03d}.timestamps.json or .txt in {ep_dir}: the description has no chapter "
                     f"timestamps. Build the episode (novel build) and run novel meta again.")
        return {"rows": [], "duration": None, "kind": None, "file": None}, warns
    rows: list[tuple[int, float]] = []
    duration = None
    try:
        if kind == "json":
            data = json.loads(f.read_text(encoding="utf-8"))
            duration = data.get("duration")
            rows = [(int(c["chapter"]), float(c["start"])) for c in data.get("chapters") or []]
        else:
            for line in f.read_text(encoding="utf-8").splitlines():
                m = TXT_STAMP.match(line)
                if m:
                    sec = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                    rows.append((int(m.group(4)), float(sec)))
            warns.append(f"{f.name}: older text file, no JSON. The first chapter is listed at 0:00 and the "
                         f"cards before it are unknown, so no 'Intro' line is added.")
    except (ValueError, KeyError, TypeError, OSError) as e:
        warns.append(f"could not read {f.name} ({type(e).__name__}): no chapter timestamps")
        rows = []
    rows = [(c, s) for c, s in rows if start <= c <= end]
    if not rows and f is not None and not any("could not read" in w for w in warns):
        warns.append(f"{f.name} has no chapters {start}-{end}: no chapter timestamps")
    return {"rows": rows, "duration": duration, "kind": kind, "file": f.name}, warns


def build_stamps(rows, duration, kind):
    """[(seconds, label)] and warnings, by the YouTube rules (first 0:00, 10 s apart)."""
    warns: list[str] = []
    if not rows:
        return [], warns
    first_true = rows[0][1]
    out: list[tuple[int, str]] = []
    if kind == "json" and first_true >= MIN_GAP:
        out.append((0, "Intro"))
        out.append((int(first_true), f"{STAMP_WORD} {rows[0][0]}"))
    else:
        out.append((0, f"{STAMP_WORD} {rows[0][0]}"))
    for ch, start in rows[1:]:
        sec = int(start)
        if sec - out[-1][0] < MIN_GAP:
            warns.append(f"dropped the stamp for Chapter {ch} ({fmt_stamp(sec, False)}): "
                         f"less than {MIN_GAP} s after '{out[-1][1]}'")
            continue
        out.append((sec, f"{STAMP_WORD} {ch}"))
    return out, warns


def stamp_lines(stamps, duration) -> list[str]:
    last = stamps[-1][0] if stamps else 0
    hour_form = (duration or 0) >= 3600 or last >= 3600
    return [f"{fmt_stamp(s, hour_form)} {label}" for s, label in stamps]


def chapter_texts(book: str, ep_dir: Path, start: int, end: int):
    """[(chapter, title, text)] of the English chapters. The episode's chapters/ folder first,
    then the book's chapters/ folder. The title is the first line ('Chapter 11: Title')."""
    from novel.core import paths
    found: dict[int, Path] = {}
    for folder in (ep_dir / "chapters", paths.chapters_dir(book)):
        if folder.is_dir():
            for p in sorted(folder.glob("*.txt")):
                m = CHAPTER_FILE.match(p.name)
                if m and start <= int(m.group(1)) <= end:
                    found.setdefault(int(m.group(1)), p)
    out = []
    for ch in sorted(found):
        text = found[ch].read_text(encoding="utf-8", errors="replace")
        first, _, body = text.partition("\n")
        m = FIRST_LINE_TITLE.match(first)
        title = m.group(1) if m else ""
        out.append((ch, title, body.strip() if m else text.strip()))
    return out


def condense(chapters) -> tuple[str, int]:
    """The view the AI gets: per chapter the first HEAD_CHARS and last TAIL_CHARS characters."""
    parts, total = [], 0
    for ch, _title, body in chapters:
        if len(body) <= HEAD_CHARS + TAIL_CHARS:
            piece = body
        else:
            piece = body[:HEAD_CHARS].rstrip() + "\n[...]\n" + body[-TAIL_CHARS:].lstrip()
        parts.append(f"--- Chapter {ch} ---\n{piece}")
        total += len(piece)
    return "\n\n".join(parts), total


# ------------------------------------------------------------------ templates

def _arith(m, vals):
    base = vals[m.group(1)]
    return str(base + int(m.group(2))) if m.group(2) else str(base)


def fill_title(template: str, vals: dict) -> str:
    return ARITH.sub(lambda m: _arith(m, vals), template).strip()


def fill_tags(tags, vals: dict) -> list[str]:
    return [ARITH.sub(lambda m: _arith(m, vals), str(t)).strip() for t in tags]


def series_phrase(title_template: str) -> str:
    """The text the title must start with: everything before the first ' | ' (or placeholder)."""
    head = title_template.split(" | ")[0] if " | " in title_template else title_template
    return ARITH.split(head)[0].strip(" |")


_PLAYLIST = re.compile(r"\[playlist link\]|<playlist link>", re.I)
_TEASER = re.compile(r"\[[^\]]*teaser[^\]]*\](\.?)", re.I)
_QUESTION = re.compile(r"\[[^\]]*comment question[^\]]*\]", re.I)
_STAMP_START = re.compile(r"^\s*(\[mm:ss|(?:\d+:)?\d{1,2}:\d{2}\b)", re.I)
_ELLIPSIS_ROW = re.compile(r"^\s*\.\.\.")


def fill_description(template: str, vals: dict, *, teaser: str, question: str, stamp_block: list[str],
                     playlist_url: str | None, prev_url: str | None, next_url: str | None,
                     ep_number: int) -> str:
    """Fill the book's description template. Notes are removed, unknown links drop their line."""
    lines = template.splitlines()
    # The chapter block: the run of lines from the first stamp-like line to the next blank line.
    # Its header line (the line just before it) goes too when there are no stamps.
    first = next((i for i, ln in enumerate(lines) if _STAMP_START.match(ln)), None)
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if first is not None and i == first:
            j = i
            while j < len(lines) and lines[j].strip():
                j += 1
            if stamp_block:
                out.extend(stamp_block)
            elif out and out[-1].strip():
                out.pop()                      # the header ("Chapters") of an empty block
                if out and not out[-1].strip() and j < len(lines) and not lines[j].strip():
                    j += 1                     # and one of the two blank lines around it
            i = j
            continue
        ln = NOTE.sub("", ln).rstrip()
        if _PLAYLIST.search(ln):
            if not playlist_url:
                i += 1
                continue
            ln = _PLAYLIST.sub(playlist_url, ln)
        elif "[N-1]" in ln or "[N+1]" in ln:
            url = prev_url if "[N-1]" in ln else next_url
            if not url or (("[N-1]" in ln) and ep_number <= 1):
                i += 1
                continue
            ln = f"{ARITH.sub(lambda m: _arith(m, vals), ln).rstrip()}: {url}"
        ln = _TEASER.sub(lambda m: teaser if (not m.group(1) or teaser.endswith(('.', '!', '?'))) else teaser + '.', ln)
        ln = _QUESTION.sub(lambda m: question, ln)
        ln = ARITH.sub(lambda m: _arith(m, vals), ln)
        out.append(ln)
        i += 1
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    return text


# ------------------------------------------------------------------ AI

class AIFailure(Exception):
    pass


class ClaudeUnavailable(AIFailure):
    pass


RULES = """You write three free-text pieces for the YouTube publish pack of a Hindi/Hinglish "novel explained" episode.
Everything else (title, description, tags, chapter timestamps) is filled in by code. You only return JSON.

RULES
- teaser: 2 to 3 short sentences in Hinglish (Hindi written in Latin letters, never Devanagari). Spoiler-light: say what
  the viewer will find out, not what happens. Our own words. NEVER quote or paraphrase passages of the novel: no
  sentences from the book, no chapter titles, no dialogue. Do not reuse any run of 6 or more words from the chapters.
  Search phrases stay in English.
- comment_question: ONE short question for viewers about this episode, Hinglish, one line.
- thumbnail_hooks: exactly 3 hooks, one per angle (a: the mystery or danger, b: a character, e.g. "WHO IS ...?",
  c: the stakes or a milestone). Each: ALL CAPS English, 2-5 words in total, two lines split with | and exactly one
  keyword marked for yellow with *...*, in the form "LINE ONE|*KEYWORD* LINE". Examples of the form:
  "THIS VILLAGE|IS *NOT NORMAL*", "WHO IS|*LUMIAN LEE?*", "LOTM 2|*BEGINS!*". A curiosity gap about THIS episode:
  a question, a warning or an unfinished claim. Tease, never reveal the twist. Never repeat the title (it already says
  Episode, chapters, Hindi); never quote the novel or a chapter title.
- thumbnail_character: the one character whose face goes on the thumbnail (a name).

Return ONLY this JSON object, no other text:
{"teaser": "...", "comment_question": "...", "thumbnail_hooks": ["...", "...", "..."], "thumbnail_character": "..."}
"""


def build_prompt(*, book_title: str, ep: int, start: int, end: int, condensed: str, sent_chars: int,
                 research: list[str], idea: str | None, errors: list[str] | None = None) -> str:
    parts = [RULES, "HOOK: make the title, the teaser and the thumbnail lines addictive: an open question or a stake "
                    "the viewer must see answered, a concrete image instead of a summary, no spoiler of the ending and "
                    "no claim the episode does not deliver. Never mention chapters anywhere (no \"Chapter N\", \"Ch N\", ranges).",
             f"BOOK: {book_title}", f"EPISODE: {ep} (Chapter {start} to {end})"]
    if idea:
        parts.append(f"TEASER IDEA from the channel owner (use it as the direction, polish it): {idea}")
    if research:
        parts.append("WHAT PEOPLE SEARCH ON YOUTUBE (autocomplete, ranked; for context only):\n"
                     + "\n".join(f"- {p}" for p in research))
    parts.append(f"THE EPISODE'S ENGLISH CHAPTERS (condensed: first {HEAD_CHARS} and last {TAIL_CHARS} characters "
                 f"of each; {sent_chars} characters in total). Read for understanding only; do not copy from it.\n\n"
                 + condensed)
    if errors:
        parts.append("YOUR PREVIOUS ANSWER FAILED THESE CHECKS. Fix every one and return the full JSON again:\n"
                     + "\n".join(f"- {e}" for e in errors))
    return "\n\n".join(parts)


def _split_cmd(text: str) -> list[str]:
    import shlex
    parts = shlex.split(text, posix=(os.name != "nt"))
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def claude_command() -> list[str]:
    env = os.environ.get("NOVEL_CLAUDE_CMD", "").strip()
    if env:
        return _split_cmd(env)
    exe = shutil.which("claude")
    if not exe:
        raise ClaudeUnavailable("the claude CLI was not found on PATH")
    return [exe, *CLAUDE_ARGS]


def parse_envelope(stdout: str) -> str:
    """The model's text out of `claude -p --output-format json` (one object, or a list of events)."""
    try:
        data = json.loads(stdout)
    except ValueError:
        raise AIFailure("claude output is not JSON")
    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
    if not isinstance(data, dict):
        raise AIFailure("claude output has no result object")
    if data.get("is_error"):
        raise AIFailure(f"claude reported an error: {str(data.get('result'))[:160]}")
    text = data.get("result")
    if not isinstance(text, str) or not text.strip():
        raise AIFailure("claude result is empty")
    return text


def parse_ai_json(text: str) -> dict:
    """The model's JSON object (a code fence around it is fine), checked for shape."""
    s = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S | re.I)
    if m:
        s = m.group(1).strip()
    if not s.startswith("{"):
        a, b = s.find("{"), s.rfind("}")
        if a < 0 or b <= a:
            raise AIFailure("no JSON object in the model's answer")
        s = s[a:b + 1]
    try:
        obj = json.loads(s)
    except ValueError as e:
        raise AIFailure(f"the model's JSON does not parse: {e}")
    if not isinstance(obj, dict):
        raise AIFailure("the model's JSON is not an object")
    if not isinstance(obj.get("teaser"), str) or not isinstance(obj.get("comment_question"), str):
        raise AIFailure("the model's JSON lacks teaser or comment_question")
    hooks = obj.get("thumbnail_hooks")
    if not isinstance(hooks, list) or not all(isinstance(h, str) for h in hooks):
        raise AIFailure("the model's JSON lacks thumbnail_hooks (a list of strings)")
    return obj


def call_claude(prompt: str, timeout: int = CLAUDE_TIMEOUT) -> str:
    cmd = claude_command()
    try:
        with tempfile.TemporaryDirectory() as cwd:     # no project CLAUDE.md is picked up
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        raise ClaudeUnavailable(f"cannot start '{cmd[0]}'")
    except subprocess.TimeoutExpired:
        raise AIFailure(f"claude timed out after {timeout} s")
    except OSError as e:
        raise ClaudeUnavailable(f"cannot start '{cmd[0]}': {e}")
    if p.returncode != 0:
        raise AIFailure(f"claude exited with {p.returncode}: {(p.stderr or p.stdout).strip()[:160]}")
    return parse_envelope(p.stdout)


def call_gemini(system: str, prompt: str, *, model: str, log: Path) -> str:
    """The Gemini API through the pipeline's own Gemini class (key rotation, JSON output)."""
    try:
        from novel.stages.episode_translate import Gemini
        log.parent.mkdir(parents=True, exist_ok=True)
        return Gemini(model, log).ask(system, prompt, json_out=True, temperature=0.7)
    except SystemExit as e:
        raise AIFailure(f"Gemini: {e}")
    except Exception as e:
        raise AIFailure(f"Gemini: {type(e).__name__}: {str(e)[:160]}")


class AI:
    """Runs Claude first, falls back to Gemini, remembers which one answered and why."""

    def __init__(self, engine: str, gemini_model: str, log: Path):
        self.engine, self.reason = engine, None
        self.gemini_model, self.log = gemini_model, log

    def ask(self, prompt: str) -> dict:
        if self.engine == "claude":
            last = None
            for _ in range(CLAUDE_ATTEMPTS):
                try:
                    return parse_ai_json(call_claude(prompt))
                except ClaudeUnavailable as e:
                    last = str(e)
                    break
                except AIFailure as e:
                    last = str(e)
            self.engine, self.reason = "gemini", f"claude failed: {last}"
            print(f"  Claude CLI failed ({last}); using Gemini instead.", file=sys.stderr)
        last = None
        for _ in range(2):
            try:
                return parse_ai_json(call_gemini(RULES, prompt, model=self.gemini_model, log=self.log))
            except AIFailure as e:
                last = str(e)
        raise AIFailure(f"Gemini also failed: {last}")


# ------------------------------------------------------------------ settings table

def checklist(series: str, category: str, playlist_name: str | None, publish_time: str | None) -> list[tuple[str, str]]:
    """The settings table from the coi-episode-publish skill (same every episode)."""
    return [
        ("Category", category or "Entertainment"),
        ("Video language + audio language", "Hindi"),
        ("Playlist", playlist_name or f"{series} Explained in Hindi"),
        ("Audience", "No, not made for kids"),
        ("Comments", "On"),
        ("Subtitles", "Hindi .srt if available, else auto-captions"),
        ("Thumbnail", "1280x720 from `novel thumb` (3 hooks below); Test & compare if Studio offers it, "
                      "else upload thumb-a.jpg"),
        ("End screen", "last 20 s: next episode (or the playlist) + subscribe"),
        ("Publish time", publish_time or "7-9 PM IST, same day pattern every time"),
        ("Paid promotion / altered content", "No / No"),
    ]


def render_txt(pack: dict, *, book: str, ep: int, checklist_rows, character: str | None) -> str:
    bar = "=" * 72
    out = []
    if not pack["valid"]:
        out += [bar, "NOT VALID - fix these before pasting anything:", *[f"  - {e}" for e in pack["errors"]], bar, ""]
    out += [f"{bar}\n1. TITLE ({len(pack['title'])}/{validate.TITLE_MAX})\n{bar}", pack["title"], "",
            f"{bar}\n2. DESCRIPTION ({len(pack['description'])}/{validate.DESCRIPTION_MAX})\n{bar}",
            pack["description"], "",
            f"{bar}\n3. TAGS ({len(pack['tags'])} tags, {validate.tags_length(pack['tags'])}/{validate.TAGS_MAX} characters)"
            f"\n{bar}", ", ".join(pack["tags"]), "",
            f"{bar}\n4. STUDIO SETTINGS\n{bar}"]
    out += [f"[ ] {k}: {v}" for k, v in checklist_rows]
    out += ["", f"{bar}\n5. THUMBNAIL HOOKS (3 variants; novel thumb reads them from publish.json)\n{bar}"]
    out += [f"{i}. {h}" for i, h in enumerate(pack["thumbnail_hooks"], 1)] or ["(none: pass --hook to novel thumb)"]
    if character:
        out.append(f"Face on the thumbnail: {character}")
    art = f" --image <art>" if not character else f" --image {character.lower().replace(' ', '-')}"
    out += ["", f"Next: novel thumb --book {book} --ep {ep}{art}", ""]
    return "\n".join(out)


# ------------------------------------------------------------------ files

def archive_previous(pub_dir: Path, now: datetime) -> Path | None:
    old = [pub_dir / n for n in ("publish.json", "publish.txt") if (pub_dir / n).is_file()]
    if not old:
        return None
    dest = pub_dir / "old" / f"{now:%Y%m%d-%H%M%S}"
    k = 1
    while dest.exists():
        k += 1
        dest = pub_dir / "old" / f"{now:%Y%m%d-%H%M%S}-{k}"
    dest.mkdir(parents=True)
    for f in old:
        shutil.move(str(f), str(dest / f.name))
    return dest


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
