#!/usr/bin/env bash
# train the "hey Reedus" wake word locally on the build box (needs cuda). free, no service.
#   bash train_wakeword.sh setup     conda env + pinned deps + patched sample generator
#   bash train_wakeword.sh data      ~20 GB of negatives / RIRs / noise (resumable)
#   bash train_wakeword.sh train     generate -> augment -> train, ~1.5-3h on a 3090
#   bash train_wakeword.sh all       all three
#   bash train_wakeword.sh ship      copy the .onnx to the xeon
# the pins are load-bearing: openWakeWord's pipeline is a colab notebook whose pinned world
# has moved on, and a naive install fails in four separate places.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# the 20 GB workdir lives in the repo tree (gitignored). REEDUS_WW_WORK puts it on another disk.
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
WORK="${REEDUS_WW_WORK:-$REPO_ROOT/reedus_wakeword}"
ENVNAME="${REEDUS_WW_ENV:-reedus-wakeword}"
PHRASE="${REEDUS_WW_PHRASE:-hey readus}"
MODEL_NAME="${REEDUS_WW_NAME:-hey_reedus}"
N_SAMPLES="${REEDUS_WW_SAMPLES:-30000}"
STEPS="${REEDUS_WW_STEPS:-50000}"
XEON="${REEDUS_XEON:-dain@192.168.1.249}"
XEON_DIR="${REEDUS_XEON_WW:-/mnt/nvme/reedus/wakeword}"

OWW="$WORK/openWakeWord"
PIPER="$WORK/piper-sample-generator"
CFG="$WORK/${MODEL_NAME}.yaml"

conda_sh() {
  for c in "$HOME/miniconda3" "$HOME/anaconda3" "/opt/conda"; do
    [[ -f "$c/etc/profile.d/conda.sh" ]] && { echo "$c/etc/profile.d/conda.sh"; return 0; }
  done
  echo "ERROR: could not find conda.sh, set it up or edit conda_sh()" >&2
  return 1
}

activate() {
  # shellcheck disable=SC1090
  source "$(conda_sh)"
  conda activate "$ENVNAME"
}

setup() {
  mkdir -p "$WORK"
  # shellcheck disable=SC1090
  source "$(conda_sh)"
  if ! conda env list | awk '{print $1}' | grep -qx "$ENVNAME"; then
    echo "== creating conda env $ENVNAME (python 3.11) =="
    conda create -y -n "$ENVNAME" python=3.11
  fi
  conda activate "$ENVNAME"

  echo "== system deps =="
  if ! command -v espeak-ng >/dev/null 2>&1; then
    echo "   espeak-ng missing. Install it, then re-run:" >&2
    echo "     sudo apt install -y espeak-ng libespeak-ng-dev ffmpeg sox" >&2
    exit 1
  fi

  echo "== openWakeWord =="
  [[ -d "$OWW/.git" ]] || git clone https://github.com/dscripka/openWakeWord.git "$OWW"

  echo "== piper-sample-generator @ v2.0.0 =="
  if [[ ! -d "$PIPER/.git" ]]; then
    git clone https://github.com/rhasspy/piper-sample-generator.git "$PIPER"
    git -C "$PIPER" checkout v2.0.0
  fi
  if [[ ! -f "$PIPER/models/en_US-libritts_r-medium.pt" ]]; then
    mkdir -p "$PIPER/models"
    echo "   fetching the piper voice model"
    wget -q --show-progress -O "$PIPER/models/en_US-libritts_r-medium.pt" \
      https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt
  fi
  if grep -q 'torch.load(model_path)' "$PIPER/generate_samples.py" 2>/dev/null; then
    sed -i 's/torch\.load(model_path)/torch.load(model_path, weights_only=False)/' \
      "$PIPER/generate_samples.py"
    echo "   patched generate_samples.py for torch>=2.6 (weights_only)"
  fi

  echo "== python deps (pins are load-bearing) =="
  pip install --quiet "setuptools<82" "numpy<2"
  pip install --quiet torch torchaudio --index-url https://download.pytorch.org/whl/cu121 \
    || pip install --quiet torch "torchaudio<2.10"
  pip install --quiet "torchaudio<2.10" || true
  pip install --quiet piper-phonemize==1.1.0 webrtcvad
  pip install --quiet mutagen==1.47.0 torchinfo==1.8.0 torchmetrics==1.2.0 \
      speechbrain==0.5.14 audiomentations==0.33.0 torch-audiomentations==0.11.0 \
      acoustics==0.2.6 pronouncing==0.2.0 datasets==2.14.6 deep-phonemizer==0.0.19 \
      onnx onnxruntime scipy scikit-learn tqdm
  pip install --quiet -e "$OWW"

  echo
  echo "setup ok. Phrase check before you spend an hour on it:"
  espeak-ng -v en-us --ipa -q "$PHRASE" || true
  echo "   want:  hˈeɪ ɹˈiːdəs      (i.e. 'hey REE-dus', not 'hey re-ED-us')"
}

data() {
  activate
  python "$HERE/wakeword_prepare_data.py" --root "$WORK" --hours-music 1.0
}

write_config() {
  mkdir -p "$WORK"
  cat > "$CFG" <<YAML
model_name: "$MODEL_NAME"

target_phrase:
  - "$PHRASE"

custom_negative_phrases:
  - "readers"
  - "reads us"
  - "reed us"
  - "radius"
  - "readiness"
  - "hey readers"
  - "hey reader"
  - "ready"
  - "hey ready"
  - "read us"

n_samples: $N_SAMPLES
n_samples_val: $(( N_SAMPLES / 5 ))
tts_batch_size: 100
augmentation_batch_size: 16

piper_sample_generator_path: "$PIPER"
output_dir: "$WORK/$MODEL_NAME"

rir_paths:
  - "$WORK/mit_rirs"
background_paths:
  - "$WORK/background_clips"
background_paths_duplication_rate:
  - 1
false_positive_validation_data_path: "$WORK/validation_set_features.npy"
augmentation_rounds: 2

feature_data_files:
  ACAV100M_sample: "$WORK/openwakeword_features_ACAV100M_2000_hrs_16bit.npy"

batch_n_per_class:
  ACAV100M_sample: 1024
  adversarial_negative: 50
  positive: 50

model_type: "dnn"
layer_size: 32
steps: $STEPS
max_negative_weight: 1500
target_false_positives_per_hour: 0.2
YAML
  echo "wrote $CFG"
}

train() {
  activate
  write_config
  cd "$OWW"
  echo "== 1/3 generating clips (positives + adversarial negatives) =="
  python openwakeword/train.py --training_config "$CFG" --generate_clips
  echo "== 2/3 augmenting (RIR + background mix), the slow, partly CPU-bound part =="
  python openwakeword/train.py --training_config "$CFG" --augment_clips
  echo "== 3/3 training =="
  set +e
  python openwakeword/train.py --training_config "$CFG" --train_model
  local rc=$?
  set -e
  echo
  local onnx
  onnx="$(find "$WORK/$MODEL_NAME" -name '*.onnx' 2>/dev/null | head -1)"
  if [[ -n "$onnx" ]]; then
    ls -lh "$onnx"
    if [[ $rc -ne 0 ]]; then
      echo
      echo "NOTE: the run exited $rc AFTER writing the model. If the error was onnx_tf /"
      echo "      tensorflow, that is the optional tflite conversion and it does not matter,"
      echo "      ONNX is what runs on the Xeon. Ship it:"
    fi
    echo "  bash $0 ship"
  else
    echo "ERROR: training exited $rc and no .onnx was written." >&2
    exit "$rc"
  fi
}

ship() {
  local onnx
  onnx="$(find "$WORK/$MODEL_NAME" -name '*.onnx' | head -1)"
  if [[ -z "$onnx" ]]; then
    echo "ERROR: no .onnx under $WORK/$MODEL_NAME, run 'train' first" >&2
    exit 1
  fi
  echo "shipping $onnx -> $XEON:$XEON_DIR/${MODEL_NAME}.onnx"
  ssh "$XEON" "mkdir -p $XEON_DIR"
  scp "$onnx" "$XEON:$XEON_DIR/${MODEL_NAME}.onnx"
  cat <<MSG

On the Xeon (ssh $XEON), before starting the voice loop:

  export DEPLOY=/mnt/nvme/reedus/reedus_llm/reedus_companion/deploy
  export REEDUS_WAKEWORD_MODEL=$XEON_DIR/${MODEL_NAME}.onnx
  bash \$DEPLOY/setup_wakeword.sh check      # did it load
  bash \$DEPLOY/setup_wakeword.sh listen     # does it hear you
MSG
}

case "${1:-all}" in
  setup)  setup ;;
  data)   data ;;
  config) write_config ;;
  train)  train ;;
  ship)   ship ;;
  all)    setup; data; train ;;
  *) echo "usage: $0 {setup|data|config|train|ship|all}" >&2; exit 2 ;;
esac
