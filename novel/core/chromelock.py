"""One lock per Chrome DevTools port, for the whole machine.

Only one job at a time may drive the signed-in Chrome (images, character creation, other projects'
tools that honour the same file). The lock is a file created with O_EXCL under
%LOCALAPPDATA%/novel-pipeline (or $NOVEL_LOCK_DIR). A lock whose process is gone, or whose pid now
belongs to a different process (creation time differs), is stale and is replaced.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from . import jobs

DEFAULT_URL = "http://127.0.0.1:9222"


class ChromeBusy(SystemExit):
    """Another job holds the Chrome lock. Exits with the message when uncaught."""

    def __init__(self, message, holder=None):
        super().__init__(message)
        self.message, self.holder = message, holder


def lock_dir() -> Path:
    env = os.environ.get("NOVEL_LOCK_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "novel-pipeline"


def port_of(cdp_url: str) -> int:
    return urlparse(cdp_url).port or 9222


def cdp_port() -> int:
    from . import settings
    try:
        return port_of(str(settings.get("chrome.cdp_url", default=DEFAULT_URL)))
    except SystemExit:
        return port_of(DEFAULT_URL)


def lock_path(port=None) -> Path:
    return lock_dir() / f"chrome-cdp-{port or cdp_port()}.lock"


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _alive(h) -> bool:
    return bool(h) and jobs.pid_alive(h.get("pid"), h.get("created"))


def holder(port=None):
    """The lock's content plus "alive": bool, or None when there is no lock file."""
    h = _read(lock_path(port))
    if h is None:
        return None
    h["alive"] = _alive(h)
    return h


def describe(h) -> str:
    s = (f"{h.get('tool', 'a tool')} (pid {h.get('pid')}, {h.get('cmd')}; book {h.get('book')}, "
         f"episode {h.get('episode')}) since {h.get('started')} on {h.get('host')}")
    if h.get("log"):
        s += f"; log: {h['log']}"
    return s


class ChromeLock:
    def __init__(self, path: Path, data: dict):
        self.path, self.data, self.released = path, data, False

    def release(self):
        if self.released:
            return
        self.released = True
        cur = _read(self.path)
        if cur and cur.get("pid") == self.data["pid"] and cur.get("created") == self.data["created"]:
            try:
                self.path.unlink()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def acquire(cmd="", book=None, episode=None, port=None, wait_seconds=0, log=None,
            say=lambda m: print(m, file=sys.stderr)) -> ChromeLock:
    """Take the lock now or raise ChromeBusy naming the holder. Use as a context manager:

        with chromelock.acquire(cmd="novel images", book=b, episode=n):
            ...drive Chrome...
    """
    path = lock_path(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    me = os.getpid()
    data = {"pid": me, "created": jobs.create_time(me), "tool": "novel", "cmd": cmd or " ".join(sys.argv),
            "book": book, "episode": episode, "started": jobs.stamp(), "host": socket.gethostname()}
    log = log or os.environ.get("NOVEL_JOB_LOG")
    if log:
        data["log"] = str(log)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            h = _read(path)
            if h is not None and not _alive(h):
                say(f"chrome lock: stale lock from pid {h.get('pid')} ({h.get('started')}), replacing it")
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() < deadline:
                time.sleep(1)
                continue
            who = describe(h) if h else "an unreadable lock file"
            raise ChromeBusy(f"Chrome (port {port or cdp_port()}) is in use by {who}.\n"
                             f"Only one job may drive it at a time. Lock file: {path}", h)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return ChromeLock(path, data)
