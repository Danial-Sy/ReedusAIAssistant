#include "audio.h"

#include <driver/i2s.h>
#include <esp_heap_caps.h>
#include <freertos/queue.h>

#include "config.h"

namespace audio {

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

struct MicFrame {
  int16_t s[FRAME_SAMPLES];
};

static int32_t rxBuf[I2S_FRAMES * 2];
static int32_t txBuf[I2S_FRAMES * 2];
// filter state: input history, newest last
static float micHist[NTAPS - 1 + I2S_FRAMES];
static float spkHist[NTAPS / 3 - 1 + FRAME_SAMPLES];

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

// I2S RX (I2S_FRAMES stereo @ I2S_RATE) -> FRAME_SAMPLES mono @16k
static void micDown(int16_t* out) {
  const int H = NTAPS - 1;
  float* x = micHist;
  for (size_t i = 0; i < I2S_FRAMES; i++)
    x[H + i] = (float)(rxBuf[i * 2 + MIC_CHANNEL] >> 16) * MIC_GAIN;
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

// FRAME_SAMPLES mono @16k -> I2S TX (I2S_FRAMES stereo @ I2S_RATE)
static void spkUp(const float* in) {
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
      int32_t s = (int32_t)constrain(acc, -32768.0f, 32767.0f) << 16;
      size_t o = (n * R + p) * 2;
      txBuf[o] = s;
      txBuf[o + 1] = s;
    }
  }
  memmove(x, &x[FRAME_SAMPLES], H * sizeof(float));
}

static void audioTask(void*) {
  static int16_t play[FRAME_SAMPLES];
  static float playF[FRAME_SAMPLES];
  MicFrame f;
  for (;;) {
    size_t n = 0;
    i2s_read(PORT, rxBuf, sizeof(rxBuf), &n, portMAX_DELAY);
    if (n == sizeof(rxBuf)) {
      micDown(f.s);
      if (xQueueSend(micQ, &f, 0) != pdTRUE) {  // net is behind: keep the newest audio
        MicFrame old;
        xQueueReceive(micQ, &old, 0);
        xQueueSend(micQ, &f, 0);
        micDrops++;
      }
    }

    size_t got = ringPop(play, FRAME_SAMPLES);
    float g = gain, acc = 0.0f;
    for (size_t i = 0; i < FRAME_SAMPLES; i++) {
      float v = i < got ? play[i] * g : 0.0f;
      playF[i] = v;
      acc += v * v;
    }
    spkUp(playF);
    // mouth: fast attack, slower release, scaled so normal speech reaches ~0.8
    float rms = sqrtf(acc / FRAME_SAMPLES) / 32768.0f;
    float target = constrain(rms * 5.0f, 0.0f, 1.0f);
    lvl = target > lvl ? target : lvl * 0.82f + target * 0.18f;
    size_t w = 0;
    i2s_write(PORT, txBuf, sizeof(txBuf), &w, portMAX_DELAY);
  }
}

bool begin() {
  ring = (int16_t*)heap_caps_malloc(PLAY_CAP * sizeof(int16_t), MALLOC_CAP_SPIRAM);
  if (!ring) ring = (int16_t*)malloc(PLAY_CAP * sizeof(int16_t));
  if (!ring) return false;
  memset(micHist, 0, sizeof(micHist));
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
bool playing() { return isPlaying; }
float level() { return lvl; }

}  // namespace audio
