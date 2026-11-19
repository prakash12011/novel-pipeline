"""novel.stages.kpool: one queue over several servers, with fake runs (no network, no ComfyUI)."""

import threading
import time
from types import SimpleNamespace

import pytest

from novel.kcomfy import ComfyError
from novel.stages.kpool import Pool, PoolStopped


def fake_run(n, alive=True):
    """A run whose server answers while alive["up"]; fn(run) code decides what a job does."""
    state = {"up": alive}

    def ping():
        if not state["up"]:
            raise ComfyError("gone")
        return {}

    return SimpleNamespace(n=n, comfy=SimpleNamespace(ping=ping), state=state, made=[])


def test_two_servers_share_the_jobs():
    a, b = fake_run(1), fake_run(2)
    pool = Pool([a, b], say=lambda t: None)
    gate = threading.Barrier(2, timeout=5)              # both servers must be busy at the same time

    def job(i):
        def fn(run):
            if i < 2:
                gate.wait()
            run.made.append(i)
            return (run.n, i)
        return fn

    futs = [pool.add(f"j{i}", job(i)) for i in range(6)]
    pool.finish()
    assert sorted(f.result()[1] for f in futs) == list(range(6))
    assert a.made and b.made and len(a.made) + len(b.made) == 6


def test_a_shot_waits_for_its_plate():
    a, b = fake_run(1), fake_run(2)
    pool = Pool([a, b], say=lambda t: None)
    order = []
    plate = pool.add("plate", lambda run: (time.sleep(0.3), order.append("plate")))
    shots = [pool.add(f"s{i}", lambda run, i=i: order.append(f"s{i}"), ready=plate.done) for i in range(3)]
    other = pool.add("other", lambda run: order.append("other"))          # no dependency: may run at once
    pool.finish()
    assert order.index("plate") < min(order.index(f"s{i}") for i in range(3))
    assert order.index("other") < order.index("plate")
    assert all(f.exception() is None for f in [plate, other, *shots])


def test_a_server_that_dies_hands_its_job_to_the_other_and_stops():
    a, b = fake_run(1), fake_run(2)
    said, calls = [], []
    pool = Pool([a, b], say=said.append)

    def fn(run):
        calls.append(run.n)
        if len(calls) == 1:                                              # the first server to take it dies
            run.state["up"] = False
            raise ComfyError("tunnel dropped")
        return run.n

    fut = pool.add("pic", fn)
    pool.finish()
    dead = calls[0]
    assert len(calls) == 2 and calls[1] != dead and fut.result() == calls[1]
    assert f"server {dead} lost; its picture goes to the other server" in said
    assert pool.workers == 0 and not pool.dropped


def test_a_job_that_fails_on_a_live_server_fails_and_three_in_a_row_stop_the_worker():
    a = fake_run(1)
    pool = Pool([a], say=lambda t: None)

    def boom(run):
        raise ComfyError("CUDA out of memory")

    futs = [pool.add(f"j{i}", boom) for i in range(5)]
    pool.finish()
    assert all(isinstance(f.exception(), (ComfyError, PoolStopped)) for f in futs)
    assert [type(f.exception()) for f in futs] == [ComfyError] * 3 + [PoolStopped] * 2
    assert pool.tripped and pool.dropped == 2


def test_every_server_dead_fails_the_rest_and_finish_returns():
    a, b = fake_run(1), fake_run(2)
    pool = Pool([a, b], say=lambda t: None)

    def fn(run):
        run.state["up"] = False
        raise ComfyError("gone")

    futs = [pool.add(f"j{i}", fn) for i in range(4)]
    t0 = time.time()
    pool.finish()
    assert time.time() - t0 < 10
    assert all(f.done() and isinstance(f.exception(), (ComfyError, PoolStopped)) for f in futs)
    assert pool.workers == 0 and pool.dropped >= 1
    with pytest.raises(PoolStopped):
        pool.add("late", lambda run: 1).result()                         # nobody is left to take it


def test_a_job_is_tried_on_two_servers_at_most():
    a, b, c = fake_run(1), fake_run(2), fake_run(3)
    pool = Pool([a, b, c], say=lambda t: None)
    seen = []

    def fn(run):
        seen.append(run.n)
        run.state["up"] = False
        raise ComfyError("gone")

    fut = pool.add("pic", fn)
    pool.finish()
    assert len(seen) == 2 and isinstance(fut.exception(), ComfyError)


def test_front_jumps_the_queue():
    a = fake_run(1)
    pool = Pool([a], say=lambda t: None)
    release, order = threading.Event(), []
    pool.add("first", lambda run: release.wait(5))                       # holds the only server
    time.sleep(0.1)
    queued = [pool.add(f"q{i}", lambda run, i=i: order.append(f"q{i}")) for i in range(2)]
    front = pool.add("redraw", lambda run: order.append("redraw"), front=True)
    release.set()
    pool.finish()
    assert order == ["redraw", "q0", "q1"] and front.done() and all(f.done() for f in queued)


def test_a_late_server_joins_and_takes_jobs(monkeypatch):
    monkeypatch.setattr(Pool, "join_every", 0.05)
    a, late = fake_run(1), fake_run(2)
    said, release = [], threading.Event()
    asked = []

    def join():
        asked.append(1)
        return late if len(asked) >= 3 else None

    pool = Pool([a], say=said.append, join=join)
    pool.add("hold", lambda run: release.wait(5))                        # server 1 is busy: server 2 must take the rest
    rest = [pool.add(f"j{i}", lambda run: run.n) for i in range(3)]
    results = [f.result(timeout=5) for f in rest]
    release.set()
    pool.finish()
    assert results == [2, 2, 2] and "server 2 joined" in said and len(asked) == 3


def test_halt_fails_the_waiting_jobs_and_lets_the_one_in_flight_finish():
    a = fake_run(1)
    pool = Pool([a], say=lambda t: None)
    release, ran = threading.Event(), []
    flying = pool.add("flying", lambda run: (release.wait(5), ran.append("flying")))
    time.sleep(0.1)
    waiting = pool.add("waiting", lambda run: ran.append("waiting"))
    pool.halt(interrupted=True)
    release.set()
    pool.finish()
    assert ran == ["flying"] and flying.exception() is None
    assert isinstance(waiting.exception(), PoolStopped) and pool.interrupted


def test_finish_waits_for_until():
    a = fake_run(1)
    pool = Pool([a], say=lambda t: None)
    busy = {"n": 1}
    threading.Timer(0.3, lambda: busy.update(n=0)).start()
    t0 = time.time()
    pool.finish(until=lambda: busy["n"])
    assert time.time() - t0 >= 0.25
