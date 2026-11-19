"""the wake word, on the relay for now.

phase 1 runs the same openWakeWord "hey reedus" model the run box uses. the S3 only sends audio
while its speech gate is open (the XVF3800 heard a voice, plus a 400 ms pre-roll), so nothing
streams while the room is quiet. phase 2 moves detection onto the S3 (microWakeWord) and the
gate closes until the name is said.

RELAY_WAKE:
    model    openWakeWord with REEDUS_WAKEWORD_MODEL (the default)
    gate     every time the S3's speech gate opens counts as the wake word. for bring-up
             before the model is deployed: anything said near Lite gets an answer
with RELAY_WAKE=model and no model file, Lite doesn't wake at all (and says why in the log)
"""
from __future__ import annotations


class ModelWake:
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


class GateWake:
    """Bring-up stand-in: the speech gate opening is the wake word."""
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


def make(kind: str = "model"):
    kind = (kind or "model").lower()
    if kind == "gate":
        return GateWake()
    try:
        w = ModelWake()
    except Exception as e:                                       # noqa: BLE001
        print(f"[relay] wake word failed to load: {type(e).__name__}: {e}", flush=True)
        return NoWake()
    if not w.available:
        print(f"[relay] *** NO WAKE WORD *** ({w.error}). Put hey_reedus.onnx in relay/models/ "
              f"(or set REEDUS_WAKEWORD_MODEL). Until then Lite won't wake; RELAY_WAKE=gate "
              f"answers any speech instead (bring-up only: a TV would spend your pool).", flush=True)
        return NoWake()
    return w
