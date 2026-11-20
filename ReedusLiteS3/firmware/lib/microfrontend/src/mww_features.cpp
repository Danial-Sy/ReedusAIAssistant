#include "mww_features.h"

#include <stdlib.h>

#include "tensorflow/lite/experimental/microfrontend/lib/frontend.h"
#include "tensorflow/lite/experimental/microfrontend/lib/frontend_util.h"

namespace mww {

int8_t quantize(uint16_t feature) {
  const int32_t scale = 256, div = 666;  // 666 = 25.6 * 26.0, rounded
  int32_t v = ((int32_t)feature * scale + div / 2) / div - 128;
  if (v < -128) v = -128;
  if (v > 127) v = 127;
  return (int8_t)v;
}

static void configure(FrontendConfig* c) {
  c->window.size_ms = WINDOW_MS;
  c->window.step_size_ms = STEP_MS;
  c->filterbank.num_channels = FEATURES;
  c->filterbank.lower_band_limit = 125.0f;
  c->filterbank.upper_band_limit = 7500.0f;
  c->noise_reduction.smoothing_bits = 10;
  c->noise_reduction.even_smoothing = 0.025f;
  c->noise_reduction.odd_smoothing = 0.06f;
  c->noise_reduction.min_signal_remaining = 0.05f;
  c->pcan_gain_control.enable_pcan = 1;
  c->pcan_gain_control.strength = 0.95f;
  c->pcan_gain_control.offset = 80.0f;
  c->pcan_gain_control.gain_bits = 21;
  c->log_scale.enable_log = 1;
  c->log_scale.scale_shift = 6;
}

bool Features::begin() {
  if (state_) return true;
  FrontendState* s = (FrontendState*)calloc(1, sizeof(FrontendState));
  if (!s) return false;
  FrontendConfig cfg;
  configure(&cfg);
  if (!FrontendPopulateState(&cfg, s, SAMPLE_RATE)) {
    free(s);
    return false;
  }
  state_ = s;
  return true;
}

void Features::end() {
  if (!state_) return;
  FrontendFreeStateContents((FrontendState*)state_);
  free(state_);
  state_ = nullptr;
}

void Features::reset() {
  if (state_) FrontendReset((FrontendState*)state_);
}

size_t Features::process(const int16_t* samples, size_t n, int8_t* out, bool* ready,
                         uint16_t* raw) {
  *ready = false;
  if (!state_ || !n) return n;
  size_t used = 0;
  FrontendOutput o = FrontendProcessSamples((FrontendState*)state_, samples, n, &used);
  if (o.size == (size_t)FEATURES) {
    for (int i = 0; i < FEATURES; i++) {
      out[i] = quantize(o.values[i]);
      if (raw) raw[i] = o.values[i];
    }
    *ready = true;
  }
  return used;
}

}  // namespace mww
