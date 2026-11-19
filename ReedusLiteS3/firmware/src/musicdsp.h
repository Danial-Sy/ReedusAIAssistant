// music on Lite's speaker: the parts that are pure math, so they can be tested on a laptop
// (no I2S, no FreeRTOS). audio.cpp owns the buffer and calls these from the audio task.
//
//   music arrives at 24 kHz mono -> Up2 doubles it to the 48 kHz I2S rate
//   Ducker pulls the music down under speech and while someone's talking to Reedus
//   Edge fades music in when it starts and out when it stops, so neither clicks
#pragma once
#include <math.h>
#include <stdint.h>
#include <string.h>

namespace musicdsp {

// 64 taps, Kaiser(7) windowed sinc, 11.25 kHz cutoff at 48 kHz, gain 2 (zero-stuffing).
// flat to 10 kHz (-0.16 dB), images of the 24 kHz input down 76 dB by 14 kHz.
// regenerate: fs=48000, N=64, beta=7, fc=11250, normalised so the taps sum to 2
static const int MTAPS = 64;
static const float MFIR[MTAPS] = {
    0.00008050f, 0.00019141f, -0.00020910f, -0.00057584f, 0.00031750f, 0.00129786f,
    -0.00026999f, -0.00246403f, -0.00015892f, 0.00413211f, 0.00128945f, -0.00626498f,
    -0.00352661f, 0.00868305f, 0.00733719f, -0.01102045f, -0.01322238f, 0.01268601f,
    0.02170539f, -0.01281664f, -0.03337250f, 0.01017959f, 0.04905330f, -0.00289089f,
    -0.07038170f, -0.01250168f, 0.10164604f, 0.04459532f, -0.15795660f, -0.12826139f,
    0.33838025f, 0.85431873f, 0.85431873f, 0.33838025f, -0.12826139f, -0.15795660f, 0.04459532f,
    0.10164604f, -0.01250168f, -0.07038170f, -0.00289089f, 0.04905330f, 0.01017959f,
    -0.03337250f, -0.01281664f, 0.02170539f, 0.01268601f, -0.01322238f, -0.01102045f,
    0.00733719f, 0.00868305f, -0.00352661f, -0.00626498f, 0.00128945f, 0.00413211f,
    -0.00015892f, -0.00246403f, -0.00026999f, 0.00129786f, 0.00031750f, -0.00057584f,
    -0.00020910f, 0.00019141f, 0.00008050f};

// 24 kHz -> 48 kHz, N input samples per call -> 2N outputs. keeps history across calls
template <int N>
struct Up2 {
  static const int T = MTAPS / 2;  // taps per phase
  float hist[T - 1 + N];
  Up2() { reset(); }
  void reset() { memset(hist, 0, sizeof(hist)); }
  void process(const float* in, float* out) {
    const int H = T - 1;
    for (int i = 0; i < N; i++) hist[H + i] = in[i];
    for (int n = 0; n < N; n++) {
      float a0 = 0.0f, a1 = 0.0f;
      for (int k = 0; k < T; k++) {
        float x = hist[H + n - k];
        a0 += x * MFIR[2 * k];
        a1 += x * MFIR[2 * k + 1];
      }
      out[2 * n] = a0;
      out[2 * n + 1] = a1;
    }
    memmove(hist, &hist[N], H * sizeof(float));
  }
};

// a gain that glides: quick down (so Reedus is heard at once), slow back up (so it doesn't pump)
struct Ducker {
  float g = 1.0f;
  float down, up;  // per-sample coefficients
  Ducker(float rate = 48000.0f, float attackS = 0.06f, float releaseS = 0.45f) {
    down = 1.0f - expf(-1.0f / (attackS * rate));
    up = 1.0f - expf(-1.0f / (releaseS * rate));
  }
  void apply(float target, float* buf, int n) {
    for (int i = 0; i < n; i++) {
      g += (target - g) * (target < g ? down : up);
      buf[i] *= g;
    }
  }
};

// start: a short fade in. stop (flush or a late network): the last sample slides to zero
// instead of the speaker cone jumping. FADE samples at the input rate (~5 ms at 24 kHz)
struct Edge {
  static const int FADE = 120;
  float last = 0.0f;
  int in = FADE;   // < FADE while fading in
  int tail = 0;    // > 0 while fading out
  void start() { in = 0; }
  // got = how many of buf's n samples are real music; the rest get the fade-out or silence
  void apply(float* buf, int got, int n) {
    for (int i = 0; i < n; i++) {
      if (i < got) {
        float s = buf[i];
        if (in < FADE) s *= (float)(in++) / FADE;
        buf[i] = last = s;
        tail = FADE;
      } else if (tail > 0) {
        buf[i] = last * (float)tail / FADE;
        tail--;
      } else {
        buf[i] = 0.0f;
      }
    }
  }
};

}  // namespace musicdsp
