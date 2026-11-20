# microfrontend

The TFLite Micro audio frontend that turns 16 kHz audio into the 40-channel spectrogram microWakeWord models eat. It was dropped from the tflite-micro repo, so this is the copy in [rhasspy/pymicro-features](https://github.com/rhasspy/pymicro-features) 2.0.2 (commit `96bd69c`), the library microWakeWord's training pipeline uses for its features. Using the same code on the S3 is what makes the device see what the model was trained on.

| | |
|---|---|
| `src/tensorflow/lite/experimental/microfrontend/lib/` | the frontend, unchanged except that `kiss_fft_int16.{h,cc}` include the FFT from `mfe_kissfft/` |
| `src/mfe_kissfft/` | kissfft (BSD, Mark Borgerding), flattened from `kissfft/` + `kissfft/tools/`. Only ever compiled inside `kiss_fft_int16.cc` (16-bit fixed point, in its own namespace), so it can't clash with the float kissfft inside ESP_TF |
| `src/mww_features.{h,cpp}` | ours: the frontend set up like microWakeWord / ESPHome `micro_wake_word`, and ESPHome's uint16 → int8 input scaling |

`firmware/tools/check_wake_features.py` compiles this folder natively and checks it against pymicro_features bit for bit.

Licenses: Apache-2.0 (`LICENSE.tensorflow`) for the frontend, BSD-3 for kissfft (in the file headers).
