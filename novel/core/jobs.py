"""Background jobs: detached child processes that outlive the terminal.

A job is `python -X utf8 -m novel _job <name> --book B --ep N [args]`, started detached with its
output appended to <episode>/logs/<name>-<stamp>.log. State lives in <episode>/jobs/<name>.json.
The starter writes `running` + pid; the job process writes done / failed / stopped when its
command returns. A `running` state whose process is gone reads as `crashed`.

Process checks use ctypes on Windows. os.kill(pid, 0) is NEVER used there (it terminates).
A pid is only trusted together with its creation time, so a reused pid is not mistaken for ours.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import paths

NAMES = ("tts", "captions", "images")
NOT_BUILT = "novel images is not built yet"
STATES = ("running", "done", "failed", "stopped", "crashed")
STILL_ACTIVE = 259

# ------------------------------------------------------------------ processes

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _QUERY_LIMITED = 0x1000


def _open(pid: int):
    h = _k32.OpenProcess(_QUERY_LIMITED, False, int(pid))
    return h or None


def create_time(pid: int):
    """Creation time of a process (Windows FILETIME, 100 ns units) or None if unknown/gone."""
    if os.name == "nt":
        h = _open(pid)
        if not h:
            return None
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            _k32.CloseHandle(h)
    try:                                                     # Linux: start time in clock ticks
        return int(Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def pid_alive(pid, created=None) -> bool:
    """True if the process exists and (when `created` is given) is the same process."""
    if not pid:
        return False
    pid = int(pid)
    if os.name == "nt":
        h = _open(pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
                return False
        finally:
            _k32.CloseHandle(h)
    elif not Path(f"/proc/{pid}").exists():
        return False
    if created is not None:
        now = create_time(pid)
        if now is not None and now != created:
            return False
    return True


def terminate_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        import signal
        os.kill(int(pid), signal.SIGTERM)


# ------------------------------------------------------------------ files

def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def jobs_dir(ep_root: Path) -> Path:
    return Path(ep_root) / "jobs"


def logs_dir(ep_root: Path) -> Path:
    return Path(ep_root) / "logs"


def state_file(ep_root: Path, name: str) -> Path:
    return jobs_dir(ep_root) / f"{name}.json"


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for i in range(20):                       # Windows: a reader may hold the target for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.05)


def _raw_state(ep_root, name):
    f = state_file(ep_root, name)
    if not f.exists():
        return None
    for _ in range(5):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            time.sleep(0.05)
    return None


def read_state(ep_root, name):
    """The job's state dict (or None). `running` with a dead process becomes `crashed`."""
    st = _raw_state(ep_root, name)
    if st and st.get("state") == "running" and not pid_alive(st.get("pid"), st.get("created")):
        st = _raw_state(ep_root, name)        # the job may have finished between the two reads
        if st and st.get("state") == "running":
            st.update(state="crashed", finished=stamp(),
                      note="the process is gone without reporting a result; see the log")
            write_json_atomic(state_file(ep_root, name), st)
    return st


def list_states(ep_root) -> list[dict]:
    d = jobs_dir(ep_root)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        st = read_state(ep_root, f.stem)
        if st:
            out.append(st)
    return out


def newest_log(ep_root, name):
    d = logs_dir(ep_root)
    logs = sorted(d.glob(f"{name}-*.log")) if d.exists() else []
    return logs[-1] if logs else None


def last_line(path) -> str:
    try:
        lines = [l for l in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""


def tail_lines(path, n: int) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-n:] if n > 0 else lines


# ------------------------------------------------------------------ starting

@dataclass
class StartResult:
    status: str                 # started | running | failed
    pid: int | None = None
    log: str | None = None
    reason: str = ""
    flags: str = ""


def _creation_flags(with_breakaway: bool) -> int:
    f = 0x08000000 | 0x00000200              # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    if with_breakaway:
        f |= 0x01000000                      # CREATE_BREAKAWAY_FROM_JOB
    return f


def spawn_detached(cmd, env, log_fh, cwd=None):
    """Popen a detached child. Returns (process, flag description)."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT, env=env,
              cwd=str(cwd) if cwd else None, close_fds=True)
    if os.name == "nt":
        try:
            p = subprocess.Popen(cmd, creationflags=_creation_flags(True), **kw)
            return p, "CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP|CREATE_BREAKAWAY_FROM_JOB"
        except OSError:                      # the parent's job object forbids breakaway
            p = subprocess.Popen(cmd, creationflags=_creation_flags(False), **kw)
            return p, "CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP (breakaway refused)"
    return subprocess.Popen(cmd, start_new_session=True, **kw), "start_new_session"


def job_command(name, book, ep, args=()) -> list[str]:
    return [sys.executable, "-X", "utf8", "-m", "novel", "_job", name,
            "--book", book, "--ep", str(int(ep)), *map(str, args)]


def start(book: str, ep: int, name: str, args=(), spawn=None) -> StartResult:
    spawn = spawn or spawn_detached
    root = paths.ep_root(book, ep)
    cur = read_state(root, name)
    if cur and cur.get("state") == "running":
        return StartResult("running", cur.get("pid"), cur.get("log"),
                           f"already running (log: {cur.get('log')})")
    logs_dir(root).mkdir(parents=True, exist_ok=True)
    log = logs_dir(root) / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.log"
    n = 1
    while log.exists():                      # two starts in one second
        log = logs_dir(root) / f"{name}-{datetime.now():%Y%m%d-%H%M%S}-{n}.log"
        n += 1
    cmd = job_command(name, book, ep, args)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{stamp()}  command: {' '.join(cmd)}\n")
        fh.write(f"{stamp()}  settings: book={book} episode=E{int(ep):03d} args={list(map(str, args))} "
                 f"home={paths.home()} data={paths.book_dir(book)}\n")
    env = dict(os.environ)
    repo = str(paths.repo_root())
    env["PYTHONPATH"] = repo + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    env["NOVEL_JOB_LOG"] = str(log)          # lets the Chrome lock name the log of its holder
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        with open(log, "ab") as fh:
            proc, flags = spawn(cmd, env, fh, repo)
    except OSError as e:
        return StartResult("failed", None, str(log), f"could not start the process: {e}")
    state = {"name": name, "pid": proc.pid, "created": create_time(proc.pid), "cmd": cmd,
             "book": book, "episode": int(ep), "started": stamp(), "state": "running",
             "exit_code": None, "finished": None, "log": str(log), "note": ""}
    write_json_atomic(state_file(root, name), state)
    return StartResult("started", proc.pid, str(log), flags=flags)


# ------------------------------------------------------------------ stopping

def stop_process(ep_root, name):
    """Terminate the job's process tree if pid and creation time match the state file.
    Returns (ok, message)."""
    st = read_state(ep_root, name)
    if not st:
        return False, f"no {name} job has been started for this episode"
    if st.get("state") != "running":
        return False, f"{name} is not running (state: {st.get('state')})"
    pid, created = st.get("pid"), st.get("created")
    if not pid_alive(pid, created):
        return False, f"{name}: pid {pid} is not the process this tool started; nothing terminated"
    if created is not None and create_time(pid) != created:
        return False, f"{name}: pid {pid} creation time differs from the state file; nothing terminated"
    terminate_tree(pid)
    for _ in range(40):
        if not pid_alive(pid, created):
            break
        time.sleep(0.1)
    st = _raw_state(ep_root, name) or st
    st.update(state="stopped", finished=stamp(), exit_code=None, note="stopped by `novel jobs --stop`")
    write_json_atomic(state_file(ep_root, name), st)
    return True, f"{name}: stopped (pid {pid} and its child processes terminated)"


# ------------------------------------------------------------------ registry (what a job runs)

class JobError(Exception):
    """A job that cannot run; the message becomes the state's note."""


def _tts(book, ep, args):
    from novel.commands import tts_cmd
    from novel.core import context
    from novel.stages import episode_tts as T
    context.activate(book)
    state = T.load_state(ep)
    unpulled = T.run_waiting(state)
    chapters = None
    for i, a in enumerate(args):
        if a == "--chapters" and i + 1 < len(args):
            chapters = args[i + 1]
    if not unpulled:
        if not chapters and not T.default_chapters(ep):
            print("nothing to narrate: no locked chapter is without audio, and no run waits to be pulled")
            return 0
        push = ["push", "--book", book, "--ep", str(ep)] + (["--chapters", chapters] if chapters else [])
        print("pushing to Kaggle:", " ".join(push))
        code = tts_cmd.main(push)
        if code:
            return code
    else:
        print("a Kaggle run is already waiting to be pulled: watching it")
    return tts_cmd.main(["watch", "--book", book, "--ep", str(ep)])


def _captions(book, ep, args):
    from novel.commands import captions_cmd
    return captions_cmd.main(["--book", book, "--ep", str(ep), *args])


def _images(book, ep, args):
    from novel import cli
    handler = next((h for n, _, h in cli.COMMANDS if n == "images"), None)
    if handler is None:
        raise JobError(NOT_BUILT)
    from . import context
    context.activate(book)
    stop = paths.ep_root(book, ep) / "flow" / "STOP"
    if stop.exists():                         # left over from an earlier stop request
        stop.unlink()
        print("removed a stale flow/STOP file")
    return cli._load_handler(handler)(["--book", book, "--ep", str(ep), *args])


REGISTRY = {"tts": _tts, "captions": _captions, "images": _images}
CHAPTER_JOBS = ("tts", "captions", "images")      # jobs that accept --chapters


def load_test_registrations():
    """NOVEL_TEST_JOBS=<file.py>: the file's register(registry) may replace entries (tests only)."""
    f = os.environ.get("NOVEL_TEST_JOBS")
    if not f:
        return
    spec = importlib.util.spec_from_file_location("novel_test_jobs", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.register(REGISTRY)


# ------------------------------------------------------------------ the job process

class _Stamped:
    """Text stream that puts a time stamp in front of every line."""

    def __init__(self, raw):
        self.raw, self.buf = raw, ""
        self.encoding = "utf-8"

    def write(self, s):
        self.buf += str(s)
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.raw.write(f"{stamp()}  {line}\n")
            self.raw.flush()
        return len(s)

    def flush(self):
        if self.buf:
            self.raw.write(f"{stamp()}  {self.buf}\n")
            self.buf = ""
        self.raw.flush()

    def isatty(self):
        return False


def run_job(name: str, book: str, ep: int, args) -> int:
    """Runs inside the job process (`novel _job`). Returns the exit code."""
    root = paths.ep_root(book, ep)
    sf = state_file(root, name)
    for _ in range(100):                     # wait until the starter has written our pid
        st = _raw_state(root, name)
        if st and st.get("pid") == os.getpid():
            break
        time.sleep(0.05)
    raw, old_out, old_err = sys.stdout, sys.stdout, sys.stderr
    sys.stdout = sys.stderr = _Stamped(raw)
    note, code = "", 1
    try:
        load_test_registrations()
        fn = REGISTRY.get(name)
        if fn is None:
            raise JobError(f"unknown job '{name}'")
        print(f"job {name} started (pid {os.getpid()})")
        r = fn(book, ep, list(args))
        code = 0 if r is None else int(r)
    except JobError as e:
        note = str(e)
        print(f"FAILED: {note}")
    except SystemExit as e:
        if isinstance(e.code, int) or e.code is None:
            code = e.code or 0
        else:
            code, note = 1, str(e.code)
            print(note)
    except KeyboardInterrupt:
        code = 130
    except BaseException:
        note = "crashed with an exception (traceback in the log)"
        traceback.print_exc()
    final = "done" if code == 0 else "stopped" if code in (6, 130) else "failed"
    print(f"job {name} finished: {final} (exit code {code})")
    sys.stdout.flush()
    sys.stdout, sys.stderr = old_out, old_err
    st = _raw_state(root, name) or {"name": name, "pid": os.getpid()}
    st.update(state=final, exit_code=code, finished=stamp(),
              note=note or ("stop requested" if final == "stopped" else ""))
    write_json_atomic(sf, st)
    return code
