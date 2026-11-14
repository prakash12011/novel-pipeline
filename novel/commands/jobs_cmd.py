"""novel jobs [--book B] [--ep N] [--tail NAME [-n LINES]] [--follow] [--stop NAME] [--all]

Show, tail or stop the background jobs started by `novel run`."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta

from novel.core import context, jobs, paths

USAGE = ("usage: novel jobs [--book B] [--ep N] [--all]\n"
         "       novel jobs --tail NAME [-n LINES] [--follow]\n"
         "       novel jobs --stop NAME")


def _parse(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _short(dt):
    if dt is None:
        return "-"
    return dt.strftime("%H:%M:%S") if dt.date() == datetime.now().date() else dt.strftime("%d %b %H:%M")


def _duration(td):
    s = max(int(td.total_seconds()), 0)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"


def _row(st, ep_root, with_ep):
    started, finished = _parse(st.get("started")), _parse(st.get("finished"))
    if st.get("state") == "running":
        when = "running " + _duration(datetime.now() - started) if started else "running"
    else:
        when = "ended " + _short(finished)
    log = st.get("log")
    last = jobs.last_line(log) if log else ""
    if len(last) > 70:
        last = last[:67] + "..."
    ex = st.get("exit_code")
    row = [st.get("name", "?"), st.get("state", "?"), _short(started), when, "-" if ex is None else str(ex), last]
    if with_ep:
        row.insert(0, f"E{int(st.get('episode') or 0):03d}")
    return row


def _table(rows, header):
    widths = [max(len(str(r[i])) for r in rows + [header]) for i in range(len(header) - 1)]
    for r in [header] + rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)) + "  " + str(r[-1]))


def _episodes(book):
    d = paths.episodes_dir(book)
    return sorted(p for p in d.glob("E*") if p.is_dir()) if d.exists() else []


def _show(book, ep, all_eps):
    rows = []
    if all_eps:
        cutoff = datetime.now() - timedelta(hours=24)
        for root in _episodes(book):
            for st in jobs.list_states(root):
                fin = _parse(st.get("finished"))
                if st.get("state") == "running" or (fin and fin >= cutoff):
                    rows.append(_row(st, root, True))
        header = ["EP", "JOB", "STATE", "STARTED", "TIME", "EXIT", "LAST LOG LINE"]
        if not rows:
            print("no jobs running or finished in the last 24 hours")
            return 0
    else:
        root = paths.ep_root(book, ep)
        rows = [_row(st, root, False) for st in jobs.list_states(root)]
        header = ["JOB", "STATE", "STARTED", "TIME", "EXIT", "LAST LOG LINE"]
        if not rows:
            print("no jobs yet. Start them with: novel run")
            return 0
    _table(rows, header)
    return 0


def _tail(root, name, n, follow, sleep=time.sleep):
    log = jobs.newest_log(root, name)
    if log is None:
        print(f"no log for '{name}' yet", file=sys.stderr)
        return 1
    print(f"--- {log}")
    lines = jobs.tail_lines(log, n)
    print("\n".join(lines))
    if not follow:
        return 0
    pos = log.stat().st_size
    try:
        while True:
            sleep(1)
            size = log.stat().st_size
            if size > pos:
                with open(log, "rb") as f:
                    f.seek(pos)
                    print(f.read().decode("utf-8", errors="replace"), end="", flush=True)
                pos = size
    except KeyboardInterrupt:
        return 0


def _stop(book, ep, name):
    root = paths.ep_root(book, ep)
    if name == "images":
        stop = root / "flow" / "STOP"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text(f"stop requested {jobs.stamp()} by novel jobs --stop images\n", encoding="utf-8")
        st = jobs.read_state(root, "images")
        if not st or st.get("state") != "running":
            print("note: no images job is running here; the file only stops a run that is started "
                  "by hand (a new `novel run` removes it)")
        print(f"Stop file written: {stop}\n"
              "The image run will stop after the current picture; nothing is killed. "
              "Run `novel images` again later to continue.")
        return 0
    ok, msg = jobs.stop_process(root, name)
    print(msg)
    if ok and name == "tts":
        print("The Kaggle run itself keeps going. Use `novel tts watch` or `novel tts pull` later.")
    return 0 if ok else 1


def main(argv) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel jobs", usage=USAGE)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--tail", metavar="NAME")
    ap.add_argument("-n", type=int, default=40, dest="lines")
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--stop", metavar="NAME")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)

    book, ep = context.resolve(a.book, a.ep)
    if ep is None and not a.all:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    if a.all:
        print(f"[{book}]  all episodes")
        return _show(book, ep, True)
    print(context.header(book, ep))
    if a.stop:
        return _stop(book, ep, a.stop)
    if a.tail:
        return _tail(paths.ep_root(book, ep), a.tail, a.lines, a.follow)
    return _show(book, ep, False)


def job_main(argv) -> int:
    """Hidden `novel _job <name> --book B --ep N [args...]`: the process that `novel run` detaches."""
    if not argv:
        print("usage: novel _job <name> --book B --ep N [args...] (internal)", file=sys.stderr)
        return 2
    name, rest = argv[0], argv[1:]
    ap = argparse.ArgumentParser(prog="novel _job", add_help=False)
    ap.add_argument("--book", required=True)
    ap.add_argument("--ep", type=int, required=True)
    a, extra = ap.parse_known_args(rest)
    return jobs.run_job(name, a.book, a.ep, extra)
