"""a stand-in for the `sounddevice` module that talks to Reedus Lite instead of a sound card.

install(hub) puts it in sys.modules["sounddevice"]. every `import sounddevice as sd` in the
voice stack is inside a function, so the wake listener, both barge watchers, jingles and
PlaybackStream all land here without a line of them changing. only what they use exists.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Optional

import numpy as np

from reedus_companion.lite import hub as hub_mod
from reedus_companion.lite import protocol as P
from reedus_companion.lite.resample import Resampler

_HUB = None

# what PlaybackStream asks the "DAC" for. 24k = kokoro and inworld's native rate, so
# PlaybackStream passes speech through untouched and the proper filter here does 24k -> 16k
OUTPUT_RATE = 24000
_PIECE_S = 0.04


class PortAudioError(Exception):
    pass


class CallbackFlags:
    def __init__(self, input_overflow: bool = False):
        self.input_overflow = input_overflow
        self.output_underflow = False

    def __bool__(self) -> bool:
        return self.input_overflow


class _Default:
    device = [0, 0]
    samplerate = None
    channels = None
    dtype = None
    latency = None


default = _Default()

_DEVICE = {
    "name": "reedus-lite (network)",
    "index": 0,
    "hostapi": 0,
    "max_input_channels": 1,
    "max_output_channels": 1,
    "default_samplerate": float(OUTPUT_RATE),
    "default_low_input_latency": 0.02,
    "default_low_output_latency": 0.02,
    "default_high_input_latency": 0.1,
    "default_high_output_latency": 0.12,
}


class _DeviceList(list):
    def __str__(self) -> str:
        return "\n".join(f"  {d['index']} {d['name']}, 1 in, 1 out" for d in self)


def query_devices(device=None, kind=None):
    if device is None and kind is None:
        return _DeviceList([dict(_DEVICE)])
    return dict(_DEVICE)


def check_input_settings(**_kw):
    return None


def check_output_settings(**_kw):
    return None


def _hub():
    if _HUB is None:
        raise PortAudioError("Reedus Lite hub not started (lite.netaudio.install)")
    return _HUB


def _shape(x: np.ndarray, channels: int, dtype: str) -> np.ndarray:
    if dtype in ("int16", np.int16):
        x = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)
    out = x.reshape(-1, 1)
    if channels > 1:
        out = np.repeat(out, channels, axis=1)
    return out


class _StreamBase:
    def __init__(self, samplerate=None, blocksize=None, device=None, channels=1,
                 dtype="float32", latency=None, callback=None, finished_callback=None,
                 **_kw):
        self.samplerate = float(samplerate or P.RATE)
        self.blocksize = int(blocksize or 0)
        self.device = device
        self.channels = int(channels or 1)
        self.dtype = dtype or "float32"
        self.callback = callback
        self.finished_callback = finished_callback
        self.active = False
        self.closed = False
        self.latency = 0.02

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        self.close()
        return False

    @property
    def stopped(self) -> bool:
        return not self.active


class InputStream(_StreamBase):
    """Reads what the Lite's mics hear. Blocking read() or a callback thread, like portaudio."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sub = None
        self._rs = None if int(self.samplerate) == P.RATE else Resampler(P.RATE, int(self.samplerate))
        self._pending = np.zeros(0, dtype=np.float32)
        self._cb_thread: Optional[threading.Thread] = None
        self._cb_stop = threading.Event()

    def start(self) -> None:
        if self.active:
            return
        if self._sub is None:
            self._sub = _hub().subscribe()
        self.active = True
        if self.callback is not None:
            self._cb_stop.clear()
            self._cb_thread = threading.Thread(target=self._cb_loop, daemon=True)
            self._cb_thread.start()

    def stop(self) -> None:
        self.active = False
        self._cb_stop.set()
        t = self._cb_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._cb_thread = None

    abort = stop

    def close(self) -> None:
        self.stop()
        if self._sub is not None:
            _hub().unsubscribe(self._sub)
            self._sub = None
        self.closed = True

    @property
    def read_available(self) -> int:
        return 0

    def _pull(self, frames: int) -> tuple[np.ndarray, bool]:
        """`frames` samples at our rate. Paces in real time even with no device attached."""
        if self._sub is None:
            self.start()
        overflow = False
        while self._pending.size < frames:
            need = frames - self._pending.size
            src_n = need if self._rs is None else int(np.ceil(need * P.RATE / self.samplerate)) + 1
            t0 = time.monotonic()
            x, of = self._sub.read(src_n, timeout=src_n / P.RATE + 0.25)
            overflow = overflow or of
            if not _hub().connected:
                # no device: hand back silence at the rate a real mic would
                spent = time.monotonic() - t0
                wait = src_n / P.RATE - spent
                if wait > 0:
                    time.sleep(wait)
            if self._rs is not None:
                x = self._rs.feed(x)
            self._pending = np.concatenate([self._pending, x])
        out, self._pending = self._pending[:frames], self._pending[frames:]
        return out, overflow

    def read(self, frames: int):
        x, of = self._pull(int(frames))
        return _shape(x, self.channels, self.dtype), of

    def _cb_loop(self) -> None:
        block = self.blocksize or int(self.samplerate * 0.05)
        while not self._cb_stop.is_set():
            x, of = self._pull(block)
            if self._cb_stop.is_set():
                break
            try:
                self.callback(_shape(x, self.channels, self.dtype), block, None,
                              CallbackFlags(input_overflow=of))
            except Exception:                                    # noqa: BLE001
                break


class OutputStream(_StreamBase):
    """Writes speech to the Lite's speaker, paced to its playhead so stop() means silence."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._rs = None if int(self.samplerate) == P.RATE else Resampler(int(self.samplerate), P.RATE)
        self._gen = None

    def start(self) -> None:
        self.active = True
        self._gen = _hub().gen

    def write(self, data) -> bool:
        hub = _hub()
        if not self.active:
            self.start()
        x = np.asarray(data)
        if x.dtype == np.int16:
            x = x.astype(np.float32) / 32768.0
        x = x.astype(np.float32)
        if x.ndim > 1:
            x = x.mean(axis=1)
        if self._rs is not None:
            x = self._rs.feed(x)
        piece = int(P.RATE * _PIECE_S)
        for i in range(0, x.size, piece):
            while hub.ahead() > hub_mod.LEAD_S:
                if hub.gen != self._gen:
                    return False
                time.sleep(0.01)
            if hub.gen != self._gen:
                return False                # barge-in flushed us, drop the rest
            hub.queue_speech(x[i:i + piece], gen=self._gen)
        return False

    def stop(self) -> None:
        """Like portaudio: returns once what was written has played."""
        if not self.active:
            return
        hub = _hub()
        deadline = time.monotonic() + 15.0
        while hub.ahead() > 0 and hub.gen == self._gen and time.monotonic() < deadline:
            time.sleep(0.01)
        self.active = False

    def abort(self) -> None:
        _hub().flush()
        self.active = False

    def close(self) -> None:
        self.stop()
        self.closed = True


def install(hub) -> None:
    """Route every `import sounddevice` to the Lite."""
    global _HUB
    _HUB = hub
    sys.modules["sounddevice"] = sys.modules[__name__]
