#!/usr/bin/env bash
# train the "Reedus" wake word as a microWakeWord model for the XIAO ESP32-S3 (needs cuda).
#   bash train_mww.sh setup      conda env (tensorflow + microWakeWord) and the piper TTS checkout
#   bash train_mww.sh data       negative features (~26 GB) + background audio (resumable)
#   bash train_mww.sh samples    TTS (piper + Kokoro): "Reedus" positives + sound-alike negatives
#   bash train_mww.sh features   augment them into spectrogram features, plus LibriSpeech as
#                                clean-speech negatives (CPU, all cores)
#   bash train_mww.sh train      train mixednet, quantize, stream: ~1 h on a 3090 Ti
#   bash train_mww.sh ship       copy the .tflite + manifest json into this folder
#   bash train_mww.sh all        all of it
# just "Reedus", no "hey": two syllables is short for a wake word, so most of the work here is
# the sound-alike negatives (readers, radius, ready...). "read us" and "Rita's" (flapped d) are
# the same sound as the name, so they're deliberately not negatives: they will wake him.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${REEDUS_MWW_WORK:-$HOME/reedus_mww}"
ENVNAME="${REEDUS_MWW_ENV:-reedus-mww}"
# piper runs in the openWakeWord env (torch + piper-phonemize already pinned there)
TTS_ENV="${REEDUS_TTS_ENV:-reedus-wakeword}"
OWW_WORK="${REEDUS_WW_WORK:-$HOME/Reedus/reedus_wakeword}"
NAME="${REEDUS_MWW_NAME:-reedus}"
N_POS="${REEDUS_MWW_SAMPLES:-24000}"
N_NEG="${REEDUS_MWW_NEG_SAMPLES:-1200}"   # per sound-alike
RUN="${REEDUS_MWW_RUN:-$NAME}"            # trained_models/<run>, so tries don't clobber each other

MWW="$WORK/microWakeWord"
PIPER="$WORK/piper-sample-generator"

# spellings piper says as REE-dus (espeak: ɹˈiːdəs). "reedus" itself comes out ree-ED-us.
# punctuation changes the prosody, "readiss" is the lazier second vowel people actually say.
POSITIVES=("readus:0.5" "readus!:0.17" "readus?:0.17" "readiss:0.16")
SOUNDALIKES=(
  "readers" "reader" "reading" "radius" "reduce" "reduced" "ready" "already" "Regis"
  "reeds" "reed" "read" "Remus" "Reese" "Reese's" "Venus" "Judas" "Medusa"
  "needs us" "feeds us" "leads us" "read this" "need this" "redness" "Rufus" "Brutus"
  "Reebok" "really" "Rhodes" "Reno's" "freedom" "Phoebus" "Peter's" "readies"
  # what the first model woke on in LibriSpeech, and their neighbours
  "greatest" "great is" "bade us" "made us" "paid us" "gave us" "freed us" "treat us"
  "greed is" "creed is" "breed is" "relation" "reassured" "real" "period" "reptiles"
  "radios" "Freda's" "Rita" "readout" "beat us" "need it" "credits" "edits" "Reedy"
  "redo" "remedies" "Theodore" "Lettuce" "media"
)
# Kokoro (a second engine): American phonemes, British gets ː added. flapped d (T) is how
# most Americans actually say it
KOKORO_POS=("ɹˈidəs:0.6" "ɹˈidɪs:0.25" "ɹˈiTəs:0.15")
N_KOKORO="${REEDUS_MWW_KOKORO_SAMPLES:-10000}"
N_KOKORO_NEG="${REEDUS_MWW_KOKORO_NEG_SAMPLES:-300}"   # per sound-alike
KOKORO_ENV="${REEDUS_KOKORO_ENV:-reedus-kokoro}"

conda_sh() {
  for c in "$HOME/miniconda3" "$HOME/anaconda3" "/opt/conda"; do
    [[ -f "$c/etc/profile.d/conda.sh" ]] && { echo "$c/etc/profile.d/conda.sh"; return 0; }
  done
  echo "ERROR: could not find conda.sh" >&2
  return 1
}

activate() {
  # shellcheck disable=SC1090
  source "$(conda_sh)"
  conda activate "$1"
}

setup() {
  mkdir -p "$WORK/logs"
  # shellcheck disable=SC1090
  source "$(conda_sh)"
  if ! conda env list | awk '{print $1}' | grep -qx "$ENVNAME"; then
    conda create -y -q -n "$ENVNAME" python=3.11
  fi
  conda activate "$ENVNAME"
  [[ -d "$MWW/.git" ]] || git clone https://github.com/kahrendt/microWakeWord.git "$MWW"
  # audio-metadata from a fork: upstream pins an attrs that breaks
  pip install -q 'git+https://github.com/whatsnowplaying/audio-metadata@d4ebb238e6a401bb1a5aaaac60c9e2b3cb30929f'
  pip install -q -e "$MWW" 'tensorflow[and-cuda]' pyarrow
  # datasets 4+ decodes audio through torchcodec (all of torch); 3.x uses soundfile
  pip install -q "datasets<4" soundfile librosa tensorboard
  # the nvidia wheels can't find each other (cusolver -> nvJitLink) without this, and then
  # tensorflow quietly runs on the CPU
  local nv; nv="$(python -c 'import nvidia; print(list(nvidia.__path__)[0])')"
  mkdir -p "$CONDA_PREFIX/etc/conda/activate.d"
  cat > "$CONDA_PREFIX/etc/conda/activate.d/nvidia_libs.sh" <<SH
export LD_LIBRARY_PATH="\$(ls -d "$nv"/*/lib | tr '\n' :)\${LD_LIBRARY_PATH:-}"
SH
  conda deactivate; conda activate "$ENVNAME"
  python -c "import tensorflow as tf; g=tf.config.list_physical_devices('GPU'); print('GPU:', g); assert g" \
    2>&1 | tail -1

  # piper: reuse the openWakeWord setup (train_wakeword.sh setup) if it's there
  if [[ ! -e "$PIPER" ]]; then
    if [[ -d "$OWW_WORK/piper-sample-generator" ]]; then
      ln -s "$OWW_WORK/piper-sample-generator" "$PIPER"
    else
      echo "ERROR: no piper-sample-generator. Run reedus_llm/reedus_companion/deploy/train_wakeword.sh setup" >&2
      echo "       first (it builds the $TTS_ENV env and the patched checkout)" >&2
      exit 1
    fi
  fi
  # Kokoro: its own env, it wants a much newer torch than piper's
  if ! conda env list | awk '{print $1}' | grep -qx "$KOKORO_ENV"; then
    conda create -y -q -n "$KOKORO_ENV" python=3.11
  fi
  conda activate "$KOKORO_ENV"
  pip install -q kokoro soundfile scipy
  echo "setup ok"
}

data() {
  activate "$ENVNAME"
  mkdir -p "$WORK/negative_datasets"
  local root="https://huggingface.co/datasets/kahrendt/microwakeword/resolve/main"
  for f in dinner_party dinner_party_eval no_speech speech; do
    if [[ -d "$WORK/negative_datasets/$f" ]]; then echo "  have $f"; continue; fi
    wget -c -q --show-progress --progress=dot:giga -O "$WORK/negative_datasets/$f.zip" "$root/$f.zip"
    unzip -q "$WORK/negative_datasets/$f.zip" -d "$WORK/negative_datasets"
    rm "$WORK/negative_datasets/$f.zip"
  done
  python "$HERE/mww_data.py" --root "$WORK" --oww-rirs "$OWW_WORK/mit_rirs"
  # LibriSpeech train-clean-100 (6 GB): clean close-up speech for negatives. test-clean is
  # separate, for measuring false wakes (mww_eval.py)
  mkdir -p "$WORK/librispeech_train"
  for i in $(seq -w 0 13); do
    local f="$WORK/librispeech_train/00$i.parquet"
    [[ -s "$f" ]] || { wget -c -q -O "$f.part" \
      "https://huggingface.co/datasets/openslr/librispeech_asr/resolve/main/all/train.clean.100/00$i.parquet" \
      && mv "$f.part" "$f"; }
  done
}

gen() {  # gen <text> <count> <outdir>
  local dir="$3"
  if [[ -d "$dir" ]] && (( $(find "$dir" -name '*.wav' | wc -l) >= $2 )); then return; fi
  rm -rf "$dir"
  python "$PIPER/generate_samples.py" "$1" --max-samples "$2" --batch-size 200 \
    --length-scales 0.8 0.9 1.0 1.1 1.25 --noise-scales 0.6 0.667 0.75 0.85 0.95 \
    --noise-scale-ws 0.6 0.8 1.0 --output-dir "$dir" 2>&1 | grep -v -i -E "warn|resampl|Batch" || true
}

slug() {  # "readus!" and "readus?" must not land in the same folder
  echo "$1" | tr 'A-Z' 'a-z' | sed -e 's/!/_bang/g' -e 's/?/_q/g' | tr -c 'a-z_\n' '_'
}

samples() {
  activate "$TTS_ENV"
  local out="$WORK/samples"
  for p in "${POSITIVES[@]}"; do
    local text="${p%%:*}" frac="${p##*:}"
    local n; n=$(python -c "print(int($N_POS*$frac))")
    echo "  + \"$text\" x $n"
    gen "$text" "$n" "$out/positive/$(slug "$text")"
  done
  for w in "${SOUNDALIKES[@]}"; do
    echo "  - \"$w\" x $N_NEG"
    gen "$w" "$N_NEG" "$out/soundalike/$(slug "$w")"
  done
  activate "$KOKORO_ENV"
  for p in "${KOKORO_POS[@]}"; do
    local ph="${p%%:*}" frac="${p##*:}"
    local n; n=$(python -c "print(int($N_KOKORO*$frac))")
    python "$HERE/mww_kokoro.py" --phonemes "$ph" --n "$n" \
      --out "$out/positive_kokoro/$(printf %s "$ph" | md5sum | cut -c1-6)" 2>&1 | grep -E "clips|voices"
  done
  python "$HERE/mww_kokoro.py" --texts "${SOUNDALIKES[@]}" --n "$N_KOKORO_NEG" \
    --out "$out/soundalike_kokoro" 2>&1 | grep -c clips | sed 's/$/ Kokoro sound-alikes/'
  echo "positives: $(find "$out/positive" "$out/positive_kokoro" -name '*.wav' | wc -l)" \
       " sound-alikes: $(find "$out/soundalike" "$out/soundalike_kokoro" -name '*.wav' | wc -l)"
}

features() {
  activate "$ENVNAME"
  python "$HERE/mww_features.py" --clips "$WORK/samples/positive" --out "$WORK/features/positive" --work "$WORK"
  python "$HERE/mww_features.py" --clips "$WORK/samples/soundalike" --out "$WORK/features/soundalike" --work "$WORK"
  for s in positive_kokoro soundalike_kokoro; do
    python "$HERE/mww_features.py" --clips "$WORK/samples/$s" --out "$WORK/features/$s" --work "$WORK"
  done
  python "$HERE/mww_speech.py" --parquet "$WORK/librispeech_train" --out "$WORK/features/librispeech" --work "$WORK"
}

write_config() {
  mkdir -p "$WORK/trained_models"
  cat > "$WORK/trained_models/$RUN.yaml" <<YAML
window_step_ms: 10
train_dir: $WORK/trained_models/$RUN
clip_duration_ms: 1500
batch_size: 128
eval_step_interval: 500

features:
  - {features_dir: $WORK/features/positive, sampling_weight: 2.0, penalty_weight: 1.0, truth: true, truncation_strategy: truncate_start, type: mmap}
  - {features_dir: $WORK/features/positive_kokoro, sampling_weight: 1.5, penalty_weight: 1.0, truth: true, truncation_strategy: truncate_start, type: mmap}
  - {features_dir: $WORK/features/soundalike, sampling_weight: 3.0, penalty_weight: 1.5, truth: false, truncation_strategy: truncate_start, type: mmap}
  - {features_dir: $WORK/features/soundalike_kokoro, sampling_weight: 1.5, penalty_weight: 1.5, truth: false, truncation_strategy: truncate_start, type: mmap}
  - {features_dir: $WORK/features/librispeech, sampling_weight: 8.0, penalty_weight: 1.0, truth: false, truncation_strategy: random, type: mmap}
  - {features_dir: $WORK/negative_datasets/speech, sampling_weight: 10.0, penalty_weight: 1.0, truth: false, truncation_strategy: random, type: mmap}
  - {features_dir: $WORK/negative_datasets/dinner_party, sampling_weight: 10.0, penalty_weight: 1.0, truth: false, truncation_strategy: random, type: mmap}
  - {features_dir: $WORK/negative_datasets/no_speech, sampling_weight: 5.0, penalty_weight: 1.0, truth: false, truncation_strategy: random, type: mmap}
  - {features_dir: $WORK/negative_datasets/dinner_party_eval, sampling_weight: 0.0, penalty_weight: 1.0, truth: false, truncation_strategy: split, type: mmap}

# two phases: learn it, then settle at a lower rate with negatives weighted harder
training_steps: [${REEDUS_MWW_STEPS:-40000, 20000}]
learning_rates: [0.001, 0.0002]
positive_class_weight: [1, 1]
negative_class_weight: [20, 25]
# light SpecAugment: TTS voices are cleaner and more alike than people are
time_mask_max_size: [5, 5]
time_mask_count: [2, 2]
freq_mask_max_size: [5, 5]
freq_mask_count: [2, 2]

# best checkpoint = most recall averaged over the cutoffs that keep false wakes under 2 an hour
target_minimization: 0.9
minimization_metric: null
maximization_metric: average_viable_recall
YAML
  echo "wrote $WORK/trained_models/$RUN.yaml"
}

train() {
  activate "$ENVNAME"
  write_config
  cd "$WORK"
  python -m microwakeword.model_train_eval \
    --training_config "$WORK/trained_models/$RUN.yaml" \
    --train 1 --restore_checkpoint 1 \
    --test_tf_nonstreaming 0 --test_tflite_nonstreaming 0 \
    --test_tflite_nonstreaming_quantized 0 --test_tflite_streaming 0 \
    --test_tflite_streaming_quantized 1 \
    --use_weights best_weights \
    mixednet \
    --pointwise_filters "64,64,64,64" \
    --repeat_in_block "1,1,1,1" \
    --mixconv_kernel_sizes '[5],[7,11],[9,15],[23]' \
    --residual_connection "0,0,0,0" \
    --first_conv_filters 32 \
    --first_conv_kernel_size 5 \
    --stride 3
}

ship() {
  local src="$WORK/trained_models/$RUN/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite"
  [[ -f "$src" ]] || { echo "ERROR: no $src, run train first" >&2; exit 1; }
  mkdir -p "$HERE/models"
  cp "$src" "$HERE/models/$NAME.tflite"
  local cutoff="${REEDUS_MWW_CUTOFF:-0.90}" window="${REEDUS_MWW_WINDOW:-5}"
  # ESPHome's micro_wake_word v2 manifest. our firmware reads the same fields
  cat > "$HERE/models/$NAME.json" <<JSON
{
  "type": "micro",
  "wake_word": "Reedus",
  "author": "Reedus",
  "website": "https://github.com/Danial-Sy/ReedusAIAssistant",
  "model": "$NAME.tflite",
  "trained_languages": ["en"],
  "version": 2,
  "micro": {
    "probability_cutoff": $cutoff,
    "sliding_window_size": $window,
    "feature_step_size": 10,
    "tensor_arena_size": 30000,
    "minimum_esphome_version": "2024.7.0"
  }
}
JSON
  ls -l "$HERE/models/"
}

case "${1:-all}" in
  setup)    setup ;;
  data)     data ;;
  samples)  samples ;;
  features) features ;;
  config)   write_config ;;
  train)    train ;;
  ship)     ship ;;
  all)      setup; data; samples; features; train; ship ;;
  *) echo "usage: $0 {setup|data|samples|features|config|train|ship|all}" >&2; exit 2 ;;
esac
