"""barge-in while reedus is speaking or thinking."""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Deque, List, Optional

import numpy as np

from reedus_companion import config


def _dump_wav(tag: str, chunks, sr: int = 16000) -> None:
    """Dump what a watcher heard to REEDUS_BARGE_DUMP, if set."""
    out = (os.environ.get("REEDUS_BARGE_DUMP") or "").strip()
    if not out or not chunks:
        return
    try:
        import wave
        os.makedirs(out, exist_ok=True)
        audio = np.concatenate([np.asarray(c, dtype=np.float32).reshape(-1) for c in chunks])
        if audio.size == 0:
            return
        path = os.path.join(out, f"{tag}-{int(time.time())}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        _dbg(f"wrote {path} ({audio.size / float(sr):.1f}s), play it to hear what the "
             f"detector was given")
    except Exception as e:                                       # noqa: BLE001
        _dbg(f"dump failed: {type(e).__name__}: {e}")


def _speak_barge_wakeword() -> bool:
    """Does speak-barge require the wake word. On by default."""
    return os.environ.get("REEDUS_BARGE_WAKEWORD", "1") != "0"


def _detector():
    """The wake-word detector, or None to keep the energy gate."""
    try:
        from reedus_companion.voice.wakeword import shared_detector
        det = shared_detector()
        return det if det.available else None
    except Exception:                                            # noqa: BLE001
        return None


def _dbg_on() -> bool:
    return os.environ.get("REEDUS_DEBUG", "0") != "0"


def _dbg(msg: str) -> None:
    """Think-barge diagnostics, on with REEDUS_BARGE_DEBUG. A silent mic failure used to look like nothing."""
    if _dbg_on():
        print(f"\033[2m  [{msg}]\033[0m", flush=True)


def barge_enabled() -> bool:
    return os.environ.get("REEDUS_BARGE", "1") != "0"


def think_barge_enabled() -> bool:
    return os.environ.get("REEDUS_THINK_BARGE", "1") != "0"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_ms(name: str, default_ms: float) -> float:
    return max(0.0, _env_float(name, default_ms)) / 1000.0


def barge_thresh_from_bleed(bleed_rms: float,
                            floor: Optional[float] = None,
                            mult: Optional[float] = None,
                            margin: Optional[float] = None) -> float:
    """Adaptive speak-barge threshold from measured speaker->mic bleed."""
    floor = _env_float("REEDUS_BARGE_RMS_MIN", 0.038) if floor is None else floor
    mult = _env_float("REEDUS_BARGE_BLEED_MULT", 1.55) if mult is None else mult
    margin = _env_float("REEDUS_BARGE_MARGIN", 0.012) if margin is None else margin
    bleed = max(0.0, float(bleed_rms))
    return max(floor, bleed * mult + margin)


class BargeWatcher:
    """Background mic watch during TTS. Call start() before finish(), stop() after."""

    def __init__(self, speaker, on_barge=None):
        self.speaker = speaker
        self.on_barge = on_barge
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.audio: Optional[np.ndarray] = None
        self.hit = False
        self.bleed_rms = 0.0
        self.thresh = 0.0
        self.wake_thresh = 0.0
        self.peak_wake = 0.0

    def start(self) -> None:
        if not barge_enabled() or self.speaker is None:
            return
        self._stop.clear()
        self.hit = False
        self.audio = None
        self.bleed_rms = 0.0
        self.thresh = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> Optional[np.ndarray]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if not self.hit and self.wake_thresh and _dbg_on():
            _dbg(f"speak barge: no wake word over his voice, peak score "
                 f"{self.peak_wake:.3f} vs speak-thresh {self.wake_thresh:.2f}"
                 + ("  -> the bleed is burying it; needs echo cancellation (pipewire echo-cancel module)"
                    if self.peak_wake < self.wake_thresh * 0.4 else
                    "  -> close; lower REEDUS_WAKEWORD_THRESHOLD_SPEAK"))
        return self.audio if self.hit else None

    def _wait_for_playback(self, timeout_s: float = 8.0) -> bool:
        """Don't arm on pre-audio hush, wait until the DAC is actually open."""
        playing = getattr(self.speaker, "playing", None)
        deadline = time.monotonic() + timeout_s
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                if callable(playing) and playing():
                    return True
            except Exception:
                return True
            time.sleep(0.04)
        return not self._stop.is_set()

    def _measure_bleed(self, stream, block: int, seconds: float = 0.35) -> float:
        """p80 of mic RMS while TTS is playing = speaker bleed baseline."""
        n = max(4, int(seconds / 0.05))
        samples: List[float] = []
        for _ in range(n):
            if self._stop.is_set():
                break
            data, _ = stream.read(block)
            if data is None or len(data) == 0:
                continue
            chunk = np.asarray(data, dtype=np.float32).reshape(-1)
            if chunk.size:
                samples.append(float(np.sqrt(np.mean(chunk * chunk))))
        if not samples:
            return 0.0
        samples.sort()
        return samples[int(0.8 * (len(samples) - 1))]

    def _run(self) -> None:
        try:
            import sounddevice as sd
        except Exception:
            return
        sr = int(config.MIC_SAMPLE_RATE)
        block = int(sr * 0.05)
        hold_s = _env_ms("REEDUS_BARGE_HOLD_MS", 160)
        ignore_s = _env_ms("REEDUS_BARGE_IGNORE_MS", 450)
        need_hit = max(2, int(hold_s / 0.05))
        legacy = os.environ.get("REEDUS_BARGE_RMS")
        if legacy and "REEDUS_BARGE_RMS_MIN" not in os.environ:
            os.environ.setdefault("REEDUS_BARGE_RMS_MIN", legacy)

        if not self._wait_for_playback():
            return
        time.sleep(ignore_s)
        if self._stop.is_set():
            return
        buf: list = []
        speaking = False
        hit_blocks = 0
        silent_blocks = 0
        need_silent = max(1, int(0.85 / 0.05))
        pre_max = max(1, int(_env_float("REEDUS_BARGE_PREROLL", 0.15) / 0.05))
        try:
            with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                                blocksize=block, device=config.MIC_DEVICE) as stream:
                det = _detector() if _speak_barge_wakeword() else None
                if det is not None:
                    det.reset()
                    thresh = self.thresh = 0.0
                    self.wake_thresh = _env_float("REEDUS_WAKEWORD_THRESHOLD_SPEAK",
                                                  max(0.15, det.threshold * 0.6))
                    _dbg(f"speak barge armed on the WAKE WORD ({det.describe()}) "
                         f"speak-thresh={self.wake_thresh:.2f}")
                else:
                    self.bleed_rms = self._measure_bleed(stream, block)
                    self.thresh = barge_thresh_from_bleed(self.bleed_rms)
                    thresh = self.thresh
                    _dbg(f"speak barge armed on ENERGY bleed={self.bleed_rms:.3f} "
                         f"thresh={thresh:.3f}")
                while not self._stop.is_set():
                    data, _ = stream.read(block)
                    if data is None or len(data) == 0:
                        continue
                    chunk = np.asarray(data, dtype=np.float32).reshape(-1)
                    rms = float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0
                    if not speaking:
                        # name-triggered where a wake model exists: it costs ~5% of a core so it can
                        # just run, and speaker bleed stops mattering because his own voice never says his name
                        if det is not None:
                            if not det.feed(chunk, threshold=self.wake_thresh):
                                # pre-roll is tiny on purpose: every block here was recorded while he was
                                # talking, and a 2s buffer got whisper transcribing HIM. keep just enough
                                # not to clip the name's onset.
                                self.peak_wake = max(self.peak_wake, det.peak_score)
                                buf.append(chunk)
                                if len(buf) > pre_max:
                                    buf = buf[-pre_max:]
                                continue
                            speaking = True
                            silent_blocks = 0
                            try:
                                self.speaker.stop()
                            except Exception:
                                pass
                            self.hit = True
                            if self.on_barge:
                                try:
                                    self.on_barge()
                                except Exception:
                                    pass
                            continue
                        if rms >= thresh:
                            hit_blocks += 1
                            buf.append(chunk)
                            if hit_blocks >= need_hit:
                                speaking = True
                                silent_blocks = 0
                                try:
                                    self.speaker.stop()
                                except Exception:
                                    pass
                                self.hit = True
                                if self.on_barge:
                                    try:
                                        self.on_barge()
                                    except Exception:
                                        pass
                        else:
                            hit_blocks = 0
                            buf = []
                        continue
                    buf.append(chunk)
                    if rms < max(0.012, thresh * 0.4):
                        silent_blocks += 1
                        if silent_blocks >= need_silent:
                            break
                    else:
                        silent_blocks = 0
                    if sum(len(b) for b in buf) > sr * 12:
                        break
        except Exception:
            return
        if self.hit and buf:
            self.audio = np.concatenate(buf)
            _dbg(f"speak barge captured {self.audio.size / float(sr):.1f}s "
                 f"({pre_max} blocks pre-roll)")


class _MicTap:
    """Non-blocking mic tap, portaudio pushes blocks from its own thread."""

    def __init__(self, sr: int, block: int, device, seconds: float = 30.0):
        self.sr, self.block, self.device = sr, block, device
        self.q: Deque = deque(maxlen=max(8, int(seconds / 0.05)))
        self.overflows = 0
        self._stream = None

    def __enter__(self):
        import sounddevice as sd

        def _cb(indata, frames, time_info, status):          # noqa: ANN001
            if status:
                try:
                    if status.input_overflow:
                        self.overflows += 1
                except Exception:                            # noqa: BLE001
                    pass
            try:
                self.q.append(np.asarray(indata[:, 0], dtype=np.float32).copy())
            except Exception:                                # noqa: BLE001
                pass

        self._stream = sd.InputStream(samplerate=self.sr, channels=1, dtype="float32",
                                      blocksize=self.block, device=self.device, callback=_cb)
        self._stream.start()
        return self

    def __exit__(self, *exc):
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:                                    # noqa: BLE001
            pass
        return False

    def get(self, timeout: float = 0.5):
        """Next block, or None if nothing arrived within `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            if self.q:
                return self.q.popleft()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.005)


class ThinkCancelWatcher:
    """Barge during persona generation: sets `cancel` and keeps recording the new utterance."""

    def __init__(self, cancel: threading.Event, on_hit=None, speaker=None):
        self.cancel = cancel
        self.on_hit = on_hit
        self.speaker = speaker
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.hit = False
        self.audio: Optional[np.ndarray] = None
        self.armed = False
        self.thresh = 0.0
        self.peak_rms = 0.0
        self.open_blocks = 0      # blocks the gate was actually listening on
        self.busy_blocks = 0      # blocks skipped because the speaker was live
        self.overflows = 0        # mic blocks the OS dropped before we saw them

    @staticmethod
    def capture_enabled() -> bool:
        return os.environ.get("REEDUS_THINK_BARGE_CAPTURE", "1") != "0"

    @staticmethod
    def capture_max_sec() -> float:
        return max(1.0, _env_float("REEDUS_THINK_BARGE_MAX_SEC", 12.0))

    def start(self) -> None:
        if not think_barge_enabled() or self.cancel is None:
            return
        self._stop.clear()
        self.hit = False
        self.audio = None
        self.armed = False
        self.peak_rms = 0.0
        self.open_blocks = self.busy_blocks = self.overflows = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        self._stop.set()
        if self._thread is not None:
            # a hit means the thread is capturing the replacement utterance; waiting here IS the
            # listen that would happen next, cutting it short loses the turn
            timeout = (min(self.capture_max_sec() + 2.0, 20.0)
                       if (self.hit and self.capture_enabled()) else 1.5)
            self._thread.join(timeout=timeout)
            self._thread = None
        self._report()
        return self.hit

    def _report(self) -> None:
        """Say why nothing happened, with numbers you can act on."""
        if self.hit or not think_barge_enabled():
            return
        if not self.armed:
            _dbg("think barge never armed this turn (mic not opened, see the error above, "
                 "or the turn ended inside REEDUS_THINK_BARGE_DELAY)")
            return
        if self.open_blocks == 0:
            _dbg(f"think barge armed but heard nothing, the speaker was live for all "
                 f"{self.busy_blocks} blocks")
            return
        hint = ""
        if self.overflows:
            hint += (f"  -> {self.overflows} mic blocks DROPPED (CPU starved), "
                     f"lower REEDUS_THREADS")
        det = _detector()
        if det is not None:
            near = det.peak_score >= det.threshold * 0.5
            verdict = ("close, but under threshold" if near
                       else "the wake word was not heard")
            if near:
                hint += "  -> lower REEDUS_WAKEWORD_THRESHOLD"
            _dbg(f"think barge: {verdict} - peak wake score {det.peak_score:.3f} vs "
                 f"thresh {det.threshold:.2f} over {self.open_blocks} blocks "
                 f"({self.busy_blocks} while speaking){hint}")
            return
        verdict = "no speech" if self.peak_rms < self.thresh * 0.6 else "PEAK BELOW THRESHOLD"
        if self.peak_rms >= self.thresh * 0.6:
            hint += "  -> lower REEDUS_THINK_BARGE_RMS"
        _dbg(f"think barge: {verdict} - peak rms {self.peak_rms:.3f} vs thresh "
             f"{self.thresh:.3f} over {self.open_blocks} blocks "
             f"({self.busy_blocks} skipped as speaker-busy){hint}")

    def _speaker_busy(self) -> bool:
        """True while an ack / jingle / TTS is on the wire, the mic hears that as voice."""
        if self.speaker is None:
            return False
        speaking = getattr(self.speaker, "speaking", None)
        if callable(speaking):
            try:
                return bool(speaking())
            except Exception:
                return False
        playing = getattr(self.speaker, "playing", None)
        try:
            if callable(playing) and playing():
                return True
        except Exception:
            pass
        try:
            from reedus_companion.voice.tts import _OUTPUT_LOCK
            if _OUTPUT_LOCK.acquire(blocking=False):
                _OUTPUT_LOCK.release()
                return False
            return True
        except Exception:
            return False

    def _measure_ambient(self, tap: "_MicTap", seconds: float = 0.30) -> float:
        """Quiet floor of the room right now, 20th percentile not the mean."""
        n = max(4, int(seconds / 0.05))
        vals: List[float] = []
        for _ in range(n):
            if self._stop.is_set() or self.cancel.is_set():
                break
            chunk = tap.get(timeout=0.2)
            if chunk is None or not chunk.size:
                continue
            vals.append(float(np.sqrt(np.mean(chunk * chunk))))
        if not vals:
            return 0.0
        vals.sort()
        return vals[int(0.2 * (len(vals) - 1))]

    def _run(self) -> None:
        try:
            import sounddevice as sd                          # noqa: F401
        except Exception as e:                                # noqa: BLE001
            _dbg(f"think barge OFF - sounddevice unavailable: {type(e).__name__}: {e}")
            return
        sr = int(config.MIC_SAMPLE_RATE)
        block = int(sr * 0.05)
        floor = _env_float("REEDUS_THINK_BARGE_RMS", 0.035)
        det = _detector()
        # the 0.8s arm delay stops the tail of your own command cancelling the turn it started.
        # in detector mode that tail can't trigger the wake word, so the delay is just deafness.
        time.sleep(_env_float("REEDUS_THINK_BARGE_DELAY", 0.8) if det is None else 0.15)
        if self._stop.is_set() or self.cancel.is_set():
            return
        window = max(4, int(_env_float("REEDUS_THINK_BARGE_WINDOW", 0.35) / 0.05))
        need = max(3, int(_env_float("REEDUS_THINK_BARGE_HOLD", 0.20) / 0.05))
        recent: Deque = deque(maxlen=window)
        preroll_n = max(1, int(_env_float("REEDUS_THINK_BARGE_PREROLL", 0.7) / 0.05))
        pre: Deque = deque(maxlen=preroll_n)
        dumping = bool((os.environ.get("REEDUS_BARGE_DUMP") or "").strip())
        heard: List = []
        try:
            with _MicTap(sr, block, config.MIC_DEVICE) as tap:
                # ambient sampling only sets an energy threshold, skip it in detector mode
                ambient = 0.0 if det is not None else self._measure_ambient(tap)
                thresh = max(floor, ambient * _env_float(
                    "REEDUS_THINK_BARGE_AMBIENT_MULT", 2.2) + 0.008)
                self.thresh = thresh
                self.armed = True
                if det is not None:
                    det.reset()
                    _dbg(f"think barge armed on the WAKE WORD ({det.describe()})")
                else:
                    _dbg(f"think barge armed on ENERGY ambient={ambient:.3f} "
                         f"thresh={thresh:.3f} (need {need} of last {window})")
                last_log = time.monotonic()
                while not self._stop.is_set() and not self.cancel.is_set():
                    chunk = tap.get(timeout=0.25)
                    if chunk is None or not chunk.size:
                        continue
                    rms = float(np.sqrt(np.mean(chunk * chunk)))
                    self.peak_rms = max(self.peak_rms, rms)
                    self.overflows = tap.overflows
                    if dumping:
                        heard.append(chunk)
                    busy = self._speaker_busy()

                    if det is not None:
                        fired = det.feed(chunk)
                        self.open_blocks += 1
                        if busy:
                            pre.clear()   # his voice must not lead the captured utterance
                            self.busy_blocks += 1
                            continue
                        pre.append(chunk)
                        if not fired:
                            continue
                        recent.extend([True] * need)
                    else:
                        if busy:
                            recent.clear()
                            pre.clear()
                            self.busy_blocks += 1
                            continue
                        self.open_blocks += 1
                        pre.append(chunk)
                        recent.append(rms >= thresh)
                    if _dbg_on() and time.monotonic() - last_log >= 3.0:
                        last_log = time.monotonic()
                        if det is not None:
                            _dbg(f"think barge listening: wake score peak "
                                 f"{det.peak_score:.3f} / thresh {det.threshold:.2f}  "
                                 f"blocks {self.open_blocks} dropped {tap.overflows}")
                        else:
                            _dbg(f"think barge listening: peak rms {self.peak_rms:.3f} / "
                                 f"thresh {thresh:.3f}  blocks {self.open_blocks} "
                                 f"dropped {tap.overflows}")
                    if sum(recent) >= need:
                        self.hit = True
                        self.cancel.set()
                        if self.on_hit:
                            try:
                                self.on_hit()
                            except Exception:                 # noqa: BLE001
                                pass
                        if self.capture_enabled():
                            self._capture(tap, list(pre), thresh)
                        return
        except Exception as e:                                # noqa: BLE001
            _dbg(f"think barge FAILED to open the mic ({config.MIC_DEVICE!r}): "
                 f"{type(e).__name__}: {e}")
            return
        finally:
            _dump_wav("think", heard, sr)

    def _capture(self, tap: "_MicTap", pre: List, thresh: float) -> None:
        """Keep recording the interrupting utterance until it ends."""
        max_sec = self.capture_max_sec()
        end_sil = max(0.3, _env_float("REEDUS_THINK_BARGE_SILENCE_SEC", 1.0))
        need_silent = max(1, int(end_sil / 0.05))
        quiet = max(0.010, thresh * 0.35)
        buf: List = list(pre)
        silent_run = 0
        for _ in range(int(max_sec / 0.05)):
            chunk = tap.get(timeout=0.5)
            if chunk is None:
                break                       # mic went away, keep what we have
            if not chunk.size:
                continue
            buf.append(chunk)
            rms = float(np.sqrt(np.mean(chunk * chunk)))
            if rms < quiet:
                silent_run += 1
                if silent_run >= need_silent:
                    break
            else:
                silent_run = 0
        if buf:
            self.audio = np.concatenate(buf)
            _dbg(f"think barge captured {self.audio.size / 16000.0:.1f}s of interruption")
