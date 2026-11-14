"""novel new [slug] [options]: start a new book.

Asks a few questions (one per line, the default in [brackets], Enter accepts it),
then creates the book folder, book.yaml, and makes it the current book.
Every question has a flag, and --yes accepts the default for anything not given.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from novel.core import context, paths, settings

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
STOP_WORDS = {"the", "a", "an", "of", "and", "to", "in", "on", "for"}
SUBFOLDERS = ["source", "chapters", "bible", "gemini", "inbox", "research", "extras", "_work", "episodes"]


class Cancelled(Exception):
    """The user pressed Ctrl+C, closed the input, or answered n at the end."""


class BadValue(ValueError):
    """An answer or flag value that is not acceptable; the message says why."""


def read_line(prompt: str) -> str:
    """The one place answers come from (tests replace this function)."""
    return input(prompt)


# ---- validators: each returns the cleaned value or raises BadValue ----------------

def check_title(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise BadValue("the title cannot be empty. Type the book's name, e.g. Circle of Inevitability")
    return v


def check_slug(v: str, root: Path) -> str:
    v = (v or "").strip()
    if not SLUG_RE.match(v):
        raise BadValue("use only lowercase letters a-z, digits 0-9 and '-' (no spaces), "
                       "starting with a letter or digit. Example: coi")
    if (root / v).exists():
        raise BadValue(f"'{v}' already exists in {root}. Pick another short name")
    return v


def check_cpe(v) -> int:
    try:
        n = int(str(v).strip())
    except ValueError:
        raise BadValue("type a whole number between 1 and 50, e.g. 10") from None
    if not 1 <= n <= 50:
        raise BadValue("chapters per episode must be between 1 and 50")
    return n


def check_flow(v: str) -> str:
    v = (v or "").strip()
    if v and not (re.match(r"^https?://", v) and "/project/" in v):
        raise BadValue("that does not look like a Flow project link. It should start with https:// "
                       "and contain /project/ (copy it from the browser address bar while the "
                       "project is open), or leave it empty and add it later")
    return v


def check_nonempty(name):
    def f(v):
        v = (v or "").strip()
        if not v:
            raise BadValue(f"{name} cannot be empty")
        return v
    return f


def default_slug(title: str, root: Path) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower())]
    sig = [w for w in words if w not in STOP_WORDS] or words
    if len(sig) >= 2:
        base = "".join(w[0] for w in sig)
    elif sig:
        base = sig[0]
    else:
        base = "book"
    slug, i = base, 2
    while (root / slug).exists():
        slug = f"{base}-{i}"
        i += 1
    return slug


# ---- asking ------------------------------------------------------------------------

def _fail(message: str):
    """A bad flag value: say so on stderr and exit 2 (usage error), not 1."""
    print(f"novel new: {message}", file=sys.stderr)
    raise SystemExit(2)


class Asker:
    def __init__(self, yes: bool):
        self.yes = yes

    def ask(self, label, flag, given, default, check, hint="", required=False):
        """Value from the flag, else (--yes) the default, else ask until valid."""
        if given is not None:
            try:
                value = check(given)
            except BadValue as e:
                _fail(f"{flag}: {e}")
            print(f"{label}: {value}")
            return value
        if self.yes:
            if required and default in (None, ""):
                _fail(f"{flag} is required (there is no default)")
            try:
                return check(default if default is not None else "")
            except BadValue as e:
                _fail(f"{flag}: {e}")
        if hint:
            print(f"  {hint}")
        shown = f" [{default}]" if default not in (None, "") else ""
        while True:
            try:
                answer = read_line(f"{label}{shown}: ")
            except (KeyboardInterrupt, EOFError):
                raise Cancelled() from None
            answer = answer.strip()
            if answer == "" and default not in (None, ""):
                answer = str(default)
            try:
                return check(answer)
            except BadValue as e:
                print(f"  Not valid: {e}")

    def confirm(self, label, default=True) -> bool:
        if self.yes:
            return default
        while True:
            try:
                a = read_line(f"{label} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                raise Cancelled() from None
            if a == "":
                return default
            if a in ("y", "yes"):
                return True
            if a in ("n", "no"):
                return False
            print("  Please type y or n.")


# ---- book.yaml ---------------------------------------------------------------------

OPTIONAL_HINTS = """
# ---- Optional settings you can add later (remove the leading '# ') ----
# book:
#   volumes: [[1, 120, 1], [121, 300, 2]]   # [first_chapter, last_chapter, volume_number], if numbering restarts
#   chapter_regex: '^Chapter\\s+(\\d+)'   # only if `novel extract` cannot find the chapters
# video:
#   logo: {enabled: true, file: assets/logo.png}
#   subscribe: {silent: {every_chapters: 5}}
# publish:
#   tags: [novel, audiobook]
#   hashtags: [novel]
#   playlist_url: https://www.youtube.com/playlist?list=...
# See every setting and where it comes from:  novel settings show
"""


def build_book_yaml(values: dict, base: dict) -> str:
    """Only the book: block plus values that differ from the defaults, then comment lines."""
    data: dict = {"book": {
        "title": values["title"], "author": values["author"], "slug": values["slug"],
        "series_label": values["series_label"],
        "chapters_per_episode": values["chapters_per_episode"]}}

    def differs(section, key, value, default_value):
        if value != default_value:
            data.setdefault(section, {})[key] = value

    if values["flow_project"]:
        data.setdefault("images", {})["flow_project"] = values["flow_project"]
    differs("images", "flow_model", values["flow_model"], (base.get("images") or {}).get("flow_model"))
    differs("tts", "voice", values["voice"], (base.get("tts") or {}).get("voice"))
    if values["kaggle_profile"]:
        data.setdefault("tts", {})["kaggle_profile"] = values["kaggle_profile"]
    if values["disclaimer_text"]:
        data["video"] = {"disclaimer": {"text": values["disclaimer_text"]}}
    head = "# Settings for this book only. Anything not listed here uses the built-in default.\n"
    return head + yaml.safe_dump(data, sort_keys=False, allow_unicode=True) + OPTIONAL_HINTS


# ---- command -----------------------------------------------------------------------

def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel new", description="Start a new book.")
    ap.add_argument("slug", nargs="?", help="short folder name, e.g. coi")
    ap.add_argument("--slug", dest="slug_opt")
    ap.add_argument("--title")
    ap.add_argument("--author")
    ap.add_argument("--series-label")
    ap.add_argument("--chapters-per-episode")
    ap.add_argument("--flow-project")
    ap.add_argument("--flow-model")
    ap.add_argument("--voice")
    ap.add_argument("--kaggle-profile")
    ap.add_argument("--disclaimer-text")
    ap.add_argument("--yes", action="store_true", help="accept the default for every question not given")
    a = ap.parse_args(argv)

    root = paths.data_dir()          # clear, fixable error if novel settings init was not run
    base = settings.effective(book="")
    home_cfg = base
    kaggle = home_cfg.get("kaggle") or {}
    profiles = sorted((kaggle.get("profiles") or {}).keys())
    asker = Asker(a.yes)

    try:
        print("Starting a new book. Press Enter to accept the answer in [brackets]; Ctrl+C to stop.\n")
        title = asker.ask("Book title", "--title", a.title, None, check_title,
                          "The name of the novel as readers know it.", required=True)

        given_slug = a.slug if a.slug is not None else a.slug_opt
        slug = asker.ask("Short folder name", "--slug", given_slug, default_slug(title, root),
                         lambda v: check_slug(v, root),
                         "Used for the folder and in commands (lowercase letters, digits and '-').")
        author = asker.ask("Author", "--author", a.author, "", lambda v: (v or "").strip(),
                           "Optional; press Enter to leave empty.")
        series = asker.ask("Series label on title cards", "--series-label", a.series_label, title,
                           check_nonempty("the series label"),
                           "The name shown on the video title cards.")
        cpe = asker.ask("Chapters per episode", "--chapters-per-episode", a.chapters_per_episode,
                        (base.get("book") or {}).get("chapters_per_episode", 10), check_cpe,
                        "How many chapters go into one video episode (1-50).")
        flow_project = asker.ask("Google Flow project URL", "--flow-project", a.flow_project, "",
                                 check_flow,
                                 "Open your project in Flow and copy the address. Optional now; "
                                 "images cannot be made without it.")
        flow_model = asker.ask("Flow image model", "--flow-model", a.flow_model,
                               (base.get("images") or {}).get("flow_model"), check_nonempty("the model"))
        voice = asker.ask("Narration voice", "--voice", a.voice,
                          (base.get("tts") or {}).get("voice"), check_nonempty("the voice"))
        k_default = kaggle.get("account") or ""
        if profiles:
            k_hint = "Kaggle account that does the voice-over. Available: " + ", ".join(profiles)
        else:
            k_hint = "Kaggle account for the voice-over (none set up on this PC yet; optional)."
        k_check = (lambda v: (v or "").strip()) if not profiles else _profile_check(profiles)
        kaggle_profile = asker.ask("Kaggle profile", "--kaggle-profile", a.kaggle_profile,
                                   k_default, k_check, k_hint)
        if a.disclaimer_text is not None:
            disclaimer = asker.ask("Disclaimer text", "--disclaimer-text", a.disclaimer_text, "",
                                   check_nonempty("the disclaimer text"))
        elif asker.yes or asker.confirm("Use the standard disclaimer text (built from the title "
                                        "and author)?", True):
            if asker.yes:
                print("Disclaimer: standard text")
            disclaimer = ""
        else:
            disclaimer = asker.ask("Disclaimer text", "--disclaimer-text", None, "",
                                   check_nonempty("the disclaimer text"),
                                   "Type the full text shown at the start of every episode.")

        values = {"title": title, "slug": slug, "author": author, "series_label": series,
                  "chapters_per_episode": cpe, "flow_project": flow_project,
                  "flow_model": flow_model, "voice": voice, "kaggle_profile": kaggle_profile,
                  "disclaimer_text": disclaimer}
        book = root / slug
        print("\nSummary")
        print(f"  folder:               {book}")
        print(f"  title:                {title}")
        print(f"  author:               {author or '(none)'}")
        print(f"  series label:         {series}")
        print(f"  chapters per episode: {cpe}")
        print(f"  Flow project:         {flow_project or '(not set yet)'}")
        print(f"  Flow image model:     {flow_model}")
        print(f"  voice:                {voice}")
        print(f"  Kaggle profile:       {kaggle_profile or '(none)'}")
        print(f"  disclaimer:           {'standard text' if not disclaimer else disclaimer}")
        if not asker.confirm("\nCreate?", True):
            raise Cancelled()
    except Cancelled:
        print("\nStopped. Nothing was created.")
        return 1

    for sub in SUBFOLDERS:
        (book / sub).mkdir(parents=True, exist_ok=True)
    (book / "book.yaml").write_text(build_book_yaml(values, base), encoding="utf-8")
    context.write_state(slug)

    print(f"\nCreated {book}")
    print(f"Settings file: {book / 'book.yaml'}")
    print(f"'{slug}' is now the current book.\n")
    print("Next steps:")
    print(f"  1. Copy the EPUB file(s) into {book / 'source'}")
    print("  2. novel extract")
    print("  3. novel episode add")
    return 0


def _profile_check(profiles):
    def f(v):
        v = (v or "").strip()
        if v and v not in profiles:
            raise BadValue("unknown profile. Choose one of: " + ", ".join(profiles))
        return v
    return f


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
