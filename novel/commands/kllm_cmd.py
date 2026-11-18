"""novel kllm up | status | url | down"""

from __future__ import annotations

import argparse

USAGE = ("usage: novel kllm up [--account NAME] [--idle-minutes 30] [--max-hours 11.5] [--context 131072]\n"
         "                     [--thinking on|off] [--pad-kb N] [--smoke] [--dry-run] [--no-wait]\n"
         "       novel kllm status\n"
         "       novel kllm url ADDRESS     (store the server's address by hand if it did not arrive by itself)\n"
         "       novel kllm down [--no-wait]\n"
         "A coding LLM (Qwen3.6-35B-A3B) on a Kaggle GPU T4 x2 notebook behind a Cloudflare tunnel; it stops itself\n"
         "after the idle time. `up` also writes the address and key into Pi's models.json: pi --model kaggle/qwen-local\n"
         "State: <novel home>/kllm/server.json (global, not per book). --smoke is a CPU test run with a tiny model.")

SUBCOMMANDS = ("up", "status", "url", "down")


def build_parser(sub: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"novel kllm {sub}")
    if sub == "up":
        ap.add_argument("--account", help="Kaggle profile (default: settings tts.kaggle_profile, then kaggle.account)")
        ap.add_argument("--idle-minutes", type=float, help="the kernel exits after this long without a request "
                                                           "(default 30)")
        ap.add_argument("--max-hours", type=float, help="the kernel exits after this long at the latest "
                                                        "(default 11.5)")
        ap.add_argument("--context", type=int, default=131072,
                        help="the largest context to try; it falls back to 65536, then 32768 if that does not load")
        ap.add_argument("--thinking", choices=("on", "off"), default="off",
                        help="let the model reason before answering (slower; default off)")
        ap.add_argument("--pad-kb", type=int, help="padding for streamed replies through the tunnel, in kB "
                                                   "(default 0 = none; see docs/kaggle-commands.md)")
        ap.add_argument("--smoke", action="store_true", help="test run: CPU kernel, tiny model, no Pi config")
        ap.add_argument("--dry-run", action="store_true", help="stage everything and list it; call nothing")
        ap.add_argument("--no-wait", action="store_true", help="push the kernel and return without waiting")
    if sub == "url":
        ap.add_argument("address", help="the server's address, e.g. https://xxxx.trycloudflare.com")
    if sub == "down":
        ap.add_argument("--no-wait", action="store_true", help="do not wait for the kernel to end")
    return ap


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub not in SUBCOMMANDS:
        print(f"novel kllm: unknown subcommand '{sub}' ({' | '.join(SUBCOMMANDS)})")
        return 2
    a = build_parser(sub).parse_args(rest)
    from novel.stages import kllm_run                      # imported only when it is needed
    try:
        return getattr(kllm_run, f"cmd_{sub}")(a)
    except kllm_run.KllmRunError as e:
        print(e)
        return 1
