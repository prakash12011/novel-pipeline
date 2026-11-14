"""The subscribe pseudo-episode: message from settings, card text, cards rebuilt on change, the command."""

import shutil
import time

import pytest

from novel.core import context
from novel.stages import episode as E
from novel.stages import episode_extras as X

from test_join import BOOK, set_book, world  # noqa: F401  (the fixture builds the fixture book)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_default_message_is_generic_and_needs_no_api(world):
    set_book(world)
    assert X.spoken_lines() == X.SPOKEN
    assert not any("Circle" in x or "सर्कल" in x for x in X.SPOKEN)


def test_spoken_lines_from_settings(world, capsys):
    set_book(world, {"video": {"subscribe": {"spoken_lines": ["पहली लाइन", "दूसरी लाइन"]}}})
    hindi = X.setup_message()
    assert hindi == E.root(900) / "hindi" / "09001.txt"
    assert hindi.read_text(encoding="utf-8") == "पहली लाइन\n\nदूसरी लाइन\n"
    assert "extras" in str(hindi)
    out = capsys.readouterr().out
    assert "novel tts push --ep 900 --chapters 9001" in out and "novel tts pull --ep 900" in out


def test_card_lines_use_title_and_channel(world):
    set_book(world, {"book": {"title": "Test Novel"}, "channel": {"name": "My Channel"}})
    cfg = E.cfg()
    lines = X.card_lines(cfg)
    assert lines[0] == "Enjoying the story?" and "Test Novel" in lines[3]
    assert cfg["channel"]["name"] == "My Channel"
    set_book(world, {"video": {"subscribe": {"card_text": "A\nB\nC\nD"}}})
    assert X.card_lines(E.cfg()) == ["A", "B", "C", "D"]


def test_cards_rebuild_when_card_text_changes(world):
    set_book(world)
    X.cards_ready(24000)
    voiced = E.root(900) / "video" / "subscribe-voiced.mp4"
    m1 = voiced.stat().st_mtime_ns
    X.cards_ready(24000)
    assert voiced.stat().st_mtime_ns == m1                 # nothing changed: reused
    time.sleep(0.05)
    set_book(world, {"video": {"subscribe": {"card_text": "New\nBUTTON\nreact\ntag"}}})
    X.cards_ready(24000)
    assert voiced.stat().st_mtime_ns != m1


def test_extras_command_voice(world, capsys):
    set_book(world)
    context.write_state(BOOK)
    from novel import cli
    assert cli.main(["extras", "voice"]) == 0
    assert "novel tts pull --ep 900" in capsys.readouterr().out
    assert (E.root(900) / "manifest.json").exists()
    assert cli.main(["extras", "cards"]) == 0
    assert (E.root(900) / "video" / "subscribe-silent.mp4").exists()
