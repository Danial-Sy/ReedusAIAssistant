"""text-to-speech via kokoro-82M, paced, pitched and lightly robotized."""
from __future__ import annotations
import os
import random
import threading
import wave
from pathlib import Path
from typing import Optional

from reedus_companion import config
from reedus_companion.voice.robotize import robotize
from reedus_companion.voice.speech_text import (
    clean_for_speech, apply_pronunciations, segment_for_pacing)


class TTS:
    def __init__(self, voice: Optional[str] = None, pitch: Optional[float] = None,
                 robot: Optional[float] = None):
        # cap synth threads BEFORE kokoro loads: torch grabs every core, and synth runs while
        # llama.cpp is generating, which collapsed 22 -> 4 tok/s. 4 threads keeps synth ahead of playback.
        import torch
        torch.set_num_threads(int(__import__("os").environ.get("REEDUS_TTS_THREADS", "2")))
        from kokoro import KPipeline
        try:
            self.pipeline = KPipeline(lang_code=config.KOKORO_LANG,
                                      repo_id="hexgrad/Kokoro-82M")
        except TypeError:
            self.pipeline = KPipeline(lang_code=config.KOKORO_LANG)
        self.voice = voice or config.KOKORO_VOICE
        self.sample_rate = config.TTS_SAMPLE_RATE
        self.pitch = config.VOICE_PITCH_SEMITONES if pitch is None else pitch
        self.pitch_range = getattr(config, "VOICE_PITCH_RANGE", 1.0)
        self.robot = config.ROBOT_AMOUNT if robot is None else robot
        # voice is a preset name or a blended style tensor
        self.voice_arg = self._resolve_voice(voice)

    def _resolve_voice(self, override):
        """Return a single preset name, or a weighted blend of voice style vectors."""
        if override:                                 # explicit voice= wins
            return override
        blend = getattr(config, "KOKORO_VOICE_BLEND", None)
        if not blend:
            return self.voice
        try:
            total = float(sum(w for _, w in blend)) or 1.0
            mixed = None
            for name, w in blend:
                pack = self.pipeline.load_voice(name) * (float(w) / total)
                mixed = pack if mixed is None else mixed + pack
            return mixed
        except Exception:
            return self.voice

    # --- pacing ---
    @staticmethod
    def _pause_secs(seg: str, kind: str) -> float:
        """Total gap after a segment, by its ending punctuation, jittered ~20%."""
        if kind == "end":
            return 0.0
        if kind == "ellipsis" or seg.endswith("..."):
            base = config.TTS_PAUSE_ELLIPSIS
        elif seg.endswith("?"):
            base = getattr(config, "TTS_PAUSE_QUESTION", config.TTS_PAUSE_SENTENCE)
        elif seg.endswith("!"):
            base = getattr(config, "TTS_PAUSE_EXCLAIM", config.TTS_PAUSE_SENTENCE)
        elif kind == "soft":
            base = config.TTS_PAUSE_SOFT
        else:
            base = config.TTS_PAUSE_SENTENCE
        return max(0.0, base * random.uniform(0.85, 1.2))

    @staticmethod
    def _speed_for(seg: str) -> float:
        """Per-segment speed: exclamations get a quick burst, trailing '...' slows."""
        if seg.endswith("!"):
            return getattr(config, "TTS_SPEED_EXCLAIM", config.KOKORO_SPEED)
        if seg.endswith("..."):
            return getattr(config, "TTS_SPEED_ELLIPSIS", config.KOKORO_SPEED)
        return config.KOKORO_SPEED

    @staticmethod
    def _contour_for(seg: str) -> float:
        """Per-segment pitch offset in semitones, the between-sentence melody."""
        if seg.endswith("?"):
            return getattr(config, "TTS_PITCH_QUESTION", 0.0)
        if seg.endswith("!"):
            return getattr(config, "TTS_PITCH_EXCLAIM", 0.0)
        if seg.endswith("..."):
            return getattr(config, "TTS_PITCH_ELLIPSIS", 0.0)
        return 0.0

    def _trim(self, audio):
        """Strip kokoro's own silence padding so the inserted pauses are the only gaps."""
        try:
            import librosa
            trimmed, _ = librosa.effects.trim(audio, top_db=getattr(config, "TTS_TRIM_DB", 35.0))
            return trimmed if trimmed.size else audio
        except Exception:
            return audio

    # --- synthesis ---
    def _kokoro_raw(self, text: str, speed: Optional[float] = None):
        """Raw Kokoro audio for some text (no pitch/robot), float32 @ 24 kHz."""
        import numpy as np
        chunks = []
        for _gs, _ps, audio in self.pipeline(
                text, voice=self.voice_arg, speed=speed or config.KOKORO_SPEED):
            a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            chunks.append(a.astype(np.float32))
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def _synth_paced(self, text: str):
        """Default path: synth per segment, trim each, insert the real pauses."""
        import numpy as np
        segments = segment_for_pacing(text) or [(text, "end")]
        parts = []
        last = len(segments) - 1
        for idx, (seg, kind) in enumerate(segments):
            a = self._kokoro_raw(seg, speed=self._speed_for(seg))
            if a.size:
                a = self._trim(a)
                off = self._contour_for(seg)
                if abs(off) >= 0.05:
                    a = self._pitch_psola(a, off, 1.0)   # contour offset only, base+range later
                parts.append(a)
            if idx < last:
                secs = self._pause_secs(seg, kind)
                n = int(self.sample_rate * secs)
                if n:
                    parts.append(np.zeros(n, dtype=np.float32))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    def synth(self, text: str):
        """Return a float32 mono numpy array (paced + pitched + robotized)."""
        import numpy as np
        text = clean_for_speech((text or "").strip())
        text = apply_pronunciations(text, config.VOICE_PRONUNCIATIONS)
        if not text:
            return np.zeros(0, dtype=np.float32)

        if config.PAUSE_SCALE > 0:
            audio = self._synth_paced(text)
        else:
            audio = self._kokoro_raw(text)
        if audio.size == 0:
            return audio

        audio = self._pitch_psola(audio, self.pitch, self.pitch_range)
        if self.robot > 0:
            audio = robotize(audio, self.sample_rate, amount=self.robot,
                             chorus_hz=config.ROBOT_CHORUS_HZ, bits=config.ROBOT_BITS)
        return audio

    def _pitch_psola(self, audio, semitones: float, range_mult: float = 1.0):
        """PSOLA pitch shift + pitch-range widening via praat-parselmouth."""
        if abs(semitones) < 0.01 and abs(range_mult - 1.0) < 0.01:
            return audio
        try:
            import numpy as np
            import parselmouth
            from parselmouth.praat import call
            snd = parselmouth.Sound(audio.astype(np.float64),
                                    sampling_frequency=self.sample_rate)
            manip = call(snd, "To Manipulation", 0.01, 60, 400)
            tier = call(manip, "Extract pitch tier")
            if abs(range_mult - 1.0) >= 0.01:
                pitch = call(snd, "To Pitch", 0.0, 60, 400)
                med = call(pitch, "Get quantile", 0, 0, 0.5, "Hertz")
                if med and med > 0:
                    call(tier, "Formula", f"{med} * (self/{med})^{range_mult}")
            if abs(semitones) >= 0.01:
                call(tier, "Multiply frequencies", snd.xmin, snd.xmax,
                     2 ** (float(semitones) / 12))
            call([tier, manip], "Replace pitch tier")
            out = call(manip, "Get resynthesis (overlap-add)")
            return out.values[0].astype("float32")
        except Exception:
            pass
        try:
            import librosa
            return librosa.effects.pitch_shift(
                audio, sr=self.sample_rate, n_steps=float(semitones)).astype("float32")
        except Exception:
            return audio

    # --- output ---
    def warm(self) -> None:
        """One discarded synth so the first spoken turn isn't compiling graphs."""
        try:
            self.synth("Hey.")
        except Exception:
            pass

    def speak(self, text: str):
        audio = self.synth(text)
        if audio.size == 0:
            return
        play_audio(audio, self.sample_rate, device=config.TTS_DEVICE)

    def to_wav(self, text: str, path: str):
        import numpy as np
        audio = self.synth(text)
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm)


def playback_rate(device=None) -> int:
    """Sample rate the output device actually accepts."""
    import sounddevice as sd
    try:
        info = sd.query_devices(device if device is not None else sd.default.device[1])
        rate = int(info.get("default_samplerate") or 0)
        if rate >= 8000:
            return rate
    except Exception:
        pass
    return 48000


def _resample_linear(audio, src: int, dst: int):
    import numpy as np
    if src == dst or audio is None or getattr(audio, "size", 0) == 0:
        return audio
    n = int(round(len(audio) * (dst / float(src))))
    if n < 1:
        return audio
    x = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
    xi = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(xi, x, audio.astype(np.float32)).astype(np.float32)


_OUTPUT_LOCK = threading.RLock()


class PlaybackStream:
    """Keep the USB DAC open for a whole turn."""

    def __init__(self, src_rate: int, device=None):
        self.src_rate = int(src_rate)
        self.device = device
        self._dst = None
        self._stream = None
        self._running = False
        self._lock = threading.Lock()
        self._holds_output = False

    def write(self, audio) -> None:
        import numpy as np
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return
        with self._lock:
            self._start()
            dst = int(self._dst)
            if dst != self.src_rate:
                x = _resample_linear(x, self.src_rate, dst)
            gain = float(os.environ.get("REEDUS_TTS_GAIN", "2.5"))
            x = _loudness(x, gain)
            self._stream.write(x.reshape(-1, 1))

    def pad(self, secs: float) -> None:
        """Write silence. No-op until the stream is actually running."""
        import numpy as np
        if secs <= 0:
            return
        with self._lock:
            if not self._running:
                return
            n = int(self._dst * secs)
            if n:
                self._stream.write(np.zeros((n, 1), dtype=np.float32))

    def _start(self) -> None:
        import numpy as np
        import sounddevice as sd
        if self._stream is None:
            _OUTPUT_LOCK.acquire()
            self._holds_output = True
            try:
                self._dst = playback_rate(self.device)
                self._stream = sd.OutputStream(
                    samplerate=self._dst, channels=1, dtype="float32",
                    device=self.device, blocksize=0,
                )
            except Exception:
                if self._holds_output:
                    self._holds_output = False
                    try:
                        _OUTPUT_LOCK.release()
                    except Exception:
                        pass
                raise
        if not self._running:
            self._stream.start()
            self._running = True
            preroll = float(os.environ.get("REEDUS_TTS_PREROLL", "0.10"))
            n = int(self._dst * max(preroll, 0.0))
            if n:
                self._stream.write(np.zeros((n, 1), dtype=np.float32))

    def close(self) -> None:
        with self._lock:
            if self._stream is None and not self._holds_output:
                return
            try:
                if self._running and self._stream is not None:
                    self._stream.stop()
            except Exception:
                pass
            try:
                if self._stream is not None:
                    self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._running = False
            if self._holds_output:
                self._holds_output = False
                try:
                    _OUTPUT_LOCK.release()
                except Exception:
                    pass


def play_audio(audio, src_rate: int, device=None, retries: int = 5) -> None:
    """One-shot playback (TTS.speak / jingles). Retries on USB Device unavailable."""
    import time
    last = None
    for attempt in range(max(1, retries)):
        pb = PlaybackStream(src_rate, device)
        try:
            pb.write(audio)
            pb.pad(0.06)          # last samples survive the close
            pb.close()
            time.sleep(0.04)      # USB DAC settle
            return
        except Exception as e:                                  # noqa: BLE001
            last = e
            try:
                pb.close()
            except Exception:
                pass
            time.sleep(0.12 * (attempt + 1))
    if last is not None:
        raise last


def _loudness(audio, gain: float):
    """Make it audible on a quiet USB DAC without hard-clip grain."""
    import numpy as np
    x = np.asarray(audio, dtype=np.float32)
    if x.size == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if peak < 1e-6:
        return x
    target = min(0.89, peak * max(float(gain), 0.0))
    return x * (target / peak)


if __name__ == "__main__":
    import sys
    text = " ".join(sys.argv[1:]) or ("Oh! You're back. I finished going through those notes... "
                                      "a few things didn't add up, honestly. Want me to walk you through it?")
    tts = TTS()
    out = Path(config.VOICE_DIR) / "reedus_preview.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    tts.to_wav(text, str(out))
    print(f"wrote preview -> {out}")
    print(f"voice={tts.voice}  pitch=+{tts.pitch} st  range=x{tts.pitch_range}  robot={tts.robot}  sr={tts.sample_rate}")
