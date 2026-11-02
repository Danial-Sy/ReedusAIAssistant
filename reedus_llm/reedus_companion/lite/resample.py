"""streaming polyphase resampler, numpy only.

PlaybackStream's linear interp is fine for a DAC that takes 48k, but 24k -> 16k with no
lowpass folds Inworld's 8-12 kHz sibilants back down as hiss. this one filters first and
keeps state across chunks so sentence joins don't click.
"""
from __future__ import annotations

from math import gcd

import numpy as np


def _design(L: int, M: int, taps_per_phase: int) -> np.ndarray:
    """Kaiser-windowed sinc lowpass at the lower Nyquist, gain L (makes up for zero-stuffing)."""
    n = taps_per_phase * L
    cutoff = 0.92 / max(L, M)                   # fraction of the upsampled Nyquist
    t = np.arange(n) - (n - 1) / 2.0
    h = cutoff * np.sinc(cutoff * t) * np.kaiser(n, 8.0)
    return (h / h.sum() * L).astype(np.float32)


class Resampler:
    """feed() float32 chunks at src, get float32 at dst. Stateful."""

    def __init__(self, src: int, dst: int, taps_per_phase: int = 24):
        self.src, self.dst = int(src), int(dst)
        g = gcd(self.src, self.dst)
        self.L, self.M = self.dst // g, self.src // g
        self.passthrough = self.L == self.M
        self.T = taps_per_phase
        h = _design(self.L, self.M, self.T)
        # phase p, tap i -> h[p + i*L]; output n reads x[k0 - i]
        self.H = np.stack([h[p::self.L][:self.T] for p in range(self.L)]).astype(np.float32)
        self.reset()

    def reset(self) -> None:
        self._hist = np.zeros(self.T - 1, dtype=np.float32)
        self._consumed = 0          # input samples before _hist[0] + len(_hist)
        self._n = 0                 # next output index

    def feed(self, x) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if self.passthrough:
            return x
        if x.size == 0:
            return x
        buf = np.concatenate([self._hist, x])
        base = self._consumed - (self.T - 1)    # absolute index of buf[0]
        total = self._consumed + x.size         # inputs seen so far
        # output n needs input k0 = (n*M)//L to exist: n*M < total*L
        n_end = (total * self.L + self.M - 1) // self.M
        if n_end <= self._n:
            self._roll(buf, total)
            return np.zeros(0, dtype=np.float32)
        n = np.arange(self._n, n_end, dtype=np.int64)
        t = n * self.M
        k0 = t // self.L
        p = t % self.L
        idx = (k0 - base)[:, None] - np.arange(self.T)[None, :]
        vals = np.where(idx >= 0, buf[np.clip(idx, 0, buf.size - 1)], 0.0)
        y = np.einsum("ij,ij->i", vals, self.H[p]).astype(np.float32)
        self._n = n_end
        self._roll(buf, total)
        return y

    def _roll(self, buf: np.ndarray, total: int) -> None:
        self._hist = buf[-(self.T - 1):].copy() if self.T > 1 else np.zeros(0, np.float32)
        self._consumed = total


def to_int16(x) -> bytes:
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def from_int16(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="<i2").astype(np.float32) / 32768.0
