"""novel doctor [--online] [--fix-path] [--json]

Each check is a small function returning a Result. Checks never raise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from novel.core import paths, secrets, settings

OK, MISSING, WARN, SKIPPED = "ok", "MISSING", "warn", "skipped"

PACKAGES = [("PyYAML", "yaml"), ("Pillow", "PIL"), ("numpy", "numpy"),
            ("kaggle", "kaggle"), ("pytest", "pytest")]
SECRET_NAMES = ("HF_TOKEN*", "access_token*", "kaggle.json", ".env", "secrets.env")
DEFAULT_CHAINNER = "D:/chaiNNer/app/chaiNNer.exe"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    required: bool = True


def _res(name, ok, detail, fix, required, bad=None):
    """ok -> OK; otherwise MISSING (required) or warn (optional)."""
    if ok:
        return Result(name, OK, detail, "", required)
    return Result(name, MISSING if required else WARN, detail, fix, required)


# ---------- helpers (replaceable in tests) ----------

def which(cmd):
    return shutil.which(cmd)


def run(args, timeout=10):
    """(returncode, combined output); (None, message) when it cannot run."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def http_get(url, timeout=2.0, headers=None):
    """(status_code, body); (None, message) when unreachable."""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _setting(key, default=None):
    try:
        v = settings.get(key, default=None)
    except (Exception, SystemExit):
        return default
    return default if v in (None, "") else v


def _ver(text):
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


# ---------- checks ----------

def check_python(version=None):
    v = tuple(version or sys.version_info[:3])
    s = ".".join(str(x) for x in v)
    return _res("python", v >= (3, 11), f"Python {s}", "install Python 3.11 or newer", True)


def check_packages(importer=None):
    import importlib
    imp = importer or importlib.import_module
    missing = []
    import contextlib
    import io
    for pip_name, mod in PACKAGES:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                imp(mod)
        except Exception:  # noqa: BLE001
            missing.append(pip_name)
    req = paths.repo_root() / "requirements.txt"
    return _res("python packages", not missing,
                "all importable" if not missing else "missing: " + ", ".join(missing),
                f'py -m pip install -r "{req}"', True)


def check_node():
    exe = which("node")
    if not exe:
        return Result("node", MISSING, "not on PATH", "install Node.js 18 or newer (https://nodejs.org)", True)
    code, out = run([exe, "--version"])
    v = _ver(out) if code == 0 else None
    if not v:
        return Result("node", MISSING, "could not read version", "reinstall Node.js 18 or newer", True)
    return _res("node", v[0] >= 18, f"{out.strip()} at {exe}", "install Node.js 18 or newer (https://nodejs.org)", True)


def check_playwright():
    d = paths.repo_root() / "flow" / "node_modules" / "playwright-core"
    return _res("playwright-core", d.is_dir(), str(d) if d.is_dir() else "flow/node_modules/playwright-core not found",
                f'cd "{paths.repo_root() / "flow"}" && npm ci', True)


def check_ffmpeg():
    out = []
    ff = which("ffmpeg")
    if not ff:
        out.append(Result("ffmpeg", MISSING, "not on PATH", "install ffmpeg (full build with libass) and put it on PATH", True))
    else:
        code, text = run([ff, "-version"])
        first = text.splitlines()[0] if text else ff
        out.append(Result("ffmpeg", OK, first.strip()[:70], "", True))
    fp = which("ffprobe")
    if not fp:
        out.append(Result("ffprobe", MISSING, "not on PATH", "install ffmpeg (it ships ffprobe) and put it on PATH", True))
    else:
        out.append(Result("ffprobe", OK, fp, "", True))
    if ff:
        code, text = run([ff, "-hide_banner", "-filters"], timeout=15)
        has = code == 0 and re.search(r"^\s*\S+\s+ass\s", text, re.M) is not None
        out.append(_res("ffmpeg ass filter", has, "libass available" if has else "ffmpeg has no 'ass' filter",
                        "install a full ffmpeg build with libass (gyan.dev 'full' build)", True))
    else:
        out.append(Result("ffmpeg ass filter", MISSING, "ffmpeg missing", "install ffmpeg (full build with libass)", True))
    return out


def check_settings_file():
    f = paths.settings_file()
    fix = 'novel settings init --data-dir "<path>"'
    if not f.exists():
        return Result("settings file", MISSING, f"{f} not found", fix, True)
    try:
        import yaml
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        return Result("settings file", MISSING, f"cannot read {f}: {e}", fix + " --force", True)
    if not isinstance(data, dict) or not data.get("data_dir"):
        return Result("settings file", MISSING, f"data_dir not set in {f}", fix + " --force", True)
    return Result("settings file", OK, f"{f} (data_dir set)", "", True)


def check_data_dir():
    fix = 'novel settings init --data-dir "<path>" --force'
    try:
        import yaml
        data = yaml.safe_load(paths.settings_file().read_text(encoding="utf-8")) or {}
        d = data.get("data_dir")
    except Exception:  # noqa: BLE001
        d = None
    if not d:
        return Result("data folder", MISSING, "data_dir unknown", fix, True)
    p = Path(str(d)).expanduser()
    if not p.is_dir():
        return Result("data folder", MISSING, f"{p} does not exist", f'create the folder "{p}" or: {fix}', True)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".novel-doctor-", dir=str(p))
        os.close(fd)
        os.remove(tmp)
    except Exception as e:  # noqa: BLE001
        return Result("data folder", MISSING, f"{p} is not writable ({e})", f'fix the folder permissions on "{p}"', True)
    return Result("data folder", OK, f"{p} (writable)", "", True)


def check_chrome(http=None):
    out = []
    exe = _setting("chrome.exe")
    fix = 'novel settings init --data-dir "<path>" --chrome-exe "<path>" --force'
    out.append(_res("chrome exe", bool(exe) and Path(str(exe)).is_file(),
                    str(exe) if exe else "settings.chrome.exe not set", fix, True))
    prof = _setting("chrome.profile")
    out.append(_res("chrome profile", bool(prof) and Path(str(prof)).is_dir(),
                    str(prof) if prof else "settings.chrome.profile not set",
                    'novel settings init --data-dir "<path>" --chrome-profile "<path>" --force', True))
    cdp = str(_setting("chrome.cdp_url", "http://127.0.0.1:9222")).rstrip("/")
    code, body = (http or http_get)(cdp + "/json/version", 2.0)
    if code == 200:
        out.append(Result("chrome devtools", OK, f"{cdp} answers", "", False))
    else:
        out.append(Result("chrome devtools", SKIPPED, "Chrome not running; start it with `novel chrome`", "", False))
    return out


def kaggle_account():
    acct = _setting("kaggle.account")
    user = None
    if acct:
        user = _setting(f"kaggle.profiles.{acct}.username")
    return acct, user


def kaggle_token_candidates(acct):
    d = Path.home() / ".kaggle"
    c = []
    if acct:
        c.append(d / f"access_token-{acct}")
    c.append(d / "access_token")
    return c


def kaggle_cmd():
    """The command that runs the Kaggle CLI: `kaggle` on PATH, else `python -m kaggle`."""
    exe = which("kaggle")
    if exe:
        return [exe], f"{exe}"
    return [sys.executable, "-m", "kaggle"], "via python -m kaggle (not on PATH)"


def check_kaggle_cli():
    fix = 'py -m pip install -r "%s"' % (paths.repo_root() / "requirements.txt")
    cmd, how = kaggle_cmd()
    code, out = run(cmd + ["--version"], 30)
    if code != 0:
        return Result("kaggle cli", MISSING, "kaggle --version failed", fix, True)
    ver = next((l.strip() for l in out.splitlines() if l.strip().lower().startswith("kaggle")), "kaggle")
    return Result("kaggle cli", OK, f"{ver[:40]} ({how})", "", True)


def check_kaggle_token():
    acct, _ = kaggle_account()
    cands = kaggle_token_candidates(acct)
    for c in cands:
        if c.is_file():
            return Result("kaggle token", OK, f"{c} exists", "", True)
    want = cands[0]
    how = (f"1. Open https://www.kaggle.com/settings in your browser (signed in as the narration account), "
           f"find the API section and press Create New Token. Kaggle shows the token once: copy it. "
           f"2. Save it as a plain text file holding only the token, at {want} "
           f"(the folder {want.parent} may need to be made). The file name must be exactly "
           f"{want.name}. Run `novel doctor` again; nothing else is needed.")
    if not acct:
        how += (" (No Kaggle account is named in your settings yet: put `kaggle: {account: <name>}` in "
                f"{paths.settings_file()}; the token file then ends in that name.)")
    return Result("kaggle token", MISSING, f"{want} not found", how, True)


def check_kaggle_online(runner=None):
    runner = runner or run
    cmd, _ = kaggle_cmd()
    code, out = runner(cmd + ["datasets", "list", "--mine", "--csv"], 60)
    if code != 0:
        return [Result("kaggle online", WARN, "datasets list --mine failed",
                       "check the token (kaggle auth) and the network", False)]
    res = [Result("kaggle online", OK, "datasets list --mine works", "", False)]
    _, user = kaggle_account()
    if not user:
        res.append(Result("kaggle hf-token dataset", SKIPPED, "kaggle username not in settings", "", False))
        return res
    found = re.search(rf"(^|[\s,/]){re.escape(user)}/hf-token(\s|,|$)", out, re.M) is not None
    res.append(_res("kaggle hf-token dataset", found,
                    f"{user}/hf-token exists" if found else f"{user}/hf-token not found",
                    f"create the private dataset {user}/hf-token holding your Hugging Face token", False))
    return res


def check_gemini_keys():
    n = len(secrets.gemini_keys())
    return _res("gemini keys", n > 0, f"{n} key(s)" if n else "no GEMINI_API_KEY in secrets.env",
                f"add GEMINI_API_KEY=... to {paths.secrets_file()} (see secrets.env.example)", True)


def check_gemini_online(http=None):
    keys = secrets.gemini_keys()
    if not keys:
        return [Result("gemini online", SKIPPED, "no keys", "", False)]
    out = []
    for i, k in enumerate(keys, 1):
        code, _ = (http or http_get)("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                                     8.0, {"x-goog-api-key": k})
        if code == 200:
            out.append(Result(f"gemini key {i}", OK, "accepted", "", False))
        elif code in (400, 401, 403):
            out.append(Result(f"gemini key {i}", WARN, f"rejected (HTTP {code})", f"replace key {i} in {paths.secrets_file()}", False))
        elif code == 429:
            out.append(Result(f"gemini key {i}", OK, "accepted (rate limited right now)", "", False))
        else:
            out.append(Result(f"gemini key {i}", WARN, "no answer (network?)", "check the network and retry", False))
    return out


def check_claude():
    exe = which("claude")
    if not exe:
        return Result("claude cli", WARN, "not on PATH; `novel qa` and `novel meta` fall back to Gemini",
                      "optional: install Claude Code (https://claude.com/claude-code)", False)
    code, out = run([exe, "--version"], 20)
    return Result("claude cli", OK if code == 0 else WARN, (out.strip().splitlines() or [exe])[0][:60],
                  "" if code == 0 else "reinstall Claude Code", False)


def check_youtube_key():
    ok = bool(secrets.load().get("YOUTUBE_API_KEY"))
    return Result("youtube api key", OK if ok else WARN, "present" if ok else "YOUTUBE_API_KEY not set (`novel research` needs it)",
                  "" if ok else f"add YOUTUBE_API_KEY=... to {paths.secrets_file()}", False)


def check_chainner():
    exe = os.environ.get("CHAINNER") or _setting("chainner.exe", DEFAULT_CHAINNER)
    ok = Path(str(exe)).is_file()
    return Result("chainner", OK if ok else WARN, str(exe) if ok else f"{exe} not found; HD upscaling is skipped",
                  "" if ok else "optional: install chaiNNer and set it with `novel settings init --chainner-exe <path> --force`", False)


def check_fonts():
    try:
        import yaml
        d = yaml.safe_load(settings.defaults_file().read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        return Result("fonts", MISSING, f"cannot read defaults.yaml: {e}", "restore config/defaults.yaml", True)
    names = sorted({v for v in settings.flatten(d).values()
                    if isinstance(v, str) and v.replace("\\", "/").startswith("assets/fonts/")})
    missing = [n for n in names if not settings.asset_path(n).is_file()]
    return _res("fonts", not missing, f"{len(names)} font file(s) present" if not missing else "missing: " + ", ".join(missing),
                "restore the files under <repo>/assets/fonts/ (git checkout assets/fonts)", True)


def check_stray_secrets(extra_dirs=None):
    import fnmatch
    dirs = [paths.repo_root()]
    try:
        dirs.append(paths.data_dir())
    except (Exception, SystemExit):
        pass
    if extra_dirs:
        dirs.extend(extra_dirs)
    found = []
    skip = {"node_modules", ".git", "__pycache__", ".venv", "venv"}
    for i, d in enumerate(dirs):
        try:
            if i == 0 or extra_dirs and d in extra_dirs:
                it = []
                for root, subs, files in os.walk(d):
                    subs[:] = [s for s in subs if s not in skip]
                    it.extend(Path(root) / f for f in files)
            else:
                it = [p for p in d.iterdir() if p.is_file()]
        except OSError:
            continue
        for p in it:
            if any(fnmatch.fnmatch(p.name.lower(), pat.lower()) for pat in SECRET_NAMES):
                if p.name.lower().endswith(".example"):
                    continue
                found.append(p)
    if not found:
        return Result("stray secret files", OK, "none in the repo or data folder", "", True)
    return Result("stray secret files", MISSING, "found: " + "; ".join(str(p) for p in found),
                  f"move them to {paths.home()} or delete them", True)


# ---------- PATH ----------

def user_path_get():
    if sys.platform != "win32":
        return os.environ.get("PATH", "")
    code, out = run(["reg", "query", r"HKCU\Environment", "/v", "Path"], 10)
    if code != 0:
        return ""
    m = re.search(r"Path\s+REG_(?:EXPAND_)?SZ\s+(.*)", out)
    return m.group(1).strip() if m else ""


def user_path_set(value):
    """Write the USER PATH (never setx)."""
    cmd = ["powershell", "-NoProfile", "-Command",
           "[Environment]::SetEnvironmentVariable('Path', $env:NOVEL_NEW_PATH, 'User')"]
    env = dict(os.environ, NOVEL_NEW_PATH=value)
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    return p.returncode == 0


def _norm(p):
    return os.path.normcase(os.path.normpath(p.strip().strip('"'))).rstrip("\\/")


def path_has(path_value, folder):
    want = _norm(str(folder))
    return any(_norm(x) == want for x in path_value.split(os.pathsep) if x.strip())


def check_bin_on_path(user_path=None, process_path=None):
    folder = paths.repo_root() / "bin"
    up = user_path_get() if user_path is None else user_path
    pp = os.environ.get("PATH", "") if process_path is None else process_path
    if path_has(up, folder) or path_has(pp, folder):
        return Result("bin on PATH", OK, str(folder), "", False)
    return Result("bin on PATH", WARN, f"{folder} is not on PATH (you cannot type `novel` anywhere)",
                  "run `novel doctor --fix-path`, or add the folder to your user PATH", False)


def fix_path(getter=None, writer=None, ask=None, out=print):
    """Offer to append <repo>/bin to the USER PATH. Returns True when written."""
    folder = str(paths.repo_root() / "bin")
    cur = (getter or user_path_get)()
    if path_has(cur, folder):
        out(f"{folder} is already on the user PATH.")
        return False
    new = (cur.rstrip(os.pathsep) + os.pathsep if cur else "") + folder
    out("This will append one folder to your USER PATH (nothing else changes):")
    out(f"  add: {folder}")
    answer = (ask or input)("Add it? [y/N] ")
    if answer.strip().lower() != "y":
        out("Not changed.")
        return False
    ok = (writer or user_path_set)(new)
    out("User PATH updated. Open a new terminal to use it." if ok else "Could not update the user PATH.")
    return bool(ok)


# ---------- run ----------

def collect(online=False):
    results = [check_python(), check_packages(), check_node(), check_playwright()]
    results += check_ffmpeg()
    results += [check_settings_file(), check_data_dir()]
    results += check_chrome()
    results += [check_kaggle_cli(), check_kaggle_token()]
    if online:
        results += check_kaggle_online()
    results.append(check_gemini_keys())
    if online:
        results += check_gemini_online()
    results += [check_claude(), check_youtube_key(), check_chainner(), check_fonts(),
                check_stray_secrets(), check_bin_on_path()]
    return results


def exit_code(results):
    return 1 if any(r.required and r.status == MISSING for r in results) else 0


def format_results(results):
    lines = []
    width = max((len(r.name) for r in results), default=10)
    for r in results:
        tag = "R" if r.required else "o"
        lines.append(f"[{tag}] {r.name.ljust(width)}  {r.status.ljust(7)}  {r.detail}")
        if r.fix and r.status in (MISSING, WARN):
            lines.append(f"      FIX: {r.fix}")
    bad = sum(1 for r in results if r.required and r.status == MISSING)
    warn = sum(1 for r in results if r.status == WARN)
    ok = sum(1 for r in results if r.status == OK)
    skipped = sum(1 for r in results if r.status == SKIPPED)
    verdict = "READY" if bad == 0 else "NOT READY"
    lines.append(f"{verdict}: {ok} ok, {bad} required missing, {warn} warnings, {skipped} skipped")
    return lines


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="novel doctor")
    ap.add_argument("--online", action="store_true", help="also test Kaggle and the Gemini keys over the network")
    ap.add_argument("--fix-path", action="store_true", help="offer to add <repo>/bin to the user PATH")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    a = ap.parse_args(argv)
    results = collect(a.online)
    if a.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        for line in format_results(results):
            print(line)
    if a.fix_path:
        print("", file=sys.stderr if a.json else sys.stdout)
        fix_path(out=(lambda s: print(s, file=sys.stderr)) if a.json else print)
    return exit_code(results)
