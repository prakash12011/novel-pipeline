"""The audible alert when a Flow run pauses for the person (no sound is made: the beep is injected)."""

import sys
import threading
import time
from datetime import datetime, timezone

from novel import flowrun as F

BEL = chr(7)
REAL_SYSTEM_SOUND = F.system_sound      # the conftest silences F.system_sound in every test
STOP = F.Stop(datetime.now(timezone.utc), "captcha", "images", "k")


def test_beeps_once_at_the_start_and_again_every_interval_until_answered():
    beeps, asked = [], []

    def ask(prompt):
        asked.append(prompt)
        time.sleep(0.5)                      # the person is away for half a second
        return ""

    assert F.ask_continue(STOP, ask, say=lambda *_: None, beep=lambda: beeps.append(time.time()), repeat_seconds=0.1) is True
    assert len(asked) == 1                   # the alert thread never answers: the prompt was asked once
    assert 3 <= len(beeps) <= 7
    n = len(beeps)
    time.sleep(0.3)
    assert len(beeps) == n                   # and it stops with the keypress
    assert not [t for t in threading.enumerate() if t.name == "flow-alert"]


def test_zero_means_one_beep_only():
    beeps = []
    F.ask_continue(STOP, lambda p: (time.sleep(0.3), "")[1], say=lambda *_: None, beep=lambda: beeps.append(1), repeat_seconds=0)
    assert beeps == [1]


def test_stop_answer_and_interrupt_end_the_alert_too():
    beeps = []
    assert F.ask_continue(STOP, lambda p: "s", say=lambda *_: None, beep=lambda: beeps.append(1), repeat_seconds=0.05) is False

    def interrupted(p):
        raise KeyboardInterrupt
    assert F.ask_continue(STOP, interrupted, say=lambda *_: None, beep=lambda: beeps.append(1), repeat_seconds=0.05) is False
    time.sleep(0.2)
    assert not [t for t in threading.enumerate() if t.name == "flow-alert"]


def test_a_failing_beep_never_breaks_the_prompt():
    def broken():
        raise RuntimeError("no sound card")
    assert F.ask_continue(STOP, lambda p: "", say=lambda *_: None, beep=broken, repeat_seconds=0.05) is True


def test_default_beep_writes_bel_and_asks_windows_for_the_sound(monkeypatch, capsys):
    played = []
    monkeypatch.setattr(F, "system_sound", lambda: played.append(1))
    F.alert_beep()
    assert BEL in capsys.readouterr().out and played == [1]


def test_system_sound_never_fails(monkeypatch):
    import types
    boom = types.ModuleType("winsound")

    def message_beep(*a):
        raise OSError("no device")
    boom.MessageBeep = message_beep
    monkeypatch.setitem(sys.modules, "winsound", boom)
    REAL_SYSTEM_SOUND()                      # does not raise
    monkeypatch.setitem(sys.modules, "winsound", None)     # import fails (not Windows)
    REAL_SYSTEM_SOUND()


def test_repeat_interval_comes_from_the_setting():
    assert F.alert_seconds({"images": {"alert_repeat_seconds": 12}}) == 12
    assert F.alert_seconds({"images": {"alert_repeat_seconds": 0}}) == 0
    assert F.alert_seconds({"images": {}}) == 30 and F.alert_seconds({}) == 30
    assert F.alert_seconds({"images": {"alert_repeat_seconds": "x"}}) == 30


def test_old_absolute_plan_paths_from_another_pc_are_cut_at_the_episode_folder(tmp_path):
    ep = tmp_path / "NovelData" / "coi" / "episodes" / "E003"
    old = "C:/Other PC/Novel Data/coi/episodes/E003/images/c0021/s01-2k.jpg"
    assert F.relative_plan_path(old, ep) == "images/c0021/s01-2k.jpg"
    assert F.relative_plan_path((ep / "flow" / "journal.jsonl").as_posix(), ep) == "flow/journal.jsonl"
    assert F.relative_plan_path("images/x.jpg", ep) == "images/x.jpg"
    assert F.relative_plan_path("C:/somewhere/else/x.jpg", ep) == "C:/somewhere/else/x.jpg"
