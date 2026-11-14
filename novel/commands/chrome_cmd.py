"""novel chrome [--dry-run] [--url URL] [--book B]

Start the shared Chrome for Flow with remote debugging on, unless it already answers.
Never closes Chrome and never opens a second instance on a port that answers."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request

from novel.core import chromelock, context, settings

USAGE = "usage: novel chrome [--dry-run] [--url URL] [--book B]"
FLOW_URL = "https://labs.google/fx/tools/flow"
DEFAULT_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def probe(cdp_url: str, timeout: float = 2.0):
    """The browser name/version if Chrome answers on the DevTools port, else None."""
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/version", timeout=timeout) as r:
            return str(json.loads(r.read().decode("utf-8")).get("Browser") or "Chrome")
    except Exception:
        return None


def launch(argv):
    """Start Chrome detached from this terminal."""
    kw = {}
    if os.name == "nt":
        try:
            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True,
                                    creationflags=0x00000008 | 0x00000200 | 0x01000000)
        except OSError:
            kw["creationflags"] = 0x00000008 | 0x00000200
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, **kw)


def command_line(exe, port, profile, url):
    return [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}", url]


def start(exe, profile, cdp_url, url, *, dry_run=False, answers=None, do_launch=None,
          sleep=time.sleep, clock=time.monotonic, wait_seconds=20.0, say=print) -> int:
    """All of the start logic. `answers(cdp_url)` -> version or None; `do_launch(argv)` starts Chrome."""
    answers, do_launch = answers or probe, do_launch or launch
    port = chromelock.port_of(cdp_url)
    argv = command_line(exe, port, profile, url)
    version = answers(cdp_url)
    if dry_run:
        say("dry run: " + " ".join(f'"{x}"' if " " in x else x for x in argv))
        say(f"Chrome is already running on port {port} ({version}); a real run would do nothing."
            if version else f"Nothing answers on port {port}; a real run would start it with that command.")
        return 0
    if version:
        say(f"Chrome is already running on port {port} ({version})")
        return 0
    do_launch(argv)
    say(f"Chrome is starting on port {port} ...")
    deadline = clock() + wait_seconds
    while True:
        version = answers(cdp_url)
        if version:
            break
        if clock() >= deadline:
            say(f"Chrome did not answer on port {port} within {int(wait_seconds)} s. "
                "If a window opened, it may be starting slowly: run `novel chrome` again. "
                "If another Chrome already uses this profile, close that window first "
                "(two Chromes cannot share one profile).")
            return 1
        sleep(0.5)
    say(f"Chrome answers on port {port} ({version}). Next:\n"
        "  1. Sign in to Flow if asked.\n"
        "  2. Open the book's Flow project.\n"
        f"  3. In the prompt bar choose one 16:9 image with the model from images.flow_model "
        f"({settings.get('images.flow_model', default='Nano Banana Pro')}).")
    return 0


def main(argv, **inject) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel chrome", usage=USAGE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--url")
    ap.add_argument("--book")
    a = ap.parse_args(argv)

    cdp_url = str(settings.get("chrome.cdp_url", default=chromelock.DEFAULT_URL))
    exe = settings.get("chrome.exe", default=None) or DEFAULT_EXE
    profile = settings.get("chrome.profile", default=None)
    if not profile:
        raise SystemExit('chrome.profile is not set. Fix with: novel settings init --force '
                         '--data-dir "<path>" --chrome-profile "<path>"')
    url = a.url
    if not url:
        try:
            book, _ = context.resolve(a.book, None)
            url = settings.get("images.flow_project", book=book, default=None)
        except SystemExit:
            url = None
    url = url or FLOW_URL
    if not a.dry_run and not inject:
        if not os.path.exists(str(exe)):
            raise SystemExit(f"Chrome not found at {exe} (setting chrome.exe)")
        if not os.path.exists(str(profile)):
            raise SystemExit(f"Chrome profile not found at {profile} (setting chrome.profile)")
    return start(str(exe), str(profile), cdp_url, url, dry_run=a.dry_run, **inject)
