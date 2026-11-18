"""novel ktrial scenes | prompts | up | url | status | down | run | report"""

from __future__ import annotations

import argparse

from novel import kcomfy
from novel.core import context

USAGE = ("usage: novel ktrial scenes --chapter N [--book B] [--ep N] [--max-characters 2] [--from-production]\n"
         "                          [--force]\n"
         "       novel ktrial prompts --chapter N [--book B] [--ep N]\n"
         "       novel ktrial up --chapter N [--account NAME] [--tunnel ngrok|cloudflared] [--domain HOST]\n"
         "                       [--idle-minutes 20] [--max-hours 6] [--gpu 1] [--comfy-arg=--ARG ...] [--dry-run]\n"
         "                       [--model flux|minimax-h3|qwen-image-turbo] [--ngrok-dataset SLUG]\n"
         "                       [--no-wait]\n"
         "       novel ktrial url --chapter N ADDRESS     (store the server's address by hand: no fixed domain)\n"
         "       novel ktrial status|down|report --chapter N\n"
         "       novel ktrial run --chapter N [--only s05,s07,g01] [--limit N] [--reroll] [--redo] [--plates-only]\n"
         "                        [--via kaggle|openrouter] [--resolution 1K|2K] [--budget D]\n"
         "       novel ktrial charsheet --chapter N [--keys a,b] [--missing]   (Qwen character sheets -> bible/qwen)\n"
         "       novel ktrial front --chapter N [--keys a,b] [--redo]   (front views -> bible/qwen/front)\n"
         "       novel ktrial qa --chapter N [--status]   (picture QA by hand / the status list of each picture)\n"
         "       novel ktrial qa-probe --chapter N [--picture FILE]   (which QA models see pictures and answer)\n"
         "Everything else goes to <book>/_work/ktrial/c<chapter>; nothing in episodes/ is written.")

SUBCOMMANDS = ("scenes", "prompts", "up", "url", "status", "down", "run", "report", "charsheet", "front", "qa",
               "qa-probe")


def build_parser(sub: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"novel ktrial {sub}")
    ap.add_argument("--book", help="default: the current book (novel use)")
    ap.add_argument("--chapter", type=int, required=True, help="the chapter to try, e.g. 24")
    ap.add_argument("--ep", type=int, help="the episode (default: the one whose range holds the chapter)")
    if sub == "scenes":
        ap.add_argument("--max-characters", type=int, default=2, help="most people in one picture (default 2)")
        ap.add_argument("--from-production", action="store_true",
                        help="no Gemini call: copy the production scene file into the trial folder")
        ap.add_argument("--force", action="store_true", help="replace an existing trial scene file")
        ap.add_argument("--model", help="Gemini model for this run only (default: settings gemini.api_model)")
        ap.add_argument("--writer", choices=("gemini", "opus"), default="gemini",
                        help="who writes the lines: gemini (default) or opus = Claude Opus 5.5 through OpenRouter "
                             "(OPENROUTER_API_KEY, costs money; told the pictures are for FLUX.3 klein)")
    if sub == "up":
        ap.add_argument("--account", help="Kaggle profile (default: settings tts.kaggle_profile, then kaggle.account)")
        ap.add_argument("--tunnel", choices=("ngrok", "cloudflared"), default="ngrok")
        ap.add_argument("--domain", metavar="HOST",
                        help="the account's fixed ngrok dev domain (listed at dashboard.ngrok.com/domains); remembered "
                             "in <kaggle home>/ngrok-domain, or set NOVEL_NGROK_DOMAIN. Only with --tunnel ngrok")
        ap.add_argument("--idle-minutes", type=int, default=20, help="the kernel exits after this long without work")
        ap.add_argument("--max-hours", type=float, default=6.0, help="the kernel exits after this long at the latest")
        ap.add_argument("--gpu", type=int, default=1, help="the GPU ComfyUI uses (TTS uses the other one)")
        ap.add_argument("--comfy-arg", action="append", default=[],
                        help="an extra ComfyUI argument, written with = (--comfy-arg=--fp16-unet); repeatable")
        ap.add_argument("--model", choices=kcomfy.MODELS, default=kcomfy.FLUX,
                        help="flux: FLUX.3 klein (default); minimax-h3: MiniMax-H3-Image GGUF on both cards (--gpu is "
                             "ignored); qwen-image-turbo: Qwen-Image-2.1-Turbo INT8 on one card; `run` then writes to "
                             "<trial>/minimax-h3/ or <trial>/qwen-image-turbo/")
        ap.add_argument("--ngrok-dataset", metavar="SLUG",
                        help="the private Kaggle dataset (of --account) that holds the ngrok token, file "
                             "NGROK_AUTHTOKEN; default ngrok-token. For a second ngrok account: give its own --domain "
                             "(not remembered, the stored one is left alone); without one the address is not fixed")
        ap.add_argument("--dry-run", action="store_true", help="stage everything and list it; call nothing")
        ap.add_argument("--no-wait", action="store_true", help="push the kernel and return without waiting for the server")
    if sub == "charsheet":
        ap.add_argument("--keys", help="comma list of bible keys to make a Qwen character sheet for")
        ap.add_argument("--missing", action="store_true",
                        help="also every referenced person of the chapter that has no sheet yet")
    if sub == "front":
        ap.add_argument("--keys", help="comma list of bible keys (default: everyone with a Qwen sheet)")
        ap.add_argument("--redo", action="store_true", help="draw them again (the old pictures go to old/)")
    if sub == "qa":
        ap.add_argument("--status", action="store_true",
                        help="only print the status list: each picture's state, tries and what QA found")
    if sub == "qa-probe":
        ap.add_argument("--picture", help="the picture to send (default: the first one of the chapter)")
    if sub == "url":
        ap.add_argument("address", help="the server's address, e.g. https://xxxx.ngrok-free.app")
    if sub == "run":
        ap.add_argument("--only", help="comma list of pictures (s05) and/or place plates (g01)")
        ap.add_argument("--limit", type=int, help="make at most this many pictures")
        ap.add_argument("--reroll", action="store_true", help="make the chosen pictures again with the next seed")
        ap.add_argument("--redo", action="store_true", help="make the chosen pictures again with the same seed")
        ap.add_argument("--plates-only", action="store_true", help="only the place plates")
        ap.add_argument("--via", choices=("kaggle", "openrouter"), default="kaggle",
                        help="kaggle: the ComfyUI server (default); openrouter: the hosted API, pictures go to "
                             "<trial>/openrouter/ (needs OPENROUTER_API_KEY, costs money)")
        ap.add_argument("--resolution", choices=("1K", "2K"), default="1K",
                        help="with --via openrouter: the picture size tier (default 1K)")
        ap.add_argument("--budget", type=float, default=1.50,
                        help="with --via openrouter: stop before the money spent in this run would pass this many "
                             "dollars (default 1.50)")
    return ap


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub not in SUBCOMMANDS:
        print(f"novel ktrial: unknown subcommand '{sub}' ({' | '.join(SUBCOMMANDS)})")
        return 2
    a = build_parser(sub).parse_args(rest)

    book, _ = context.resolve(a.book, a.ep)
    a.book = book
    context.activate(book)
    print(context.header(book))

    from novel.stages import ktrial
    if sub == "scenes":
        return ktrial.cmd_scenes(a)
    if sub == "prompts":
        return ktrial.cmd_prompts(a)
    from novel.stages import ktrial_run                    # the GPU half: imported only when it is needed
    trial = ktrial.trial_dir(book, a.chapter)
    if sub == "front":
        from novel.stages import kfront
        return kfront.cmd_front(a, trial)
    if sub == "qa-probe":
        return _qa_probe(a, trial)
    return getattr(ktrial_run, f"cmd_{sub}")(a, trial)


def _qa_probe(a, trial) -> int:
    from pathlib import Path
    from novel import kqa
    from novel.core import settings
    key = kqa.find_key()
    if not key:
        print(f"no {kqa.KEY_VAR}: add a line {kqa.KEY_VAR}=... to secrets.env")
        return 1
    pics = sorted((trial / kcomfy.QW_DIR / "images").glob("s*.jpg"))
    pic = Path(a.picture) if a.picture else (pics[0] if pics else None)
    if pic is None or not pic.exists():
        print("no picture to send: pass --picture FILE")
        return 1
    cfg = (settings.effective(a.book).get("images") or {}).get("qa") or {}
    print(f"asking every QA model about {pic}")
    return kqa.probe(cfg, key, pic)
