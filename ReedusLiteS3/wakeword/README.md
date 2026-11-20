# "Reedus" wake word (microWakeWord)

The on-device wake word for Reedus Lite (roadmap step 2): a ~62 KB int8 streaming model the XIAO ESP32-S3 runs on its own mic (`firmware/src/wake.cpp`), so nothing leaves Lite until you say the name. It's just **"Reedus"**, no "hey". It's trained with [microWakeWord](https://github.com/kahrendt/microWakeWord) on synthetic speech, on a CUDA box (a 3090 Ti takes about 2 h end to end, most of it unattended).

| file | |
|---|---|
| `models/reedus.tflite`, `models/reedus.json` | the model and its manifest (ESPHome `micro_wake_word` v2 format: cutoff, window, arena size) |
| `train_mww.sh` | the whole pipeline, in stages |
| `mww_data.py` | background audio for augmentation (AudioSet, FMA, MIT room impulse responses) |
| `mww_kokoro.py` | the second TTS engine (Kokoro), blended voices, with 4 voices held out for testing |
| `mww_features.py` | augments clips and writes spectrogram features, sharded over all cores |
| `mww_speech.py` | LibriSpeech train-clean-100 as clean-speech negatives |
| `mww_eval.py` | runs the `.tflite` the way the S3 will (streaming + sliding window): recall per folder, false wakes per hour on long audio |

## Running it

```bash
bash ReedusLiteS3/wakeword/train_mww.sh all
```

Or stage by stage: `setup`, `data` (~35 GB, resumable), `samples`, `features`, `train`, `ship`. The big files live in `~/reedus_mww` (`REEDUS_MWW_WORK`), not in the repo. `REEDUS_MWW_RUN=<name>` trains into its own folder, so one try doesn't overwrite another. `ship` copies the model into `models/`, and `REEDUS_MWW_CUTOFF` / `REEDUS_MWW_WINDOW` set the manifest's threshold.

Piper (the first TTS engine) runs in the `reedus-wakeword` env from the openWakeWord setup (`reedus_llm/reedus_companion/deploy/train_wakeword.sh setup`). Kokoro gets its own env, because it needs a much newer torch.

## Pronunciation and what counts as "Reedus"

- espeak (and so piper) says `reedus` as ree-ED-us. The positives are spelled **`readus`** (ɹˈiːdəs) and `readiss`, with `!`/`?` for prosody. Kokoro gets phonemes directly: `ɹˈidəs`, `ɹˈidɪs`, and the flapped **`ɹˈiTəs`**, which is how most Americans actually say it.
- **"read us" and "Rita's" sound the same as the name**, so they're deliberately not negatives: they will wake him. Utterances containing them are also dropped from the speech negatives.
- Two syllables is short for a wake word, so false wakes are the main risk. The sound-alike list in `train_mww.sh` (readers, radius, Regis, greatest, bade us…) does most of the work. When the model wakes on something in real use, add that word to `SOUNDALIKES` and retrain.

## Checking a model

microWakeWord's own test uses held-out piper clips, which flatters recall. `mww_eval.py` adds three sets it never trained on:

```bash
python mww_eval.py models/reedus.tflite --pos ~/reedus_mww/ood/kokoro_holdout_pos ~/reedus_mww/ood/espeak \
    --neg ~/reedus_mww/ood/kokoro_holdout_neg --long ~/reedus_mww/ood/librispeech_test_clean.wav
```

- `kokoro_holdout_*`: 4 Kokoro voices never used in training, saying the name and sound-alikes
- `espeak`: a third, very robotic engine, as a worst case for recall
- `librispeech_test_clean.wav`: 5.4 h of real read speech, for false wakes per hour (speakers disjoint from the training speech)

### The shipped model (v2, 2026-11-19), at cutoff 0.90, window 5

| | v1 (piper only) | **v2 (shipped)** |
|---|---|---|
| held-out Kokoro voices, "REE-dus" | 91% | **99%** |
| held-out Kokoro voices, flapped "REE-ɾus" | 29% | **85%** |
| espeak (robotic, worst case) | 30% | 43% |
| false wakes, LibriSpeech test-clean (5.4 h of nonstop speech) | 6.7/h | **0.6/h** |
| false wakes, dinner-party audio (microWakeWord's own test) | 0.56/h | **≤0.19/h** |
| sound-alikes that still wake him: "readers" / "greatest" / "Regis" | 19% / 18% / 23% | 35% / 15% / 6% |

"readers" is the weak spot (British "REE-duhz" is nearly the name). The cutoff lives in `models/reedus.json`, so it can be tuned without retraining: 0.95 cuts LibriSpeech false wakes to 0.4/h and "readers" to 28%, at the cost of ~8 points of recall on the flapped form. On the S3, `wake 0.95` in the serial monitor tries a cutoff live, and `wake` shows the probabilities as you talk; to keep one, set `WAKE_CUTOFF` in `firmware/src/config.h` (or edit the .json) and rebuild.

## In the firmware

`pio run` embeds `models/reedus.tflite` and its manifest (`firmware/tools/embed_wake_model.py`, so a retrained model only needs a rebuild). The S3 computes features with the same TFLite Micro frontend microWakeWord trains with (`firmware/lib/microfrontend`), runs the model on TFLite Micro with Espressif's ESP-NN kernels, and decides like ESPHome's `micro_wake_word`: the average of the last `sliding_window_size` probabilities over the cutoff, then 2 s before the next wake. `firmware/tools/check_wake_features.py` builds that feature path on the laptop and checks it against pymicro_features and this folder's eval path.

The real test is your voice through the XVF3800: record yourself saying "Reedus" 30 times, plus a long stretch of ordinary talking or TV, as 16 kHz mono wavs, and run them through `--pos` and `--long`.

The training data has mixed licenses (AudioSet, FMA, LibriSpeech, kahrendt's negative sets), so the model is for personal, non-commercial use.
