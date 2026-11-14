"""Shared test setup: no test may make a sound."""

import pytest

from novel import flowrun


@pytest.fixture(autouse=True)
def _silent_alerts(monkeypatch):
    monkeypatch.setattr(flowrun, "system_sound", lambda: None)
