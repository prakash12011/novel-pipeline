"""Switch between Kaggle accounts (and their Hugging Face tokens).

Two accounts are in play, each with its own Kaggle API token, its own HF
token, its own datasets and its own notebook - so switching is not just
swapping one file.

Token files are kept side by side and are NEVER moved or deleted:

    ~/.kaggle/access_token-prakash     ~/.kaggle/access_token-seema
    <project>/HF_TOKEN_PRAKASH         <project>/HF_TOKEN_SEEMA

`use` COPIES the chosen pair onto the active names (~/.kaggle/access_token
and <project>/HF_TOKEN), which is what the Kaggle CLI and the dataset build
actually read. The originals stay untouched, so switching back always works.

It also rewrites .kaggle-pull/kernel/kernel-metadata.json, because the kernel
id and both dataset_sources are namespaced by username. Pushing one account's
metadata while authenticated as the other fails with a permissions error that
does not obviously point at the real cause.

Usage:
    python -m abpipe.account status
    python -m abpipe.account list
    python -m abpipe.account use seema
    python -m abpipe.account check      # verify datasets exist on this account
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

KAGGLE_DIR = Path.home() / ".kaggle"
ACTIVE_TOKEN = KAGGLE_DIR / "access_token"
PROJECT = Path(__file__).resolve().parent.parent
ACTIVE_HF = PROJECT / "HF_TOKEN"


def profiles_file() -> Path:
    return Path(os.environ.get("NOVEL_HOME") or Path.home() / ".novel-pipeline") / "kaggle-active.json"


# only the active name is kept here; profiles come from settings.kaggle.profiles
KERNEL_META = PROJECT / ".kaggle-pull" / "kernel" / "kernel-metadata.json"

def load_profiles() -> dict:
    """Kaggle profiles from settings.kaggle.profiles; the active name from PROFILES."""
    from novel.core import settings
    cfg = settings.get("kaggle.profiles", default=None) or {}
    profiles = {name: {"kaggle_username": (meta or {}).get("username"),
                       "kernel_slug": (meta or {}).get("kernel_slug", "hindi-audiobook-daily")}
                for name, meta in cfg.items()}
    active = None
    pf = profiles_file()
    if pf.exists():
        active = json.loads(pf.read_text(encoding="utf-8")).get("active")
    return {"active": active, "profiles": profiles}


def save_profiles(p: dict) -> None:
    pf = profiles_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"active": p.get("active")}, indent=2), encoding="utf-8")


def token_paths(name: str) -> tuple[Path, Path]:
    """Where this profile's two secrets live. Neither is ever written to."""
    return (KAGGLE_DIR / ("access_token-" + name),
            PROJECT / ("HF_TOKEN_" + name.upper()))


def whoami():
    """Ask the Kaggle CLI who the currently active token belongs to.

    On failure the kaggle package prints its entire multi-paragraph setup
    help to stdout before raising, which buries this tool's own output. We
    only want the username or None, so both streams are swallowed.

    Retried three times: this API fails intermittently on transient network
    problems (DNS, aborted connections - both seen repeatedly here). A single
    failure previously made a switch report a bad token and skip rewriting the
    metadata, when the token was in fact perfectly valid.
    """
    import contextlib
    import io
    import time
    for attempt in range(3):
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                from kaggle.api.kaggle_api_extended import KaggleApi
                api = KaggleApi()
                api.authenticate()
                name = api.config_values.get("username")
            if name:
                return name
        except Exception:
            pass
        if attempt < 2:
            time.sleep(3)
    return None


# ------------------------------------------------- episode flow (token only)
# Everything below is what `novel tts` needs. It never touches an HF token file or
# kernel-metadata.json: the notebook reads the HF token from the Kaggle dataset
# <username>/hf-token.


def resolve_profile(cli_name=None) -> str:
    """--account, else settings tts.kaggle_profile, else settings kaggle.account."""
    from novel.core import settings
    name = (cli_name or settings.get("tts.kaggle_profile", default=None)
            or settings.get("kaggle.account", default=None))
    if not name:
        sys.exit("no Kaggle account chosen: pass --account NAME, or set tts.kaggle_profile "
                 "(or kaggle.account) in settings.yaml")
    return str(name)


def owner(profile: str) -> str:
    """The Kaggle username of a profile (settings kaggle.profiles.<name>.username)."""
    profiles = load_profiles()["profiles"]
    if profile not in profiles:
        sys.exit(f"unknown Kaggle profile '{profile}'. known: {', '.join(profiles) or '(none)'}; "
                 f"add it under kaggle.profiles in settings.yaml")
    user = profiles[profile].get("kaggle_username")
    if not user:
        sys.exit(f"kaggle.profiles.{profile}.username is not set in settings.yaml")
    return user


def activate_token(profile: str) -> Path:
    """Make `profile` the active Kaggle login: COPY <kaggle home>/access_token-<profile>
    onto <kaggle home>/access_token (what the CLI reads). The original stays; the
    previous active token is kept as access_token.previous. Nothing else is touched.
    <kaggle home> is $NOVEL_KAGGLE_HOME, else ~/.kaggle."""
    from .kbatch import kaggle_home
    home = kaggle_home()
    src, dst = home / f"access_token-{profile}", home / "access_token"
    if not src.exists():
        sys.exit(f"Kaggle token for '{profile}' not found: expected {src}. Save the account's "
                 f"API token (Kaggle > Settings > API) there.")
    head = src.read_text(encoding="utf-8", errors="replace").strip()[:4]
    if head != "KGAT":
        sys.exit(f"{src} does not look like a Kaggle token (expected KGAT...). Nothing changed.")
    if not dst.exists() or dst.read_bytes() != src.read_bytes():
        if dst.exists():
            shutil.copy2(dst, home / "access_token.previous")
        shutil.copy2(src, dst)
    p = load_profiles()
    p["active"] = profile
    save_profiles(p)
    return dst


def cmd_list(prof: dict) -> None:
    print("profiles:")
    for name, meta in prof["profiles"].items():
        kt, hf = token_paths(name)
        mark = "*" if name == prof.get("active") else " "
        user = meta.get("kaggle_username") or "(unknown)"
        print("  %s %-9s kaggle=%-22s token=%-8s hf=%s" % (
            mark, name, user,
            "ok" if kt.exists() else "MISSING",
            "ok" if hf.exists() else "MISSING"))
    print("")
    print("* = active")


def cmd_status(prof: dict) -> None:
    print("recorded active :", prof.get("active") or "(none)")
    print("access_token    :", "present" if ACTIVE_TOKEN.exists() else "MISSING")
    print("HF_TOKEN        :", "present" if ACTIVE_HF.exists() else "MISSING")
    who = whoami()
    print("authenticates as:", who or "(authentication failed)")
    if who and prof.get("active"):
        want = prof["profiles"][prof["active"]].get("kaggle_username")
        if want and want != who:
            print("  WARNING: profile says %s but the token authenticates "
                  "as %s" % (want, who))
    if KERNEL_META.exists():
        m = json.loads(KERNEL_META.read_text(encoding="utf-8"))
        print("kernel id       :", m.get("id"))
        print("dataset sources :", ", ".join(m.get("dataset_sources", [])))


def cmd_use(prof: dict, name: str) -> int:
    if name not in prof["profiles"]:
        print("unknown profile '%s'. known: %s"
              % (name, ", ".join(prof["profiles"])))
        return 2
    kt, hf = token_paths(name)
    missing = [str(p) for p in (kt, hf) if not p.exists()]
    if missing:
        print("cannot switch, these files are missing:")
        for m in missing:
            print("   ", m)
        return 2

    # Validate the token TYPE before copying. The two token kinds are both 37
    # characters, so a paste into the wrong file is invisible by size and gets
    # propagated silently - it surfaces hours later inside a Kaggle kernel as
    # "Invalid user token", which points nowhere near the real cause.
    # Prefixes only; values are never read aloud or logged.
    bad = []
    kt_head = kt.read_text(encoding="utf-8", errors="replace").strip()[:5]
    hf_head = hf.read_text(encoding="utf-8", errors="replace").strip()[:3]
    if not kt_head.startswith("KGAT"):
        bad.append("%s does not look like a Kaggle token (expected KGAT...)" % kt.name)
    if hf_head != "hf_":
        bad.append("%s does not look like a Hugging Face token (expected hf_...)" % hf.name)
    if bad:
        print("cannot switch, token files look wrong:")
        for b in bad:
            print("   ", b)
        print("  Nothing was changed. Fix the file contents and retry.")
        return 2

    # Keep a copy of whatever is active before overwriting, so a mistake here
    # is never destructive.
    if ACTIVE_TOKEN.exists():
        shutil.copy2(ACTIVE_TOKEN, KAGGLE_DIR / "access_token.previous")
    if ACTIVE_HF.exists():
        shutil.copy2(ACTIVE_HF, PROJECT / "HF_TOKEN.previous")

    shutil.copy2(kt, ACTIVE_TOKEN)      # copy, never move
    shutil.copy2(hf, ACTIVE_HF)
    print("activated '%s' (originals left in place)" % name)

    who = whoami()
    if not who:
        print("  WARNING: the new token did not authenticate. Check it is valid.")
        return 1
    print("  authenticates as:", who)

    meta = prof["profiles"][name]
    if not meta.get("kaggle_username"):
        meta["kaggle_username"] = who
        print("  learned kaggle username for '%s': %s" % (name, who))
    elif meta["kaggle_username"] != who:
        print("  NOTE: profile recorded %s but token says %s; using %s"
              % (meta["kaggle_username"], who, who))
        meta["kaggle_username"] = who

    _rewrite_kernel_metadata(who, meta.get("kernel_slug"))
    prof["active"] = name
    save_profiles(prof)
    return 0


def _rewrite_kernel_metadata(username: str, slug) -> None:
    """Re-namespace the kernel and its datasets to the active account."""
    if not KERNEL_META.exists():
        print("  (no kernel-metadata.json yet - skipped)")
        return
    m = json.loads(KERNEL_META.read_text(encoding="utf-8"))
    slug = slug or str(m.get("id", "/")).split("/")[-1]
    m["id"] = "%s/%s" % (username, slug)
    m["title"] = slug
    m["code_file"] = slug + ".ipynb"
    m["dataset_sources"] = ["%s/audiobook-data" % username,
                            "%s/hf-token" % username]
    # Pin the GPU. Left unset, Kaggle assigns whatever is free - and a P100
    # (Pascal, compute capability 6.0) CANNOT run this pipeline: bitsandbytes
    # 4-bit quantization, which config.yaml uses for the translation model,
    # needs compute capability >= 7.5 and dies with "named symbol not found
    # at ops.cu" partway through a run. That cost two wasted runs on
    # 2026-09-02. The T4 is 7.5 and is the only shape verified end to end.
    m["machine_shape"] = "NvidiaTeslaT4"
    m["enable_gpu"] = True
    m["enable_internet"] = True
    m["is_private"] = True      # licensed novel text must never go public
    KERNEL_META.write_text(json.dumps(m, indent=2), encoding="utf-8")
    print("  kernel id       :", m["id"])
    print("  dataset sources :", ", ".join(m["dataset_sources"]))

    # The staged notebook filename must match code_file or `kernels push`
    # rejects the directory.
    src = PROJECT / "kaggle_daily.ipynb"
    dst = KERNEL_META.parent / m["code_file"]
    if src.exists():
        shutil.copy2(src, dst)
        print("  notebook staged :", dst.name)


def cmd_check(prof: dict) -> int:
    """Confirm the active account actually has what a run needs."""
    who = whoami()
    if not who:
        print("not authenticated")
        return 1
    print("checking account:", who)
    ok = True
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for slug in ("audiobook-data", "hf-token"):
        r = subprocess.run(
            [sys.executable, "-m", "kaggle", "datasets", "files",
             "%s/%s" % (who, slug)],
            capture_output=True, text=True, env=env)
        good = r.returncode == 0 and "404" not in (r.stdout + r.stderr)
        ok = ok and good
        print("  dataset %-16s %s" % (
            slug, "OK" if good else "MISSING - must be uploaded"))
    if not ok:
        print("")
        print("A fresh account needs both datasets uploaded before a run will")
        print("work. The chapter text and HF token are local already; only the")
        print("upload step is missing.")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Switch Kaggle accounts safely.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    sub.add_parser("list")
    sub.add_parser("check")
    u = sub.add_parser("use")
    u.add_argument("name")
    args = ap.parse_args()

    prof = load_profiles()
    if args.cmd == "use":
        sys.exit(cmd_use(prof, args.name))
    elif args.cmd == "check":
        sys.exit(cmd_check(prof))
    elif args.cmd == "list":
        cmd_list(prof)
    else:
        cmd_status(prof)


if __name__ == "__main__":
    main()
