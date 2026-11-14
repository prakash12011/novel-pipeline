"""The command table: every row has a handler that imports, and `novel --help` lists every user command."""

import importlib

from novel import cli


def test_every_row_has_an_importable_handler_or_says_not_built_yet(capsys):
    for name, help_text, spec in cli.COMMANDS:
        if spec is None:
            assert help_text is None or "not built yet" in help_text, name
            continue
        module, _, func = spec.partition(":")
        assert callable(getattr(importlib.import_module(module), func)), name


def test_help_lists_every_user_command_and_hides_only_internal_ones(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    listed = [line.split()[0] for line in out.splitlines()[3:] if line.strip()]
    user = [name for name, help_text, _ in cli.COMMANDS if help_text is not None]
    assert listed == user
    hidden = [name for name, help_text, _ in cli.COMMANDS if help_text is None]
    assert all(name.startswith("_") for name in hidden)
    for name in ("prompt", "import", "qa", "approve", "characters", "images", "status", "render", "build",
                 "captions", "tts", "run", "jobs", "meta", "thumb"):
        assert name in listed
