"""The documents must not drift from the tool.

COMMANDS.md and README.md: every command line in a ```cmd fenced block that starts with `novel ` (or with the
full path of novel.cmd) is checked without running anything:

  * the command is in novel.cli.COMMANDS and is built (its handler is not None);
  * for tts, characters, episode, extras and settings the sub-command exists;
  * every --option used is accepted by that command. Each handler is asked for its own help (`<sub> --help`,
    which every handler answers without side effects); the option must appear in that text.

Output samples use ```text fences and are not read. Lines that do not start with `novel` (git, py, npm) are not
read either.

docs/settings.md must list exactly the keys of config/defaults.yaml (with the right default and the right
chapter scope). Every ```yaml example in the documents must use real keys, and a `chapters:` block must obey
settings.CHAPTER_SCOPE.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import tempfile
from functools import lru_cache
from pathlib import Path
from unittest import mock

import pytest
import yaml

from novel import cli
from novel.core import settings

ROOT = Path(__file__).resolve().parents[1]
DOC_FILES = [ROOT / "COMMANDS.md", ROOT / "README.md"]
SETTINGS_DOC = ROOT / "docs" / "settings.md"

HANDLERS = {name: spec for name, _help, spec in cli.COMMANDS}
VISIBLE = [name for name, help_text, _spec in cli.COMMANDS if help_text is not None]
SUBCOMMANDS = {
    "tts": {"push", "status", "pull", "watch", "reset"},
    "characters": {"list", "props", "sheet", "convert", "make"},
    "episode": {"add", "list"},
    "extras": {"voice", "cards"},
    "settings": {"show", "init"},
}

NOVEL_LINE = re.compile(r'^(?:novel|"[^"]*novel\.cmd")\s+(?P<rest>\S.*)$')
FENCE = re.compile(r"^```\s*(?P<info>[\w-]*)\s*$")


# ------------------------------------------------------------------ reading the documents

def fenced_blocks(path: Path):
    """[(info, first_line_number, [lines])] for every fenced block of a markdown file."""
    blocks, cur = [], None
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = FENCE.match(line.strip())
        if m and cur is None:
            cur = (m.group("info"), no + 1, [])
        elif line.strip() == "```" and cur is not None:
            blocks.append(cur)
            cur = None
        elif cur is not None:
            cur[2].append(line)
    assert cur is None, f"{path.name}: a fenced block is never closed (it starts at line {cur[1] - 1})"
    return blocks


def novel_lines():
    """[(file name, line number, command line text after `novel`)] from the ```cmd blocks."""
    out = []
    for path in DOC_FILES:
        for info, first, lines in fenced_blocks(path):
            if info != "cmd":
                continue
            for k, line in enumerate(lines):
                m = NOVEL_LINE.match(line.strip())
                if m:
                    out.append((path.name, first + k, m.group("rest").strip()))
    return out


NOVEL_LINES = novel_lines()


# ------------------------------------------------------------------ asking a handler for its help

@lru_cache(maxsize=None)
def help_text(command: str, sub: str | None) -> tuple[int | None, str]:
    """(exit code, help text) of `novel <command> [<sub>] --help`, run in-process in a throw-away home."""
    handler = cli._load_handler(HANDLERS[command])
    argv = ([sub] if sub else []) + ["--help"]
    out, err = io.StringIO(), io.StringIO()
    code: int | None = None
    with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"NOVEL_HOME": home}), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            result = handler(argv)
            code = 0 if result is None else result
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return code, out.getvalue() + err.getvalue()


def has_option(text: str, option: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(option) + r"(?![\w-])", text) is not None


def tokens(rest: str) -> list[str]:
    return shlex.split(rest, posix=False)


# ------------------------------------------------------------------ commands and options in the documents

def test_documents_contain_commands():
    names = {(f, c.split()[0]) for f, _no, c in NOVEL_LINES}
    assert len(NOVEL_LINES) >= 30, "extraction found too few command lines: is the ```cmd fence convention broken?"
    assert {f for f, _ in names} == {"COMMANDS.md", "README.md"}


@pytest.mark.parametrize("fname,lineno,rest", NOVEL_LINES, ids=[f"{f}:{n}: novel {r}"[:90] for f, n, r in NOVEL_LINES])
def test_command_line_is_real(fname, lineno, rest):
    toks = tokens(rest)
    command = toks[0]
    assert command in HANDLERS, f"{fname}:{lineno}: `novel {command}` is not in the COMMANDS table"
    assert HANDLERS[command] is not None, f"{fname}:{lineno}: `novel {command}` is not built yet"
    assert command != "_job"

    sub = None
    if command in SUBCOMMANDS:
        positional = [t for t in toks[1:] if not t.startswith("-")]
        assert positional, f"{fname}:{lineno}: `novel {command}` needs one of {sorted(SUBCOMMANDS[command])}"
        sub = positional[0]
        assert sub in SUBCOMMANDS[command], \
            f"{fname}:{lineno}: `novel {command} {sub}`: unknown sub-command (known: {sorted(SUBCOMMANDS[command])})"

    code, text = help_text(command, sub)
    assert code == 0, f"{fname}:{lineno}: `novel {command} {sub or ''} --help` did not work (exit {code}):\n{text}"
    options = []
    for t in toks[1:]:
        if t.startswith("--"):
            options.append(t.split("=", 1)[0])
    for opt in options:
        assert has_option(text, opt) or opt == "--help", \
            f"{fname}:{lineno}: `novel {command}{' ' + sub if sub else ''}` has no option {opt}.\nIts help says:\n{text}"


def inline_spans():
    """[(file name, line number, text)] of `novel ...` code spans in the prose (outside fenced blocks)."""
    out = []
    for path in DOC_FILES:
        in_fence = False
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for span in re.findall(r"`([^`]+)`", line):
                if re.match(r"novel\s+[a-z]+", span):
                    out.append((path.name, no, span.strip()))
    return out


INLINE_SPANS = inline_spans()


@pytest.mark.parametrize("fname,lineno,span", INLINE_SPANS, ids=[f"{f}:{n}: {s}"[:90] for f, n, s in INLINE_SPANS])
def test_inline_command_is_real(fname, lineno, span):
    """Commands written in the prose (not in a ```cmd block): the command, its sub-command and every
    --option written in the span must exist. Optional [brackets] and | alternatives are allowed."""
    m = re.match(r"novel\s+([a-z_-]+)(?:\s+([a-z]+))?", span)
    command, word = m.group(1), m.group(2)
    assert command in HANDLERS and HANDLERS[command] is not None, \
        f"{fname}:{lineno}: `{span}`: `novel {command}` is not a built command"
    sub = None
    if command in SUBCOMMANDS and word:
        sub = word
        assert sub in SUBCOMMANDS[command], f"{fname}:{lineno}: `{span}`: unknown sub-command {sub!r}"
    elif command in SUBCOMMANDS:
        return                                           # e.g. "`novel extras` ..." with no sub-command named
    code, text = help_text(command, sub)
    assert code == 0, f"{fname}:{lineno}: `{span}`: help did not work:\n{text}"
    for opt in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", span):
        assert has_option(text, opt) or opt == "--help", \
            f"{fname}:{lineno}: `{span}`: no option {opt}.\nIts help says:\n{text}"


def test_every_command_has_a_reference_entry():
    commands_md = (ROOT / "COMMANDS.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"^### novel ([\w-]+)\s*$", commands_md, re.M))
    missing = [n for n in VISIBLE if n not in headings]
    assert not missing, f"COMMANDS.md has no `### novel <name>` entry for: {', '.join(missing)}"
    unknown = sorted(headings - set(HANDLERS))
    assert not unknown, f"COMMANDS.md describes commands that do not exist: {', '.join(unknown)}"


def test_documented_commands_are_built():
    built = [n for n in VISIBLE if HANDLERS[n] is not None]
    assert built == VISIBLE, "a visible command without a handler"


def test_sub_command_tables_are_current():
    """The SUBCOMMANDS table above must match what the handlers accept (so a new sub-command is noticed)."""
    for command, subs in SUBCOMMANDS.items():
        for sub in subs:
            code, text = help_text(command, sub)
            assert code == 0, f"novel {command} {sub} --help failed:\n{text}"
        code, text = help_text(command, None)
        for sub in subs:
            assert re.search(r"(?<![\w-])" + sub + r"(?![\w-])", text), \
                f"`novel {command} --help` does not mention {sub}"


# ------------------------------------------------------------------ settings

def table_rows(path: Path):
    """[(key, default cell, layers cell)] for every table row whose first cell is a `dotted.key`."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        m = re.fullmatch(r"`([a-z_0-9]+(?:\.[a-z_0-9]+)*)`", cells[0]) if cells else None
        if m and len(cells) >= 4:
            rows.append((m.group(1), cells[1], cells[-1]))
    return rows


def defaults_flat() -> dict:
    data = yaml.safe_load((ROOT / "config" / "defaults.yaml").read_text(encoding="utf-8"))
    return settings.flatten(data)


def test_settings_doc_keys_match_defaults():
    documented = [k for k, _d, _l in table_rows(SETTINGS_DOC)]
    assert len(documented) == len(set(documented)), "a key is documented twice in docs/settings.md"
    actual = set(defaults_flat())
    missing = sorted(actual - set(documented))
    extra = sorted(set(documented) - actual)
    assert not missing, f"keys in config/defaults.yaml but not in docs/settings.md: {missing}"
    assert not extra, f"keys in docs/settings.md but not in config/defaults.yaml: {extra}"


def test_settings_doc_defaults_and_scope_are_right():
    flat = defaults_flat()
    for key, default_cell, layers in table_rows(SETTINGS_DOC):
        if key not in flat:
            continue                                    # reported by the test above
        cell = default_cell.strip("`").replace("\\|", "|")
        assert yaml.safe_load(cell) == flat[key], \
            f"docs/settings.md: default of {key} is written as {default_cell}, but defaults.yaml has {flat[key]!r}"
        chapter = "chapter" in layers.lower()
        assert chapter == settings._in_scope(key), \
            f"docs/settings.md: {key} {'is' if chapter else 'is not'} marked chapter, " \
            f"but settings.CHAPTER_SCOPE says the opposite"


def test_chapter_scope_keys_are_all_documented():
    flat = defaults_flat()
    for scope_key in settings.CHAPTER_SCOPE:
        assert any(k == scope_key or k.startswith(scope_key + ".") for k in flat), \
            f"CHAPTER_SCOPE has {scope_key}, which is not in defaults.yaml"


def yaml_examples():
    out = []
    for path in DOC_FILES + [SETTINGS_DOC]:
        for info, first, lines in fenced_blocks(path):
            if info == "yaml":
                out.append((path.name, first, "\n".join(lines)))
    return out


YAML_EXAMPLES = yaml_examples()


@pytest.mark.parametrize("fname,lineno,text", YAML_EXAMPLES, ids=[f"{f}:{n}" for f, n, _ in YAML_EXAMPLES])
def test_yaml_examples_are_valid_settings(fname, lineno, text):
    data = yaml.safe_load(text)
    assert isinstance(data, dict), f"{fname}:{lineno}: the example is not a YAML mapping"
    flat = defaults_flat()
    chapters = data.pop("chapters", None)
    for key in settings.flatten(data):
        assert key in flat, f"{fname}:{lineno}: `{key}` is not a setting in config/defaults.yaml"
    if chapters is not None:
        assert isinstance(chapters, dict), f"{fname}:{lineno}: chapters: must be a mapping of chapter numbers"
        for n, block in chapters.items():
            assert isinstance(n, int), f"{fname}:{lineno}: chapter keys are numbers, got {n!r}"
            for key in settings.flatten(block or {}):
                assert key in flat, f"{fname}:{lineno}: chapters.{n}: `{key}` is not a setting"
            settings.check_chapter_block(block or {}, f"{fname}:{lineno} chapters.{n}")


def test_yaml_examples_exist():
    assert len(YAML_EXAMPLES) >= 8


# ------------------------------------------------------------------ nothing secret

SECRET_SHAPES = [r"AIza[0-9A-Za-z_\-]{20,}", r"\bhf_[A-Za-z0-9]{20,}", r"\bsk-[A-Za-z0-9]{20,}",
                 r"[\w.+-]+@[\w-]+\.[\w.]+"]


@pytest.mark.parametrize("path", DOC_FILES + [SETTINGS_DOC], ids=lambda p: p.name)
def test_no_secrets_or_emails(path):
    text = path.read_text(encoding="utf-8")
    for shape in SECRET_SHAPES:
        found = re.findall(shape, text)
        assert not found, f"{path.name} contains something that looks like a key or an email address: {found[:3]}"
