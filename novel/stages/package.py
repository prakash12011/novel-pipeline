"""Bundle finished chapters into one navigable audiobook file per batch.

Produces .m4b rather than a concatenated .mp3: m4b carries real chapter
markers, so a player can skip between chapters and remember position. A
plain mp3 concat plays start-to-finish with no way to jump to chapter 855.

Chapter boundaries come from the manifest's per-chapter `seconds`, which the
TTS step records from the actual sample count - so the markers line up with
the audio without re-probing every file.

Usage:
    python -m abpipe.package                    # every done chapter, in 10s
    python -m abpipe.package --group-size 10
    python -m abpipe.package --start 850 --end 859
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .manifest import Manifest, DONE


def _ffmetadata(chapters: list, title: str) -> str:
    """Chapter markers in ffmpeg's metadata format, in milliseconds."""
    lines = [";FFMETADATA1", f"title={title}", "genre=Audiobook", ""]
    start_ms = 0
    for ch in chapters:
        end_ms = start_ms + int((ch.seconds or 0) * 1000)
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            # '=' and ';' are field separators in ffmetadata and must be escaped.
            "title=" + ch.title.replace("=", r"\=").replace(";", r"\;"),
            "",
        ]
        start_ms = end_ms
    return "\n".join(lines)


def package_group(chapters: list, audio_dir: Path, out_path: Path,
                  cover: Path | None, bitrate: str) -> None:
    work = out_path.parent
    work.mkdir(parents=True, exist_ok=True)
    concat_file = work / f".{out_path.stem}.concat.txt"
    meta_file = work / f".{out_path.stem}.meta.txt"

    # ffmpeg's concat demuxer wants forward slashes and escaped quotes even on
    # Windows; absolute paths avoid any working-directory surprises.
    concat_file.write_text(
        "\n".join(
            "file '%s'" % (audio_dir / ch.audio).resolve().as_posix().replace("'", r"'\''")
            for ch in chapters
        ),
        encoding="utf-8",
    )
    meta_file.write_text(_ffmetadata(chapters, out_path.stem), encoding="utf-8")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(concat_file),
           "-i", str(meta_file)]
    if cover:
        cmd += ["-i", str(cover)]
    cmd += ["-map", "0:a", "-map_metadata", "1"]
    if cover:
        cmd += ["-map", "2:v", "-disposition:v", "attached_pic", "-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", bitrate, str(out_path)]

    subprocess.run(cmd, check=True)
    concat_file.unlink(missing_ok=True)
    meta_file.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="workspace")
    ap.add_argument("--out", default=None, help="default: <root>/books")
    ap.add_argument("--group-size", type=int, default=10)
    ap.add_argument("--start", type=int, help="first chapter number, e.g. 850")
    ap.add_argument("--end", type=int, help="last chapter number, e.g. 859")
    ap.add_argument("--bitrate", default="64k")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        raise SystemExit(
            "ffmpeg not found on PATH. It is required to build .m4b files.\n"
            "  Windows : winget install Gyan.FFmpeg   (then reopen the terminal)\n"
            "  Linux   : apt-get install -y ffmpeg"
        )

    root = Path(args.root)
    audio_dir = root / "audio"
    out_dir = Path(args.out) if args.out else root / "books"

    man = Manifest.load(root / "manifest.json")
    done = [c for c in man.chapters if c.status == DONE and c.audio]
    if not done:
        raise SystemExit("no finished chapters with audio in the manifest")

    def number(ch) -> int:
        return int(ch.source.split("_", 1)[0])

    if args.start is not None:
        done = [c for c in done if number(c) >= args.start]
    if args.end is not None:
        done = [c for c in done if number(c) <= args.end]
    if not done:
        raise SystemExit("no finished chapters in that range")

    covers = sorted((root / "covers").glob("*.jpg")) + sorted((root / "covers").glob("*.png"))
    cover = covers[0] if covers else None
    if cover is None:
        print("note: no image in workspace/covers/, building without cover art")

    for i in range(0, len(done), args.group_size):
        group = done[i:i + args.group_size]
        name = f"COI_{number(group[0]):04d}-{number(group[-1]):04d}.m4b"
        out_path = out_dir / name
        mins = sum(c.seconds or 0 for c in group) / 60
        print(f"building {name}  ({len(group)} chapters, {mins:.0f} min)", flush=True)
        package_group(group, audio_dir, out_path, cover, args.bitrate)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"  -> {out_path}  ({size_mb:.1f} MB)", flush=True)


if __name__ == "__main__":
    main()
