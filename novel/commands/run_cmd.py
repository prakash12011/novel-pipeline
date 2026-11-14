"""novel run [--book B] [--ep N] [--no-tts] [--no-captions] [--no-images] [--images-in-background] [--chapters A-B]

Starts TTS and captions as background jobs that keep running when this terminal closes, then runs the image step
in the FOREGROUND of this terminal (`novel images`, called in-process) so you watch it and can step in when Flow
asks for a CAPTCHA. --images-in-background restores the hidden background job (no one is at the keyboard then, so
a Flow stop ends the run and the stop gate applies next time)."""

from __future__ import annotations

import argparse

from novel.core import context, jobs

USAGE = ("usage: novel run [--book B] [--ep N] [--no-tts] [--no-captions] [--no-images] "
         "[--images-in-background] [--chapters A-B]")


def images_main(argv, **kw):
    """The foreground image step: the `novel images` handler, in this process (tests replace this)."""
    from novel.commands import images_cmd
    return images_cmd.main(argv, **kw)


def main(argv) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    ap = argparse.ArgumentParser(prog="novel run", usage=USAGE)
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    for n in jobs.NAMES:
        ap.add_argument(f"--no-{n}", action="store_true", help=f"do not start the {n} job")
    ap.add_argument("--images-in-background", action="store_true",
                    help="run images as a hidden background job (old behaviour)")
    ap.add_argument("--chapters", help="e.g. 3 or 2-8, passed to the jobs that accept it")
    a = ap.parse_args(argv)

    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        raise SystemExit("no current episode. Pass --ep N or run: novel use <book> --ep N")
    print(context.header(book, ep))

    failed = 0
    foreground = not a.images_in_background and not a.no_images
    for name in jobs.NAMES:
        if name == "images" and foreground:
            continue
        if getattr(a, f"no_{name}"):
            print(f"{name:<9} skipped   (--no-{name})")
            continue
        args = ["--chapters", a.chapters] if a.chapters and name in jobs.CHAPTER_JOBS else []
        try:
            r = jobs.start(book, ep, name, args)
        except Exception as e:                    # one job that cannot start must not stop the others
            r = jobs.StartResult("failed", reason=f"{type(e).__name__}: {e}")
        if r.status == "started":
            print(f"{name:<9} started   pid {r.pid}   log {r.log}")
        elif r.status == "running":
            print(f"{name:<9} already running   pid {r.pid}   log {r.log}")
        else:
            failed += 1
            print(f"{name:<9} FAILED to start: {r.reason}")
    print("Check progress:  novel jobs    Stop images cleanly:  novel jobs --stop images")
    if not foreground:
        return 1 if failed else 0
    started = [n for n in jobs.NAMES if n != "images" and not getattr(a, f"no_{n}")]
    if started:
        print("")
        print(f"{' and '.join(started)} {'is' if len(started) == 1 else 'are'} running in the background "
              "(`novel jobs` to check them).")
    print("Pictures are now being generated in THIS window: keep it, and the Flow Chrome window, visible. "
          "Ctrl+C once stops after the current picture.")
    print("")
    args = ["--book", book, "--ep", str(ep)] + (["--chapters", a.chapters] if a.chapters else [])
    code = images_main(args)
    return code if code else (1 if failed else 0)
