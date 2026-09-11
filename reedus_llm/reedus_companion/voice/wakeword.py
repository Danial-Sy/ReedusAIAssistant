"""neural wake-word detection via openWakeWord, the locally trained hey_reedus model."""
from __future__ import annotations

import glob
import os
import sys
import time
from typing import Dict, List, Optional

import numpy as np

FRAME_SAMPLES = 1280
SAMPLE_RATE = 16000
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE


def wakeword_enabled() -> bool:
    return os.environ.get("REEDUS_WAKEWORD", "1") != "0"


def model_paths() -> List[str]:
    """Configured model paths, or whatever is sitting in the standard directory."""
    raw = (os.environ.get("REEDUS_WAKEWORD_MODEL") or "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    root = (os.environ.get("REEDUS_ROOT") or "/mnt/nvme/reedus").rstrip("/")
    found = sorted(glob.glob(f"{root}/wakeword/*.onnx"))
    return found or sorted(glob.glob(f"{root}/wakeword/*.tflite"))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class WakeWordDetector:
    """Feed it audio blocks; ask it whether the name was said."""

    def __init__(self, paths: Optional[List[str]] = None,
                 threshold: Optional[float] = None):
        self.available = False
        self.error = ""
        self.names: List[str] = []
        self.discovered = False      # found by directory scan, not configured
        self.threshold = (threshold if threshold is not None
                          else _env_float("REEDUS_WAKEWORD_THRESHOLD", 0.5))
        self.refractory = _env_float("REEDUS_WAKEWORD_REFRACTORY", 1.5)
        self._model = None
        self._buf = np.zeros(0, dtype=np.int16)
        self._last_hit = 0.0
        self.last_scores: Dict[str, float] = {}
        self.peak_score = 0.0
        self.frames_seen = 0

        if not wakeword_enabled():
            self.error = "REEDUS_WAKEWORD=0"
            return
        explicit = bool((os.environ.get("REEDUS_WAKEWORD_MODEL") or "").strip())
        paths = paths if paths is not None else model_paths()
        self.discovered = bool(paths) and not explicit
        if not paths:
            root = (os.environ.get("REEDUS_ROOT") or "/mnt/nvme/reedus").rstrip("/")
            self.error = (f"REEDUS_WAKEWORD_MODEL unset and no .onnx in {root}/wakeword/, "
                          f"either `source deploy/xeon.env` (which sets it), or train one: "
                          f"bash deploy/train_wakeword.sh all")
            return
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            self.error = f"model file(s) not found: {', '.join(missing)}"
            return
        try:
            from openwakeword.model import Model
        except Exception as e:                                   # noqa: BLE001
            self.error = (f"openwakeword not installed ({type(e).__name__}: {e}), "
                          f"run deploy/setup_wakeword.sh")
            return
        kwargs: dict = {"wakeword_models": paths}
        fw = (os.environ.get("REEDUS_WAKEWORD_FRAMEWORK") or "").strip()
        if not fw:
            exts = {os.path.splitext(p)[1].lower() for p in paths}
            fw = "onnx" if ".onnx" in exts else "tflite"
        kwargs["inference_framework"] = fw
        vad = (os.environ.get("REEDUS_WAKEWORD_VAD", "0.3") or "").strip()
        if vad:
            try:
                kwargs["vad_threshold"] = float(vad)
            except ValueError:
                pass
        try:
            self._model = Model(**kwargs)
        except Exception as e:                                   # noqa: BLE001
            if "vad_threshold" in kwargs:
                kwargs.pop("vad_threshold")
                try:
                    self._model = Model(**kwargs)
                except Exception as e2:                          # noqa: BLE001
                    self.error = f"could not load model: {type(e2).__name__}: {e2}"
                    return
            else:
                self.error = f"could not load model: {type(e).__name__}: {e}"
                return
        try:
            self.names = list(getattr(self._model, "models", {}).keys())
        except Exception:                                        # noqa: BLE001
            self.names = []
        self.available = True

    # --- feeding ---
    @staticmethod
    def _to_int16(block) -> np.ndarray:
        """float32 [-1, 1] from sounddevice -> int16 PCM for the model."""
        arr = np.asarray(block).reshape(-1)
        if arr.dtype == np.int16:
            return arr
        arr = np.clip(arr.astype(np.float32), -1.0, 1.0)
        return (arr * 32767.0).astype(np.int16)

    def feed(self, block, threshold: Optional[float] = None) -> bool:
        """Push one audio block. True when the wake word was detected in it."""
        if not self.available or block is None:
            return False
        try:
            pcm = self._to_int16(block)
        except Exception:                                        # noqa: BLE001
            return False
        if pcm.size:
            self._buf = np.concatenate([self._buf, pcm]) if self._buf.size else pcm
        fired = False
        while self._buf.size >= FRAME_SAMPLES:
            frame, self._buf = self._buf[:FRAME_SAMPLES], self._buf[FRAME_SAMPLES:]
            self.frames_seen += 1
            try:
                scores = self._model.predict(frame)
            except Exception:                                    # noqa: BLE001
                continue
            if not scores:
                continue
            self.last_scores = dict(scores)
            top = max(scores.values())
            self.peak_score = max(self.peak_score, float(top))
            if top >= (self.threshold if threshold is None else threshold):
                now = time.monotonic()
                if now - self._last_hit >= self.refractory:
                    self._last_hit = now
                    fired = True
                    self._reset_context()
        return fired

    def _reset_context(self) -> None:
        """Drop the model's rolling feature history and our rebuffer, nothing else."""
        self._buf = np.zeros(0, dtype=np.int16)
        try:
            reset = getattr(self._model, "reset", None)
            if callable(reset):
                reset()
        except Exception:                                        # noqa: BLE001
            pass

    def reset(self) -> None:
        """Phase boundary: new listener, new stream, clean slate."""
        self._reset_context()
        self._last_hit = 0.0
        self.peak_score = 0.0

    def describe(self) -> str:
        if self.available:
            how = " (auto-found; REEDUS_WAKEWORD_MODEL unset)" if self.discovered else ""
            return (f"wakeword: {', '.join(self.names) or '?'} "
                    f"thresh={self.threshold:.2f} refractory={self.refractory:.1f}s{how}")
        return f"wakeword unavailable ({self.error})"


_SHARED: Optional[WakeWordDetector] = None


def shared_detector() -> WakeWordDetector:
    """Process-wide detector, loading the model twice would double RAM and CPU."""
    global _SHARED
    if _SHARED is None:
        _SHARED = WakeWordDetector()
    return _SHARED


# --- live tuning ---
def _listen(seconds: float = 0.0) -> int:
    """Live scores from the mic, this is how you pick a threshold."""
    import sounddevice as sd
    from reedus_companion import config

    det = shared_detector()
    print(det.describe())
    if not det.available:
        print("\nNothing to listen with. Fix the above, then re-run.", file=sys.stderr)
        return 2
    print(f"mic: {config.MIC_DEVICE!r}   frame: {FRAME_SAMPLES} samples "
          f"({FRAME_SECONDS * 1000:.0f} ms)")
    print("\nSay the wake word. Ctrl-C to stop.")
    print("  Aim for: clear peaks near 1.0 when you say it, and a quiet baseline "
          "(< 0.1) when you don't.\n")

    hits = 0
    top_quiet = 0.0
    deadline = time.monotonic() + seconds if seconds else None
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=FRAME_SAMPLES, device=config.MIC_DEVICE) as stream:
            while deadline is None or time.monotonic() < deadline:
                block, _overflow = stream.read(FRAME_SAMPLES)
                fired = det.feed(block[:, 0])
                score = max(det.last_scores.values()) if det.last_scores else 0.0
                bar = "#" * int(score * 40)
                if fired:
                    hits += 1
                    print(f"  {score:5.3f} |{bar:<40}|  *** DETECTED (#{hits}) ***")
                else:
                    top_quiet = max(top_quiet, score)
                    print(f"  {score:5.3f} |{bar:<40}|", end="\r", flush=True)
    except KeyboardInterrupt:
        pass
    print(f"\n\ndetections: {hits}   highest non-detection score: {top_quiet:.3f}")
    if hits and top_quiet < det.threshold * 0.5:
        print("Looks healthy, clear separation between speech and silence.")
    elif not hits:
        print("No detections. If the bar barely moved when you spoke, the mic or the model "
              "is wrong; if it rose but stayed under the threshold, lower "
              "REEDUS_WAKEWORD_THRESHOLD.")
    else:
        print("Detections fired, but the quiet baseline is high, raise "
              "REEDUS_WAKEWORD_THRESHOLD to avoid false triggers.")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="openWakeWord detector for Reedus")
    ap.add_argument("--listen", action="store_true",
                    help="live scores from the mic (use this to pick a threshold)")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="stop after N seconds instead of Ctrl-C")
    ap.add_argument("--check", action="store_true",
                    help="report whether the detector loaded, then exit")
    args = ap.parse_args()
    if args.check or not args.listen:
        det = shared_detector()
        print(det.describe())
        return 0 if det.available else 1
    return _listen(args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())
