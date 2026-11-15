"""Command table and dispatcher for the `novel` CLI.

Each row of COMMANDS is (name, help_text, handler). A handler is either None
(command not built yet) or a "package.module:function" string, imported only
when the command runs. The function is called as function(argv) -> int, where
argv is the list of arguments after the command name.
"""

import importlib
import sys

from novel import __version__

COMMANDS = [
    ("doctor", "Check this PC for everything the pipeline needs", "novel.commands.doctor_cmd:main"),
    ("settings", "Show effective settings and where each value comes from", "novel.commands.settings_cmd:main"),
    ("use", "Select the current book and episode", "novel.commands.use_cmd:main"),
    ("new", "Start a new book (asks the first-time questions)", "novel.commands.new_cmd:main"),
    ("extract", "Turn the EPUB in source/ into chapter text files", "novel.commands.extract_cmd:main"),
    ("episode", "add | list episodes", "novel.commands.episode_cmd:main"),
    ("prompt", "Write the Gemini prompt and source files for a chapter batch", "novel.commands.prompt_cmd:main"),
    ("import", "File a Gemini reply from the clipboard or a file", "novel.commands.import_cmd:main"),
    ("write", "Write the Hindi, bible and picture lines with the Gemini API", "novel.commands.write_cmd:main"),
    ("qa", "Check translations, scenes and prompts", "novel.commands.qa_cmd:main"),
    ("approve", "Lock the Hindi and write image prompts and the Flow plan", "novel.commands.approve_cmd:main"),
    ("characters", "list | props | sheet | convert | make", "novel.commands.characters_cmd:main"),
    ("chrome", "Start the shared Chrome for Flow (never closes it)", "novel.commands.chrome_cmd:main"),
    ("run", "Narration and captions in the background, pictures in this window", "novel.commands.run_cmd:main"),
    ("jobs", "Show, tail or stop background jobs", "novel.commands.jobs_cmd:main"),
    ("tts", "push | status | pull | watch | reset", "novel.commands.tts_cmd:main"),
    ("captions", "Write English subtitle text", "novel.commands.captions_cmd:main"),
    ("images", "Generate missing images in Flow", "novel.commands.images_cmd:main"),
    ("sheet", "Write a contact sheet for one chapter", "novel.commands.sheet_cmd:main"),
    ("clean", "Remove the Gemini mark from downloaded pictures", "novel.commands.clean_cmd:main"),
    ("hd", "Make the 1920x1080 copies of the pictures (mark handled, 1K upscaled)", "novel.commands.hd_cmd:main"),
    ("status", "Show what is ready and what is missing", "novel.commands.status_cmd:main"),
    ("render", "Render one chapter", "novel.commands.render_cmd:main"),
    ("build", "Render chapters and join the episode", "novel.commands.build_cmd:main"),
    ("extras", "voice | cards for the subscribe message", "novel.commands.extras_cmd:main"),
    ("research", "Collect YouTube search suggestions", "novel.commands.research_cmd:main"),
    ("meta", "Write title, description and tags", "novel.commands.meta_cmd:main"),
    ("thumb", "Make thumbnails", "novel.commands.thumb_cmd:main"),
    ("published", "Mark an episode as published", "novel.commands.published_cmd:main"),
    ("migrate", "Copy an old project into the new layout", "novel.commands.migrate_cmd:main"),
    ("_job", None, "novel.commands.jobs_cmd:job_main"),   # internal: the process `novel run` detaches
]


def _print_usage(out):
    print("usage: novel <command> [options]", file=out)
    print("", file=out)
    print("commands:", file=out)
    shown = [row for row in COMMANDS if row[1] is not None]      # help None = hidden internal command
    width = max(len(name) for name, _, _ in shown)
    for name, help_text, _ in shown:
        print(f"  {name.ljust(width)}  {help_text}", file=out)


def _load_handler(spec):
    module_name, _, func_name = spec.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, func_name)


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)

    if not argv or argv[0] in ("-h", "--help"):
        _print_usage(sys.stdout)
        return 0

    if argv[0] == "--version":
        print(f"novel {__version__}")
        return 0

    name, rest = argv[0], argv[1:]
    table = {cmd_name: (help_text, handler) for cmd_name, help_text, handler in COMMANDS}

    if name not in table:
        print(f"novel: unknown command '{name}'", file=sys.stderr)
        print("Run 'novel --help' to see the commands.", file=sys.stderr)
        return 2

    _, handler = table[name]
    if handler is None:
        print(f"novel {name}: not built yet", file=sys.stderr)
        return 2

    result = _load_handler(handler)(rest)
    return 0 if result is None else result
