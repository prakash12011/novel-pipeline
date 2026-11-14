"""Pull chapters out of a book's source EPUBs (or text files) into chapters/.

Works for any novel. Every `*.epub` in the source folder is read in spine order
(META-INF/container.xml -> OPF manifest and spine); a spine document is a
chapter when its <title> or first h1/h2/h3 matches the chapter pattern.
Standard library only.

The default pattern accepts `Chapter 12`, `Chapter 12: Title`, `Chapter 12 - Title`.
A book overrides it with `book.chapter_regex` in book.yaml (named groups `num`
and, optionally, `title`). `book.volumes` is a list of
`[first_chapter, last_chapter, volume_number]` used to label chapters by volume
(empty: one volume).
"""

from __future__ import annotations

import html
import posixpath
import re
import statistics
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

# Default chapter pattern: "Chapter 12", "Chapter 12: Title", "Chapter 12 - Title".
DEFAULT_CHAPTER_REGEX = r"^\s*Chapter\s+(?P<num>\d+)\s*(?:[:.\-–—]\s*(?P<title>.*?))?\s*$"

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
HEADING_RE = re.compile(r"<h[123][^>]*>(.*?)</h[123]>", re.S | re.I)
PARA_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


# Some volumes store accented letters double-encoded: UTF-8 bytes read as
# cp1252 and encoded again, so "Dariège" arrives as "DariÃ¨ge" and
# "Cœur" as "CÅ“ur". A damaged letter is a lead character followed by one or
# two continuation characters; re-encoding just that run as cp1252 gives back
# the original UTF-8. Runs that don't decode are left alone.
def _cp1252_range(lo: int, hi: int) -> str:
    out = ""
    for cp in range(0x80, 0x2200):
        try:
            b = chr(cp).encode("cp1252")
        except UnicodeEncodeError:
            continue
        if len(b) == 1 and lo <= b[0] <= hi:
            out += re.escape(chr(cp))
    return out


_CONT = _cp1252_range(0x80, 0xBF)
MOJIBAKE_RE = re.compile(f"[{_cp1252_range(0xC2, 0xDF)}][{_CONT}]"
                         f"|[{_cp1252_range(0xE0, 0xEF)}][{_CONT}]{{2}}")


def _fix_mojibake(text: str) -> str:
    def fix(m: re.Match) -> str:
        try:
            return m.group(0).encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return m.group(0)
    return MOJIBAKE_RE.sub(fix, text)


def _clean(fragment: str) -> str:
    fragment = COMMENT_RE.sub("", fragment)
    fragment = TAG_RE.sub("", fragment)
    fragment = _fix_mojibake(html.unescape(fragment))
    return WS_RE.sub(" ", fragment).strip()


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "untitled"


def compile_chapter_regex(pattern: str | None) -> re.Pattern:
    rx = re.compile(pattern or DEFAULT_CHAPTER_REGEX, re.I)
    if "num" not in rx.groupindex:
        raise ValueError("chapter regex needs a named group (?P<num>...)")
    return rx


def _match(rx: re.Pattern, text: str) -> tuple[int, str] | None:
    m = rx.search(text)
    if not m:
        return None
    num = m.group("num")
    if not num or not num.isdigit():
        return None
    title = m.group("title") if "title" in rx.groupindex else None
    return int(num), (title or "").strip()


@dataclass
class Chapter:
    num: int
    title: str
    body: str
    source: str = ""          # epub / text file name
    volume: int = 1

    @property
    def words(self) -> int:
        return len(self.body.split())

    @property
    def filename(self) -> str:
        return f"{self.num:04d}_{slugify(self.title)}.txt"

    def text(self) -> str:
        head = f"Chapter {self.num}: {self.title}" if self.title else f"Chapter {self.num}"
        return f"{head}\n\n{self.body}\n"


@dataclass
class Scan:
    epubs: list[str] = field(default_factory=list)      # source files read, in chapter order
    chapters: list[Chapter] = field(default_factory=list)   # unique, in book order
    skipped: int = 0                                     # documents that are not chapters
    duplicates: list[int] = field(default_factory=list)  # chapter numbers seen more than once
    gaps: list[tuple[int, int]] = field(default_factory=list)  # missing (first, last) ranges

    @property
    def numbers(self) -> list[int]:
        return [c.num for c in self.chapters]


def volume_of(num: int, volumes) -> int:
    for lo, hi, vol in volumes or []:
        if int(lo) <= num <= int(hi):
            return int(vol)
    return 1


# ---------- EPUB reading ----------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def spine_documents(z: zipfile.ZipFile) -> list[str]:
    """Zip member names of the spine documents, in reading order."""
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_path = None
    for el in container.iter():
        if _local(el.tag) == "rootfile" and el.get("full-path"):
            opf_path = el.get("full-path")
            break
    if not opf_path:
        raise ValueError("container.xml has no rootfile")
    opf = ET.fromstring(z.read(opf_path))
    base = posixpath.dirname(opf_path)
    manifest: dict[str, str] = {}
    spine: list[str] = []
    for el in opf.iter():
        name = _local(el.tag)
        if name == "item" and el.get("id") and el.get("href"):
            manifest[el.get("id")] = el.get("href")
        elif name == "itemref" and el.get("idref"):
            spine.append(el.get("idref"))
    names = set(z.namelist())
    out = []
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        member = posixpath.normpath(posixpath.join(base, unquote(href.split("#", 1)[0])))
        if member in names:
            out.append(member)
    return out


def parse_document(raw: bytes, rx: re.Pattern) -> tuple[int, str, str] | None:
    """(number, title, body) when the document is a chapter, else None."""
    content = raw.decode("utf-8", errors="replace")
    candidates = []
    m = TITLE_RE.search(content)
    if m:
        candidates.append(_clean(m.group(1)))
    h = HEADING_RE.search(content)
    if h:
        candidates.append(_clean(h.group(1)))
    found = None
    for text in candidates:
        found = _match(rx, text)
        if found:
            break
    if not found:
        return None
    num, title = found

    ugc_at = content.find('chapter-ugc"')
    body_src = content[ugc_at:] if ugc_at != -1 else content
    paras = [_clean(p) for p in PARA_RE.findall(body_src)]
    paras = [p for p in paras if p]
    return num, title, "\n\n".join(paras)


def read_epub(path: Path, rx: re.Pattern) -> tuple[list[Chapter], int]:
    chapters: list[Chapter] = []
    skipped = 0
    with zipfile.ZipFile(path) as z:
        for member in spine_documents(z):
            parsed = parse_document(z.read(member), rx)
            if parsed is None:
                skipped += 1
                continue
            num, title, body = parsed
            chapters.append(Chapter(num, title, body, source=path.name))
    return chapters, skipped


# ---------- plain text reading ----------

def split_text(content: str, rx: re.Pattern, source: str = "") -> list[Chapter]:
    """A line that matches the chapter pattern starts a chapter. Text before
    the first such line is ignored."""
    chapters: list[Chapter] = []
    cur: tuple[int, str] | None = None
    lines: list[str] = []

    def flush():
        if cur is None:
            return
        paras = []
        block: list[str] = []
        for ln in lines + [""]:
            if ln.strip():
                block.append(ln)
            elif block:
                paras.append(_fix_mojibake(WS_RE.sub(" ", " ".join(block))).strip())
                block = []
        chapters.append(Chapter(cur[0], WS_RE.sub(" ", cur[1]).strip(), "\n\n".join(p for p in paras if p),
                                source=source))

    for line in content.splitlines():
        hit = _match(rx, line) if line.strip() and len(line) < 200 else None
        if hit:
            flush()
            cur, lines = hit, []
        else:
            lines.append(line)
    flush()
    return chapters


# ---------- scan ----------

def _find_gaps(nums: list[int]) -> list[tuple[int, int]]:
    uniq = sorted(set(nums))
    gaps = []
    for a, b in zip(uniq, uniq[1:]):
        if b - a > 1:
            gaps.append((a + 1, b - 1))
    return gaps


def scan(source_dir: Path, chapter_regex: str | None = None, volumes=None) -> Scan:
    rx = compile_chapter_regex(chapter_regex)
    result = Scan()
    source_dir = Path(source_dir)
    epubs = sorted(source_dir.glob("*.epub")) if source_dir.is_dir() else []
    per_file: list[tuple[Path, list[Chapter]]] = []
    if epubs:
        for p in epubs:
            chs, skipped = read_epub(p, rx)
            result.skipped += skipped
            per_file.append((p, chs))
    else:
        for p in sorted(source_dir.glob("*.txt")) if source_dir.is_dir() else []:
            per_file.append((p, split_text(p.read_text(encoding="utf-8", errors="replace"),
                                           rx, p.name)))
    per_file.sort(key=lambda pc: (min((c.num for c in pc[1]), default=10**9), pc[0].name))
    seen: set[int] = set()
    for p, chs in per_file:
        result.epubs.append(p.name)
        for c in chs:
            if c.num in seen:
                if c.num not in result.duplicates:
                    result.duplicates.append(c.num)
                continue
            seen.add(c.num)
            c.volume = volume_of(c.num, volumes)
            result.chapters.append(c)
    result.gaps = _find_gaps(result.numbers)
    return result


# ---------- writing ----------

def differing(chapters: list[Chapter], out_dir: Path) -> list[Path]:
    """Existing chapter files whose content differs from what would be written."""
    out = []
    for c in chapters:
        dest = Path(out_dir) / c.filename
        if dest.exists() and dest.read_text(encoding="utf-8") != c.text():
            out.append(dest)
    return out


def write_chapters(chapters: list[Chapter], out_dir: Path) -> int:
    """Write the files that are missing or changed; returns how many were written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for c in chapters:
        dest = out_dir / c.filename
        text = c.text()
        if dest.exists() and dest.read_text(encoding="utf-8") == text:
            continue
        dest.write_text(text, encoding="utf-8")
        n += 1
    return n


def extract(ebook_dir: Path, out_dir: Path, start: int, end: int,
            chapter_regex: str | None = None, volumes=None) -> list[tuple[int, str, int]]:
    """Write chapters start..end; returns [(number, title, word count)]."""
    sc = scan(Path(ebook_dir), chapter_regex, volumes)
    chosen = [c for c in sc.chapters if start <= c.num <= end]
    write_chapters(chosen, Path(out_dir))
    return [(c.num, c.title, c.words) for c in chosen]


def print_report(report: list[tuple[int, str, int]]) -> None:
    if not report:
        print("no chapters extracted")
        return
    counts = [w for _, _, w in report]
    print(f"{len(report)} chapters extracted")
    print(
        f"words: min={min(counts)} max={max(counts)} "
        f"mean={statistics.mean(counts):.0f} median={statistics.median(counts):.0f}"
    )
    flagged = [(n, t, w) for n, t, w in report if w < 300 or w > 6000]
    if flagged:
        print(f"\nflagged ({len(flagged)}, outside 300-6000 words):")
        for n, t, w in flagged:
            print(f"  {n:>4}  {w:>5} words  {t}")
