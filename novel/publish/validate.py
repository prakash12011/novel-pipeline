"""YouTube publish checks. Pure functions: text in, a list of error strings out (empty = fine).

Every rule is its own function so it can be tested alone. Limits come from the
coi-episode-publish skill (title <= 100, tags < 500 characters, exactly 3 hashtags,
first timestamp 0:00, at least 3 stamps, ascending, 10 s apart) plus YouTube's own
limits (description <= 5000 characters, no angle brackets in title or description).
"""

from __future__ import annotations

import re

TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_MAX = 500
MIN_STAMPS = 3
MIN_GAP_SECONDS = 10
HASHTAG_COUNT = 3
SHARED_RUN_WORDS = 6

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
PLACEHOLDER = re.compile(r"\[[^\]\n]*\]|<[^>\n]*>")
HASHTAG = re.compile(r"^#[A-Za-z0-9_]+$")
HASHTAG_IN_TEXT = re.compile(r"(?<!\S)#\S+")
STAMP_LINE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s+(\S.*)$")
WORD = re.compile(r"[a-z0-9']+")

# Errors that come from AI-written text (the only ones worth asking the AI to redo).
AI_PREFIXES = ("teaser", "comment", "hooks")


def has_devanagari(text: str) -> bool:
    return bool(DEVANAGARI.search(text or ""))


def find_placeholders(text: str) -> list[str]:
    """Leftover template pieces: anything in [...] or <...>."""
    return PLACEHOLDER.findall(text or "")


# ------------------------------------------------------------------ title

def check_title(title: str, series_phrase: str, limit: int = TITLE_MAX) -> list[str]:
    errs = []
    if not title.strip():
        return ["title: empty"]
    if len(title) > limit:
        errs.append(f"title: {len(title)} characters, limit {limit}")
    if series_phrase and not title.startswith(series_phrase):
        errs.append(f"title: must start with the series phrase '{series_phrase}'")
    if has_devanagari(title):
        errs.append("title: contains Devanagari (use English letters)")
    if "<" in title or ">" in title:
        errs.append("title: YouTube does not allow < or >")
    return errs


# ------------------------------------------------------------------ description

def check_description(desc: str, limit: int = DESCRIPTION_MAX) -> list[str]:
    errs = []
    if not desc.strip():
        return ["description: empty"]
    if len(desc) > limit:
        errs.append(f"description: {len(desc)} characters, limit {limit}")
    if "<" in desc or ">" in desc:
        errs.append("description: YouTube does not allow < or >")
    return errs


def parse_stamp_lines(desc: str) -> list[tuple[int, str, bool]]:
    """(seconds, label, uses_hour_form) for every `m:ss Label` / `h:mm:ss Label` line."""
    out = []
    for line in desc.splitlines():
        m = STAMP_LINE.match(line)
        if not m:
            continue
        h, mm, ss, label = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        out.append(((int(h) * 3600 if h else 0) + mm * 60 + ss, label.strip(), h is not None))
    return out


def check_stamps(stamps: list[tuple[int, str, bool]]) -> list[str]:
    errs = []
    if len(stamps) < MIN_STAMPS:
        errs.append(f"timestamps: {len(stamps)} found, YouTube needs at least {MIN_STAMPS}")
    if stamps and stamps[0][0] != 0:
        errs.append("timestamps: the first one must be 0:00")
    for (a, la, _), (b, lb, _) in zip(stamps, stamps[1:]):
        if b <= a:
            errs.append(f"timestamps: '{lb}' is not after '{la}' (must ascend)")
        elif b - a < MIN_GAP_SECONDS:
            errs.append(f"timestamps: '{lb}' is only {b - a} s after '{la}' (need {MIN_GAP_SECONDS})")
    forms = {hour for _, _, hour in stamps}
    if len(forms) > 1:
        errs.append("timestamps: mixes m:ss and h:mm:ss (use one form for all)")
    return errs


def check_timestamps(desc: str) -> list[str]:
    return check_stamps(parse_stamp_lines(desc))


# ------------------------------------------------------------------ tags and hashtags

def tags_length(tags: list[str]) -> int:
    """Length by YouTube's counting: each tag's characters, 2 extra for the quotes YouTube
    puts around a tag that contains a space, plus one comma between neighbouring tags."""
    total = sum(len(t) + (2 if " " in t else 0) for t in tags)
    return total + max(0, len(tags) - 1)


def check_tags(tags: list[str], expected_count: int | None = None, limit: int = TAGS_MAX) -> list[str]:
    errs = []
    if expected_count is not None and len(tags) != expected_count:
        errs.append(f"tags: {len(tags)} tags, the template has {expected_count}")
    n = tags_length(tags)
    if n > limit:
        errs.append(f"tags: {n} characters by YouTube's counting, limit {limit}")
    for t in tags:
        if has_devanagari(t):
            errs.append(f"tags: '{t}' contains Devanagari")
        if not t.strip():
            errs.append("tags: an empty tag")
    return errs


def check_hashtags(hashtags: list[str], desc: str | None = None, count: int = HASHTAG_COUNT) -> list[str]:
    errs = []
    if len(hashtags) != count:
        errs.append(f"hashtags: {len(hashtags)} given, need exactly {count}")
    for h in hashtags:
        if not HASHTAG.match(h):
            errs.append(f"hashtags: '{h}' is not a single #Word")
    if desc is not None:
        found = HASHTAG_IN_TEXT.findall(desc)
        if len(found) != count:
            errs.append(f"hashtags: the description has {len(found)} hashtags, need exactly {count}")
    return errs


# ------------------------------------------------------------------ AI text

def _words(text: str) -> list[str]:
    return WORD.findall((text or "").lower().replace("’", "'"))


def shared_runs(text: str, sources: list[str], n: int = SHARED_RUN_WORDS) -> list[str]:
    """Runs of n consecutive words of `text` that also appear in any source text (first 3)."""
    w = _words(text)
    if len(w) < n:
        return []
    grams = {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}
    hit: list[str] = []
    for src in sources:
        sw = _words(src)
        for i in range(len(sw) - n + 1):
            g = tuple(sw[i:i + n])
            if g in grams:
                hit.append(" ".join(g))
                grams.discard(g)
                if not grams or len(hit) >= 3:
                    return hit
    return hit


def _norm_phrase(s: str) -> str:
    return " ".join(_words(s))


def title_in_text(text: str, titles: list[str]) -> list[str]:
    """Novel chapter titles of two or more words that appear as a whole phrase in text.
    One-word titles ('Letter', 'Method') are ignored: they are ordinary words."""
    hay = f" {_norm_phrase(text)} "
    out = []
    for t in titles:
        p = _norm_phrase(t)
        if len(p.split()) >= 2 and f" {p} " in hay:
            out.append(t)
    return out


def check_teaser(teaser: str, sources: list[str], titles: list[str] | None = None,
                 n: int = SHARED_RUN_WORDS, field: str = "teaser") -> list[str]:
    errs = []
    if not teaser.strip():
        return [f"{field}: empty"]
    if has_devanagari(teaser):
        errs.append(f"{field}: contains Devanagari (Hinglish in Latin letters)")
    for run in shared_runs(teaser, sources, n):
        errs.append(f"{field}: shares {n} consecutive words with the novel: \"{run}\"")
    for t in title_in_text(teaser, titles or []):
        errs.append(f"{field}: contains the novel's chapter title \"{t}\"")
    return errs


def check_comment_question(q: str, sources: list[str] | None = None,
                           titles: list[str] | None = None) -> list[str]:
    if not q.strip():
        return ["comment: the comment question is empty"]
    errs = []
    if "\n" in q.strip():
        errs.append("comment: must be one line")
    if len(q) > 300:
        errs.append(f"comment: {len(q)} characters, keep it short (300)")
    errs += check_teaser(q, sources or [], titles, field="comment")
    return errs


_HOOK_ACCENT = re.compile(r"\*[^*|]+\*")


def check_hook(hook: str) -> list[str]:
    """One hook: 'LINE ONE|*KEYWORD* LINE', capitals, 2-5 words, one accent keyword."""
    errs = []
    if hook.count("|") != 1:
        errs.append(f"hooks: '{hook}' needs exactly one | between the two lines")
    if len(_HOOK_ACCENT.findall(hook)) != 1 or hook.count("*") != 2:
        errs.append(f"hooks: '{hook}' needs exactly one *KEYWORD* in yellow")
    bare = hook.replace("|", " ").replace("*", "")
    if has_devanagari(bare):
        errs.append(f"hooks: '{hook}' must use English letters")
    if bare != bare.upper():
        errs.append(f"hooks: '{hook}' must be ALL CAPS")
    nwords = len(bare.split())
    if not 2 <= nwords <= 5:
        errs.append(f"hooks: '{hook}' has {nwords} words, use 2-5")
    return errs


def check_hooks(hooks, count: int = 3) -> list[str]:
    if not isinstance(hooks, list) or not all(isinstance(h, str) for h in hooks):
        return ["hooks: must be a list of strings"]
    errs = []
    if len(hooks) != count:
        errs.append(f"hooks: {len(hooks)} given, need exactly {count}")
    for h in hooks:
        errs += check_hook(h)
    if len({h.strip().lower() for h in hooks}) != len(hooks):
        errs.append("hooks: two hooks are the same")
    return errs


# ------------------------------------------------------------------ everything

def validate_pack(pack: dict, *, series_phrase: str, expected_tags: int, english_sources: list[str],
                  chapter_titles: list[str], check_ai_text: bool = True,
                  check_hook_text: bool = True) -> list[str]:
    """All rules over a finished pack (keys: title, description, tags, hashtags, teaser,
    comment_question, thumbnail_hooks). Returns every error."""
    errs: list[str] = []
    errs += check_title(pack["title"], series_phrase)
    errs += check_description(pack["description"])
    errs += check_timestamps(pack["description"])
    errs += check_tags(pack["tags"], expected_tags)
    errs += check_hashtags(pack["hashtags"], pack["description"])
    for where, text in (("title", pack["title"]), ("description", pack["description"]),
                        ("tags", ", ".join(pack["tags"]))):
        left = find_placeholders(text)
        if left:
            errs.append(f"{where}: leftover placeholder {left[0]}")
    if check_ai_text:
        errs += check_teaser(pack.get("teaser", ""), english_sources, chapter_titles)
        errs += check_comment_question(pack.get("comment_question", ""), english_sources, chapter_titles)
    if check_hook_text:
        errs += check_hooks(pack.get("thumbnail_hooks"))
    return errs


def ai_errors(errors: list[str]) -> list[str]:
    return [e for e in errors if e.startswith(AI_PREFIXES)]
