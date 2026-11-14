"""Node unit tests for flow/ (model comparison, plan paths, bible v2): runs `node --test flow/flow.test.mjs`."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def test_flow_unit_tests_pass():
    r = subprocess.run(["node", "--test", "flow/flow.test.mjs"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
