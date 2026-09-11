#!/usr/bin/env python3
"""wake-word detector + idle-wake integration. no mic, no model, no openwakeword."""
from __future__ import annotations
import sys
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_fails = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


def _install_stub(score_for):
    """Fake openwakeword whose score is a function of the frame."""
    mod = types.ModuleType("openwakeword")
    sub = types.ModuleType("openwakeword.model")

    class _StubModel:
        last_kwargs = {}

        def __init__(self, **kw):
            _StubModel.last_kwargs = kw
            fw = kw.get("inference_framework")
            paths = kw.get("wakeword_models") or []
            if fw == "tflite" and any(str(p).endswith(".onnx") for p in paths):
                raise ValueError("The tflite inference framework is selected, "
                                 "but onnx models were provided!")
            self.models = {"reedus": object()}
            self.n_frames = 0

        def predict(self, frame):
            assert len(frame) == 1280, f"model got a {len(frame)}-sample frame, not 1280"
            assert frame.dtype == np.int16, f"model got {frame.dtype}, not int16"
            self.n_frames += 1
            return {"reedus": float(score_for(frame))}

        def reset(self):
            pass

    sub.Model = _StubModel
    mod.model = sub
    sys.modules["openwakeword"] = mod
    sys.modules["openwakeword.model"] = sub
    return _StubModel


def _fresh(env):
    import importlib
    import os
    for k in [k for k in os.environ if k.startswith("REEDUS_WAKEWORD")]:
        os.environ.pop(k)
    os.environ.update(env)
    import reedus_companion.voice.wakeword as W
    importlib.reload(W)
    return W


import os
_MODEL = Path("/tmp/_reedus_stub_model.onnx")
_MODEL.write_bytes(b"stub")
_TFL = Path("/tmp/_reedus_stub_model.tflite")
_TFL.write_bytes(b"stub")

# [1]
print("[1/5] a missing detector degrades, it does not break")
W = _fresh({"REEDUS_ROOT": "/tmp/_reedus_no_such_root"})
check(not W.WakeWordDetector().available, "no model configured -> unavailable")
check("WAKEWORD_MODEL" in W.WakeWordDetector().error
      and "xeon.env" in W.WakeWordDetector().error,
      "...and points at the likely cause (env not sourced), not just 'go train one'")
W = _fresh({"REEDUS_WAKEWORD": "0", "REEDUS_WAKEWORD_MODEL": str(_MODEL),
            "REEDUS_ROOT": "/tmp/_reedus_no_such_root"})
check(not W.WakeWordDetector().available, "REEDUS_WAKEWORD=0 -> unavailable (real off switch)")
W = _fresh({"REEDUS_WAKEWORD_MODEL": "/nope/reedus.onnx",
            "REEDUS_ROOT": "/tmp/_reedus_no_such_root"})
check("not found" in W.WakeWordDetector().error, "bad path -> names the file")
check(W.WakeWordDetector().feed(np.zeros(1280, np.float32)) is False,
      "feeding an unavailable detector is a no-op, not a crash")

import os as _os
_auto_root = Path("/tmp/_reedus_autoroot")
(_auto_root / "wakeword").mkdir(parents=True, exist_ok=True)
(_auto_root / "wakeword" / "hey_reedus.onnx").write_bytes(b"stub")
W = _fresh({"REEDUS_ROOT": str(_auto_root)})
check(W.model_paths() == [str(_auto_root / "wakeword" / "hey_reedus.onnx")],
      "a model in $REEDUS_ROOT/wakeword is found with the env var unset")

# [2]
print("\n[2/5] inference framework follows the file extension")
Stub = _install_stub(lambda f: 0.0)
W = _fresh({"REEDUS_WAKEWORD_MODEL": str(_MODEL), "REEDUS_WAKEWORD_VAD": ""})
d = W.WakeWordDetector()
check(d.available, f"loads an .onnx model ({d.error})")
check(Stub.last_kwargs.get("inference_framework") == "onnx",
      ".onnx -> onnx (the default would raise; reproduced live before fixing)")
W = _fresh({"REEDUS_WAKEWORD_MODEL": str(_TFL), "REEDUS_WAKEWORD_VAD": ""})
check(W.WakeWordDetector().available
      and Stub.last_kwargs.get("inference_framework") == "tflite", ".tflite -> tflite")
W = _fresh({"REEDUS_WAKEWORD_MODEL": str(_MODEL), "REEDUS_WAKEWORD_FRAMEWORK": "tflite",
            "REEDUS_WAKEWORD_VAD": ""})
check(not W.WakeWordDetector().available, "an explicit override still wins (and here, fails)")

# [3]
print("\n[3/5] rebuffering is block-size invariant")
# Loud second half of a 2s buffer = the "spoken name".
audio = np.concatenate([np.zeros(16000, np.float32),
                        np.full(16000, 0.5, dtype=np.float32)])
Stub = _install_stub(lambda f: 1.0 if np.abs(f).mean() > 3000 else 0.0)
results = {}
for block in (1280, 800, 333, 4096):
    W = _fresh({"REEDUS_WAKEWORD_MODEL": str(_MODEL), "REEDUS_WAKEWORD_VAD": ""})
    det = W.WakeWordDetector()
    hits = sum(det.feed(audio[i:i + block]) for i in range(0, len(audio) - block, block))
    results[block] = hits
check(all(v == 1 for v in results.values()),
      f"same detection count at every block size: {results}")
check(len(set(results.values())) == 1, "a caller never has to know the frame size")

# [4]
print("\n[4/5] one spoken name is one detection")
W = _fresh({"REEDUS_WAKEWORD_MODEL": str(_MODEL), "REEDUS_WAKEWORD_VAD": "",
            "REEDUS_WAKEWORD_REFRACTORY": "1.5"})
det = W.WakeWordDetector()
loud = np.full(1280, 0.5, dtype=np.float32)
hits = sum(det.feed(loud) for _ in range(40))       # ~3.2s of continuous name
check(hits == 1, f"refractory collapses a held detection to one hit (got {hits})")
check(det.peak_score >= 1.0, "peak score is still reported for tuning")


W = _fresh({"REEDUS_WAKEWORD_MODEL": str(_MODEL), "REEDUS_WAKEWORD_VAD": "",
            "REEDUS_WAKEWORD_REFRACTORY": "60"})
det = W.WakeWordDetector()
check(det.feed(loud) is True, "first frame of a name fires")
check(det.feed(loud) is False, "...and the refractory holds inside the phase")
det.reset()
check(det.feed(loud) is True,
      "reset() clears the refractory, so a new phase can fire immediately "
      "(waking him then interrupting used to be swallowed)")


W = _fresh({"REEDUS_WAKEWORD_MODEL": str(_MODEL), "REEDUS_WAKEWORD_VAD": "",
            "REEDUS_WAKEWORD_THRESHOLD": "0.9"})
Stub = _install_stub(lambda f: 0.5)
W = _fresh({"REEDUS_WAKEWORD_MODEL": str(_MODEL), "REEDUS_WAKEWORD_VAD": "",
            "REEDUS_WAKEWORD_THRESHOLD": "0.9"})
det = W.WakeWordDetector()
mid = np.full(1280, 0.3, dtype=np.float32)
check(det.feed(mid) is False, "a 0.5 score does not clear the configured 0.9 bar")
det.reset()
check(det.feed(mid, threshold=0.3) is True,
      "...but clears a per-call 0.3 bar (speak-barge uses this)")

# [5]
print("\n[5/5] idle wake re-attaches the name so the session logic is unchanged")
from reedus_companion.voice.wake import classify_utterance
from reedus_companion.voice.session import WakeSession

cases = [
    ("",                              "wake_only", ""),
    ("what's the weather",            "wake_cmd",  "what's the weather"),
    ("never mind",                    "dismiss",   None),
    ("goodbye",                       "stop",      None),
    ("what do you see",               "wake_cmd",  "what do you see"),
]
for spoken, want_kind, want_text in cases:
    built = f"Reedus {spoken}".strip()
    kind, payload = classify_utterance(built)
    ok = kind == want_kind and (want_text is None or payload == want_text)
    check(ok, f"detector + {spoken!r:22} -> {built!r} -> {kind}"
              + (f" {payload!r}" if want_text is not None else ""))

s = WakeSession()
check(s.on_transcript("Reedus").action == "arm" and s.active,
      "bare detection arms the session")
s2 = WakeSession()
ev = s2.on_transcript("Reedus what's the weather")
check(ev.action == "turn" and ev.text == "what's the weather" and s2.active,
      "detection + command runs the turn in one step (no second listen)")

from reedus_companion.voice.listener import WakeListener
W = _fresh({"REEDUS_ROOT": "/tmp/_reedus_no_such_root"})
check(not WakeListener().available,
      "WakeListener reports unavailable with no model -> voice_loop keeps the old idle path")

print()
if _fails:
    print(f"{len(_fails)} FAILED")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
