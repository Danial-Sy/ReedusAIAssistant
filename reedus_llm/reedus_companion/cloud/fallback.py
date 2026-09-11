"""cloud wrappers that announce the failure out loud, then fall back to local."""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional

from reedus_companion.cloud import CloudUnavailable

STALL_BRAIN = "Lost the cloud there, give me a second, I'm switching to local."
STALL_VOICE = "Switching my voice back to local, hang on."
STALL_VOICE_QUOTA = ("My cloud voice is out of credit, so I'm back on the local one. "
                     "You'll want to top up Inworld.")


class FallbackBackend:
    """Wraps a cloud Backend; degrades to a locally-built one on CloudUnavailable."""

    PROFILE: List[Dict] = []

    def __init__(self, cloud, make_local: Callable[[], object],
                 announce: Optional[Callable[[str], None]] = None):
        self._cloud = cloud
        self._make_local = make_local
        self._announce = announce
        self._local = None
        self._lock = threading.RLock()

    @property
    def degraded(self) -> bool:
        return self._local is not None

    @property
    def active(self):
        return self._local if self._local is not None else self._cloud

    def _fall_back(self, reason: str):
        with self._lock:
            if self._local is not None:
                return self._local
            print(f"[reedus] cloud unavailable ({reason}), falling back to local.",
                  flush=True)
            if self._announce is not None:
                try:
                    self._announce(STALL_BRAIN)
                except Exception:
                    pass
            t0 = time.perf_counter()
            self._local = self._make_local()
            print(f"[reedus] local brain up in {time.perf_counter() - t0:.1f}s", flush=True)
            try:
                self._cloud.close()
            except Exception:
                pass
            self._cloud = None
            return self._local

    def router_ready(self) -> bool:
        try:
            return bool(self.active.router_ready())
        except Exception:
            return False

    def chat(self, messages, max_tokens, **kw) -> str:
        if self._local is None:
            try:
                return self._cloud.chat(messages, max_tokens, **kw)
            except CloudUnavailable as e:
                self._fall_back(str(e))
        return self._local.chat(messages, max_tokens, **kw)

    def warm(self) -> float:
        target = self.active
        warm = getattr(target, "warm", None)
        if warm is None:
            return 0.0
        try:
            return float(warm() or 0.0)
        except CloudUnavailable as e:
            self._fall_back(str(e))
            warm = getattr(self._local, "warm", None)
            return float(warm() or 0.0) if warm else 0.0

    def close(self) -> None:
        for target in (self._cloud, self._local):
            try:
                if target is not None:
                    target.close()
            except Exception:
                pass


class FallbackTTS:
    """Wraps a cloud TTS; degrades to Kokoro on CloudUnavailable."""

    def __init__(self, cloud, make_local: Callable[[], object]):
        self._cloud = cloud
        self._make_local = make_local
        self._local = None
        self.sample_rate = getattr(cloud, "sample_rate", 24000)
        self._lock = threading.RLock()

    @property
    def degraded(self) -> bool:
        return self._local is not None

    def _fall_back(self, reason: str):
        with self._lock:
            if self._local is not None:
                return self._local
            print(f"[reedus] cloud voice unavailable ({reason}), falling back to Kokoro.",
                  flush=True)
            t0 = time.perf_counter()
            self._local = self._make_local()
            print(f"[reedus] local voice up in {time.perf_counter() - t0:.1f}s", flush=True)
            if getattr(self._local, "sample_rate", self.sample_rate) != self.sample_rate:
                # both are 24 kHz so this shouldn't happen, but a silent mismatch is chipmunk audio
                print(f"[reedus] WARNING: local voice is "
                      f"{self._local.sample_rate} Hz but the stream is open at "
                      f"{self.sample_rate} Hz", flush=True)
            self._cloud = None
            return self._local

    def synth(self, text: str):
        if self._local is None:
            try:
                return self._cloud.synth(text)
            except CloudUnavailable as e:
                quota = getattr(e, "provider", "") == "inworld-quota"
                target = self._fall_back(str(e))
                stall = STALL_VOICE_QUOTA if quota else STALL_VOICE
                try:
                    return target.synth(stall + " " + text)
                except Exception:
                    return target.synth(text)
        return self._local.synth(text)

    def warm(self) -> float:
        target = self._local if self._local is not None else self._cloud
        try:
            return float(getattr(target, "warm", lambda: 0.0)() or 0.0)
        except CloudUnavailable as e:
            return float(getattr(self._fall_back(str(e)), "warm", lambda: 0.0)() or 0.0)

    def speak(self, text: str) -> None:
        target = self._local if self._local is not None else self._cloud
        try:
            return target.speak(text)
        except CloudUnavailable as e:
            return self._fall_back(str(e)).speak(text)

    def to_wav(self, text: str, path: str) -> None:
        target = self._local if self._local is not None else self._cloud
        try:
            return target.to_wav(text, path)
        except CloudUnavailable as e:
            return self._fall_back(str(e)).to_wav(text, path)
