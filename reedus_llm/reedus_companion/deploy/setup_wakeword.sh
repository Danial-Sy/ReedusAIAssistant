#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/xeon.env"
ROOT="${REEDUS_ROOT:-/mnt/nvme/reedus}"
MODEL_DIR="${REEDUS_WAKEWORD_DIR:-$ROOT/wakeword}"
CMD="${1:-install}"

PY=python3
command -v python >/dev/null 2>&1 && PY=python

have_oww() { "$PY" -c "import openwakeword" >/dev/null 2>&1; }

install_it() {
  if have_oww; then
    echo "[oww] openwakeword already installed"
  else
    echo "[oww] installing openwakeword + onnxruntime into $(basename "${CONDA_PREFIX:-python}")"
    "$PY" -m pip install --quiet openwakeword onnxruntime || {
      echo "ERROR: pip install failed. Is the reedus-serve env active?" >&2
      echo "  source $ROOT/miniconda3/etc/profile.d/conda.sh && conda activate reedus-serve" >&2
      exit 1
    }
  fi
  mkdir -p "$MODEL_DIR"
  echo "[oww] fetching the pretrained models (used for the self-test only)"
  "$PY" - <<'PY'
import openwakeword.utils as U
U.download_models()
print("  ok")
PY
  self_test
}

pretrained_dir() {
  "$PY" -c "import openwakeword, os; print(os.path.join(os.path.dirname(openwakeword.__file__), 'resources', 'models'))"
}

self_test() {
  local d
  d="$(pretrained_dir)"
  local probe="$d/hey_jarvis_v0.1.onnx"
  if [[ ! -f "$probe" ]]; then
    echo "[oww] self-test skipped, no pretrained onnx at $probe" >&2
    return 0
  fi
  echo "[oww] self-test: loading a stock model and scoring synthetic speech"
  REEDUS_WAKEWORD_MODEL="$probe" REEDUS_WAKEWORD_VAD="" "$PY" - <<'PY'
import numpy as np, subprocess, sys, tempfile, wave, os
sys.path.insert(0, os.environ.get("REEDUS_CODE", "/mnt/nvme/reedus/reedus_llm"))
from reedus_companion.voice.wakeword import WakeWordDetector, FRAME_SAMPLES

det = WakeWordDetector()
if not det.available:
    print(f"  FAIL: {det.error}"); raise SystemExit(1)
print(f"  loaded: {det.describe()}")

# espeak-ng if present: openWakeWord is trained on synthetic speech so TTS reliably triggers
# it, which makes this a real end-to-end check of the audio contract, not just an import test
try:
    wav = os.path.join(tempfile.mkdtemp(), "probe.wav")
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", wav, "hey jarvis"],
                   check=True, capture_output=True)
    with wave.open(wav) as w:
        sr, n = w.getframerate(), w.getnframes()
        a = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    m = int(round(len(a) * 16000 / sr))
    a = np.interp(np.linspace(0, 1, m, endpoint=False),
                  np.linspace(0, 1, len(a), endpoint=False), a).astype(np.float32)
    a = np.concatenate([np.zeros(16000, np.float32), a, np.zeros(16000, np.float32)])
    hits = sum(det.feed(a[i:i + FRAME_SAMPLES])
               for i in range(0, len(a) - FRAME_SAMPLES, FRAME_SAMPLES))
    print(f"  synthetic 'hey jarvis' -> {hits} detection(s), peak score {det.peak_score:.3f}")
    if hits:
        print("  PASS, the audio path and the model runtime are both good.")
    else:
        print("  WARN, model loaded but did not fire on synthetic speech. Check that")
        print("         MIC_SAMPLE_RATE is 16000 and onnxruntime is healthy.")
except FileNotFoundError:
    print("  (espeak-ng not installed, skipping the synthetic-speech check)")
    print("  install it for a stronger self-test:  sudo apt install -y espeak-ng")
except subprocess.CalledProcessError as e:
    print(f"  (espeak-ng failed: {e}), skipping synthetic-speech check")

# this runs continuously, so it has to be small
import time
noise = (np.random.randn(FRAME_SAMPLES) * 0.02).astype(np.float32)
for _ in range(30):
    det.feed(noise)
t0 = time.perf_counter()
N = 250
for _ in range(N):
    det.feed(noise)
el = time.perf_counter() - t0
audio_s = N * FRAME_SAMPLES / 16000
print(f"  cost: {el / audio_s * 100:.1f}% of one core, continuous")
PY
}

case "$CMD" in
  install) install_it ;;
  check)
    if have_oww; then echo "openwakeword: installed"; else echo "openwakeword: NOT installed"; exit 1; fi
    "$PY" -m reedus_companion.voice.wakeword --check
    ;;
  listen)  exec "$PY" -m reedus_companion.voice.wakeword --listen ;;
  test)    self_test ;;
  *) echo "usage: $0 {install|check|listen|test}" >&2; exit 2 ;;
esac

if [[ "$CMD" == "install" ]]; then
  cat <<MSG

Next: you need a model for "Reedus", there is no pretrained one.
  bash $HERE/train_wakeword.sh all   (build box, needs cuda, outputs .onnx)

Then drop it at $MODEL_DIR/reedus.onnx and:
  export REEDUS_WAKEWORD_MODEL=$MODEL_DIR/reedus.onnx
  bash $HERE/setup_wakeword.sh listen      # confirm it hears you, pick a threshold
MSG
fi
