"""Hindi speech synthesis.

Three backends with a deliberate quality/hardware tradeoff:

  indicf5 - AI4Bharat's F5-based model. Good Hindi. Needs a GPU; on a 4-core
            CPU it runs roughly 8-15x slower than realtime, which makes a
            4-hour batch take two days. Kaggle only.

  maya2   - Maya 2 Native, built-in voices, no reference clip. Needs a GPU and
            is on trial: its backbone is bf16 and the T4 has no bf16 units. See
            maya2_worker.py.

  kokoro  - 82M params, runs 2-4x faster than realtime on a Ryzen 3. Its Hindi
            voices are graded C by the authors: clear but flat. This is the
            fallback for when Kaggle quota is gone, not the target.

VERIFY BEFORE A LONG RUN: IndicF5's call signature and output sample rate come
from its model card and have changed before. Synthesise one paragraph and check
the wav sounds right and plays at correct speed before committing a batch.

IndicF5 and Maya 2 run out-of-process, via indicf5_worker.py and
maya2_worker.py, each in a separate venv with its own pinned dependency stack -
see those files' docstrings for why. Neither can share an environment with
sarvam-translate's vLLM/Gemma3 requirements.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import NamedTuple

import numpy as np
import soundfile as sf

from .spoken_numbers import speakable


# Silence inserted after a chunk that does NOT end a paragraph - just enough to
# separate two halves of a long sentence. The paragraph-ending gap is
# configurable (tts.paragraph_gap_ms); this one is not worth a knob, but it is
# named because abpipe/timing.py has to reproduce the same layout exactly.
SHORT_GAP_MS = 120


class Chunk(NamedTuple):
    text: str
    ends_para: bool
    # Index into this chapter's non-empty paragraphs. Carried through to the
    # timing map so a scene picked out of the ENGLISH chapter can be located in
    # the audio: the Hindi output has the same paragraph count as its English
    # source (verified across every finished chapter), so the index is the same
    # on both sides. Paragraph 0 is the "Chapter N: Title" line, which is
    # narrated like any other.
    para: int


def chunk_hindi(text: str, max_chars: int) -> list[Chunk]:
    """Split into synthesis-sized pieces.

    Returns (chunk, ends_paragraph, para_index). The flag drives where silence
    is inserted, so paragraph breaks in the novel become audible pauses rather
    than the narrator running straight through a scene change.
    """
    out: list[Chunk] = []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for para_idx, para in enumerate(paras):
        # Devanagari danda and western terminators both end sentences.
        sentences = [s.strip() for s in re.split(r"(?<=[।?!.])\s+", para) if s.strip()]
        cur = ""
        pieces: list[str] = []
        for s in sentences:
            if cur and len(cur) + len(s) + 1 > max_chars:
                pieces.append(cur)
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        if cur:
            pieces.append(cur)
        for i, piece in enumerate(pieces):
            out.append(Chunk(piece, i == len(pieces) - 1, para_idx))
    return out


class WorkerBackend:
    """A TTS model running in its own process and venv, spoken to over
    stdin/stdout. See indicf5_worker.py for the protocol."""

    tag = "worker"          # prefix of the stderr lines worth showing

    def _start(self, cmd: list[str], env: dict | None = None) -> None:
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            env=env,
        )
        # stderr has to be read as it arrives. Nothing drains a pipe left
        # alone: once it fills (~64KB), a chatty library blocks on its next
        # write and the worker hangs, silently, for the rest of the session.
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self._drain = threading.Thread(target=self._read_stderr, daemon=True)
        self._drain.start()

        ready_line = self.proc.stdout.readline()
        ready = json.loads(ready_line) if ready_line else {}
        if not ready.get("ready"):
            # readline() only returns '' at EOF, so the process has exited and
            # the drain thread is about to finish.
            self._drain.join(timeout=5)
            raise RuntimeError(
                f"{self.tag} worker failed to start ({ready_line!r}):\n"
                + "".join(self.stderr_tail)
            )

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            self.stderr_tail.append(line)
            if line.startswith(f"[{self.tag}]"):
                print(line, end="", flush=True)

    def say(self, text: str) -> np.ndarray:
        self.proc.stdin.write(json.dumps({"text": text}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            self._drain.join(timeout=5)
            raise RuntimeError(f"{self.tag} worker died:\n"
                               + "".join(self.stderr_tail))
        resp = json.loads(line)
        if "error" in resp:
            raise RuntimeError(f"{self.tag} worker: {resp['error']}")
        audio, _ = sf.read(resp["wav_path"], dtype="float32")
        os.remove(resp["wav_path"])
        return audio

    def close(self) -> None:
        """Explicit shutdown, so callers freeing GPU memory for a subsequent
        model load don't depend on __del__ firing promptly (relying on
        refcounting/GC timing here has bitten this pipeline before)."""
        self.__del__()

    def __del__(self):
        proc = getattr(self, "proc", None)
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(json.dumps({"cmd": "stop"}) + "\n")
            proc.stdin.flush()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


class IndicF5(WorkerBackend):
    """Talks to indicf5_worker.py (a separate process, separate venv) over
    stdin/stdout rather than importing transformers.AutoModel directly - see
    that file's docstring for why."""

    tag = "indicf5"

    def __init__(self, cfg: dict):
        sub = cfg["indicf5"]
        self.ref_audio = sub["ref_audio"]
        self.ref_text = sub["ref_text"]
        self.sample_rate = sub["sample_rate"]

        python = sub.get("venv_python") or sys.executable
        if python == sys.executable:
            print("WARNING: no tts.indicf5.venv_python set, running IndicF5 "
                  "in this process's own environment. This is expected to "
                  "hit the meta-device bug unless this environment happens "
                  "to already have the old pinned stack installed.")
        worker = Path(__file__).with_name("indicf5_worker.py")
        # f5_tts imports matplotlib, which chokes if it inherits the parent
        # Jupyter kernel's MPLBACKEND (Jupyter's inline backend, not
        # installed in this separate venv). Force a headless backend.
        env = os.environ.copy()
        env["MPLBACKEND"] = "Agg"
        self._start(
            [python, str(worker),
             "--model-id", sub["model_id"],
             "--ref-audio", self.ref_audio,
             "--ref-text", self.ref_text,
             "--sample-rate", str(self.sample_rate),
             "--device", "cuda",
             # fp32 unless config says otherwise, so turning on half precision
             # is an explicit, measurable act rather than a silent default.
             "--precision", str(sub.get("precision", "fp32"))],
            env=env,
        )


class Maya2(WorkerBackend):
    """Talks to maya2_worker.py, in its own venv. Uses a built-in voice, so
    unlike IndicF5 there is no reference clip."""

    tag = "maya2"

    def __init__(self, cfg: dict):
        sub = cfg["maya2"]
        self.sample_rate = sub["sample_rate"]
        python = sub.get("venv_python") or sys.executable
        worker = Path(__file__).with_name("maya2_worker.py")
        self._start([
            python, str(worker),
            "--model-id", sub["model_id"],
            "--voice", sub["voice"],
            "--lang", sub["lang"],
            "--sample-rate", str(self.sample_rate),
            "--max-seats", str(sub.get("max_seats", 1)),
        ])


class Kokoro:
    def __init__(self, cfg: dict):
        from kokoro import KPipeline
        sub = cfg["kokoro"]
        self.pipe = KPipeline(lang_code=sub["lang_code"])
        self.voice = sub["voice"]
        self.sample_rate = sub["sample_rate"]

    def say(self, text: str) -> np.ndarray:
        parts = [chunk for _, _, chunk in self.pipe(text, voice=self.voice)]
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([np.asarray(p, dtype=np.float32) for p in parts])


def make_backend(cfg: dict):
    if cfg["backend"] == "indicf5":
        return IndicF5(cfg)
    if cfg["backend"] == "maya2":
        return Maya2(cfg)
    if cfg["backend"] == "kokoro":
        return Kokoro(cfg)
    raise ValueError(f"unknown tts backend: {cfg['backend']}")


def synth_chapter(text: str, backend, cfg: dict) -> tuple[np.ndarray, int, list[dict]]:
    """Synthesise a chapter and record where each chunk landed in it.

    The third return value is the timing map: one entry per chunk, with the
    second offset at which it starts and ends. These boundaries are known here
    for free - `say()` hands back the samples - and are unrecoverable later,
    once everything is concatenated into one array. Phase 4 of the visual layer
    needs them to put an image on screen at the moment its paragraph is read.

    `end_sec` deliberately EXCLUDES the trailing gap: the pause after a
    paragraph belongs to neither the chunk before it nor the one after, and a
    slide should change on a speech boundary.
    """
    # Before chunking, so chunk lengths are those of what is actually spoken.
    # Paragraph breaks are untouched, so the `para` indices are unaffected.
    if cfg.get("numbers", "english") == "english":
        text = speakable(text)

    sr = backend.sample_rate
    gap = np.zeros(int(sr * cfg["paragraph_gap_ms"] / 1000), dtype=np.float32)
    short = np.zeros(int(sr * SHORT_GAP_MS / 1000), dtype=np.float32)

    pieces: list[np.ndarray] = []
    timing: list[dict] = []
    pos = 0                     # samples written so far, i.e. the play cursor
    for i, c in enumerate(chunk_hindi(text, cfg["chars_per_chunk"])):
        wav = backend.say(c.text)
        start = pos
        pos += len(wav)
        timing.append({
            "chunk": i,
            "para": c.para,
            "start_sec": round(start / sr, 3),
            "end_sec": round(pos / sr, 3),
            "chars": len(c.text),
        })
        pieces.append(wav)
        tail = gap if c.ends_para else short
        pieces.append(tail)
        pos += len(tail)
    if not pieces:
        return np.zeros(0, dtype=np.float32), sr, []
    audio = np.concatenate(pieces)

    # The cursor above and the concatenation below are built independently, so
    # they can disagree - and if they do, every timestamp downstream is wrong by
    # a growing amount whose only symptom is images drifting out of sync in a
    # finished video. Both are integer sample counts over the same pieces, so
    # this is exact, not a tolerance.
    if pos != len(audio):
        raise RuntimeError(
            f"timing cursor ended at sample {pos} but the audio is "
            f"{len(audio)} samples - the map does not describe this waveform"
        )
    return audio, sr, timing


def write_mp3(audio: np.ndarray, sr: int, dest: Path, title: str,
              bitrate: str = "64k", channels: int = 1) -> float:
    """Encode via ffmpeg stdin. Returns duration in seconds."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767).astype(np.int16).tobytes()

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "s16le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
        "-ac", str(channels), "-b:a", bitrate,
        "-metadata", f"title={title}",
        str(dest),
    ]
    subprocess.run(cmd, input=pcm, check=True)
    return len(audio) / float(sr)
