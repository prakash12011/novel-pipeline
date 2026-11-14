"""flow/run.mjs against a fake Flow: a prompt that may have been sent is never sent again on its own.

    py -m pytest tests
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def make_plan(tmp: Path, ids: list[str]) -> Path:
    shots = [{"id": i, "chars": [], "prompt": str(tmp / f"{i}.prompt.txt"),
              "out": str(tmp / f"{i}-2k.jpg"), "meta": str(tmp / f"{i}.flow.json"),
              "used": str(tmp / f"{i}.used.txt")} for i in ids]
    plan = tmp / "plan.json"
    plan.write_text(json.dumps({"project": "x/project/abc", "header_mark": "Wide",
                                "journal": str(tmp / "journal.jsonl"), "shots": shots}))
    return plan


def run(plan: Path, script: dict, *extra: str) -> list[str]:
    env = dict(os.environ, FLOW_GEN=str(ROOT / "tests" / "fake_gen.mjs"),
               FAKE_SCRIPT=json.dumps(script))
    subprocess.run(["node", "flow/run.mjs", str(plan), "--pause", "0", "--chapter-pause", "0", *extra],
                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    log = plan.parent / "calls.log"
    return log.read_text().split() if log.exists() else []


def status(plan: Path) -> dict:
    return json.loads((plan.parent / "status.json").read_text())


def test_failure_after_sending_is_never_resent(tmp_path):
    plan = make_plan(tmp_path, ["s01", "s02"])
    run(plan, {"s01": ["after", "ok"]})
    assert status(plan)["needs_check"] == ["s01"]
    (tmp_path / "calls.log").unlink()
    calls = run(plan, {"s01": ["ok"]})
    assert calls == []                       # s02 already done, s01 left alone
    assert status(plan)["needs_check"] == ["s01"]


def test_resend_only_when_asked(tmp_path):
    plan = make_plan(tmp_path, ["s01"])
    run(plan, {"s01": ["after", "ok"]})
    (tmp_path / "calls.log").unlink()
    assert run(plan, {"s01": ["after", "ok"]}, "--resend", "s01") == ["s01"]
    assert (tmp_path / "s01-2k.jpg").exists()


def test_found_tile_is_downloaded_not_resent(tmp_path):
    plan = make_plan(tmp_path, ["s01"])
    calls = run(plan, {"s01": ["after-tile"]})
    assert calls == ["s01", "s01", "--recover"]
    assert status(plan)["saved"] == ["s01"]


def test_failure_before_sending_is_retried_once(tmp_path):
    plan = make_plan(tmp_path, ["s01"])
    assert run(plan, {"s01": ["before", "ok"]}) == ["s01", "s01"]
    assert status(plan)["saved"] == ["s01"]


def test_wall_stops_the_run(tmp_path):
    plan = make_plan(tmp_path, ["s01", "s02", "s03"])
    assert run(plan, {"s01": ["ok"], "s02": ["wall"]}) == ["s01", "s02"]
    assert "Flow put up" in status(plan)["stopped"]


def test_refused_is_recorded_and_not_resent(tmp_path):
    plan = make_plan(tmp_path, ["s01", "s02"])
    run(plan, {"s01": ["refused"]})
    assert status(plan)["refused"] == ["s01"]
    (tmp_path / "calls.log").unlink()
    assert run(plan, {"s01": ["ok"]}) == []


# --- paths from another directory, and the stop file -------------------------------------------------

def run_from(cwd: Path, plan: Path, script: dict, extra_env: dict | None = None, *extra: str):
    env = dict(os.environ, FLOW_GEN=str(ROOT / "tests" / "fake_gen.mjs"),
               FAKE_SCRIPT=json.dumps(script), **(extra_env or {}))
    return subprocess.run(["node", str(ROOT / "flow" / "run.mjs"), str(plan), "--pause", "0",
                           "--chapter-pause", "0", *extra],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def test_run_from_another_directory_absolute_paths(tmp_path):
    plan = make_plan(tmp_path, ["s01", "s02"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = run_from(elsewhere, plan, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "s01-2k.jpg").exists() and (tmp_path / "s02-2k.jpg").exists()
    assert status(plan)["saved"] == ["s01", "s02"]


def test_relative_paths_resolved_against_base(tmp_path):
    work = tmp_path / "work"
    elsewhere = tmp_path / "elsewhere"
    (work / "out").mkdir(parents=True)
    elsewhere.mkdir()
    shots = [{"id": i, "chars": [], "prompt": f"{i}.prompt.txt", "out": f"out/{i}-2k.jpg",
              "meta": f"out/{i}.flow.json", "used": f"out/{i}.used.txt"} for i in ["s01", "s02"]]
    plan = work / "plan.json"
    plan.write_text(json.dumps({"project": "x/project/abc", "header_mark": "Wide", "base": str(work),
                                "journal": "journal.jsonl", "shots": shots}))
    r = run_from(elsewhere, plan, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (work / "out" / "s01-2k.jpg").exists() and (work / "out" / "s02-2k.jpg").exists()
    assert (work / "journal.jsonl").exists() and not (elsewhere / "journal.jsonl").exists()


def test_stop_file_stops_between_shots_and_resumes(tmp_path):
    plan = make_plan(tmp_path, ["s01", "s02", "s03"])
    r = run_from(tmp_path, plan, {}, {"FAKE_STOP_AFTER": "s01"})
    assert r.returncode == 6, r.stdout + r.stderr
    assert "stop file" in r.stdout
    st = status(plan)
    assert st["stopped"] == "stop file" and st["saved"] == ["s01"] and st["left"] == 2
    assert not (tmp_path / "STOP").exists()
    assert (tmp_path / "calls.log").read_text().split() == ["s01"]
    journaled = {json.loads(line)["id"] for line in (tmp_path / "journal.jsonl").read_text().splitlines()}
    assert journaled == {"s01"}                       # s02 and s03 were never sent, so never journaled
    (tmp_path / "calls.log").unlink()
    r = run_from(tmp_path, plan, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "calls.log").read_text().split() == ["s02", "s03"]    # s01 not resent
    assert status(plan)["stopped"] is None

