"""light robotization DSP: chorus, bitcrush, soft clip."""
from __future__ import annotations
import math


def robotize(samples, sample_rate: int, amount: float = 0.15,
             chorus_hz: float = 0.7, bits: float = 7.0):
    """samples: 1-D float32 numpy array in [-1,1]. Returns processed float32 array."""
    import numpy as np
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    amount = float(max(0.0, min(1.0, amount)))
    if amount <= 0.0 or x.size == 0:
        return x

    n = x.size
    t = np.arange(n, dtype=np.float32) / float(sample_rate)

    # 1) chorus: a doubled copy through a slowly modulated ~8-16 ms delay. no feedback and a
    #    longish delay keeps it synthetic instead of metallic
    center, depth = 0.012, 0.004
    delay = (center + depth * np.sin(2.0 * math.pi * chorus_hz * t)) * float(sample_rate)
    read = np.arange(n, dtype=np.float32) - delay
    read = np.clip(read, 0.0, n - 1.0)
    i0 = np.floor(read).astype(np.int64)
    i1 = np.clip(i0 + 1, 0, n - 1)
    frac = (read - i0).astype(np.float32)
    delayed = x[i0] * (1.0 - frac) + x[i1] * frac
    chorus = 0.55 * x + 0.55 * delayed

    levels = float(2 ** int(max(2, bits)))
    crush = np.round(chorus * levels) / levels
    wet = 0.6 * chorus + 0.4 * crush

    # 3) gentle soft clip to glue it together
    wet = np.tanh(1.2 * wet).astype(np.float32)

    # blend dry/wet
    out = (1.0 - amount) * x + amount * wet

    # normalize to avoid clipping
    peak = float(np.max(np.abs(out))) or 1.0
    if peak > 1.0:
        out = out / peak
    return out.astype(np.float32)
