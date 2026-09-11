"""inworld realtime TTS-2, the cloud mode voice."""
from __future__ import annotations

import base64
import io
import os
import time
import wave
from typing import Iterator, List, Optional

import numpy as np

from reedus_companion import config
from reedus_companion.cloud import CloudUnavailable
from reedus_companion.cloud import budget
from reedus_companion.voice import voice_prefs
from reedus_companion.voice.speech_text import apply_pronunciations, clean_for_speech

_BASE = "https://api.inworld.ai"
_SYNTH = "/tts/v1/voice"
_STREAM = "/tts/v1/voice:stream"

_DEF_STYLE = ("warm and dry, a close friend who is amused by you; "
              "unhurried, natural pauses, never announcer-like")


class InworldTTS:
    """Cloud voice, api-compatible with voice.tts.TTS for StreamSpeaker."""

    def __init__(self, voice: Optional[str] = None, style: Optional[str] = None):
        key = (os.environ.get("INWORLD_API_KEY") or "").strip()
        if not key:
            raise CloudUnavailable(
                "INWORLD_API_KEY is not set. Add it to deploy/xeon.env "
                "(Inworld portal -> Runtime -> API Keys; it is a Base64 string).",
                provider="inworld", retryable=False)
        try:
            import requests
        except ImportError as e:
            raise CloudUnavailable("the requests package is missing",
                                   provider="inworld", retryable=False) from e

        self._requests = requests
        self.sample_rate = 24000                 # match kokoro exactly
        self.voice = voice or voice_prefs.inworld_voice()
        self.model = os.environ.get("REEDUS_INWORLD_MODEL", "inworld-tts-2")
        self.style = style if style is not None else os.environ.get(
            "REEDUS_INWORLD_STYLE", _DEF_STYLE)
        self._timeout = float(os.environ.get("REEDUS_INWORLD_TIMEOUT", "20"))
        try:
            self._rate = max(0.5, min(1.5, float(os.environ.get("REEDUS_INWORLD_RATE", "1.0"))))
        except ValueError:
            self._rate = 1.0
        self._delivery = os.environ.get("REEDUS_INWORLD_DELIVERY", "BALANCED").upper()

        # one keep-alive session, cold DNS was 238 ms on the first lookup
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Basic {key}",
            "Content-Type": "application/json",
        })
        self._steer = bool(self.style) and self.model.startswith("inworld-tts-2") \
            and not self.model.endswith("flash")
        print(f"[reedus] inworld tts: {self.model} voice={self.voice} "
              f"steer={'on' if self._steer else 'off'} ({voice_prefs.describe()})")


    def warm(self) -> float:
        t0 = time.perf_counter()
        try:
            self.synth("Ready.")
        except CloudUnavailable:
            raise
        except Exception:
            pass
        return time.perf_counter() - t0

    def synth(self, text: str):
        """text -> float32 mono @ self.sample_rate. Returns empty array for empty text."""
        spoken = self._prepare(text)
        if not spoken:
            return np.zeros(0, dtype=np.float32)
        payload = self._payload(spoken)
        data = self._post(_SYNTH, payload)
        b64 = data.get("audioContent") or ""
        if not b64:
            raise CloudUnavailable("inworld returned no audioContent",
                                   provider="inworld")
        audio = _pcm_from(base64.b64decode(b64), self.sample_rate)
        try:
            budget.record_audio(self.model, len(spoken),
                                audio.size / float(self.sample_rate))
        except Exception:
            pass
        return audio

    def synth_stream(self, text: str) -> Iterator[np.ndarray]:
        """Chunked synthesis. Not on the default path."""
        spoken = self._prepare(text)
        if not spoken:
            return
        import json
        resp = self._session.post(_BASE + _STREAM, json=self._payload(spoken),
                                  timeout=self._timeout, stream=True)
        self._raise_for(resp)
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            b64 = (chunk.get("result") or {}).get("audioContent") or ""
            if b64:
                yield _pcm_from(base64.b64decode(b64), self.sample_rate)
        try:
            budget.record_chars(self.model, len(spoken))
        except Exception:
            pass

    def speak(self, text: str) -> None:
        from reedus_companion.voice.tts import play_audio
        audio = self.synth(text)
        if audio.size:
            play_audio(audio, self.sample_rate, device=config.TTS_DEVICE)

    def to_wav(self, text: str, path: str) -> None:
        audio = self.synth(text)
        pcm = np.clip(audio, -1.0, 1.0)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes((pcm * 32767.0).astype("<i2").tobytes())


    def _prepare(self, text: str) -> str:
        try:
            cleaned = clean_for_speech(text or "")
            cleaned = apply_pronunciations(cleaned)
        except Exception:
            cleaned = (text or "").strip()
        return (cleaned or "").strip()

    def _payload(self, spoken: str) -> dict:
        body = {
            "text": spoken,
            "voiceId": self.voice,
            "modelId": self.model,
            "audioConfig": {
                "audioEncoding": "LINEAR16",
                "sampleRateHertz": self.sample_rate,
                "speakingRate": self._rate,
            },
        }
        if self._delivery in ("STABLE", "BALANCED", "CREATIVE"):
            body["deliveryMode"] = self._delivery
        if self._steer:
            body["description"] = self.style
        return body

    def _post(self, path: str, payload: dict) -> dict:
        try:
            resp = self._session.post(_BASE + path, json=payload,
                                      timeout=self._timeout)
        except Exception as e:                                   # noqa: BLE001
            raise CloudUnavailable(f"{type(e).__name__}: {e}",
                                   provider="inworld") from e
        if resp.status_code == 400 and self._steer and "description" in payload:
            print("[reedus] inworld rejected the steering field; "
                  "continuing without natural-language steering. "
                  "Check the field name in the Inworld docs and update "
                  "tts_inworld._payload.", flush=True)
            self._steer = False
            payload.pop("description", None)
            try:
                resp = self._session.post(_BASE + path, json=payload,
                                          timeout=self._timeout)
            except Exception as e:                               # noqa: BLE001
                raise CloudUnavailable(f"{type(e).__name__}: {e}",
                                       provider="inworld") from e
        self._raise_for(resp)
        try:
            return resp.json()
        except ValueError as e:
            raise CloudUnavailable("inworld returned non-JSON",
                                   provider="inworld") from e

    @staticmethod
    def _raise_for(resp) -> None:
        if resp.status_code < 400:
            return
        detail = ""
        try:
            detail = resp.text[:300]
        except Exception:
            pass

        low = detail.lower()
        quota_words = ("quota", "credit", "exhaust", "insufficient", "limit exceeded",
                       "billing", "payment", "upgrade", "trial", "subscription")
        is_quota = resp.status_code in (402, 403) or (
            resp.status_code == 429 and any(w in low for w in quota_words)) or (
            resp.status_code == 400 and any(w in low for w in quota_words))
        if is_quota:
            used = ""
            try:
                used = f" You have synthesised {budget.tts_minutes_used():.1f} minutes."
            except Exception:
                pass
            raise CloudUnavailable(
                f"INWORLD QUOTA/CREDIT EXHAUSTED (HTTP {resp.status_code}).{used} "
                f"The free trial is spent, upgrade the plan at "
                f"platform.inworld.ai, then set REEDUS_INWORLD_FREE_MINUTES=0. "
                f"Falling back to Kokoro. [{detail[:120]}]",
                provider="inworld-quota", retryable=False)
        if resp.status_code in (401,):
            raise CloudUnavailable(
                f"INWORLD KEY REJECTED (401), check INWORLD_API_KEY in "
                f"deploy/xeon.secrets.env. It is the Base64 string from "
                f"Runtime -> API Keys. [{detail[:120]}]",
                provider="inworld", retryable=False)

        retryable = resp.status_code in (408, 429, 500, 502, 503, 504)
        raise CloudUnavailable(f"inworld HTTP {resp.status_code}: {detail}",
                               provider="inworld", retryable=retryable)


def _pcm_from(raw: bytes, expected_rate: int) -> np.ndarray:
    """Decode LINEAR16 to float32 [-1, 1]."""
    if raw[:4] == b"RIFF":
        with wave.open(io.BytesIO(raw), "rb") as w:
            frames = w.readframes(w.getnframes())
            width, channels = w.getsampwidth(), w.getnchannels()
            rate = w.getframerate()
    else:
        frames, width, channels, rate = raw, 2, 1, expected_rate

    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(width, np.int16)
    audio = np.frombuffer(frames, dtype=dtype).astype(np.float32)
    if audio.size == 0:
        return np.zeros(0, dtype=np.float32)
    audio /= float(np.iinfo(dtype).max)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != expected_rate:
        from reedus_companion.voice.tts import _resample_linear
        audio = _resample_linear(audio, rate, expected_rate)
    return np.ascontiguousarray(audio, dtype=np.float32)
