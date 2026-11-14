"""novel settings show | init"""

from __future__ import annotations

import argparse
import sys

import yaml

from novel.core import paths, settings


def _show(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel settings show")
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--ch", type=int)
    ap.add_argument("--key", default="", help="only keys starting with this prefix")
    a = ap.parse_args(argv)
    ex = settings.explain(a.book, a.ep, a.ch)
    rows = [(k, v) for k, v in sorted(ex.items()) if k.startswith(a.key)]
    if not rows:
        print(f"no settings match '{a.key}'", file=sys.stderr)
        return 1
    width = max(len(k) for k, _ in rows)
    for k, (value, layer) in rows:
        print(f"{k.ljust(width)} = {value!r}   [{layer}]")
    return 0


def _init(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel settings init")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--chrome-exe")
    ap.add_argument("--chrome-profile")
    ap.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    ap.add_argument("--chainner-exe")
    ap.add_argument("--chainner-model")
    ap.add_argument("--kaggle-account")
    ap.add_argument("--kaggle-username")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    f = paths.settings_file()
    if f.exists() and not a.force:
        print(f"{f} already exists. Use --force to overwrite it.", file=sys.stderr)
        return 1
    if bool(a.kaggle_account) != bool(a.kaggle_username):
        print("--kaggle-account and --kaggle-username go together", file=sys.stderr)
        return 2

    data: dict = {"data_dir": a.data_dir}
    chrome = {}
    if a.chrome_exe:
        chrome["exe"] = a.chrome_exe
    if a.chrome_profile:
        chrome["profile"] = a.chrome_profile
    chrome["cdp_url"] = a.cdp_url
    data["chrome"] = chrome
    chainner = {}
    if a.chainner_exe:
        chainner["exe"] = a.chainner_exe
    if a.chainner_model:
        chainner["model"] = a.chainner_model
    if chainner:
        data["chainner"] = chainner
    if a.kaggle_account:
        data["kaggle"] = {"account": a.kaggle_account,
                          "profiles": {a.kaggle_account: {"username": a.kaggle_username}}}

    from pathlib import Path
    Path(a.data_dir).expanduser().mkdir(parents=True, exist_ok=True)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"wrote {f}")
    return 0


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: novel settings show [--book B] [--ep N] [--ch N] [--key PREFIX]\n"
              "       novel settings init --data-dir PATH [--chrome-exe P] [--chrome-profile P]\n"
              "                           [--kaggle-account NAME --kaggle-username USER] [--force]")
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "show":
        return _show(rest)
    if sub == "init":
        return _init(rest)
    print(f"novel settings: unknown subcommand '{sub}' (show | init)", file=sys.stderr)
    return 2
