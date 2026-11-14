"""The machine-wide Chrome lock. No real Chrome; locks live in a temp NOVEL_LOCK_DIR."""

import json
import os
import subprocess
import sys

import pytest
import yaml

from novel.core import chromelock, jobs

PORT = 9333


@pytest.fixture(autouse=True)
def lockenv(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.yaml").write_text(yaml.safe_dump(
        {"data_dir": str(tmp_path), "chrome": {"cdp_url": f"http://127.0.0.1:{PORT}"}}), encoding="utf-8")
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.setenv("NOVEL_LOCK_DIR", str(tmp_path / "locks"))

    def boom(*a, **k):
        raise AssertionError("os.kill must never be called")
    monkeypatch.setattr(os, "kill", boom)
    return tmp_path


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_port_comes_from_settings(lockenv):
    assert chromelock.cdp_port() == PORT
    assert chromelock.lock_path().name == f"chrome-cdp-{PORT}.lock"
    assert chromelock.lock_path().parent == lockenv / "locks"


def test_exclusive_acquire_and_contents():
    lock = chromelock.acquire(cmd="novel images", book="tb", episode=1)
    h = json.loads(chromelock.lock_path().read_text(encoding="utf-8"))
    assert h["pid"] == os.getpid() and h["tool"] == "novel" and h["cmd"] == "novel images"
    assert h["book"] == "tb" and h["episode"] == 1 and h["host"] and h["started"] and h["created"]
    assert chromelock.holder()["alive"] is True
    lock.release()
    assert not chromelock.lock_path().exists() and chromelock.holder() is None


def test_second_acquire_fails_naming_holder(monkeypatch):
    monkeypatch.setenv("NOVEL_JOB_LOG", "C:/logs/images-1.log")
    with chromelock.acquire(cmd="novel images", book="tb", episode=7):
        with pytest.raises(chromelock.ChromeBusy) as e:
            chromelock.acquire(cmd="novel characters make", book="tb", episode=7)
    msg = str(e.value)
    assert f"pid {os.getpid()}" in msg and "novel images" in msg and "since" in msg
    assert "C:/logs/images-1.log" in msg


def test_released_even_on_exception():
    with pytest.raises(RuntimeError):
        with chromelock.acquire(cmd="x"):
            raise RuntimeError("boom")
    assert not chromelock.lock_path().exists()
    with chromelock.acquire(cmd="again"):          # and it can be taken again
        pass


def test_stale_lock_with_dead_pid_is_replaced():
    p = chromelock.lock_path()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"pid": dead_pid(), "created": 1, "tool": "novel", "cmd": "old",
                             "started": "2020-01-01 00:00:00", "host": "h"}), encoding="utf-8")
    said = []
    with chromelock.acquire(cmd="new", say=said.append):
        assert json.loads(p.read_text(encoding="utf-8"))["cmd"] == "new"
    assert said and "stale" in said[0]


def test_live_pid_with_other_creation_time_is_stale():
    p = chromelock.lock_path()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"pid": os.getpid(), "created": jobs.create_time(os.getpid()) + 12345,
                             "tool": "novel", "cmd": "reused pid"}), encoding="utf-8")
    assert chromelock.holder()["alive"] is False
    said = []
    with chromelock.acquire(cmd="new", say=said.append):
        pass
    assert "stale" in said[0]


def test_wait_seconds_waits_then_gives_up(monkeypatch):
    with chromelock.acquire(cmd="holder"):
        t = []
        monkeypatch.setattr(chromelock.time, "sleep", lambda s: t.append(s))
        clock = iter([0, 0.5, 1.5, 2.5, 3.5])
        monkeypatch.setattr(chromelock.time, "monotonic", lambda: next(clock))
        with pytest.raises(chromelock.ChromeBusy):
            chromelock.acquire(cmd="waiter", wait_seconds=2)
        assert t                                        # it did wait before giving up


def test_release_leaves_a_lock_that_is_not_ours():
    lock = chromelock.acquire(cmd="mine")
    lock.data = dict(lock.data, pid=lock.data["pid"] + 1)       # pretend it was taken over
    lock.release()
    assert chromelock.lock_path().exists()
