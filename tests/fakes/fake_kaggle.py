"""A stand-in for the Kaggle CLI: only what `novel tts` calls, state kept in a folder.

    FAKE_KAGGLE_STATE            folder holding the fake's datasets and kernels (required)
    FAKE_KAGGLE_RUNNING_POLLS=N  `kernels status` says RUNNING for the first N polls after a push
    FAKE_KAGGLE_ERROR=a|b|c      the kernel ends in ERROR; `kernels logs` shows these lines
    FAKE_KAGGLE_EXTRA_LOG=a|b|c  `kernels logs` also shows these lines (the ktrial server's `KTRIAL URL ...`)
    FAKE_KAGGLE_EMPTY_LOG_WHILE_RUNNING=1  `kernels logs` is empty while the kernel is RUNNING (the real service does this)
    FAKE_KAGGLE_HASH_MISMATCH=1  the run reports a different hash for every narrated text
    FAKE_KAGGLE_CALLS=<file>     append one line per call (to prove what was invoked)

Credentials: like the real CLI it needs <KAGGLE_CONFIG_DIR>/access_token (the folder
novel passes as $NOVEL_KAGGLE_HOME), and it records which one it saw in the call log.

`kernels output` builds what the real run would leave: per staged chapter an mp3
(a quiet sine tone, 128 kbps, speech = characters / 15.3 s per chunk plus the
runner's gaps), a measured timing map written by abpipe.timing.write_map from
abpipe.tts.chunk_hindi (the real runner's own functions), the run's manifest.json
and hashes.json. Subcommands:

    datasets status|create|version|files     kernels push|status|logs|output
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHARS_PER_SECOND = 15.3
SAMPLE_RATE = 24000


def die(msg: str, code: int = 1):
    print(msg)
    sys.exit(code)


def state_dir() -> Path:
    d = os.environ.get("FAKE_KAGGLE_STATE")
    if not d:
        die("fake_kaggle: FAKE_KAGGLE_STATE is not set")
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


def key(ref: str) -> str:
    return ref.replace("/", "__")


def opt(args: list[str], name: str, default=None):
    return args[args.index(name) + 1] if name in args else default


def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ datasets


def ds_dir(ref: str) -> Path:
    return state_dir() / "datasets" / key(ref)


def datasets(sub: str, args: list[str]) -> None:
    if sub == "status":
        ref = args[0]
        print("ready" if ds_dir(ref).exists() else "403 Forbidden")   # a missing dataset is 403
        return
    if sub in ("create", "version"):
        src = Path(opt(args, "-p", ".")).resolve()
        meta = read_json(src / "dataset-metadata.json")
        ref = meta["id"]
        if sub == "version" and not ds_dir(ref).exists():
            die("404 Not Found")
        if sub == "create" and ds_dir(ref).exists():
            die("409 Conflict: dataset exists")
        if ds_dir(ref).exists():
            shutil.rmtree(ds_dir(ref))
        shutil.copytree(src, ds_dir(ref))
        print("Your private Dataset is being created. Please check progress at "
              f"https://www.kaggle.com/datasets/{ref}" if sub == "create" else
              f"Dataset version is being created. Please check progress at "
              f"https://www.kaggle.com/datasets/{ref}")
        return
    if sub == "files":
        d = ds_dir(args[0])
        if not d.exists():
            die("403 Forbidden")
        print("name  size  creationDate")
        for f in sorted(d.rglob("*")):
            if f.is_file():
                print(f"{f.relative_to(d).as_posix()}  {f.stat().st_size}  2026-01-01")
        return
    die(f"fake_kaggle: datasets {sub} is not implemented")


# ------------------------------------------------------------------ kernels


def kernel_file(ref: str) -> Path:
    return state_dir() / "kernels" / (key(ref) + ".json")


def kernels(sub: str, args: list[str]) -> None:
    if sub == "push":
        src = Path(opt(args, "-p", ".")).resolve()
        meta = read_json(src / "kernel-metadata.json")
        if not (src / meta["code_file"]).exists():
            die("code_file is missing from the kernel folder")
        for ds in meta["dataset_sources"]:
            if ds.endswith(("/hf-token", "/ngrok-token")) or ds_dir(ds).exists():
                continue
            die(f"403 Forbidden: dataset {ds} not found")
        write_json(kernel_file(meta["id"]), {"polls": 0, "sources": meta["dataset_sources"]})
        print(f"Kernel version 1 successfully pushed.  Please check progress at "
              f"https://www.kaggle.com/code/{meta['id']}")
        return
    ref = args[0] if args else ""
    kf = kernel_file(ref)
    if not kf.exists():
        die("404 Not Found")
    k = read_json(kf)
    errors = [l for l in os.environ.get("FAKE_KAGGLE_ERROR", "").split("|") if l]
    if sub == "status":
        k["polls"] += 1
        write_json(kf, k)
        n = int(os.environ.get("FAKE_KAGGLE_RUNNING_POLLS", "0"))
        word = "RUNNING" if k["polls"] <= n else ("ERROR" if errors else "COMPLETE")
        print(f'{ref} has status "KernelWorkerStatus.{word}"')
        return
    if sub == "logs":
        if os.environ.get("FAKE_KAGGLE_EMPTY_LOG_WHILE_RUNNING") and k["polls"] <= int(
                os.environ.get("FAKE_KAGGLE_RUNNING_POLLS", "0")):
            print("[]")
            return
        lines = ["[maya2] voice and model read from config.yaml", "venv built in 0.0 min"]
        cfg = staged_dataset(k) / "config.yaml"
        if cfg.exists():
            import yaml
            m = yaml.safe_load(cfg.read_text(encoding="utf-8"))["tts"]["maya2"]
            lines.append(f"[maya2] voice={m['voice']} model={m['model_id']}")
        lines += errors if errors else ["probe 5.0s audio in 5.0s  RTF 1.00", "  spoke  done"]
        lines += [l for l in os.environ.get("FAKE_KAGGLE_EXTRA_LOG", "").split("|") if l]
        print(json.dumps([{"stream_name": "stdout", "time": 0.0, "data": l + "\n"} for l in lines]))
        return
    if sub == "output":
        if errors:
            die("kernel ended in ERROR: no output")
        dest = Path(opt(args, "-p", "."))
        pattern = re.compile(opt(args, "--file-pattern", ""))
        out = dest / "_build"
        build_output(staged_dataset(k), out)
        for f in sorted(out.rglob("*")):
            rel = "audiobook/" + f.relative_to(out).as_posix()
            if f.is_file() and pattern.search(rel):
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
        shutil.rmtree(out)
        print(f"Output file downloaded to {dest}")
        return
    die(f"fake_kaggle: kernels {sub} is not implemented")


def staged_dataset(k: dict) -> Path:
    ref = next(s for s in k["sources"] if not s.endswith(("/hf-token", "/ngrok-token")))
    return ds_dir(ref)


def build_output(ds: Path, out: Path) -> None:
    """What the notebook leaves in /kaggle/working/audiobook for these staged chapters."""
    sys.dont_write_bytecode = True      # never write __pycache__ into kaggle/payload
    sys.path.insert(0, str(REPO / "kaggle" / "payload"))
    import yaml
    from abpipe import timing as TM
    from abpipe import tts as S
    from abpipe.manifest import Chapter
    from abpipe.spoken_numbers import speakable

    cfg = yaml.safe_load((ds / "config.yaml").read_text(encoding="utf-8"))["tts"]
    mismatch = os.environ.get("FAKE_KAGGLE_HASH_MISMATCH")
    chapters, hashes = [], {}
    for row in read_json(ds / "manifest.json")["chapters"]:
        name = f"{row['idx']:05d}"
        raw = (ds / "hindi" / row["hindi"]).read_bytes()
        hashes[row["hindi"]] = hashlib.sha256(raw + (b"x" if mismatch else b"")).hexdigest()
        text = raw.decode("utf-8")
        if cfg.get("numbers", "english") == "english":
            text = speakable(text)
        # The same layout synth_chapter builds: speech, then a paragraph or short gap.
        chunks, pos = [], 0
        for i, c in enumerate(S.chunk_hindi(text, cfg["chars_per_chunk"])):
            speech = int(round(len(c.text) / CHARS_PER_SECOND * SAMPLE_RATE))
            chunks.append({"chunk": i, "para": c.para, "start_sec": round(pos / SAMPLE_RATE, 3),
                           "end_sec": round((pos + speech) / SAMPLE_RATE, 3), "chars": len(c.text)})
            gap_ms = cfg["paragraph_gap_ms"] if c.ends_para else S.SHORT_GAP_MS
            pos += speech + int(SAMPLE_RATE * gap_ms / 1000)
        seconds = pos / SAMPLE_RATE
        (out / "audio").mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
             f"sine=frequency=220:sample_rate={SAMPLE_RATE}:duration={seconds:.4f}",
             "-af", "volume=0.05", "-ac", "1", "-b:a", "128k", str(out / "audio" / f"{name}.mp3")],
            check=True)
        ch = Chapter(idx=row["idx"], title=row["title"], source=row["source"], seconds=seconds)
        TM.write_map(out / "timing" / f"{name}.json", ch, SAMPLE_RATE, chunks, measured=True)
        chapters.append({**row, "status": "done", "audio": f"{name}.mp3", "timing": f"{name}.json",
                         "seconds": seconds, "tts_seconds": round(seconds / 2, 1), "rtf": 0.5,
                         "error": None})
    write_json(out / "manifest.json", {"version": 1, "chapters": chapters})
    write_json(out / "hashes.json", hashes)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        die("usage: fake_kaggle.py datasets|kernels <subcommand> ...", 2)
    group, sub, rest = argv[0], argv[1], argv[2:]
    token = Path(os.environ.get("KAGGLE_CONFIG_DIR", "")) / "access_token"
    if os.environ.get("FAKE_KAGGLE_CALLS"):
        seen = token.read_text(encoding="utf-8").strip()[-6:] if token.is_file() else "NO-TOKEN"
        with open(os.environ["FAKE_KAGGLE_CALLS"], "a", encoding="utf-8") as f:
            f.write(f"{group} {sub} token=...{seen}\n")
    if not token.is_file():
        die("401 Unauthorized: no access_token in KAGGLE_CONFIG_DIR")
    {"datasets": datasets, "kernels": kernels}.get(group, lambda *_: die(f"fake_kaggle: {group}?"))(sub, rest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
