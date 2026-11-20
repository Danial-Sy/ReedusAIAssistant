// microWakeWord's feature path: 16 kHz int16 audio in, one 40-channel int8 slice out every
// 10 ms, ready to copy into the model's input tensor. the frontend settings are the ones every
// microWakeWord model is trained with (pymicro-features / ESPHome micro_wake_word
// preprocessor_settings.h): 30 ms window, 10 ms step, 40 mel channels 125-7500 Hz, noise
// reduction, PCAN, log scale. no Arduino here, so tools/check_wake_features.py can build it
// on the laptop and compare it with pymicro_features.
#pragma once
#include <stddef.h>
#include <stdint.h>

namespace mww {

static const int FEATURES = 40;      // channels per slice
static const int STEP_MS = 10;       // one slice per 10 ms (the manifest's feature_step_size)
static const int WINDOW_MS = 30;
static const int SAMPLE_RATE = 16000;

// the frontend's uint16 output (roughly 0-670) -> the model's int8 input. training divides by
// 25.6 and the int8 input quantization covers 0-26, so input = feature * 256 / 666 - 128,
// rounded, in integers (ESPHome micro_wake_word.cpp generate_features_)
int8_t quantize(uint16_t feature);

class Features {
 public:
  bool begin();  // allocates the frontend's tables (~4 KB); false if malloc failed
  void end();
  void reset();  // forget the noise estimate and gain, start over

  // consumes up to n samples. returns how many it used; *ready is true when that completed a
  // slice, which is then in out (int8, FEATURES) and, if given, raw (the uint16 values).
  // call again with the rest of the buffer until it's all used.
  size_t process(const int16_t* samples, size_t n, int8_t* out, bool* ready,
                 uint16_t* raw = nullptr);

 private:
  void* state_ = nullptr;  // FrontendState, kept opaque so this header stays light
};

}  // namespace mww
