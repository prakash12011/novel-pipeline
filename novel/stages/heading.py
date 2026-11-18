"""The chapter heading line ("चैप्टर एक: फॉरेनर्स") at the top of a Hindi chapter file.

Older chapters open with it (paragraph 0); new ones do not: the Gemini prompts no longer ask for it and
every stored chapter text goes through strip_heading(), so the narration, the scenes and the captions
start straight with the story. Code that counts paragraphs asks body_start() where the story begins,
so both kinds of file work.

By hand (one episode's Hindi, with its audio, timing, captions and scenes when they exist):
    novel strip-heading --ep 1 [--chapters 1-10] [--dry-run]
"""

from __future__ import annotations

import re

# The heading: "चैप्टर <words>: <title>", "अध्याय 12: ...", "Chapter 3: ..." - one short line.
HEADING = re.compile(r"^\s*(चैप्टर|चेप्टर|अध्याय|Chapter|CHAPTER)\b[^\n]{0,120}$")
MAX_HEADING = 140


def is_heading(para: str | None) -> bool:
    p = (para or "").strip()
    return bool(p) and "\n" not in p and len(p) <= MAX_HEADING and bool(HEADING.match(p))


def body_start(paras: list[str]) -> int:
    """Index of the first story paragraph: 1 when paragraph 0 is the chapter heading, else 0."""
    return 1 if paras and is_heading(paras[0]) else 0


def heading_of(paras: list[str]) -> str:
    """The heading line, or '' when the text has none."""
    return paras[0].strip() if body_start(paras) else ""


def strip_heading(text: str) -> str:
    """The chapter text without a heading line at the top (unchanged when there is none)."""
    lines = text.lstrip("\ufeff").splitlines(keepends=True)
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and is_heading(lines[i]):
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        return "".join(lines[i:])
    return text
