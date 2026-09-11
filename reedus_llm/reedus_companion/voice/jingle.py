"""short UI tones: listening, ready, sleep."""
from __future__ import annotations

import os
from typing import Optional

from reedus_companion import config


def _tone(freqs, durs, sr: int = 24000, gain: float = 0.22):
    """Sine beeps with a short fade so USB DACs don't click."""
    import numpy as np
    parts = []
    for f, d in zip(freqs, durs):
        n = max(1, int(sr * d))
        t = np.arange(n, dtype=np.float32) / float(sr)
        wave = np.sin(2.0 * np.pi * float(f) * t).astype(np.float32)
        fade = min(n // 8, int(sr * 0.012))
        if fade > 0:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            wave[:fade] *= ramp
            wave[-fade:] *= ramp[::-1]
        parts.append(wave * float(gain))
        gap = int(sr * 0.04)
        if gap:
            parts.append(np.zeros(gap, dtype=np.float32))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def make_jingle(kind: str, sr: int = 24000):
    """float32 mono audio for listen | ready | sleep."""
    k = (kind or "listen").lower().strip()
    if k == "ready":
        return _tone([660.0, 880.0], [0.07, 0.09], sr=sr, gain=0.18)
    if k in ("sleep", "stop", "bye"):
        return _tone([720.0, 480.0, 320.0], [0.08, 0.08, 0.12], sr=sr, gain=0.16)
    # listen / arm / wake default
    return _tone([523.25, 659.25, 783.99], [0.06, 0.06, 0.10], sr=sr, gain=0.20)


def play_jingle(kind: str = "listen", device=None, sr: int = 24000) -> None:
    """Play a UI jingle on the TTS output device. No-op if REEDUS_JINGLE=0."""
    if os.environ.get("REEDUS_JINGLE", "1") == "0":
        return
    from reedus_companion.voice.tts import play_audio
    audio = make_jingle(kind, sr=sr)
    if getattr(audio, "size", 0) == 0:
        return
    play_audio(audio, sr, device=device if device is not None else config.TTS_DEVICE)


def jingle_path(kind: str, directory: Optional[str] = None) -> str:
    """Write a WAV preview."""
    import wave
    import numpy as np
    from pathlib import Path
    sr = 24000
    audio = make_jingle(kind, sr=sr)
    out_dir = Path(directory or (config.VOICE_DIR / "jingles"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"reedus_{kind}.wav"
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return str(path)


if __name__ == "__main__":
    import sys
    kind = sys.argv[1] if len(sys.argv) > 1 else "listen"
    path = jingle_path(kind)
    print(f"wrote {path}")
    if os.environ.get("REEDUS_JINGLE_PLAY", "1") == "1":
        play_jingle(kind)
        print(f"played {kind}")
