"""the wake word.

"Reedus" runs on the S3 (microWakeWord, firmware/src/wake.cpp). A Lite that has it says so with
the `wake` cap in hello; the relay answers wake: "device" in welcome, and from then on the S3
streams nothing until it hears the name, then sends {"type": "wake"} followed by a short
pre-roll and live audio. The relay takes that like its own detection (wake, listen, barge-in).

For a Lite without it (older firmware, or a model that didn't fit in RAM) the relay still runs
the run box's openWakeWord "hey reedus" on audio from the S3's speech gate (the XVF3800 heard a
voice, plus a 400 ms pre-roll).

RELAY_WAKE:
    device   trust the S3's wake word where there is one, openWakeWord for the rest (default)
    model    openWakeWord (REEDUS_WAKEWORD_MODEL) for every Lite; the S3s use their speech gate
    gate     every time the S3's speech gate opens counts as the wake word. for bring-up
             before any model is deployed: anything said near Lite gets an answer
with openWakeWord needed and no model file, that Lite doesn't wake at all (and the log says why)
"""
from __future__ import annotations


class ModelWake:
    on_device = False

    def __init__(self):
        from reedus_companion.voice.wakeword import WakeWordDetector
        self.det = WakeWordDetector()
        self.available = bool(self.det.available)
        self.error = getattr(self.det, "error", "")

    def feed(self, pcm: bytes) -> bool:
        return bool(self.det.feed(pcm))

    def gate_opened(self) -> bool:
        return False

    def reset(self) -> None:
        self.det.reset()

    def describe(self) -> str:
        return f"openWakeWord ({self.det.describe()})" if self.available else \
            f"openWakeWord unavailable: {self.error}"


class DeviceWake:
    """The S3 hears the name itself and says {"type": "wake"}; audio only comes after it."""
    on_device = True
    available = True
    error = ""

    def feed(self, pcm: bytes) -> bool:
        return False

    def gate_opened(self) -> bool:
        return False

    def reset(self) -> None:
        pass

    def describe(self) -> str:
        return 'on the Lite ("Reedus", microWakeWord)'


class GateWake:
    """Bring-up stand-in: the speech gate opening is the wake word."""
    on_device = False
    available = True
    error = ""

    def feed(self, pcm: bytes) -> bool:
        return False

    def gate_opened(self) -> bool:
        return True

    def reset(self) -> None:
        pass

    def describe(self) -> str:
        return "speech gate (RELAY_WAKE=gate, no wake word)"


class NoWake(GateWake):
    """No model and no opt-in: Lite stays asleep rather than answer every voice in the room."""

    def gate_opened(self) -> bool:
        return False

    def describe(self) -> str:
        return "NONE: hey_reedus.onnx is missing (see README), Lite won't wake"


def _kind(kind: str) -> str:
    kind = (kind or "device").lower()
    return kind if kind in ("model", "gate") else "device"


def _model():
    try:
        w = ModelWake()
    except Exception as e:                                       # noqa: BLE001
        print(f"[relay] wake word failed to load: {type(e).__name__}: {e}", flush=True)
        return NoWake()
    if not w.available:
        print(f"[relay] *** NO WAKE WORD *** ({w.error}) for Lites that can't hear \"Reedus\" "
              f"themselves. Put hey_reedus.onnx in relay/models/ (or set REEDUS_WAKEWORD_MODEL). "
              f"Until then they won't wake; RELAY_WAKE=gate answers any speech instead "
              f"(bring-up only: a TV would spend your pool).", flush=True)
        return NoWake()
    return w


def make(kind: str = "device", caps=None):
    """The detector for one Lite. `caps` is its hello's caps list."""
    kind = _kind(kind)
    if kind == "gate":
        return GateWake()
    if kind == "device" and "wake" in (caps or ()):
        return DeviceWake()
    return _model()


def status(kind: str = "device") -> tuple:
    """(ready, description) for ReedOS's Setup checklist."""
    kind = _kind(kind)
    if kind == "gate":
        return True, GateWake().describe()
    fallback = _model()
    if kind == "model":
        return not isinstance(fallback, NoWake), fallback.describe()
    extra = "" if isinstance(fallback, NoWake) else f"; older Lites: {fallback.describe()}"
    return True, DeviceWake().describe() + extra
