"""Background jobs: detached processes, state files, logs, `novel run`, `novel jobs`.

Real child processes run fake registrations (NOVEL_TEST_JOBS); no network, no Chrome, no Kaggle.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from novel import cli
from novel.core import jobs, paths

REPO = Path(__file__).resolve().parents[1]
BOOK = "tb"

PLUGIN = '''
import os, sys, time

def register(reg):
    def ok(book, ep, args):
        for i in range(3):
            print(f"working {i} args={args}", flush=True)
            time.sleep(0.4)
        return 0
    def fail(book, ep, args):
        print("about to fail", flush=True)
        return 3
    def slow(book, ep, args):
        print("slow job started", flush=True)
        time.sleep(60)
        return 0
    def quick(book, ep, args):
        print("quick done", flush=True)
        return 0
    reg["ok"], reg["fail"], reg["slow"] = ok, fail, slow
    if os.environ.get("FAKE_RUN"):
        reg["tts"], reg["captions"], reg["images"] = ok, quick, slow
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    plugin = tmp_path / "fake_jobs.py"
    plugin.write_text(PLUGIN, encoding="utf-8")
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.setenv("NOVEL_TEST_JOBS", str(plugin))
    monkeypatch.setenv("NOVEL_LOCK_DIR", str(tmp_path / "locks"))
    root = paths.ep_root(BOOK, 1)
    root.mkdir(parents=True)
    started = []
    yield type("Env", (), {"root": root, "home": home, "tmp": tmp_path, "plugin": plugin, "started": started})
    for st in jobs.list_states(root):           # never leave a stray process behind
        if st.get("state") == "running" and st.get("pid"):
            jobs.stop_process(root, st["name"])


def wait_state(root, name, want, timeout=20):
    end = time.time() + timeout
    st = None
    while time.time() < end:
        st = jobs.read_state(root, name)
        if st and st["state"] in want:
            return st
        time.sleep(0.1)
    raise AssertionError(f"{name} never reached {want}; last state {st}")


def test_job_runs_to_done(env):
    r = jobs.start(BOOK, 1, "ok", ["--chapters", "1-2"])
    assert r.status == "started" and r.pid
    st = jobs.read_state(env.root, "ok")
    assert st["state"] == "running" and st["pid"] == r.pid and st["created"]
    st = wait_state(env.root, "ok", ("done", "failed"))
    assert st["state"] == "done" and st["exit_code"] == 0 and st["finished"]
    text = Path(st["log"]).read_text(encoding="utf-8")
    lines = text.splitlines()
    assert "command:" in lines[0] and "settings:" in lines[1]
    work = [l for l in lines if "working" in l]
    assert len(work) == 3
    assert all(l[:4].isdigit() and l[4] == "-" and l[13] == ":" for l in lines)     # time stamps
    assert "--chapters" in work[0]
    assert (env.root / "logs").glob("ok-*.log")


def test_failing_job_is_failed(env):
    jobs.start(BOOK, 1, "fail")
    st = wait_state(env.root, "fail", ("done", "failed"))
    assert st["state"] == "failed" and st["exit_code"] == 3


def test_unknown_job_fails_with_note(env):
    jobs.start(BOOK, 1, "nonsense")
    st = wait_state(env.root, "nonsense", ("failed",))
    assert "unknown job" in st["note"]


def test_double_start_refused(env):
    first = jobs.start(BOOK, 1, "slow")
    second = jobs.start(BOOK, 1, "slow")
    assert second.status == "running" and second.pid == first.pid
    assert first.log in second.reason
    assert len(list((env.root / "logs").glob("slow-*.log"))) == 1


def test_running_with_dead_pid_reads_crashed(env):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    created = jobs.create_time(os.getpid())
    jobs.write_json_atomic(jobs.state_file(env.root, "ghost"), {
        "name": "ghost", "pid": p.pid, "created": created, "cmd": [], "started": jobs.stamp(),
        "state": "running", "exit_code": None, "finished": None, "log": "x", "note": ""})
    st = jobs.read_state(env.root, "ghost")
    assert st["state"] == "crashed" and st["finished"]
    assert json.loads(jobs.state_file(env.root, "ghost").read_text())["state"] == "crashed"


def test_alive_check_survives_os_kill_ban(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("os.kill must not be used")
    monkeypatch.setattr(os, "kill", boom)
    assert jobs.pid_alive(os.getpid())
    assert not jobs.pid_alive(os.getpid(), created=12345)       # same pid, other creation time


# ------------------------------------------------------------------ survival of the starter

STARTER = textwrap.dedent('''
    import sys, time, pathlib
    from novel.core import jobs
    go = pathlib.Path(sys.argv[1])
    for _ in range(100):                       # wait until the test has put us in its job object
        if go.exists():
            break
        time.sleep(0.05)
    r = jobs.start("tb", 1, "ok")
    pathlib.Path(sys.argv[2]).write_text(f"{r.pid} {r.flags}")
''')


def _kill_on_close_job():
    """A Windows job object that kills its processes when closed (what closing a terminal does),
    but allows breakaway. Returns (handle, assign(proc), close())."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]

    class BASIC(ctypes.Structure):
        _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                    ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                    ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

    class IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in "abcdef"]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IOC), ("p", ctypes.c_size_t), ("j", ctypes.c_size_t),
                    ("pm", ctypes.c_size_t), ("jm", ctypes.c_size_t)]

    job = k.CreateJobObjectW(None, None)
    ext = EXT()
    ext.Basic.LimitFlags = 0x2000 | 0x0800        # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    assert k.SetInformationJobObject(job, 9, ctypes.byref(ext), ctypes.sizeof(ext)), ctypes.get_last_error()

    def assign(proc):
        h = k.OpenProcess(0x1F0FFF, False, proc.pid)
        ok = k.AssignProcessToJobObject(job, h)
        k.CloseHandle(h)
        return bool(ok)

    return job, assign, lambda: k.CloseHandle(job)


def test_job_survives_starter_exit(env):
    """The starter process (inside a kill-on-close job object, like a terminal) starts a job and
    exits; the job object is then closed. The job must still be alive and finish by itself."""
    script = env.tmp / "starter.py"
    script.write_text(STARTER, encoding="utf-8")
    go, out = env.tmp / "go", env.tmp / "out.txt"
    e = dict(os.environ, PYTHONPATH=str(REPO))
    p = subprocess.Popen([sys.executable, str(script), str(go), str(out)], env=e,
                         creationflags=0x08000000 if os.name == "nt" else 0)
    close = None
    if os.name == "nt":
        _, assign, close = _kill_on_close_job()
        assert assign(p), "could not put the starter into the test job object"
    go.write_text("go")
    p.wait(timeout=30)
    pid_s, _, flags = out.read_text().partition(" ")
    pid = int(pid_s)
    print("flags that worked:", flags)
    assert p.returncode == 0
    assert jobs.pid_alive(pid), "the job died with its starter"
    if close:
        close()                                   # "close the terminal": kills whatever is still in the job
    time.sleep(0.3)
    assert jobs.pid_alive(pid), "the job was killed when the starter's job object closed"
    st = wait_state(env.root, "ok", ("done", "failed"))
    assert st["state"] == "done" and st["exit_code"] == 0
    if os.name == "nt":
        assert "BREAKAWAY" in flags and "refused" not in flags


# ------------------------------------------------------------------ novel run / novel jobs

def run_cli(*argv):
    return cli.main(list(argv))


def test_run_starts_three_jobs(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RUN", "1")
    code = run_cli("run", "--book", BOOK, "--ep", "1", "--chapters", "1-2", "--images-in-background")
    out = capsys.readouterr().out
    assert code == 0
    for name in ("tts", "captions", "images"):
        assert f"{name}" in out and "started" in out
    assert out.count("pid ") == 3 and out.count("log ") == 3
    assert "Check progress:  novel jobs    Stop images cleanly:  novel jobs --stop images" in out
    assert {s["name"] for s in jobs.list_states(env.root)} == {"tts", "captions", "images"}
    # a second run reports "already running" for the two still alive
    run_cli("run", "--book", BOOK, "--ep", "1", "--images-in-background")
    assert capsys.readouterr().out.count("already running") >= 2


def test_run_no_flags(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RUN", "1")
    code = run_cli("run", "--book", BOOK, "--ep", "1", "--no-tts", "--no-images")
    out = capsys.readouterr().out
    assert code == 0
    assert "skipped" in out and "--no-tts" in out and "--no-images" in out
    assert [s["name"] for s in jobs.list_states(env.root)] == ["captions"]


def test_run_one_failing_start_does_not_block(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RUN", "1")
    real = jobs.spawn_detached

    def spawn(cmd, e, fh, cwd=None):
        if cmd[6] == "captions":
            raise OSError("no process for you")
        return real(cmd, e, fh, cwd)
    monkeypatch.setattr(jobs, "spawn_detached", spawn)
    code = run_cli("run", "--book", BOOK, "--ep", "1", "--images-in-background")
    out = capsys.readouterr().out
    assert code == 1
    assert "captions" in out and "FAILED to start" in out
    assert out.count("started") == 2
    assert {s["name"] for s in jobs.list_states(env.root)} == {"tts", "images"}


def test_jobs_table_and_tail(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RUN", "1")
    run_cli("run", "--book", BOOK, "--ep", "1", "--no-captions", "--images-in-background")
    capsys.readouterr()
    time.sleep(1)
    assert run_cli("jobs", "--book", BOOK, "--ep", "1") == 0
    out = capsys.readouterr().out
    assert "JOB" in out and "STATE" in out and "running" in out and "tts" in out and "images" in out
    wait_state(env.root, "tts", ("done",))
    run_cli("jobs", "--book", BOOK, "--ep", "1")
    out = capsys.readouterr().out
    assert "done" in out and "ended" in out
    assert run_cli("jobs", "--book", BOOK, "--ep", "1", "--tail", "tts", "-n", "2") == 0
    out = capsys.readouterr().out.splitlines()
    assert "working 2" in out[-2] and "finished: done" in out[-1]
    assert run_cli("jobs", "--book", BOOK, "--ep", "1", "--tail", "nothing") == 1


def test_jobs_follow_stops_on_ctrl_c(env, monkeypatch, capsys):
    from novel.commands import jobs_cmd
    jobs.start(BOOK, 1, "fail")
    wait_state(env.root, "fail", ("failed",))
    calls = []

    def sleeper(s):
        calls.append(s)
        raise KeyboardInterrupt
    assert jobs_cmd._tail(env.root, "fail", 5, True, sleep=sleeper) == 0
    assert calls == [1]


def test_jobs_all_lists_recent_episodes(env, capsys):
    root2 = paths.ep_root(BOOK, 2)
    jobs.write_json_atomic(jobs.state_file(root2, "captions"), {
        "name": "captions", "episode": 2, "state": "done", "exit_code": 0, "pid": 1,
        "started": jobs.stamp(), "finished": jobs.stamp(), "log": "", "note": ""})
    old = jobs.stamp()
    jobs.write_json_atomic(jobs.state_file(env.root, "tts"), {
        "name": "tts", "episode": 1, "state": "done", "exit_code": 0, "pid": 1,
        "started": "2020-01-01 00:00:00", "finished": "2020-01-01 00:10:00", "log": "", "note": ""})
    assert run_cli("jobs", "--book", BOOK, "--all") == 0
    out = capsys.readouterr().out
    assert "E002" in out and "captions" in out and "tts" not in out


def test_stop_images_writes_stop_file_and_kills_nothing(env, capsys):
    jobs.start(BOOK, 1, "images")             # the real images job: "not built yet" while no handler
    st = wait_state(env.root, "images", ("failed", "running", "done"))
    r = jobs.start(BOOK, 1, "slow")
    killed = []
    real = jobs.terminate_tree
    jobs.terminate_tree = lambda pid: killed.append(pid)
    try:
        assert run_cli("jobs", "--book", BOOK, "--ep", "1", "--stop", "images") == 0
    finally:
        jobs.terminate_tree = real
    out = capsys.readouterr().out
    assert (env.root / "flow" / "STOP").exists()
    assert "after the current picture" in out and killed == []
    assert jobs.pid_alive(r.pid)


def test_stop_captions_terminates_matching_process_only(env, capsys):
    r = jobs.start(BOOK, 1, "slow")
    wait_state(env.root, "slow", ("running",))
    # a process we started, recorded with the WRONG creation time: must not be touched
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        jobs.write_json_atomic(jobs.state_file(env.root, "captions"), {
            "name": "captions", "pid": bystander.pid, "created": jobs.create_time(bystander.pid) + 1,
            "state": "running", "started": jobs.stamp(), "log": "", "note": ""})
        # read_state sees the mismatch as a dead job (pid reuse): nothing to terminate
        assert run_cli("jobs", "--book", BOOK, "--ep", "1", "--stop", "captions") == 1
        assert bystander.poll() is None
        # now the matching one: the job we really started, under the name "captions"
        st = jobs.read_state(env.root, "slow")
        jobs.write_json_atomic(jobs.state_file(env.root, "captions"), dict(st, name="captions"))
        capsys.readouterr()
        assert run_cli("jobs", "--book", BOOK, "--ep", "1", "--stop", "captions") == 0
        assert "stopped" in capsys.readouterr().out
        deadline = time.time() + 10
        while jobs.pid_alive(r.pid) and time.time() < deadline:
            time.sleep(0.1)
        assert not jobs.pid_alive(r.pid)
        assert jobs.read_state(env.root, "captions")["state"] == "stopped"
        assert bystander.poll() is None
    finally:
        bystander.kill()


def test_stop_tts_says_kaggle_keeps_going(env, capsys):
    jobs.start(BOOK, 1, "slow")
    st = wait_state(env.root, "slow", ("running",))
    jobs.write_json_atomic(jobs.state_file(env.root, "tts"), dict(st, name="tts"))
    assert run_cli("jobs", "--book", BOOK, "--ep", "1", "--stop", "tts") == 0
    assert "Kaggle run itself keeps going" in capsys.readouterr().out


def test_images_job_has_a_handler_now():
    assert next(h for n, _, h in cli.COMMANDS if n == "images") == "novel.commands.images_cmd:main"


def test_images_job_calls_the_handler_when_it_exists(env, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "COMMANDS", [(n, h, "tests.test_jobs:fake_images" if n == "images" else s)
                                          for n, h, s in cli.COMMANDS])
    monkeypatch.setattr(cli, "_load_handler", lambda spec: (lambda argv: seen.append(argv) or 0))
    stop = env.root / "flow" / "STOP"
    stop.parent.mkdir(parents=True)
    stop.write_text("old")
    assert jobs._images(BOOK, 1, ["--chapters", "2"]) == 0
    assert seen == [["--book", BOOK, "--ep", "1", "--chapters", "2"]] and not stop.exists()


def test_job_command_is_hidden_but_dispatches(env, capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "_job" not in out and "jobs" in out
    # dispatch in-process: a registered fake runs and the state file records the result
    jobs.write_json_atomic(jobs.state_file(env.root, "ok"), {"name": "ok", "pid": os.getpid(),
                                                              "state": "running", "log": ""})
    code = cli.main(["_job", "ok", "--book", BOOK, "--ep", "1", "--x", "1"])
    assert code == 0
    st = jobs.read_state(env.root, "ok")
    assert st["state"] == "done" and st["exit_code"] == 0


# ------------------------------------------------------------------ novel run: pictures in the foreground

def test_run_foreground_images_after_two_background_jobs(env, monkeypatch, capsys):
    from novel.commands import run_cmd
    monkeypatch.setenv("FAKE_RUN", "1")
    seen = []
    monkeypatch.setattr(run_cmd, "images_main", lambda argv, **kw: seen.append(list(argv)) or 0)
    code = run_cli("run", "--book", BOOK, "--ep", "1", "--chapters", "2-3")
    out = capsys.readouterr().out
    assert code == 0
    assert seen == [["--book", BOOK, "--ep", "1", "--chapters", "2-3"]]
    assert {s["name"] for s in jobs.list_states(env.root)} == {"tts", "captions"}      # no hidden images job
    assert "tts and captions are running in the background" in out and "novel jobs" in out
    assert "generated in THIS window" in out
    assert out.index("captions") < out.index("THIS window")


def test_run_foreground_images_exit_code_passes_through(env, monkeypatch, capsys):
    from novel.commands import run_cmd
    monkeypatch.setenv("FAKE_RUN", "1")
    monkeypatch.setattr(run_cmd, "images_main", lambda argv, **kw: 6)
    assert run_cli("run", "--book", BOOK, "--ep", "1", "--no-tts", "--no-captions") == 6


def test_run_no_images_skips_the_image_step(env, monkeypatch, capsys):
    from novel.commands import run_cmd
    monkeypatch.setenv("FAKE_RUN", "1")
    monkeypatch.setattr(run_cmd, "images_main", lambda argv, **kw: pytest.fail("images must not run"))
    assert run_cli("run", "--book", BOOK, "--ep", "1", "--no-images") == 0
    assert {s["name"] for s in jobs.list_states(env.root)} == {"tts", "captions"}


def test_run_images_in_background_keeps_the_old_job(env, monkeypatch, capsys):
    from novel.commands import run_cmd
    monkeypatch.setenv("FAKE_RUN", "1")
    monkeypatch.setattr(run_cmd, "images_main", lambda argv, **kw: pytest.fail("images must not run in the foreground"))
    assert run_cli("run", "--book", BOOK, "--ep", "1", "--images-in-background") == 0
    assert {s["name"] for s in jobs.list_states(env.root)} == {"tts", "captions", "images"}
