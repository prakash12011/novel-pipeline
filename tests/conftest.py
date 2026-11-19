"""Shared test setup: no test may make a sound; the image defaults stay the Flow ones the older tests were written for."""

import re

import pytest

from novel import flowrun
from novel.core import settings

# config/defaults.yaml now makes Qwen-Image the engine (no watermark, two people per picture). The tests of the Flow
# path, the crop and the four-people rules keep those values here; a test of the Qwen path sets images.engine itself.
FLOW_IMAGES = {"engine": "flow", "max_characters": "4", "watermark": "crop"}


@pytest.fixture(autouse=True)
def _silent_alerts(monkeypatch):
    monkeypatch.setattr(flowrun, "system_sound", lambda: None)


@pytest.fixture(autouse=True)
def _no_gemini_free_tier_count(monkeypatch):
    """Tests never touch the real <novel home>/gemini_usage.json (test_keybudget tests the counter itself)."""
    from novel.stages import episode_translate
    monkeypatch.setattr(episode_translate, "default_budget", lambda model, keys: None)
    # nor the real OpenCode Go fallback (the key is in secrets.env): test_keybudget tests the order with a fake
    monkeypatch.setattr(episode_translate.Gemini, "_opencode", lambda self, *a, **k: None)
    monkeypatch.setattr(episode_translate.Gemini, "_claude", lambda self, *a, **k: None)   # nor headless Claude Code


@pytest.fixture(autouse=True)
def _flow_image_defaults(monkeypatch, tmp_path_factory):
    text = settings.defaults_file().read_text(encoding="utf-8")
    for key, value in FLOW_IMAGES.items():
        text, n = re.subn(rf"(?m)^(  {key}:) [^#\n]*", rf"\g<1> {value}   ", text, count=1)
        assert n == 1, key
    f = tmp_path_factory.mktemp("defaults") / "defaults.yaml"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setattr(settings, "defaults_file", lambda: f)
