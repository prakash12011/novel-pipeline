"""Maya 2 Native, in its own process with its own pinned dependency stack.

Why this exists: maya3 pins torch==2.8.0 and transformers==5.6.2, which is
neither the stack sarvam-translate runs on nor IndicF5's old one. It gets a
venv of its own and speaks the same stdin/stdout JSON protocol as
indicf5_worker.py - see that file for the protocol.

UNVERIFIED ON A T4. Maya publishes numbers for an H100 and an RTX 4090 only. Its
backbone is hard-coded to bf16 (engine.py, serve/fast_backbone.py) and the T4
is Turing, which has no bf16 units, so bf16 work there is emulated. Expect it
to be slower than the published RTF, and check the startup line this prints.

Text is sent with normalize=False: synth_chapter has already run spoken_numbers.py,
so no digits reach this point, and those readings are tested across the book.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile

# Keep the real stdout for the protocol, then point fd 1 at stderr. Unlike the
# sys.stdout swap in indicf5_worker.py this also catches native code: Triton
# and CUDA print from C, below anything Python can redirect.
_protocol = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)


def _emit(obj: dict) -> None:
    _protocol.write(json.dumps(obj) + "\n")
    _protocol.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-id", required=True)
    ap.add_argument("--voice", required=True)
    ap.add_argument("--lang", required=True)
    ap.add_argument("--sample-rate", type=int, required=True)
    ap.add_argument("--max-seats", type=int, default=1)
    args = ap.parse_args()

    import numpy as np
    import soundfile as sf
    import torch
    from maya3 import Maya3TTS

    # Maya does not document CPU inference, so there is nothing to fall back
    # to. Fail here rather than discover it one chunk at a time.
    if not torch.cuda.is_available():
        raise RuntimeError("maya2 needs a GPU and torch sees none")
    print("[maya2] gpu=%s  torch=%s  bf16_native=%s  arch=%s" % (
        torch.cuda.get_device_name(0), torch.__version__,
        torch.cuda.is_bf16_supported(), ",".join(torch.cuda.get_arch_list())),
        file=sys.stderr, flush=True)

    tts = Maya3TTS.from_pretrained(args.model_id, max_seats=args.max_seats)
    print("[maya2] loaded %s  voice=%s  lang=%s  vram=%.1f GB" % (
        args.model_id, args.voice, args.lang,
        torch.cuda.max_memory_allocated() / 1024**3),
        file=sys.stderr, flush=True)

    _emit({"ready": True})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        if req.get("cmd") == "stop":
            break
        try:
            # chunk_hindi can emit a fragment of bare punctuation. There is
            # nothing to say, and one bad chunk fails the whole chapter.
            if not re.search(r"\w", req["text"]):
                audio = np.zeros(args.sample_rate // 4, dtype=np.float32)
            else:
                audio = np.asarray(
                    tts.generate(req["text"], lang=args.lang, voice=args.voice,
                                 normalize=False),
                    dtype=np.float32)

            # Same guard as indicf5_worker.py, for the same reason: a model
            # that fails numerically here tends to return silence, not raise.
            if audio.size and not np.isfinite(audio).all():
                raise RuntimeError(
                    "non-finite samples (%d NaN, %d inf)"
                    % (int(np.isnan(audio).sum()), int(np.isinf(audio).sum())))
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            if peak > 0:
                rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
                crest = peak / rms if rms > 0 else 0.0
                if crest < 2.0:
                    msg = ("crest factor %.2f (peak %.4f, rms %.4f, %.2fs). "
                           "Expected 4-12; 1.0 means a constant waveform."
                           % (crest, peak, rms, audio.size / args.sample_rate))
                    if audio.size >= args.sample_rate:
                        raise RuntimeError("output is not speech: " + msg)
                    print("[maya2] WARNING short chunk looks flat: " + msg,
                          file=sys.stderr, flush=True)

            fd, wav_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            sf.write(wav_path, audio, args.sample_rate)
            _emit({"wav_path": wav_path})
        except Exception as e:
            _emit({"error": str(e)})


if __name__ == "__main__":
    main()
