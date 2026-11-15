"""The Gemini reply contract: split a reply into blocks, parse JSON Lines, validate IMAGES/PATCH lines.

Pure functions: nothing here reads or writes files (novel.gemini.importer does the I/O).

Blocks (Appendix A of the plan):
    ===== BEGIN <KIND> <ID> -> inbox/<file> =====
    ...
    ===== END <KIND> <ID> =====
KIND is BIBLE, HINDI, IMAGES or PATCH. Tolerated chat-app damage: any run of 3+ '=', extra spaces, CRLF,
'→' for '->', markdown around the delimiter (**, backticks, a leading '>', '#'), a missing or wrong
'inbox/<file>' part (kind and id decide the file), lower-case kinds, chapter ids c21 / C0021.
Control line:  >>> MORE c0021 IMAGES  (that block is still owed; say "next").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

KINDS = ("BIBLE", "HINDI", "IMAGES", "PATCH")
JSONL_KINDS = ("BIBLE", "IMAGES", "PATCH")
TIMES = ("dawn", "day", "dusk", "night")
WHERES = ("indoor", "outdoor")
KIND_WHERE = {"interior": "indoor", "exterior": "outdoor"}   # a location kind -> the default `where`
SHOTS_V2 = ("wide", "establishing", "medium", "over-the-shoulder", "low-angle", "high-angle", "detail")
ACTION_MIN, ACTION_MAX = 60, 600
WEARING_VARIANT_AFTER = 6          # the same non-null `wearing` on more images than this: "make a variant"
EXCERPT = 90

_EDGE = re.compile(r"^[\s>#*_`]+|[\s*_`]+$")
_ARROW = r"(?:->|→|=>|–>|—>|-&gt;)"
BEGIN_RX = re.compile(r"^={3,}\s*BEGIN\s+([A-Za-z]+)\s+([A-Za-z0-9_-]+)\s*(?:" + _ARROW + r"\s*(\S*))?\s*=*$",
                      re.I)
END_RX = re.compile(r"^={3,}\s*END\s+([A-Za-z]+)\s*([A-Za-z0-9_-]*)\s*=*$", re.I)
MORE_RX = re.compile(r">{3}\s*MORE\s+([A-Za-z]\d+)\s+([A-Za-z]+)", re.I)
FENCE_RX = re.compile(r"^`{3,}\s*[\w-]*\s*$")
LABEL_RX = re.compile(r"^(?:jsonl?|json lines|code snippet|copy code)$", re.I)
NBSP = {0x00A0: " ", 0x202F: " ", 0x2007: " ", 0x2060: None, 0xFEFF: None, 0x200B: None}
SMART_DELIM = re.compile(r"[{,:\[]\s*[“”‘’„‟]|[“”‘’„‟]\s*[:,}\]]")
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


# ------------------------------------------------------------------ ids and file names

def chapter_of(text: str) -> int | None:
    """c0021, c21, C0021 -> 21."""
    m = re.fullmatch(r"[cC](\d{1,5})", text or "")
    return int(m.group(1)) if m else None


def batch_id(first: int, last: int) -> str:
    from novel.gemini.prompt import batch_id as bid
    return bid(first, last)


def bible_id(text: str) -> tuple[str, int, int, int | None] | None:
    """B021-030 -> ("B021-030", 21, 30, None); B021-030-c0021 -> ("B021-030", 21, 30, 21)."""
    m = re.fullmatch(r"[bB](\d+)-(\d+)(?:-[cC](\d+))?", text or "")
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return batch_id(a, b), a, b, (int(m.group(3)) if m.group(3) else None)


def block_name(kind: str, batch: str | None, chapter: int | None) -> str:
    """The id as the contract writes it: B021-030, B021-030-c0021, c0021."""
    if kind == "BIBLE":
        return batch + (f"-c{chapter:04d}" if chapter is not None else "")
    return f"c{chapter:04d}"


def inbox_file(kind: str, batch: str | None, chapter: int | None) -> str:
    if kind == "BIBLE":
        return f"bible-{block_name(kind, batch, chapter)}.jsonl"
    return {"HINDI": f"c{chapter:04d}.hindi.txt", "IMAGES": f"c{chapter:04d}.images.jsonl",
            "PATCH": f"c{chapter:04d}.patch.jsonl"}[kind]


INBOX_NAME_RX = re.compile(r"^(?:bible-([bB]\d+-\d+(?:-[cC]\d+)?)\.jsonl|([cC]\d+)\.(images|patch)\.jsonl|"
                           r"([cC]\d+)\.hindi\.txt)$")


def parse_inbox_name(name: str) -> tuple[str, str] | None:
    """File name in the inbox -> (KIND, id) or None when the name is not one of the contract's."""
    m = INBOX_NAME_RX.match(name)
    if not m:
        return None
    if m.group(1):
        return "BIBLE", m.group(1)
    if m.group(2):
        return m.group(3).upper(), m.group(2)
    return "HINDI", m.group(4)


# ------------------------------------------------------------------ issues

@dataclass
class Issue:
    line: int | None          # line within the block (1 = the line after BEGIN)
    what: str                 # "image 7", "group g01", "cast[0].hands", ... ("" when not known)
    msg: str
    text: str = ""            # the offending text, shortened

    def render(self, with_text: bool = False) -> str:
        where = f"line {self.line}" if self.line else "block"
        head = f"{where}{f' ({self.what})' if self.what else ''}: {self.msg}"
        if with_text and self.text:
            head += f"\n      > {self.text}"
        return head


def excerpt(text: str, col: int | None = None, width: int = EXCERPT) -> str:
    text = " ".join(str(text).split())
    if col is None or len(text) <= width:
        return text if len(text) <= width else text[:width - 3] + "..."
    a = max(0, col - width // 2)
    b = min(len(text), a + width)
    return ("..." if a else "") + text[a:b] + ("..." if b < len(text) else "")


# ------------------------------------------------------------------ splitting a reply

@dataclass
class Block:
    kind: str                       # upper case; may be an unknown kind
    raw_id: str
    begin_line: int                 # line number in the reply
    lines: list[str] = field(default_factory=list)
    complete: bool = False
    quoted: bool = False
    file_hint: str | None = None    # what the BEGIN line said after '->'
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    chapter: int | None = None
    batch: str | None = None        # BIBLE: from the id
    batch_range: tuple[int, int] | None = None
    origin: str = "reply"           # reply | inbox
    begin_idx: int = -1             # 0-based index of the BEGIN line among the reply's lines
    end_idx: int = -1               # index of the END line; -1 when the block is cut off

    @property
    def name(self) -> str:
        if self.kind in KINDS and (self.batch or self.chapter is not None):
            return block_name(self.kind, self.batch, self.chapter)
        return self.raw_id

    @property
    def label(self) -> str:
        """How the user and Gemini name it: BIBLE B021-030, IMAGES c0021."""
        return f"{self.kind} {self.name}"

    @property
    def say_repeat(self) -> str:
        """The words for Gemini: `repeat BIBLE B021-030` / `repeat c0021 IMAGES`."""
        return f"BIBLE {self.name}" if self.kind == "BIBLE" else f"{self.name} {self.kind}"

    @property
    def inbox_name(self) -> str | None:
        if self.kind not in KINDS:
            return None
        if self.kind == "BIBLE" and not self.batch:
            return None
        if self.kind != "BIBLE" and self.chapter is None:
            return None
        return inbox_file(self.kind, self.batch, self.chapter)


@dataclass
class Reply:
    blocks: list[Block]
    more: list[tuple[int, str]]     # (chapter, KIND) still owed
    ignored: int                    # non-blank lines outside blocks (fences excluded)
    notes: list[str]                # reply-level remarks (END without BEGIN, ...)
    total_lines: int


def _clean_delim(line: str) -> str:
    return _EDGE.sub("", line.translate(NBSP))


def identify(block: Block) -> None:
    """Fill kind/chapter/batch from the raw id; problems become block errors."""
    k = block.kind
    if k not in KINDS:
        block.errors.append(Issue(None, "", f"unknown block kind {k!r} (expected {', '.join(KINDS)})"))
        return
    if k == "BIBLE":
        b = bible_id(block.raw_id)
        if not b:
            block.errors.append(Issue(None, "", f"BIBLE id {block.raw_id!r} is not like B021-030 "
                                                f"(or B021-030-c0021 for a chapter's extra bible lines)"))
            return
        block.batch, a, z, block.chapter = b
        block.batch_range = (a, z)
    else:
        n = chapter_of(block.raw_id)
        if n is None:
            block.errors.append(Issue(None, "", f"{k} id {block.raw_id!r} is not a chapter id like c0021"))
            return
        block.chapter = n
    want = block.inbox_name
    if block.file_hint and want and block.file_hint.split("/")[-1].strip("`*") != want:
        block.warnings.append(Issue(None, "", f"BEGIN line names file {block.file_hint!r}; "
                                              f"using inbox/{want} (kind and id decide the file)"))


def split_reply(text: str) -> Reply:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    more: list[tuple[int, str]] = []
    notes: list[str] = []
    ignored = 0
    cur: Block | None = None
    for no, raw in enumerate(lines, 1):
        plain = raw.translate(NBSP).replace("**", "").replace("`", "")
        mm = MORE_RX.search(plain)
        if mm:
            ch = chapter_of(mm.group(1))
            if ch is not None:
                more.append((ch, mm.group(2).upper()))
            else:
                notes.append(f"reply line {no}: '>>> MORE' names {mm.group(1)!r}, not a chapter id; ignored")
            continue
        clean = _clean_delim(raw)
        mb = BEGIN_RX.match(clean)
        if mb:
            if cur is not None:                     # a new BEGIN while one is open: that one was cut off
                blocks.append(cur)
            cur = Block(kind=mb.group(1).upper(), raw_id=mb.group(2), begin_line=no,
                        quoted=raw.lstrip().startswith(">"), file_hint=mb.group(3) or None, begin_idx=no - 1)
            identify(cur)
            continue
        me = END_RX.match(clean)
        if me:
            if cur is None:
                notes.append(f"reply line {no}: END line without a BEGIN; ignored")
                continue
            kind, rid = me.group(1).upper(), me.group(2)
            same_id = (rid == "" or rid == cur.raw_id or
                       (cur.kind != "BIBLE" and chapter_of(rid) == cur.chapter) or
                       (cur.kind == "BIBLE" and bible_id(rid) and bible_id(rid)[0] == cur.batch
                        and bible_id(rid)[3] == cur.chapter))
            if kind != cur.kind or not same_id:
                cur.errors.append(Issue(len(cur.lines) + 1, "END",
                                        f"END line says {kind} {rid}, but the block began {cur.kind} {cur.raw_id}",
                                        excerpt(raw)))
            cur.complete = True
            cur.end_idx = no - 1
            blocks.append(cur)
            cur = None
            continue
        if cur is not None:
            cur.lines.append(raw)
        elif raw.strip() and not FENCE_RX.match(raw.strip()):
            ignored += 1
    if cur is not None:
        blocks.append(cur)
    for b in blocks:
        if not b.complete:
            b.errors.insert(0, Issue(None, "", f"cut off: no END line (the reply stopped inside the block after "
                                               f"{len(b.lines)} lines)"))
    return Reply(blocks, more, ignored, notes, len(lines))


def inbox_block(name: str, text: str) -> Block:
    """A file saved by hand in the inbox, read as one complete block."""
    kind, rid = parse_inbox_name(name)
    b = Block(kind=kind, raw_id=rid, begin_line=0, complete=True, origin="inbox")
    b.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # A by-hand file may still carry the BEGIN/END lines: blank them (line numbers stay the file's own).
    b.lines = ["" if (BEGIN_RX.match(_clean_delim(x)) or END_RX.match(_clean_delim(x)) or MORE_RX.search(x))
               else x for x in b.lines]
    identify(b)
    return b


# ------------------------------------------------------------------ JSON Lines

@dataclass
class Item:
    line: int                 # first line of the object within the block
    obj: dict
    last: int                 # last line (== line unless re-joined)

    @property
    def where(self) -> str:
        return f"{self.line}" if self.last == self.line else f"{self.line}-{self.last}"


def _depth(text: str) -> tuple[int, bool]:
    """Brace depth outside strings at the end of text, and whether a string is still open."""
    depth, in_str, esc = 0, False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return depth, in_str


def _drop_commas_before_close(text: str) -> str:
    out, in_str, esc, i = [], False, False, 0
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch == ",":
            j = i + 1
            while j < len(text) and text[j] in " \t":
                j += 1
            if j < len(text) and text[j] in "}]":
                i += 1
                continue
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _loads(text: str) -> tuple[dict | None, str | None, list[str], int | None]:
    """(object, error, repairs, error column)."""
    fixes = []
    t = text.strip()
    if t.endswith(","):
        t = t[:-1].rstrip()
        fixes.append("trailing comma at the end of the line removed")
    try:
        obj = json.loads(t, strict=False)
    except json.JSONDecodeError as e:
        if SMART_DELIM.search(t):
            m = SMART_DELIM.search(t)
            return None, ("uses curly quotes (“ ” ‘ ’) as JSON quotes; JSON needs straight double quotes (\"). "
                          "Not repaired: ask Gemini to repeat the block"), fixes, m.start()
        t2 = _drop_commas_before_close(t)
        if t2 != t:
            try:
                obj = json.loads(t2, strict=False)
                fixes.append("comma before } or ] removed")
            except json.JSONDecodeError:
                return None, f"not valid JSON at column {e.colno}: {e.msg}", fixes, e.colno
        else:
            return None, f"not valid JSON at column {e.colno}: {e.msg}", fixes, e.colno
    if not isinstance(obj, dict):
        return None, "not a JSON object (a line must be {...})", fixes, None
    return obj, None, fixes, None


def json_line_events(lines: list[str], quoted: bool = False):
    """The grouping of a block body into JSON objects, shared by parse_jsonl and drop_unreadable. Yields, in reading
    order: ("object", first, last, text) for one candidate object (lines first..last re-joined into text),
    ("error", Issue, first, last) for lines that are not an object (lines first..last), and ("warn", Issue).
    Fences, blank lines and language labels are skipped; nothing is repaired here (see _loads)."""
    buf: list[str] | None = None
    start = 0
    nbsp_lines: list[int] = []
    for no, raw in enumerate(lines, 1):
        s = raw
        if quoted:
            s = re.sub(r"^\s*(?:>\s?)+", "", s)
        t = s.translate(NBSP)
        if t != s:
            nbsp_lines.append(no)
        st = t.strip()
        if buf is None:
            if not st or FENCE_RX.match(st):
                continue
            if LABEL_RX.match(st):
                yield ("warn", Issue(no, "", f"skipped the label line {st!r}"))
                continue
            if not st.startswith("{"):
                yield ("error", Issue(no, "", "not a JSON object line (every line must start with {)",
                                      excerpt(st)), no, no)
                continue
            buf, start = [st], no
        else:
            if FENCE_RX.match(st) or (st.startswith('{"type"') and len(buf) == 1 and buf[0].endswith("}")):
                yield ("error", Issue(start, "", "this object never closes (a quote or brace is missing)",
                                      excerpt(" ".join(buf))), start, no - 1)
                buf = None
                if FENCE_RX.match(st):
                    continue
                buf, start = [st], no
            else:
                buf.append(st)
        text = " ".join(buf)
        depth, in_str = _depth(text)
        if depth > 0:
            continue
        buf = None
        yield ("object", start, no, text)
    if buf is not None:
        yield ("error", Issue(start, "", "this object never closes before the end of the block (cut off?)",
                              excerpt(" ".join(buf))), start, len(lines))
    if nbsp_lines:
        shown = ", ".join(map(str, nbsp_lines[:8])) + (" ..." if len(nbsp_lines) > 8 else "")
        yield ("warn", Issue(nbsp_lines[0], "", f"non-breaking spaces replaced (lines {shown})"))


def parse_jsonl(lines: list[str], quoted: bool = False) -> tuple[list[Item], list[Issue], list[Issue]]:
    """JSON objects of a block body, one per line (fences, blank lines and language labels skipped).
    Repaired silently with a warning: trailing commas, an object pretty-printed over several lines,
    non-breaking spaces. Never repaired: curly quotes used as JSON quotes."""
    items: list[Item] = []
    errs: list[Issue] = []
    warns: list[Issue] = []
    for ev in json_line_events(lines, quoted):
        if ev[0] == "object":
            _, first, last, text = ev
            obj, err, fixes, col = _loads(text)
            where = f"{first}" if first == last else f"{first}-{last}"
            if err:
                what = f"lines {where}" if first != last else ""
                errs.append(Issue(first, what, err, excerpt(text, col)))
                continue
            if first != last:
                warns.append(Issue(first, "", f"one JSON object was spread over lines {where}; re-joined"))
            for f in fixes:
                warns.append(Issue(first, "", f))
            items.append(Item(first, obj, last))
        elif ev[0] == "error":
            errs.append(ev[1])
        else:
            warns.append(ev[1])
    return items, errs, warns


@dataclass
class Dropped:
    """What drop_unreadable took out of one IMAGES block body."""
    kept: list[str] = field(default_factory=list)      # the raw lines to keep
    bad: list[str] = field(default_factory=list)       # the raw lines that are not valid JSON (as sent)
    issues: list[Issue] = field(default_factory=list)  # one per dropped object or line
    windows: list[int] = field(default_factory=list)   # window numbers read from the bad lines ("n":N)
    groups: list[str] = field(default_factory=list)    # ids of dropped group lines that could be read
    held: list[int] = field(default_factory=list)      # windows of valid image lines held back (group dropped)
    objects: int = 0                                   # valid objects kept (held-back ones not counted)


def drop_unreadable(lines: list[str], quoted: bool = False) -> Dropped:
    """Take out of a block body every line that is not valid JSON after _loads' repairs (the same repairs
    parse_jsonl makes). A valid image line whose group line was dropped is held back too: the importer would
    reject the whole block for an unknown group. The lines that are kept are returned unchanged."""
    n = len(lines)
    bad_mask, held_mask = [False] * n, [False] * n
    out = Dropped()
    valid_groups: set[str] = set()
    images: list[tuple[int, int, str, object]] = []

    def mark(mask: list[bool], first: int, last: int) -> None:
        for i in range(first - 1, last):
            mask[i] = True

    for ev in json_line_events(lines, quoted):
        if ev[0] == "object":
            _, first, last, text = ev
            obj, err, _, col = _loads(text)
            if err:
                out.issues.append(Issue(first, "", err, excerpt(text, col)))
                mark(bad_mask, first, last)
                continue
            out.objects += 1
            if obj.get("type") == "group":
                valid_groups.add(str(obj.get("group")))
            elif obj.get("type") == "image":
                images.append((first, last, str(obj.get("group")), obj.get("n")))
        elif ev[0] == "error":
            out.issues.append(ev[1])
            mark(bad_mask, ev[2], ev[3])
    bad_text = " ".join(line for line, m in zip(lines, bad_mask) if m)
    for m in re.finditer(r'"type"\s*:\s*"group"[^{}]*?"group"\s*:\s*"([^"]+)"', bad_text):
        if m.group(1) not in valid_groups and m.group(1) not in out.groups:
            out.groups.append(m.group(1))
    for first, last, group, num in images:
        if group in out.groups and group not in valid_groups:
            mark(held_mask, first, last)
            out.held.append(num)
            out.objects -= 1
    for m in re.finditer(r'"n"\s*:\s*(\d+)', bad_text):
        w = int(m.group(1))
        if w not in out.windows:
            out.windows.append(w)
    out.windows.sort()
    out.held.sort()
    for i, line in enumerate(lines):
        if bad_mask[i]:
            out.bad.append(line)
        elif not held_mask[i]:
            out.kept.append(line)
    return out


# ------------------------------------------------------------------ IMAGES / PATCH lines

@dataclass
class ChapterCtx:
    chapter: int
    count: int                      # windows (scenes mode) or markers (full mode, later)
    state: dict                     # bible state: {"characters", "locations", "names"}
    max_characters: int = 4
    banned: re.Pattern | None = None

    @property
    def characters(self) -> dict:
        return self.state["characters"].get("characters") or {}

    @property
    def locations(self) -> dict:
        return self.state["locations"].get("locations") or {}


def _is_text(v) -> bool:
    return isinstance(v, str)


def _nonempty(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _clean_text(v: str) -> str:
    return " ".join(str(v).translate(NBSP).split())


# What the scene writer sometimes puts in `others` when nobody else is in the picture. Compared after trimming,
# lower-casing and dropping one trailing full stop.
NO_OTHERS = frozenset({"empty", "none", "no one", "nobody", "n/a", "na", "-", "nil", "no others",
                       "no other people"})


def no_others(text) -> bool:
    """True when an `others` value means "nobody else": the placeholder words above, or nothing."""
    s = " ".join(str(text or "").split()).lower()
    if s.endswith("."):
        s = s[:-1].rstrip()
    return s == "" or s in NO_OTHERS


def _text_problems(value: str, ctx: ChapterCtx) -> list[str]:
    out = []
    if DEVANAGARI.search(value):
        out.append("must be English (Devanagari found)")
    if ctx.banned is not None:
        m = ctx.banned.search(value)
        if m:
            out.append(f"uses the banned word {m.group(0)!r}")
    return out


def replace_keys(text: str, ctx: ChapterCtx) -> tuple[str, list[str]]:
    """Bible keys written as words (lumian_lee) -> display names. A one-word key is also an ordinary
    word ("owl" for "The Owl"), so it is replaced only when the display name is the same word
    capitalised ("leah" -> "Leah")."""
    found = []
    names = {k: (c.get("display") or k) for k, c in ctx.characters.items()}
    names.update({k: (l.get("name") or k) for k, l in ctx.locations.items() if k not in names})
    for key in sorted(names, key=len, reverse=True):
        if "_" not in key and (names[key].lower() != key or names[key] == key):
            continue
        rx = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"(?![A-Za-z0-9_])")
        if rx.search(text):
            text = rx.sub(names[key], text)
            found.append(f"{key} -> {names[key]}")
    return text, found


def _close(key: str, pool) -> str:
    import difflib
    near = difflib.get_close_matches(key, list(pool), n=3, cutoff=0.6)
    return f" (did you mean {', '.join(near)}?)" if near else ""


def kind_where(ctx: ChapterCtx, loc) -> str | None:
    """indoor / outdoor from the location's `kind` (interior / exterior), or None when the kind says neither."""
    return KIND_WHERE.get(str((ctx.locations.get(loc) or {}).get("kind") or "").strip().lower())


def check_group(obj: dict, ctx: ChapterCtx) -> tuple[dict | None, list[tuple[str, str]], list[str]]:
    """(normalised line, errors as (field, message), warnings). `where` is required; a line without it
    (an older reply or file) falls back to the location's kind with a warning, and is an error only when
    the kind does not say either."""
    errs: list[tuple[str, str]] = []
    warns: list[str] = []
    gid = obj.get("group")
    if not (_nonempty(gid) and " " not in gid.strip()):
        errs.append(("group", "missing or not a short id like g01"))
    for f in ("loc", "light", "staging"):
        if not _nonempty(obj.get(f)):
            errs.append((f, "missing or empty"))
    t = obj.get("time")
    if not (_is_text(t) and t.strip().lower() in TIMES):
        errs.append(("time", f"{t!r} is not one of {'|'.join(TIMES)}"))
    loc = obj.get("loc")
    if _nonempty(loc) and loc not in ctx.locations:
        errs.append(("loc", f"location '{loc}' is not in the location registry{_close(loc, ctx.locations)}; "
                            f"add it with a BIBLE location line first"))
    where = obj.get("where")
    by_kind = kind_where(ctx, loc) if _nonempty(loc) else None
    need = 'write "where": "indoor" or "outdoor"'
    if where is None or (isinstance(where, str) and not where.strip()):
        if by_kind:
            where = by_kind
            warns.append(f"where is missing: using '{by_kind}' from the location's kind "
                         f"('{ctx.locations[loc].get('kind')}'); every group needs \"where\": \"indoor\" or "
                         f"\"outdoor\"")
        elif _nonempty(loc) and loc in ctx.locations:
            errs.append(("where", f"missing, and the location '{loc}' has kind "
                                  f"'{ctx.locations[loc].get('kind') or '-'}', which does not say: {need}"))
        else:
            errs.append(("where", f"missing: {need}"))
    elif not (_is_text(where) and where.strip().lower() in WHERES):
        errs.append(("where", f"{where!r} is not one of {'|'.join(WHERES)}"))
    else:
        where = where.strip().lower()
    present = obj.get("present")
    if not (isinstance(present, list) and all(_is_text(p) for p in present)):
        errs.append(("present", "must be a list of character keys"))
        present = []
    for p in present:
        if p not in ctx.characters:
            errs.append(("present", f"unknown character '{p}'{_close(p, ctx.characters)}"))
    w = obj.get("weather")
    if w is not None and not _is_text(w):
        errs.append(("weather", "must be text"))
    for f in ("light", "staging", "weather"):
        if _nonempty(obj.get(f)):
            errs += [(f, m) for m in _text_problems(obj[f], ctx)]
    extra = set(obj) - {"type", "group", "loc", "time", "where", "light", "weather", "present", "staging"}
    if errs:
        return None, errs, []
    norm = {"type": "group", "group": gid.strip(), "loc": loc, "time": t.strip().lower(), "where": where,
            "light": _clean_text(obj["light"]), "weather": _clean_text(w) if _nonempty(w) else "",
            "present": list(dict.fromkeys(present)), "staging": _clean_text(obj["staging"])}
    if extra:
        norm["_extra"] = sorted(extra)
    return norm, errs, warns


# Word lists of the light / time / where check (warnings only: they look for OBVIOUS contradictions).
SUN_WORDS = re.compile(r"(?<![a-z])(sun|sunlight|sunlit|sunshine|sunny|sunbeams?|sunrays?|daylight|midday|noon|"
                       r"afternoon|blue sky)(?![a-z])")
NIGHT_SKY_WORDS = re.compile(r"(?<![a-z])(moon|moonlight|moonlit|moonbeams?|starlight|starlit|starry|stars)(?![a-z])")
LAMP_WORDS = re.compile(r"(?<![a-z])(lamp|lamps|lamplight|lantern|lanterns|candle|candles|candlelight|torch|torches|"
                        r"firelight|fire|gaslight|gas light)(?![a-z])")
OPENING_WORDS = re.compile(r"(?<![a-z])(window|windows|skylight|doorway|door|open|arch|balcony|roof|glass)(?![a-z])")
SKY_WORDS = re.compile(r"(?<![a-z])(sky|overcast|cloud|clouds|cloudy|bright|natural|grey|gray|morning)(?![a-z])")
NEGATION = re.compile(r"(?<![a-z])(no|not|without|never)\s+(?:\w+\s+){0,2}$")


def _hits(rx: re.Pattern, text: str) -> list[str]:
    """Words of `rx` in `text` that no 'no / not / without' (up to two words before) negates."""
    return [m.group(1) for m in rx.finditer(text) if not NEGATION.search(text[:m.start()])]


def setting_problems(g: dict) -> list[str]:
    """Warnings about one group's `light` against its `time` and `where`: sun words at night; moon or star
    words by day; moon or star words indoors with no window, door or opening in the light or the staging; lamps
    as the only light outdoors by day."""
    light = str(g.get("light") or "").lower()
    stage = str(g.get("staging") or "").lower()
    time, where, gid = g.get("time"), g.get("where"), g.get("group")
    out = []
    sun, sky_night, lamps = _hits(SUN_WORDS, light), _hits(NIGHT_SKY_WORDS, light), _hits(LAMP_WORDS, light)
    if time == "night" and sun:
        out.append(f"group {gid}: the light mentions '{sun[0]}' but the time is night")
    if time == "day" and sky_night:
        out.append(f"group {gid}: the light mentions '{sky_night[0]}' but the time is day")
    if where == "indoor" and sky_night and not (OPENING_WORDS.search(light) or OPENING_WORDS.search(stage)):
        out.append(f"group {gid}: the light mentions '{sky_night[0]}' indoors, and no window or opening is "
                   f"mentioned in the light or the staging")
    if where == "outdoor" and time == "day" and lamps and not sun and not SKY_WORDS.search(light):
        out.append(f"group {gid}: the only light outdoors by day is '{lamps[0]}': is the sun or the sky meant?")
    return out


def setting_moves(groups_in_order: list[dict]) -> list[str]:
    """Notes for two consecutive groups in the same location at the same time of day but with a different
    `where`: a human should see the move through the door."""
    out = []
    for a, b in zip(groups_in_order, groups_in_order[1:]):
        if a.get("loc") and a.get("loc") == b.get("loc") and a.get("time") == b.get("time") \
                and a.get("where") and b.get("where") and a.get("where") != b.get("where"):
            out.append(f"note: group {b['group']} follows group {a['group']} in the same place ({a['loc']}) at the "
                       f"same time ({a['time']}) but moves from {a['where']} to {b['where']}: check the move "
                       f"through the door")
    return out


def attached(ctx: ChapterCtx, key: str, var: str) -> bool:
    """True when this cast member is a Flow Character (has a reference); tier-3 people are text only."""
    from novel import bible as B
    try:
        return not B.resolve(ctx.state["characters"], key, var, ctx.chapter)["text_only"]
    except B.BibleError:
        return False


def check_image(obj: dict, ctx: ChapterCtx) -> tuple[dict | None, list[tuple[str, str]], list[str]]:
    """(normalised line, errors as (field, message), warnings)."""
    from novel import bible as B
    errs: list[tuple[str, str]] = []
    warns: list[str] = []
    n = obj.get("n")
    if not (isinstance(n, int) and not isinstance(n, bool)):
        errs.append(("n", f"{n!r} is not a whole number"))
    elif not 1 <= n <= ctx.count:
        errs.append(("n", f"window {n} does not exist: chapter {ctx.chapter} has {ctx.count} "
                          f"(n runs 1-{ctx.count})"))
    if not _nonempty(obj.get("group")):
        errs.append(("group", "missing"))
    shot = obj.get("shot")
    if shot not in SHOTS_V2:
        errs.append(("shot", f"{shot!r} is not one of {', '.join(SHOTS_V2)}"))

    cast = obj.get("cast")
    norm_cast = []
    if not isinstance(cast, list):
        errs.append(("cast", "must be a list (empty [] when nobody named is in the picture)"))
        cast = []
    seen = set()
    n_attached = 0
    for i, c in enumerate(cast):
        tag = f"cast[{i}]"
        if not isinstance(c, dict):
            errs.append((tag, "must be an object {key, var, pose, hands, mood, wearing}"))
            continue
        key, var = c.get("key"), c.get("var")
        if not _nonempty(key):
            errs.append((f"{tag}.key", "missing"))
            continue
        tag = f"cast[{i}] {key}"
        if key in seen:
            errs.append((tag, "listed twice in this image"))
        seen.add(key)
        if key not in ctx.characters:
            errs.append((f"{tag}.key", f"unknown character '{key}'{_close(key, ctx.characters)}; "
                                       f"new people need a BIBLE character line first"))
        elif not _nonempty(var):
            errs.append((f"{tag}.var", "missing (usually \"base\")"))
        else:
            try:
                r = B.resolve(ctx.state["characters"], key, var, ctx.chapter)
                n_attached += 0 if r["text_only"] else 1
            except B.BibleError as e:
                errs.append((f"{tag}.var", str(e)))
        if not _nonempty(c.get("hands")):
            errs.append((f"{tag}.hands", "missing or empty: say what the hands do, e.g. \"empty, at her sides\""))
        if not _nonempty(c.get("position")):
            errs.append((f"{tag}.position", "missing or empty: say where this person is in the frame and the place, "
                                            "e.g. \"left foreground, by the door\" (two people never share a position)"))
        elif _text_problems(c["position"], ctx):
            errs += [(f"{tag}.position", m) for m in _text_problems(c["position"], ctx)]
        for f in ("pose", "mood"):
            if f not in c or not _is_text(c.get(f)):
                errs.append((f"{tag}.{f}", "missing" if f not in c else "must be text"))
        wearing = c.get("wearing")
        if wearing is not None and not _is_text(wearing):
            errs.append((f"{tag}.wearing", "must be null or text"))
            wearing = None
        for f in ("pose", "hands", "mood", "wearing"):
            if _nonempty(c.get(f)):
                errs += [(f"{tag}.{f}", m) for m in _text_problems(c[f], ctx)]
        norm_cast.append({"key": key, "var": var if _nonempty(var) else "base",
                          "position": _clean_text(c.get("position") or ""),
                          "pose": _clean_text(c.get("pose") or ""), "hands": _clean_text(c.get("hands") or ""),
                          "mood": _clean_text(c.get("mood") or ""),
                          "wearing": _clean_text(wearing) if _nonempty(wearing) else None})
    shared: dict[str, str] = {}
    for c in norm_cast:
        p = c["position"].lower().strip(" .,;")
        if p and p in shared:
            errs.append(("cast", f"{c['key']} and {shared[p]} share the position '{c['position']}': two people never "
                                 f"share a position"))
        elif p:
            shared[p] = c["key"]
    if n_attached > ctx.max_characters:
        errs.append(("cast", f"{n_attached} attached characters (with a Flow reference); at most "
                             f"{ctx.max_characters} (text-only tier-3 people do not count)"))

    others = obj.get("others")
    if not _is_text(others):
        errs.append(("others", "missing: a text, \"\" when nobody else is in the picture"))
        others = ""
    action = obj.get("action")
    if not _is_text(action):
        errs.append(("action", "missing"))
        action = ""
    action = _clean_text(action)
    action, swapped = replace_keys(action, ctx)
    if swapped:
        warns.append("action: bible keys replaced by display names (" + ", ".join(swapped) + ")")
    others = "" if no_others(others) else _clean_text(others)
    others, swapped = replace_keys(others, ctx)
    if swapped:
        warns.append("others: bible keys replaced by display names (" + ", ".join(swapped) + ")")
    if action and not ACTION_MIN <= len(action) <= ACTION_MAX:
        errs.append(("action", f"{len(action)} characters; needs {ACTION_MIN}-{ACTION_MAX}"))
    errs += [("action", m) for m in _text_problems(action, ctx)]
    errs += [("others", m) for m in _text_problems(others, ctx)]

    focus = obj.get("focus")
    if not (isinstance(focus, list) and len(focus) == 2 and
            all(isinstance(f, (int, float)) and not isinstance(f, bool) and 0 <= f <= 1 for f in focus)):
        errs.append(("focus", f"{focus!r} must be two numbers [x, y] between 0 and 1"))
    if errs:
        return None, errs, warns
    norm = {"type": "image", "n": n, "group": obj["group"].strip(), "shot": shot, "cast": norm_cast,
            "others": others, "action": action, "focus": [float(focus[0]), float(focus[1])]}
    return norm, errs, warns


def check_line(obj: dict, ctx: ChapterCtx):
    """-> (kind 'group'|'image'|None, normalised, errors[(field,msg)], warnings[str])."""
    t = obj.get("type")
    if t == "group":
        norm, errs, warns = check_group(obj, ctx)
        return "group", norm, errs, warns
    if t == "image":
        return ("image",) + check_image(obj, ctx)
    hint = " (character, variant, location, name and flag lines go in a BIBLE block)" \
        if t in ("character", "variant", "location", "name", "flag", "revise") else ""
    return None, None, [("type", f"{t!r} is not allowed here (group | image){hint}")], []


def line_label(obj: dict) -> str:
    t = obj.get("type")
    if t == "image":
        return f"image {obj.get('n')}"
    if t == "group":
        return f"group {obj.get('group')}"
    return str(t or "")


# ------------------------------------------------------------------ the whole chapter

def cast_present_errors(groups: dict, images: dict) -> list[tuple[int, str]]:
    """[(image n, message)]: everyone in a picture's cast must be listed in its group's `present` first (the model
    names who is involved in the group, then draws them). Images whose group is unknown are left to the caller."""
    out = []
    for n in sorted(images):
        im = images[n]
        g = groups.get(im.get("group"))
        if g is None:
            continue
        for c in im.get("cast") or []:
            if c["key"] not in (g.get("present") or []):
                out.append((n, f"{c['key']} is in the cast but not in group {im['group']}'s \"present\": list every "
                               f"named character who is in the place during the group in \"present\" first (add "
                               f"{c['key']} there, or take them out of the cast)"))
    return out


def chapter_warnings(groups: dict, images: dict, ctx: ChapterCtx) -> list[str]:
    """Warnings over a complete chapter: cast outside `present`, `wearing` habit, shot variety, prop habit."""
    warns = []
    order = [images[n] for n in sorted(images)]
    for im in order:
        g = groups.get(im["group"])
        if g is None:
            continue
        for c in im["cast"]:
            if c["key"] not in g["present"]:
                warns.append(f"image {im['n']}: {c['key']} is in the cast but not in group {im['group']}'s present")
    drawn: dict[str, set] = {}
    for im in order:
        drawn.setdefault(im["group"], set()).update(c["key"] for c in im["cast"])
    for gid, g in groups.items():
        if gid not in drawn:
            continue
        for key in g.get("present") or []:
            if key not in drawn[gid]:
                warns.append(f"group {gid}: {key} is listed in \"present\" but appears in none of the group's "
                             f"images (involved but never drawn): take them out of \"present\" or put them in a picture")
    seen: list[dict] = []
    for im in order:
        g = groups.get(im["group"])
        if g is not None and (not seen or seen[-1]["group"] != g["group"]):
            seen.append(g)
    for g in seen:
        by_kind = kind_where(ctx, g.get("loc"))
        if by_kind and g.get("where") and by_kind != g["where"]:
            warns.append(f"group {g['group']}: where is '{g['where']}' but the location '{g['loc']}' has kind "
                         f"'{ctx.locations[g['loc']].get('kind')}' (fine for a yard, porch or doorway; otherwise "
                         f"check it)")
        warns += setting_problems(g)
    warns += setting_moves(seen)
    wearing: dict[tuple[str, str], list[int]] = {}
    for im in order:
        for c in im["cast"]:
            if c["wearing"]:
                wearing.setdefault((c["key"], c["wearing"].lower()), []).append(im["n"])
    for (key, w), ns in wearing.items():
        if len(ns) > WEARING_VARIANT_AFTER:
            warns.append(f"{key} wears \"{w}\" on {len(ns)} images ({_ns(ns)}): make a variant (BIBLE variant line) "
                         f"instead of repeating `wearing`")
    by_group: dict[str, list[dict]] = {}
    for im in order:
        by_group.setdefault(im["group"], []).append(im)
    for gid, ims in by_group.items():
        run = 1
        for a, b in zip(ims, ims[1:]):
            run = run + 1 if a["shot"] == b["shot"] else 1
            if run == 3:
                warns.append(f"group {gid}: shot '{b['shot']}' three times in a row (ending at image {b['n']})")
        med = sum(1 for im in ims if im["shot"] == "medium")
        if len(ims) >= 6 and med * 3 > len(ims):
            warns.append(f"group {gid}: 'medium' on {med} of {len(ims)} images (at most a third)")
    for key, ch in ctx.characters.items():
        props = [p for p in (ch.get("props") or []) if isinstance(p, str) and p.strip()]
        if not props:
            continue
        mine = [im for im in order if any(c["key"] == key for c in im["cast"])]
        if len(mine) < 2:
            continue
        hits = [im["n"] for im in mine
                if any(_prop_in(p, c["hands"]) for c in im["cast"] if c["key"] == key for p in props)]
        if len(hits) * 2 > len(mine):
            warns.append(f"{key}: a signature prop ({', '.join(props)}) is in the hands on {len(hits)} of "
                         f"{len(mine)} images ({_ns(hits)}): only when the moment needs it")
    return warns


def _prop_in(prop: str, hands: str) -> bool:
    hands = hands.lower()
    words = re.findall(r"[a-z]+", prop.lower())
    if not words:
        return False
    if prop.lower() in hands:
        return True
    noun = words[-1]
    stem = noun[:-1] if noun.endswith("s") and len(noun) > 3 else noun
    return re.search(r"\b" + re.escape(stem) + r"s?\b", hands) is not None


def _ns(ns: list[int]) -> str:
    s = ", ".join(map(str, ns[:10]))
    return s + (" ..." if len(ns) > 10 else "")


def missing_windows(images: dict, count: int) -> list[int]:
    return [n for n in range(1, count + 1) if n not in images]


def ranges_text(ns: list[int]) -> str:
    """[1,2,3,7,9,10] -> '1-3, 7, 9-10'."""
    out, i = [], 0
    while i < len(ns):
        j = i
        while j + 1 < len(ns) and ns[j + 1] == ns[j] + 1:
            j += 1
        out.append(f"{ns[i]}" if i == j else f"{ns[i]}-{ns[j]}")
        i = j + 1
    return ", ".join(out)


def canonical(line: dict) -> str:
    """A stable text for comparing two normalised lines."""
    return json.dumps({k: v for k, v in line.items() if not k.startswith("_")}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def dump_line(line: dict) -> str:
    return json.dumps({k: v for k, v in line.items() if not k.startswith("_")}, ensure_ascii=False,
                      separators=(",", ":"))
