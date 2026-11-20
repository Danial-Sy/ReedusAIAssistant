#!/usr/bin/env python3
"""prove the S3 sees what the wake word model was trained on.

builds the firmware's feature path (lib/microfrontend: the TFLite Micro frontend + ESPHome's
int8 scaling) natively, runs it over wav files in 20 ms frames like the device, and checks:

  1. its uint16 spectrogram is bit-identical to pymicro_features (what microWakeWord trains on)
  2. its int8 model inputs (ESPHome's integer scaling) are within 1 step of quantizing the
     training features exactly (round, clamp) with the model's input scale and zero point
  3. the model fed those inputs (stride 3, like wake.cpp) gives nearly the same probabilities,
     and the device's detector (window 5, integer cutoff, 2 s refractory) wakes on the same
     clips as mww_eval.py's path. (mww_eval's quantize_input_data truncates and wraps values
     past 127, so its int8 inputs differ more on loud audio; the verdicts still agree)

run it in the training env (pymicro_features, microwakeword, ai_edge_litert):
    conda run -n reedus-mww python ReedusLiteS3/firmware/tools/check_wake_features.py \\
        ~/reedus_mww/ood/kokoro_holdout_pos/ph0 ~/reedus_mww/ood/kokoro_holdout_neg
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FW = HERE.parent
LIB = FW / "lib" / "microfrontend" / "src"
MODELS = FW.parent / "wakeword" / "models"
F = 40
WARMUP_SLICES = 100        # WAKE_WARMUP_SLICES
REFRACTORY_SLICES = 200    # WAKE_REFRACTORY_MS / 10


def build(out: Path) -> Path:
    srcs = sorted(str(p) for p in (LIB / "tensorflow/lite/experimental/microfrontend/lib").glob("*.cc"))
    exe = out / "wake_features_host"
    cmd = ["g++", "-O2", "-std=gnu++17", "-I", str(LIB), *srcs, str(LIB / "mww_features.cpp"),
           str(HERE / "wake_features_host.cpp"), "-o", str(exe)]
    subprocess.run(cmd, check=True)
    return exe


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, \
            f"{path}: need 16 kHz mono int16"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def device_features(exe: Path, audio: np.ndarray, tmp: Path):
    pcm, out = tmp / "in.pcm", tmp / "out.bin"
    pcm.write_bytes(audio.astype("<i2").tobytes())
    subprocess.run([str(exe), str(pcm), str(out)], check=True, stderr=subprocess.DEVNULL)
    rec = np.frombuffer(out.read_bytes(), dtype=np.dtype([("raw", "<u2", F), ("q", "i1", F)]))
    return rec["raw"], rec["q"]


def reference_features(audio: np.ndarray) -> np.ndarray:
    """microWakeWord's generate_features_for_clip(step_ms=10), back to the frontend's uint16."""
    from microwakeword.audio.audio_utils import generate_features_for_clip
    f = generate_features_for_clip(audio, step_ms=10)        # float32 = uint16 * 0.0390625 (exact)
    return np.rint(f / np.float32(0.0390625)).astype(np.uint16)


class Streamer:
    """the .tflite run like wake.cpp: int8 slices in, invoke every `stride`, uint8 out."""

    def __init__(self, model: Path):
        self.model = str(model)
        self.it = self._fresh()
        self.inp = self.it.get_input_details()[0]
        self.out = self.it.get_output_details()[0]
        self.stride = int(self.inp["shape"][1])
        self.scale, self.zp = self.inp["quantization"]

    def _fresh(self):
        from ai_edge_litert.interpreter import Interpreter
        it = Interpreter(model_path=self.model)
        it.allocate_tensors()
        return it

    def run(self, q: np.ndarray) -> np.ndarray:
        # a new interpreter per clip: reset_all_variables() doesn't clear the streaming state
        # (VAR_HANDLE resources that CALL_ONCE fills only once)
        self.it = self._fresh()
        probs = []
        for i in range(self.stride, len(q) + 1, self.stride):
            self.it.set_tensor(self.inp["index"], q[i - self.stride:i].reshape(self.inp["shape"]))
            self.it.invoke()
            probs.append(int(self.it.get_tensor(self.out["index"]).reshape(-1)[0]))
        return np.asarray(probs, dtype=np.int32)

    def exact_quantize(self, raw: np.ndarray) -> np.ndarray:
        """the training features (uint16 / 25.6) quantized properly: round, then clamp."""
        x = raw.astype(np.float32) * np.float32(0.0390625)
        return np.clip(np.rint(x / self.scale) + self.zp, -128, 127).astype(np.int8)

    def eval_quantize(self, raw: np.ndarray) -> np.ndarray:
        """microwakeword.inference.Model.quantize_input_data (what mww_eval.py measures)."""
        x = raw.astype(np.float32) * np.float32(0.0390625)
        with np.errstate(invalid="ignore"):
            return (x / self.scale + self.zp).astype(np.int64).astype(np.int8)


def detections(probs: np.ndarray, cutoff_u8: int, window: int, stride: int) -> list:
    """wake.cpp's detector: integer sliding-window sum, warm-up, refractory."""
    win = [0] * window
    idx, hits, last = 0, [], -10 ** 9
    for k, p in enumerate(probs):
        slice_no = (k + 1) * stride
        win[idx] = int(p)
        idx = (idx + 1) % window
        if slice_no < WARMUP_SLICES or slice_no - last < REFRACTORY_SLICES:
            continue
        if sum(win) > cutoff_u8 * window:
            hits.append(slice_no)
            last = slice_no
            win = [0] * window
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", type=Path, nargs="+", help="wav files or folders of them")
    ap.add_argument("--model", type=Path, default=MODELS / "reedus.tflite")
    ap.add_argument("--max", type=int, default=60, help="clips per folder")
    a = ap.parse_args()

    manifest = json.loads((a.model.with_suffix(".json")).read_text())["micro"]
    cutoff_u8 = int(manifest["probability_cutoff"] * 255)      # what ESPHome does
    window = int(manifest["sliding_window_size"])
    pad_pre, pad_post = np.zeros(16000, np.int16), np.zeros(8000, np.int16)

    files = []
    for p in a.inputs:
        files += sorted(p.rglob("*.wav"))[: a.max] if p.is_dir() else [p]
    if not files:
        print("no wav files")
        return 2

    model = Streamer(a.model)
    tot_slices = mism_slices = 0
    q_off = q_big = 0
    pdiffs = []
    det_same = det_same_eval = 0
    woke_dev = woke_ref = woke_eval = 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        exe = build(tmp)
        for f in files:
            audio = np.concatenate([pad_pre, load(f), pad_post])
            raw, q = device_features(exe, audio, tmp)
            ref = reference_features(audio)
            n = min(len(raw), len(ref))
            # the reference stops one slice early (it needs a full chunk past the end); never more
            assert abs(len(raw) - len(ref)) <= 2, (f, len(raw), len(ref))
            tot_slices += n
            mism_slices += int(np.any(raw[:n] != ref[:n], axis=1).sum())
            q_ref = model.exact_quantize(raw[:n])
            d = np.abs(q[:n].astype(np.int32) - q_ref.astype(np.int32))
            q_off += int((d > 0).sum())
            q_big += int((d > 1).sum())
            p_dev = model.run(q[:n])
            p_ref = model.run(q_ref)
            p_eval = model.run(model.eval_quantize(raw[:n]))   # == mww_eval / predict_clip
            if len(p_dev):
                pdiffs.append(np.abs(p_dev - p_ref))
            w_dev, w_ref, w_eval = (bool(detections(p, cutoff_u8, window, model.stride))
                                    for p in (p_dev, p_ref, p_eval))
            woke_dev, woke_ref, woke_eval = woke_dev + w_dev, woke_ref + w_ref, woke_eval + w_eval
            det_same += w_dev == w_ref
            det_same_eval += w_dev == w_eval

    nvals = tot_slices * F
    print(f"{len(files)} clips, {tot_slices} slices of 40 features")
    print(f"1. frontend vs pymicro_features: {mism_slices} slices differ "
          f"({'bit-identical' if mism_slices == 0 else 'MISMATCH'})")
    print(f"2. int8 inputs vs exact quantization: {q_off}/{nvals} values off by 1 "
          f"({q_off / max(1, nvals):.2%}), {q_big} off by more")
    pd = np.concatenate(pdiffs) if pdiffs else np.zeros(1)
    print(f"3. model output (uint8), device inputs vs exact: mean |diff| {pd.mean():.2f}, "
          f"99th pct {np.percentile(pd, 99):.0f}, max {pd.max()}")
    print(f"   wakes (cutoff {cutoff_u8}/255, window {window}): device {woke_dev}, exact {woke_ref}, "
          f"mww_eval path {woke_eval} of {len(files)} clips; device agrees with exact on "
          f"{det_same}, with mww_eval on {det_same_eval}")
    ok = mism_slices == 0 and q_big == 0 and det_same >= len(files) - max(1, len(files) // 50)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
