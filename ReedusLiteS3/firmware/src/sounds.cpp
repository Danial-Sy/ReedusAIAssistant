#include "sounds.h"

#include "audio.h"
#include "config.h"

namespace sounds {

struct Clip {
  int16_t* pcm = nullptr;
  size_t samples = 0;
};

static Clip wakeClip, ringClip;

// sine notes with a short fade each side (no clicks), a gap after every note: the same recipe
// as the run box's jingle.py, so it's the sound Reedus has always made
static Clip render(const float* freqs, const float* durs, int n, float gap, float gain, float fade) {
  Clip c;
  for (int i = 0; i < n; i++) c.samples += (size_t)(AUDIO_RATE * (durs[i] + gap));
  c.pcm = (int16_t*)ps_malloc(c.samples * 2);
  if (!c.pcm) c.pcm = (int16_t*)malloc(c.samples * 2);
  if (!c.pcm) {
    c.samples = 0;
    return c;
  }
  size_t k = 0;
  for (int i = 0; i < n; i++) {
    size_t len = (size_t)(AUDIO_RATE * durs[i]);
    size_t f = (size_t)min((float)(len / 8), AUDIO_RATE * fade);
    for (size_t j = 0; j < len; j++) {
      float env = 1.0f;
      if (f && j < f) env = (float)j / f;
      else if (f && j >= len - f) env = (float)(len - 1 - j) / f;
      float s = freqs[i] > 0 ? sinf(2.0f * PI * freqs[i] * j / AUDIO_RATE) : 0.0f;
      c.pcm[k++] = (int16_t)(gain * env * s * 32767.0f);
    }
    size_t g = (size_t)(AUDIO_RATE * gap);
    for (size_t j = 0; j < g; j++) c.pcm[k++] = 0;
  }
  c.samples = k;
  return c;
}

void begin() {
  // jingle.py "listen": [523.25, 659.25, 783.99] Hz, [0.06, 0.06, 0.10] s, 40 ms gaps, gain 0.20
  static const float wf[] = {523.25f, 659.25f, 783.99f}, wd[] = {0.06f, 0.06f, 0.10f};
  wakeClip = render(wf, wd, 3, 0.04f, CHIME_GAIN, 0.012f);
  static const float rf[] = {880.0f, 1175.0f}, rd[] = {0.11f, 0.11f};
  ringClip = render(rf, rd, 2, 0.07f, 0.30f, 0.008f);
}

static void play(const Clip& c) {
  if (c.pcm && c.samples) audio::pushSpeech((const uint8_t*)c.pcm, c.samples * 2);
}

void wake() {
#if WAKE_CHIME
  play(wakeClip);
#endif
}

void ring() { play(ringClip); }

}  // namespace sounds
