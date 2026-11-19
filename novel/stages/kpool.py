"""One shared job queue for the picture servers: a worker thread per server takes the next ready job, so whichever
server is free draws the next picture (`novel ktrial run`, `novel images`, `novel ktrial front`, QA redraws).

A job that fails on a server that still answers fails (the caller sees the exception). A job that fails on a server
that no longer answers goes back to the front of the queue for another server (two tries in all), and that worker
stops. When no worker is left every waiting job fails, so nobody waits for ever.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future

from .ktrial_run import MAX_FAILS_IN_ROW, RUN_ERRORS

TRIES = 2               # a job is given to at most two servers


class PoolStopped(RuntimeError):
    """The job was never run: the pool was stopped (Ctrl+C, no server left, a budget stop)."""


class _Job:
    def __init__(self, ident: str, fn, ready):
        self.ident, self.fn, self.ready = ident, fn, ready
        self.tries = 0
        self.future: Future = Future()


class Pool:
    """One worker thread per server (_Run). Jobs run in the order added, the first READY one first; front=True jumps
    the queue (QA redraws)."""

    join_every = 60.0       # seconds between looks for a late server

    def __init__(self, runs, say=print, join=None):
        """`join()` returns a new _Run once a late server answers, else None; polled while jobs remain."""
        self.say, self.join = say, join
        self.cv = threading.Condition()
        self.queue: list[_Job] = []
        self.flying = self.workers = self.dropped = 0
        self.closing = self.halted = False
        self.interrupted = self.tripped = False
        self.over = threading.Event()               # set when the pool is done: stops the helper that looks for servers
        self.threads: list[threading.Thread] = []
        for r in runs:
            self._start(r)
        if join:
            threading.Thread(target=self._joiner, daemon=True).start()

    # -- workers

    def _start(self, run) -> None:
        with self.cv:
            self.workers += 1
        t = threading.Thread(target=self._work, args=(run,), daemon=True)
        self.threads.append(t)
        t.start()

    def _joiner(self) -> None:
        while not self.over.wait(self.join_every):
            try:
                run = self.join()
            except Exception:                       # a failed look is tried again next time
                continue
            if run is not None:
                self.say(f"server {run.n} joined")
                self._start(run)
                return

    @staticmethod
    def _answers(run) -> bool:
        if getattr(run, "comfy", None) is None:     # the hosted API has no server to lose
            return True
        try:
            run.comfy.ping()
            return True
        except RUN_ERRORS:
            return False

    def _take(self):
        """The first ready job (waits for one); None when the worker should stop."""
        with self.cv:
            while True:
                if self.closing or self.halted:
                    return None
                job = next((j for j in self.queue if j.ready is None or j.ready()), None)
                if job is not None:
                    self.queue.remove(job)
                    self.flying += 1
                    return job
                self.cv.wait()

    def _work(self, run) -> None:
        fails = 0
        try:
            while (job := self._take()) is not None:
                job.tries += 1
                stop = requeue = False
                try:
                    result = job.fn(run)
                except KeyboardInterrupt:           # a real Ctrl+C reaches the main thread; this is the same stop
                    self.halt(interrupted=True)
                    job.future.set_exception(PoolStopped("interrupted"))
                    stop = True
                except RUN_ERRORS as e:
                    lost = not self._answers(run)
                    if lost and job.tries < TRIES:
                        self.say(f"server {run.n} lost; its picture goes to the other server")
                        requeue = stop = True
                    else:
                        job.future.set_exception(e)
                        fails += 1
                        if lost:
                            self.say(f"server {run.n} lost")
                        elif fails >= MAX_FAILS_IN_ROW:
                            self.tripped = True
                        stop = lost or fails >= MAX_FAILS_IN_ROW
                except BaseException as e:          # a budget stop or a bug: this job fails, the worker goes on
                    job.future.set_exception(e)
                else:
                    fails = 0
                    job.future.set_result(result)
                with self.cv:
                    self.flying -= 1
                    if requeue:
                        self.queue.insert(0, job)
                    self.cv.notify_all()
                if stop:
                    return
        finally:
            with self.cv:
                self.workers -= 1
                if not self.workers:
                    self._drop()
                self.cv.notify_all()

    def _drop(self) -> None:
        """Fail every waiting job (the caller holds the lock)."""
        waiting, self.queue = self.queue, []
        for j in waiting:
            self.dropped += 1
            j.future.set_exception(PoolStopped("no server is left to draw it"))

    # -- the caller's side

    def add(self, ident: str, fn, ready=None, front: bool = False) -> Future:
        """Queue fn(run) -> result; `ready()` says whether its dependency is met. The Future holds the result."""
        job = _Job(ident, fn, ready)
        with self.cv:
            if self.closing or self.halted or not self.workers:
                self.dropped += 1
                job.future.set_exception(PoolStopped("the pool is stopped"))
            else:
                self.queue.insert(0 if front else len(self.queue), job)
                self.cv.notify_all()
        return job.future

    def halt(self, interrupted: bool = False) -> None:
        """Take no more jobs (the ones in flight finish) and fail the waiting ones."""
        with self.cv:
            self.halted = True
            self.interrupted = self.interrupted or interrupted
            self._drop()
            self.cv.notify_all()

    def finish(self, until=None) -> None:
        """Block until the queue is empty, nothing is in flight and until() is falsy; then stop the workers."""
        with self.cv:
            try:
                # a timeout, not a bare wait: a bare lock wait is deaf to Ctrl+C on Windows
                while (self.queue and self.workers) or self.flying or (until and until()):
                    self.cv.wait(1.0)
            except KeyboardInterrupt:
                self.halt(interrupted=True)
                while self.flying:
                    self.cv.wait(1.0)
            self.closing = True
            self._drop()
            self.cv.notify_all()
        self.over.set()
        for t in self.threads:
            t.join()
