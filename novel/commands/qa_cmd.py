"""novel qa [--book B] [--ep N] [--chapters A-B] [--reviewer claude|gemini|none] [--only-code] [--parallel N]
           [--fix-prompt N] [--copy] [--with-warnings] [--force-review]

Code checks and an AI review of the imported chapters; writes <episode>/qa/report.md. With --fix-prompt N it
prints the message to paste into Gemini to correct chapter N. See novel/qa.py and novel/gemini/fix.py.
"""

from __future__ import annotations

import argparse
import sys


def main(argv) -> int:
    from novel import qa as Q
    from novel.core import context, settings
    from novel.gemini import prompt as P

    ap = argparse.ArgumentParser(prog="novel qa",
                                 description="Check the Hindi and the picture lists; AI second review; report.")
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--chapters", help="first-last, e.g. 21-22 (default: the episode's chapters with a scene file)")
    ap.add_argument("--reviewer", choices=("claude", "gemini", "none"),
                    help="who reviews (default: settings qa.reviewer); the other one is the fallback")
    ap.add_argument("--only-code", action="store_true", help="code checks only: no AI call")
    ap.add_argument("--parallel", type=int, metavar="N",
                    help="AI reviews at the same time (default: settings qa.parallel, 3)")
    ap.add_argument("--force-review", action="store_true", help="review again even if nothing changed")
    ap.add_argument("--fix-prompt", type=int, metavar="N", help="print the correction message for chapter N")
    ap.add_argument("--with-warnings", action="store_true", help="--fix-prompt: include warnings, not only errors")
    ap.add_argument("--copy", action="store_true", help="--fix-prompt: put the message on the clipboard")
    a = ap.parse_args(argv)

    book, ep = context.resolve(a.book, a.ep)
    context.activate(book)
    try:
        if a.fix_prompt is not None:
            return fix_prompt(book, ep, a)
        chapters = None
        if a.chapters:
            first, last = P.parse_chapters(a.chapters)
            chapters = list(range(first, last + 1))
        reviewer = a.reviewer or str(settings.get("qa.reviewer", book=book, default="claude"))
        fallback = str(settings.get("qa.fallback", book=book, default="gemini"))
        if reviewer not in ("claude", "gemini", "none"):
            print(f"novel qa: settings qa.reviewer is {reviewer!r}; use claude, gemini or none", file=sys.stderr)
            return 2
        if a.parallel is not None and a.parallel < 1:
            print("novel qa: --parallel must be 1 or more", file=sys.stderr)
            return 2
        code, results, path = Q.run_qa(book, ep, chapters, reviewer, fallback, a.only_code, a.force_review,
                                       parallel=a.parallel)
    except P.PromptError as e:
        print(f"novel qa: {e}", file=sys.stderr)
        return 1
    print("")
    print(Q.table_text(results))
    print("")
    for r in results:
        if r.overall == "FIX":
            print(f"Chapter {r.n}: FIX ({len(r.errors)} code error{'s' if len(r.errors) != 1 else ''}"
                  f"{', AI: ' + str(r.review_counts()[0]) + ' error(s)' if r.review else ''}). "
                  f"Fix: novel qa --fix-prompt {r.n}")
    if path:
        print(f"Report: {path}")
    for line in Q.next_commands(results):
        print(line)
    return code


def fix_prompt(book: str, ep, a) -> int:
    from novel import qa as Q
    from novel.commands import prompt_cmd
    from novel.gemini import fix as F

    n = a.fix_prompt
    run = Q.Run(book, ep)
    ep_root = run.ep_root_for(n)
    if ep_root is None:
        print(f"novel qa: chapter {n} belongs to no episode", file=sys.stderr)
        return 1
    try:
        fx = F.build(ep_root, n, with_warnings=a.with_warnings)
    except F.FixError as e:
        print(f"novel qa: {e}", file=sys.stderr)
        return 1
    for note in fx.notes:
        print(f"note: {note}", file=sys.stderr)
    print(f"Message for Gemini, chapter {n} ({fx.mode} mode"
          f"{', pictures ' + Q.C.ranges_text(fx.images) if fx.images else ''}):")
    print("-" * 72)
    print(fx.text, end="")
    print("-" * 72)
    if a.copy:
        if prompt_cmd.set_clipboard(fx.text):
            print("The message is on the clipboard (whatever was there before is replaced).")
        else:
            print("The clipboard is not available here. Copy the message above by hand.")
    print("")
    print("What happens next:")
    for k, step in enumerate(F.next_steps(n), 1):
        print(f"  {k}. {step}")
    return 0
