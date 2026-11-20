#!/usr/bin/env python3
"""run the streaming .tflite over wav files the way the S3 does, and count wakes.

each folder is scored as a whole: positives want every clip to wake him, negatives want none.
long recordings (a podcast, an hour of the room) count false wakes per hour instead.

    python mww_eval.py models/reedus.tflite --pos ood/espeak --neg ood/soundalikes --long room.wav
    python mww_eval.py models/reedus.tflite --make-espeak ood/espeak   # TTS that isn't piper
"""
from __future__ import annotations

import argparse
import subprocess
import wave
from pathlib import Path

import numpy as np

CUTOFFS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.97, 0.99)
REFRACTORY_S = 2.0   # after a wake, ignore the next 2 s (one "Reedus" = one wake)


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, f"{path}: need 16 kHz mono"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def probs(model, audio: np.ndarray) -> np.ndarray:
    return np.asarray(model.predict_clip(audio, step_ms=10), dtype=np.float32)


def wakes(p: np.ndarray, cutoff: float, window: int, step_s: float) -> int:
    """How many times the sliding-window average crosses the cutoff."""
    if len(p) < window:
        return 0
    avg = np.convolve(p, np.ones(window) / window, mode="valid")
    n, last = 0, -1e9
    for i in np.flatnonzero(avg >= cutoff):
        if (i - last) * step_s >= REFRACTORY_S:
            n, last = n + 1, i
    return n


def make_espeak(out: Path) -> None:
    """A different TTS engine than the one we trained on: an honest-ish recall check."""
    out.mkdir(parents=True, exist_ok=True)
    voices = [f"en-us+{v}" for v in ("m1", "m2", "m3", "m4", "m5", "m7", "f1", "f2", "f3", "f4",
                                      "klatt", "klatt2", "croak", "whisper")] + ["en", "en-gb-x-rp"]
    n = 0
    for v in voices:
        for speed in (130, 160, 190):
            for pitch in (35, 50, 70):
                for text in ("readus", "readiss"):
                    raw = out / f"_{n}.wav"
                    subprocess.run(["espeak-ng", "-v", v, "-s", str(speed), "-p", str(pitch),
                                    "-w", str(raw), text], check=True)
                    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i",
                                    str(raw), "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
                                    str(out / f"{n}.wav")], check=True)
                    raw.unlink()
                    n += 1
    print(f"{n} espeak clips -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model", type=Path)
    ap.add_argument("--pos", type=Path, nargs="*", default=[])
    ap.add_argument("--neg", type=Path, nargs="*", default=[])
    ap.add_argument("--long", type=Path, nargs="*", default=[], help="long wavs: false wakes/hour")
    ap.add_argument("--window", type=int, default=5, help="sliding_window_size in the manifest")
    ap.add_argument("--make-espeak", type=Path)
    a = ap.parse_args()
    if a.make_espeak:
        make_espeak(a.make_espeak)
        if not (a.pos or a.neg or a.long):
            return

    from microwakeword.inference import Model
    model = Model(str(a.model))
    step_s = model.stride * 0.010
    pad_pre, pad_post = np.zeros(16000, np.int16), np.zeros(8000, np.int16)

    print(f"{'set':<34}" + "".join(f"{c:>7}" for c in CUTOFFS))
    for kind, dirs in (("pos", a.pos), ("neg", a.neg)):
        for d in dirs:
            for sub in sorted({p.parent for p in d.rglob("*.wav")}):
                files = sorted(sub.glob("*.wav"))
                ps = [probs(model, np.concatenate([pad_pre, load(f), pad_post])) for f in files]
                rates = [np.mean([wakes(p, c, a.window, step_s) > 0 for p in ps]) for c in CUTOFFS]
                label = f"{kind} {sub.relative_to(d.parent)} ({len(files)})"
                print(f"{label[:34]:<34}" + "".join(f"{r:>7.1%}" for r in rates))
    for f in a.long:
        audio = load(f)
        hours = len(audio) / 16000 / 3600
        p = probs(model, audio)
        print(f"{'long ' + f.name + f' ({hours:.2f} h)':<34}"
              + "".join(f"{wakes(p, c, a.window, step_s) / hours:>6.1f}/h" for c in CUTOFFS))


if __name__ == "__main__":
    main()
