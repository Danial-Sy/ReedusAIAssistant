#include "audio.h"

#include <driver/i2s.h>
#include <esp_heap_caps.h>
#include <freertos/queue.h>

#include "config.h"
#include "musicdsp.h"

namespace audio {

static volatile float micLvlDb = -90.0f;  // the mic beam's loudness, for the head (micDb)

static const i2s_port_t PORT = I2S_NUM_0;
#if XVF_I2S_MASTER_48K
static const int R = 3;  // I2S runs at 48k, the wire at 16k
#else
static const int R = 1;
#endif
static const int I2S_RATE = AUDIO_RATE * R;
static const size_t I2S_FRAMES = FRAME_SAMPLES * R;  // stereo frames per 20 ms pass
static const size_t PLAY_CAP = AUDIO_RATE * PLAY_BUFFER_MS / 1000;  // samples @16k
static const size_t PREBUFFER = AUDIO_RATE * PLAY_PREBUFFER_MS / 1000;

// 48 taps, Kaiser(7) windowed sinc, 7.2 kHz cutoff at 48 kHz: flat to ~6 kHz, -88 dB by
// 10 kHz. one filter does both jobs (decimate the mics, interpolate the speech)
static const int NTAPS = 48;
static const float FIR[NTAPS] = {
    -0.00001257f, 0.00013718f, 0.00036805f, 0.00028901f, -0.00045833f, -0.00150001f,
    -0.00155210f, 0.00048080f, 0.00373693f, 0.00499174f, 0.00115005f, -0.00672157f,
    -0.01199395f, -0.00697635f, 0.00876816f, 0.02388313f, 0.02140131f, -0.00595189f,
    -0.04308112f, -0.05590659f, -0.01323485f, 0.08677670f, 0.20682965f, 0.28857659f,
    0.28857659f, 0.20682965f, 0.08677670f, -0.01323485f, -0.05590659f, -0.04308112f,
    -0.00595189f, 0.02140131f, 0.02388313f, 0.00876816f, -0.00697635f, -0.01199395f,
    -0.00672157f, 0.00115005f, 0.00499174f, 0.00373693f, 0.00048080f, -0.00155210f,
    -0.00150001f, -0.00045833f, 0.00028901f, 0.00036805f, 0.00013718f, -0.00001257f};

static QueueHandle_t micQ;
static volatile uint32_t micDrops = 0;
// the wake word's own copy of the mic (WAKE_CHANNEL), made once wake.cpp asks for it
static QueueHandle_t wakeQ = nullptr;
static volatile uint32_t wakeDrops = 0;

// speech ring (int16 @16k), producer = websocket callback, consumer = audio task
static int16_t* ring = nullptr;
static size_t rHead = 0, rTail = 0, rCount = 0;
static portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool isPlaying = false;
static volatile uint32_t nUnderruns = 0;
static volatile uint32_t drainedAt = 0;  // millis() the ring last ran dry mid-stream
static volatile float gain = 0.5f;
static volatile int vol = DEFAULT_VOLUME;
static volatile float lvl = 0.0f;
static uint8_t oddByte = 0;
static bool haveOdd = false;

// music ring (int16 @24k): deeper than speech, music can wait for the network, speech can't
#if XVF_I2S_MASTER_48K
static const bool MUSIC = true;
#else
static const bool MUSIC = false;  // the 16 kHz I2S mode has no clean 24k path; voice only
#endif
static const int MUSIC_RATE = 24000;
static const size_t M_FRAMES = FRAME_SAMPLES * 3 / 2;  // 480 @24k per 20 ms pass
static const size_t MCAP = MUSIC_RATE * MUSIC_BUFFER_MS / 1000;
static const size_t MPRE = MUSIC_RATE * MUSIC_PREBUFFER_MS / 1000;
static int16_t* mring = nullptr;
static size_t mHead = 0, mTail = 0, mCount = 0;
static portMUX_TYPE mMux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool mPlaying = false, duckOn = false;
static volatile uint32_t mUnderruns = 0;
static volatile uint32_t mDrainedAt = 0;  // ran dry mid-stream; more within 1 s = the network was late
static volatile float mLvl = 0.0f;
static musicdsp::Up2<M_FRAMES> mUp;
static musicdsp::Ducker ducker;
static musicdsp::Edge edge;

struct MicFrame {
  int16_t s[FRAME_SAMPLES];
};

static int32_t rxBuf[I2S_FRAMES * 2];
static int32_t txBuf[I2S_FRAMES * 2];
// filter state: input history, newest last
static float micHist[NTAPS - 1 + I2S_FRAMES];
#if WAKE_CHANNEL != MIC_CHANNEL
static float wakeHist[NTAPS - 1 + I2S_FRAMES];
#endif
static float spkHist[NTAPS / 3 - 1 + FRAME_SAMPLES];

static void mPush(const int16_t* s, size_t n) {
  portENTER_CRITICAL(&mMux);
  if (!mPlaying && mDrainedAt && millis() - mDrainedAt < 1000) {
    mUnderruns++;
    mDrainedAt = 0;
  }
  for (size_t i = 0; i < n; i++) {
    if (mCount == MCAP) {  // full: the server's pacing went wrong, drop the oldest
      mTail = (mTail + 1) % MCAP;
      mCount--;
    }
    mring[mHead] = s[i];
    mHead = (mHead + 1) % MCAP;
    mCount++;
  }
  portEXIT_CRITICAL(&mMux);
}

// returns how many samples were real music; sets *started when playback just (re)began
static size_t mPop(int16_t* out, size_t n, bool* started) {
  portENTER_CRITICAL(&mMux);
  size_t got = 0;
  *started = false;
  if (!mPlaying && mCount >= MPRE) {
    mPlaying = true;
    *started = true;
  }
  if (mPlaying) {
    got = n < mCount ? n : mCount;
    for (size_t i = 0; i < got; i++) {
      out[i] = mring[mTail];
      mTail = (mTail + 1) % MCAP;
    }
    mCount -= got;
    if (mCount == 0) {  // ran dry: end of the music, or a late network (mPush tells which)
      mPlaying = false;  // either way, wait for a full prebuffer rather than stutter
      mDrainedAt = millis();
    }
  }
  portEXIT_CRITICAL(&mMux);
  return got;
}

static void ringPush(const int16_t* s, size_t n) {
  // more speech right after the ring ran dry = the network was late, not the reply ending
  if (!isPlaying && drainedAt && millis() - drainedAt < 300) {
    nUnderruns++;
    drainedAt = 0;
  }
  portENTER_CRITICAL(&ringMux);
  for (size_t i = 0; i < n; i++) {
    if (rCount == PLAY_CAP) {  // full: drop the oldest, the server's pacing shouldn't allow it
      rTail = (rTail + 1) % PLAY_CAP;
      rCount--;
    }
    ring[rHead] = s[i];
    rHead = (rHead + 1) % PLAY_CAP;
    rCount++;
  }
  portEXIT_CRITICAL(&ringMux);
}

static size_t ringPop(int16_t* out, size_t n) {
  portENTER_CRITICAL(&ringMux);
  size_t got = 0;
  if (!isPlaying && rCount >= PREBUFFER) isPlaying = true;
  if (isPlaying) {
    got = n < rCount ? n : rCount;
    for (size_t i = 0; i < got; i++) {
      out[i] = ring[rTail];
      rTail = (rTail + 1) % PLAY_CAP;
    }
    rCount -= got;
    if (rCount == 0) {  // drained: end of a reply, or a late network (ringPush tells which)
      isPlaying = false;
      drainedAt = millis();
    }
  }
  portEXIT_CRITICAL(&ringMux);
  return got;
}

// I2S RX (I2S_FRAMES stereo @ I2S_RATE), one slot -> FRAME_SAMPLES mono @16k
static void micDown(int16_t* out, int channel, float* x) {
  const int H = NTAPS - 1;
  for (size_t i = 0; i < I2S_FRAMES; i++)
    x[H + i] = (float)(rxBuf[i * 2 + channel] >> 16) * MIC_GAIN;
  for (size_t n = 0; n < FRAME_SAMPLES; n++) {
    float acc;
    if (R == 1) {
      acc = x[H + n];
    } else {
      const float* p = &x[n * R + R - 1];  // the NTAPS inputs ending at block input n*R + R-1
      acc = 0.0f;
      for (int k = 0; k < NTAPS; k++) acc += p[k] * FIR[k];
    }
    out[n] = (int16_t)constrain(acc, -32768.0f, 32767.0f);
  }
  memmove(x, &x[I2S_FRAMES], H * sizeof(float));
}

// FRAME_SAMPLES mono @16k -> I2S_FRAMES mono @ I2S_RATE, as floats in int16 units
static void spkUp(const float* in, float* out) {
  const int T = NTAPS / 3;  // taps per phase
  const int H = T - 1;
  float* x = spkHist;
  for (size_t i = 0; i < FRAME_SAMPLES; i++) x[H + i] = in[i];
  for (size_t n = 0; n < FRAME_SAMPLES; n++) {
    for (int p = 0; p < R; p++) {
      float acc;
      if (R == 1) {
        acc = x[H + n];
      } else {
        // zero-stuffed by 3 then filtered: phase p uses taps p, p+3, ... (gain 3)
        acc = 0.0f;
        for (int k = 0; k < T; k++) acc += x[H + n - k] * FIR[p + k * 3];
        acc *= 3.0f;
      }
      out[n * R + p] = acc;
    }
  }
  memmove(x, &x[FRAME_SAMPLES], H * sizeof(float));
}

// newest audio wins when the reader is behind
static void sendNewest(QueueHandle_t q, const MicFrame* f, volatile uint32_t* drops) {
  if (xQueueSend(q, f, 0) != pdTRUE) {
    MicFrame old;
    xQueueReceive(q, &old, 0);
    xQueueSend(q, f, 0);
    (*drops)++;
  }
}

static void audioTask(void*) {
  static int16_t play[FRAME_SAMPLES];
  static float playF[FRAME_SAMPLES];
  static float out48[I2S_FRAMES];
  static int16_t mus[M_FRAMES];
  static float musF[M_FRAMES];
  static float mus48[I2S_FRAMES];
  MicFrame f;
  for (;;) {
    size_t n = 0;
    i2s_read(PORT, rxBuf, sizeof(rxBuf), &n, portMAX_DELAY);
    if (n == sizeof(rxBuf)) {
      micDown(f.s, MIC_CHANNEL, micHist);
      {
        float acc2 = 0.0f;
        for (int i = 0; i < FRAME_SAMPLES; i++) acc2 += (float)f.s[i] * (float)f.s[i];
        float db = 10.0f * log10f(acc2 / FRAME_SAMPLES / (32768.0f * 32768.0f) + 1e-12f);
        // fast up, slow down: a voice registers at once and lasts between syllables
        micLvlDb = db > micLvlDb ? db : micLvlDb + 0.07f * (db - micLvlDb);
      }
      sendNewest(micQ, &f, &micDrops);  // net is behind: keep the newest audio
      if (wakeQ) {
#if WAKE_CHANNEL != MIC_CHANNEL
        static MicFrame w;
        micDown(w.s, WAKE_CHANNEL, wakeHist);
        sendNewest(wakeQ, &w, &wakeDrops);
#else
        sendNewest(wakeQ, &f, &wakeDrops);
#endif
      }
    }

    size_t got = ringPop(play, FRAME_SAMPLES);
    float g = gain, acc = 0.0f;
    for (size_t i = 0; i < FRAME_SAMPLES; i++) {
      float v = i < got ? play[i] * g : 0.0f;
      playF[i] = v;
      acc += v * v;
    }
    spkUp(playF, out48);
    // mouth: fast attack, slower release, scaled so normal speech reaches ~0.8
    float rms = sqrtf(acc / FRAME_SAMPLES) / 32768.0f;
    float target = constrain(rms * 5.0f, 0.0f, 1.0f);
    lvl = target > lvl ? target : lvl * 0.82f + target * 0.18f;

    // music under it: fade edges at 24k, double to 48k, duck under speech / while listening
    if (MUSIC) {
      bool started;
      size_t mgot = mPop(mus, M_FRAMES, &started);
      if (started) edge.start();
      float macc = 0.0f;
      for (size_t i = 0; i < M_FRAMES; i++) {
        musF[i] = i < mgot ? (float)mus[i] : 0.0f;
        macc += musF[i] * musF[i];
      }
      edge.apply(musF, (int)mgot, (int)M_FRAMES);
      mUp.process(musF, mus48);
      ducker.apply(isPlaying || duckOn ? MUSIC_DUCK : 1.0f, mus48, (int)I2S_FRAMES);
      for (size_t i = 0; i < I2S_FRAMES; i++) out48[i] += mus48[i] * g;
      float mr = constrain(sqrtf(macc / M_FRAMES) / 32768.0f * 4.0f, 0.0f, 1.0f);
      mLvl = mr > mLvl ? mLvl * 0.5f + mr * 0.5f : mLvl * 0.9f + mr * 0.1f;
    }
    for (size_t i = 0; i < I2S_FRAMES; i++) {
      int32_t s = (int32_t)constrain(out48[i], -32768.0f, 32767.0f) << 16;
      txBuf[i * 2] = s;
      txBuf[i * 2 + 1] = s;
    }
    size_t w = 0;
    i2s_write(PORT, txBuf, sizeof(txBuf), &w, portMAX_DELAY);
  }
}

bool begin() {
  ring = (int16_t*)heap_caps_malloc(PLAY_CAP * sizeof(int16_t), MALLOC_CAP_SPIRAM);
  if (!ring) ring = (int16_t*)malloc(PLAY_CAP * sizeof(int16_t));
  if (!ring) return false;
  if (MUSIC) {
    mring = (int16_t*)heap_caps_malloc(MCAP * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!mring) return false;
  }
  memset(micHist, 0, sizeof(micHist));
#if WAKE_CHANNEL != MIC_CHANNEL
  memset(wakeHist, 0, sizeof(wakeHist));
#endif
  memset(spkHist, 0, sizeof(spkHist));
  micQ = xQueueCreate(25, sizeof(MicFrame));  // 0.5 s of slack for a wifi hiccup
  setVolume(DEFAULT_VOLUME);

  i2s_config_t cfg = {};
  // master firmware: the XVF3800 clocks the bus. otherwise Seeed's sketches: XIAO is master
  cfg.mode = (i2s_mode_t)((XVF_I2S_MASTER_48K ? I2S_MODE_SLAVE : I2S_MODE_MASTER) |
                          I2S_MODE_TX | I2S_MODE_RX);
  cfg.sample_rate = I2S_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 6;
  cfg.dma_buf_len = I2S_FRAMES / 2;  // 10 ms per DMA buffer
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = true;  // silence, not a stuck buffer, if we ever stall
  cfg.fixed_mclk = 0;
  if (i2s_driver_install(PORT, &cfg, 0, nullptr) != ESP_OK) return false;

  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;  // GPIO9 is the board's MCLK net: never drive it
  pins.bck_io_num = PIN_I2S_BCK;
  pins.ws_io_num = PIN_I2S_WS;
  pins.data_out_num = PIN_I2S_DOUT;
  pins.data_in_num = PIN_I2S_DIN;
  if (i2s_set_pin(PORT, &pins) != ESP_OK) return false;
  i2s_zero_dma_buffer(PORT);

  // core 1 with the Arduino loop; wifi lives on core 0
  xTaskCreatePinnedToCore(audioTask, "audio", 6144, nullptr, 6, nullptr, 1);
  return true;
}

bool popMicFrame(int16_t* out) {
  MicFrame f;
  if (xQueueReceive(micQ, &f, 0) != pdTRUE) return false;
  memcpy(out, f.s, sizeof(f.s));
  return true;
}

uint32_t micDropped() { return micDrops; }

void startWakeFeed() {
  if (!wakeQ) wakeQ = xQueueCreate(10, sizeof(MicFrame));  // 200 ms
}

bool waitWakeFrame(int16_t* out, uint32_t timeoutMs) {
  MicFrame f;
  if (!wakeQ || xQueueReceive(wakeQ, &f, pdMS_TO_TICKS(timeoutMs)) != pdTRUE) return false;
  memcpy(out, f.s, sizeof(f.s));
  return true;
}

uint32_t wakeDropped() { return wakeDrops; }

void pushSpeech(const uint8_t* pcm, size_t bytes) {
  // websocket frames are always whole samples from our server, but don't trust it
  static int16_t tmp[512];
  size_t i = 0;
  if (haveOdd && bytes) {
    tmp[0] = (int16_t)(oddByte | (pcm[0] << 8));
    ringPush(tmp, 1);
    haveOdd = false;
    i = 1;
  }
  while (i + 1 < bytes) {
    size_t n = 0;
    while (n < 512 && i + 1 < bytes) {
      tmp[n++] = (int16_t)(pcm[i] | (pcm[i + 1] << 8));
      i += 2;
    }
    ringPush(tmp, n);
  }
  if (i < bytes) {
    oddByte = pcm[i];
    haveOdd = true;
  }
}

void flush() {
  portENTER_CRITICAL(&ringMux);
  rHead = rTail = rCount = 0;
  isPlaying = false;
  drainedAt = 0;
  portEXIT_CRITICAL(&ringMux);
  haveOdd = false;
}

void setVolume(int level) {
  vol = constrain(level, 0, 100);
  // perceptual-ish: 70 -> ~0.5, 100 -> 1.0
  float x = vol / 100.0f;
  gain = x * x;
}

int volume() { return vol; }

uint32_t bufferedMs() {
  portENTER_CRITICAL(&ringMux);
  size_t c = rCount;
  portEXIT_CRITICAL(&ringMux);
  return c * 1000 / AUDIO_RATE;
}

uint32_t underruns() { return nUnderruns; }

bool musicSupported() { return MUSIC && mring != nullptr; }

void pushMusic(const uint8_t* pcm, size_t bytes) {
  if (!musicSupported()) return;
  static int16_t tmp[512];
  size_t i = 0;
  while (i + 1 < bytes) {  // the server only sends whole samples
    size_t n = 0;
    while (n < 512 && i + 1 < bytes) {
      tmp[n++] = (int16_t)(pcm[i] | (pcm[i + 1] << 8));
      i += 2;
    }
    mPush(tmp, n);
  }
}

void musicFlush() {
  portENTER_CRITICAL(&mMux);
  mHead = mTail = mCount = 0;
  mPlaying = false;  // the audio task fades the last sample out (Edge)
  mDrainedAt = 0;
  portEXIT_CRITICAL(&mMux);
}

uint32_t musicBufferedMs() {
  portENTER_CRITICAL(&mMux);
  size_t c = mCount;
  portEXIT_CRITICAL(&mMux);
  return c * 1000 / MUSIC_RATE;
}

uint32_t musicUnderruns() { return mUnderruns; }
bool musicPlaying() { return mPlaying; }
float musicLevel() { return mLvl; }
void setDuck(bool on) { duckOn = on; }
bool playing() { return isPlaying; }
float level() { return lvl; }
float micDb() { return micLvlDb; }

}  // namespace audio
