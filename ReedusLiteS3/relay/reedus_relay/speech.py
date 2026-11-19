"""audio in and out of the relay.

    Endpointer   when did they start and stop talking (energy, adaptive noise floor)
    transcribe   speech to text: Gemini (audio input, the same key as the brain) or OpenAI
    Voice        Inworld, streamed and resampled to the device's 16 kHz 20 ms frames

everything here is blocking; session.py runs it in worker threads.
"""
from __future__ import annotations

import io
import math
import os
import wave
from typing import Iterator, Optional

import numpy as np

from reedus_relay import config

FRAME_BYTES = config.FRAME_SAMPLES * 2
FRAME_S = config.FRAME_SAMPLES / config.RATE


# ---------- endpointing ----------
class Endpointer:
    """Feed 20 ms int16 frames; get 'start' once speech begins and 'end' / 'timeout' / 'max'."""

    def __init__(self, start_timeout: float = config.START_TIMEOUT_S,
                 end_silence: float = config.END_SILENCE_S,
                 max_len: float = config.MAX_UTTERANCE_S, noise_db: float = -60.0):
        self.start_timeout, self.end_silence, self.max_len = start_timeout, end_silence, max_len
        self.noise = noise_db
        self.t = 0.0
        self.started = False
        self.voiced_run = 0
        self.silence = 0.0
        self.speech_t = 0.0
        self.frames: list = []

    @staticmethod
    def level_db(pcm: bytes) -> float:
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if x.size == 0:
            return -120.0
        return 20.0 * math.log10(max(1e-6, float(np.sqrt(np.mean(x * x)))))

    def threshold(self) -> float:
        return max(self.noise + 12.0, -48.0)

    def feed(self, pcm: bytes) -> Optional[str]:
        self.t += FRAME_S
        db = self.level_db(pcm)
        voiced = db > self.threshold()
        if not voiced:
            # the floor follows the room down fast and up slowly
            self.noise = db if db < self.noise else self.noise + 0.02 * (db - self.noise)
        if self.started or voiced:
            self.frames.append(pcm)
        if not self.started:
            self.voiced_run = self.voiced_run + 1 if voiced else 0
            if self.voiced_run >= 3:
                self.started = True
                self.frames = self.frames[-6:]          # keep the onset
                return "start"
            if self.t >= self.start_timeout:
                return "timeout"
            if not voiced:
                self.frames = self.frames[-5:]
            return None
        self.speech_t += FRAME_S
        self.silence = 0.0 if voiced else self.silence + FRAME_S
        if self.silence >= self.end_silence:
            return "end"
        if self.speech_t >= self.max_len:
            return "max"
        return None

    def audio(self) -> bytes:
        return b"".join(self.frames)


def wav_bytes(pcm: bytes, rate: int = config.RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


# ---------- speech to text ----------
_STT_PROMPT = ("Transcribe exactly what the speaker says, in their language. Output only the "
               "words, no quotes, no labels. If there is no intelligible speech, output nothing.")
# USD per minute of audio for the transcription-only models (list prices, estimates)
_STT_PER_MIN = {"gpt-4o-mini-transcribe": 0.003, "gpt-4o-transcribe": 0.006, "whisper-1": 0.006}

_clients: dict = {}


def stt_provider() -> str:
    return (config.env("RELAY_STT", "gemini") or "gemini").lower()


def transcribe(pcm: bytes, provider: Optional[str] = None) -> str:
    """Text of what was said, '' if nothing. Costs land on the current meter."""
    provider = provider or stt_provider()
    if len(pcm) < FRAME_BYTES * 10:          # under 200 ms is a cough, not a sentence
        return ""
    if provider == "openai":
        return _openai_stt(pcm)
    return _gemini_stt(pcm)


def _gemini_stt(pcm: bytes) -> str:
    from google import genai
    from google.genai import types
    from reedus_companion.cloud import budget, llm_settings
    key = llm_settings.key("gemini")
    if not key:
        raise RuntimeError("speech-to-text needs GEMINI_API_KEY (or set RELAY_STT=openai)")
    client = _clients.get(("gemini", key))
    if client is None:
        client = _clients[("gemini", key)] = genai.Client(api_key=key)
    model = config.env("RELAY_STT_MODEL", "gemini-3.7-flash")
    contents = [types.Part.from_bytes(data=wav_bytes(pcm), mime_type="audio/wav"), _STT_PROMPT]
    try:
        # gemini 3 takes thinking_level (low is its floor); transcription needs no thinking
        resp = client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(max_output_tokens=256,
                                               thinking_config=types.ThinkingConfig(thinking_level="low")))
    except Exception as e:                                       # noqa: BLE001
        if "thinking" not in str(e).lower():
            raise
        resp = client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(max_output_tokens=256))
    text = (getattr(resp, "text", "") or "").strip()
    u = getattr(resp, "usage_metadata", None)
    n_in = int(getattr(u, "prompt_token_count", 0) or 0) or int(len(pcm) / 2 / config.RATE * 32) + 40
    n_out = int(getattr(u, "candidates_token_count", 0) or 0) or len(text) // 4
    budget.record_tokens(model, n_in, n_out)
    return text


def _openai_stt(pcm: bytes) -> str:
    import openai
    from reedus_companion.cloud import llm_settings
    from reedus_relay import meter
    key = llm_settings.key("openai")
    if not key:
        raise RuntimeError("RELAY_STT=openai needs OPENAI_API_KEY")
    client = _clients.get(("openai", key))
    if client is None:
        client = _clients[("openai", key)] = openai.OpenAI(api_key=key, max_retries=1, timeout=20)
    model = config.env("RELAY_STT_MODEL", "gpt-4o-mini-transcribe")
    out = client.audio.transcriptions.create(model=model, file=("speech.wav", wav_bytes(pcm), "audio/wav"))
    m = meter.current()
    if m is not None:
        m.add(len(pcm) / 2 / config.RATE / 60.0 * _STT_PER_MIN.get(model, 0.006), model)
    return (getattr(out, "text", "") or "").strip()


# ---------- voice ----------
class Voice:
    """Inworld (reedus_companion.voice.tts_inworld), resampled to the device's frames."""

    def __init__(self, tts=None):
        if tts is None:
            from reedus_companion.voice.tts_inworld import InworldTTS
            tts = InworldTTS()
        self.tts = tts
        self.rate = int(getattr(tts, "sample_rate", 24000))

    def frames(self, text: str) -> Iterator[bytes]:
        """640-byte 16 kHz int16 frames for `text`, as they're synthesised."""
        from reedus_companion.lite.resample import Resampler, to_int16
        rs = Resampler(self.rate, config.RATE)
        carry = b""
        stream = self.tts.synth_stream(text) if hasattr(self.tts, "synth_stream") \
            else iter([self.tts.synth(text)])
        for chunk in stream:
            pcm = carry + to_int16(rs.feed(np.asarray(chunk, dtype=np.float32)))
            n = len(pcm) // FRAME_BYTES * FRAME_BYTES
            for i in range(0, n, FRAME_BYTES):
                yield pcm[i:i + FRAME_BYTES]
            carry = pcm[n:]
        if carry:
            yield carry + b"\x00" * (FRAME_BYTES - len(carry))


def tone(freq: float = 880.0, secs: float = 0.12, level: float = 0.25) -> bytes:
    """A short beep at the wire rate, for 'I heard you' and errors."""
    n = int(config.RATE * secs)
    t = np.arange(n) / config.RATE
    env = np.minimum(1.0, np.minimum(t, secs - t) / 0.01)
    x = level * env * np.sin(2 * np.pi * freq * t)
    return (x * 32767).astype("<i2").tobytes()


def wake_detector():
    """The 'hey reedus' detector, or a stand-in chosen by RELAY_WAKE (see wake.py)."""
    from reedus_relay.wake import make
    return make(os.environ.get("RELAY_WAKE", "model"))
