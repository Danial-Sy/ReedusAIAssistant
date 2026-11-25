// TFLite Micro first: Arduino.h's macros upset flatbuffers otherwise
#include <ESP_TF.h>
#include <tensorflow/lite/micro/micro_allocator.h>
#include <tensorflow/lite/micro/micro_interpreter.h>
#include <tensorflow/lite/micro/micro_mutable_op_resolver.h>
#include <tensorflow/lite/micro/micro_resource_variable.h>
#include <tensorflow/lite/schema/schema_generated.h>

#include "wake.h"

#include <esp_heap_caps.h>
#include <esp_timer.h>

#include "audio.h"
#include "config.h"
#include "glance.h"
#include "mww_features.h"
#include "wake_model.h"  // generated from wakeword/models by tools/embed_wake_model.py

namespace wake {

// the streaming model keeps its state in resource variables (VAR_HANDLE / READ_VARIABLE /
// ASSIGN_VARIABLE, set up by CALL_ONCE). they live in their own small arena so the tensor
// arena can be re-tried at another size without losing them. same numbers as ESPHome.
static const size_t VAR_ARENA = 1024;
static const int MAX_WINDOW = 16;
static const uint32_t REFRACTORY_SLICES = WAKE_REFRACTORY_MS / mww::STEP_MS;

static tflite::MicroMutableOpResolver<20> resolver;
static tflite::MicroInterpreter* interp = nullptr;
static uint8_t* arena = nullptr;
static uint8_t* varArena = nullptr;
static size_t arenaSize = 0, arenaUsed = 0, frontendBytes = 0;
static bool arenaInternal = false;
static TfLiteTensor* input = nullptr;
static int stride = 1, step = 0;
static mww::Features features;
static TaskHandle_t taskHandle = nullptr;

static volatile bool ok = false, fired = false, dbg = false;
static volatile uint8_t firedAvg = 0, cutoffU8 = 0, bargeU8 = 0;
static volatile uint32_t nBarges = 0;
static const uint32_t BARGE_TAIL_MS = 1000;   // his echo's tail: still talking, for the cutoff
// near misses: the window's average climbed past NEAR_FLOOR but stayed under the cutoff (a
// "Reedus" from across the room usually looks like this). the peak of each one is reported
// once it's over, so the app can show how close a call came and the sensitivity can be set
// from that instead of guessed
static const uint8_t NEAR_FLOOR = 89;  // 0.35
static volatile uint8_t nearPeak = 0;
static volatile bool nearReady = false;
static uint8_t nearCur = 0;
static uint8_t win[MAX_WINDOW];
static int winN = WAKE_MODEL_WINDOW, winIdx = 0;
static uint32_t slices = 0, lastWake = 0;
static const char* failure = "not started";

// timing, for `wake` and report()
static volatile uint32_t nInfer = 0, inferMaxUs = 0, detections = 0, invokeErrors = 0;
static volatile uint64_t inferUsTotal = 0, featUsTotal = 0;
static uint8_t secPeakP = 0, secPeakAvg = 0;
static uint32_t secInferUs = 0, secInfers = 0, secAt = 0;

static bool registerOps() {
  // ESPHome micro_wake_word's list for streaming models (streaming_model.cpp)
  return resolver.AddCallOnce() == kTfLiteOk && resolver.AddVarHandle() == kTfLiteOk &&
         resolver.AddReshape() == kTfLiteOk && resolver.AddReadVariable() == kTfLiteOk &&
         resolver.AddStridedSlice() == kTfLiteOk && resolver.AddConcatenation() == kTfLiteOk &&
         resolver.AddAssignVariable() == kTfLiteOk && resolver.AddConv2D() == kTfLiteOk &&
         resolver.AddMul() == kTfLiteOk && resolver.AddAdd() == kTfLiteOk &&
         resolver.AddMean() == kTfLiteOk && resolver.AddFullyConnected() == kTfLiteOk &&
         resolver.AddLogistic() == kTfLiteOk && resolver.AddQuantize() == kTfLiteOk &&
         resolver.AddDepthwiseConv2D() == kTfLiteOk && resolver.AddAveragePool2D() == kTfLiteOk &&
         resolver.AddMaxPool2D() == kTfLiteOk && resolver.AddPad() == kTfLiteOk &&
         resolver.AddPack() == kTfLiteOk && resolver.AddSplitV() == kTfLiteOk;
}

// internal RAM if there's room (ESP-NN is much faster there), else PSRAM
static uint8_t* allocArena(size_t n, bool* internal) {
  uint8_t* p = (uint8_t*)heap_caps_aligned_alloc(16, n, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  *internal = p != nullptr;
  if (!p) p = (uint8_t*)heap_caps_aligned_alloc(16, n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  return p;
}

static void unload() {
  delete interp;
  interp = nullptr;
  if (arena) heap_caps_free(arena);
  arena = nullptr;
}

static bool load(size_t size) {
  unload();
  arena = allocArena(size, &arenaInternal);
  if (!arena) return false;
  // a fresh allocator + variables each try, like ESPHome's probe
  tflite::MicroAllocator* va = tflite::MicroAllocator::Create(varArena, VAR_ARENA);
  tflite::MicroResourceVariables* vars = tflite::MicroResourceVariables::Create(va, 20);
  interp = new tflite::MicroInterpreter(tflite::GetModel(WAKE_MODEL), resolver, arena, size, vars);
  if (interp->AllocateTensors() != kTfLiteOk) {
    unload();
    return false;
  }
  arenaSize = size;
  arenaUsed = interp->arena_used_bytes();
  return true;
}

static bool loadModel() {
  if (tflite::GetModel(WAKE_MODEL)->version() != TFLITE_SCHEMA_VERSION) {
    failure = "model schema version";
    return false;
  }
  if (!registerOps()) {
    failure = "op resolver full";
    return false;
  }
  varArena = (uint8_t*)heap_caps_aligned_alloc(16, VAR_ARENA, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (!varArena) {
    failure = "no RAM for the variable arena";
    return false;
  }
  // the manifest's arena size, then more: kernels and versions differ in what they need
  size_t want = (WAKE_MODEL_ARENA + 15) & ~15;
  if (!load(want) && !load((want * 3 / 2 + 15) & ~15) && !load((want * 2 + 15) & ~15)) {
    failure = "AllocateTensors failed (arena too small, or no RAM)";
    return false;
  }
  // then give back what it didn't use
  size_t fit = (arenaUsed + 64 + 15) & ~15;
  if (fit < arenaSize && !load(fit)) load(want);
  if (!interp) {
    failure = "reload after shrinking failed";
    return false;
  }
  input = interp->input(0);
  TfLiteTensor* out = interp->output(0);
  if (input->type != kTfLiteInt8 || input->dims->size != 3 || input->dims->data[0] != 1 ||
      input->dims->data[2] != mww::FEATURES || out->type != kTfLiteUInt8 ||
      out->dims->size != 2 || out->dims->data[1] != 1) {
    failure = "model isn't a microWakeWord streaming model (int8 [1,n,40] -> uint8 [1,1])";
    unload();
    return false;
  }
  stride = input->dims->data[1];
  return true;
}

static void debugSlice(uint8_t p, uint8_t avg, uint32_t us) {
  if (p > secPeakP) secPeakP = p;
  if (avg > secPeakAvg) secPeakAvg = avg;
  secInferUs += us;
  secInfers++;
  if (!dbg) return;
  if (avg >= 77) {  // a near miss (or a hit): show it as it happens
    char bar[21];
    int n = avg * 20 / 255;
    for (int i = 0; i < 20; i++) bar[i] = i < n ? '#' : (i == cutoffU8 * 20 / 255 ? '|' : '.');
    bar[20] = 0;
    Serial.printf("[wake] p %.2f  avg %.2f  %s\n", p / 255.0f, avg / 255.0f, bar);
  }
  uint32_t now = millis();
  if (now - secAt >= 1000) {
    Serial.printf("[wake] last 1 s: peak p %.2f, peak avg %.2f (cutoff %.2f)  inference %.1f ms "
                  "avg / %.1f max  frontend %.2f ms per slice  drops %u\n",
                  secPeakP / 255.0f, secPeakAvg / 255.0f, cutoffU8 / 255.0f,
                  secInfers ? secInferUs / 1000.0f / secInfers : 0.0f, inferMaxUs / 1000.0f,
                  slices ? (float)featUsTotal / 1000.0f / slices : 0.0f,
                  (unsigned)audio::wakeDropped());
    secAt = now;
    secPeakP = secPeakAvg = 0;
    secInferUs = secInfers = 0;
  }
}

// one 10 ms slice in; every `stride` slices, one inference
static void onSlice(const int8_t* s) {
  slices++;
  memcpy(input->data.int8 + mww::FEATURES * step, s, mww::FEATURES);
  if (++step < stride) return;
  step = 0;

  int64_t t0 = esp_timer_get_time();
  if (interp->Invoke() != kTfLiteOk) {
    invokeErrors++;
    return;
  }
  uint32_t us = (uint32_t)(esp_timer_get_time() - t0);
  nInfer++;
  inferUsTotal += us;
  if (us > inferMaxUs) inferMaxUs = us;

  uint8_t p = interp->output(0)->data.uint8[0];
  win[winIdx] = p;
  winIdx = (winIdx + 1) % winN;
  uint32_t sum = 0;
  for (int i = 0; i < winN; i++) sum += win[i];
  debugSlice(p, (uint8_t)(sum / winN), us);

  if (slices < WAKE_WARMUP_SLICES || slices - lastWake < REFRACTORY_SLICES) return;
  uint8_t avg = (uint8_t)(sum / winN);
  // over his own voice the name scores lower (echo smears it): a lower bar while he talks
  // (not while a timer rings: what's playing is the ring tone, not him, and at the lower bar
  // the beeps woke him, which silenced the ring after one beep)
  bool overHim = audio::sinceSpeechMs() < BARGE_TAIL_MS && !glance::ringing();
  uint8_t cut = (overHim && bargeU8 && bargeU8 < cutoffU8) ? bargeU8 : cutoffU8;
  bool hit = sum > (uint32_t)cut * winN;
  if (!hit && avg >= NEAR_FLOOR) {
    if (avg > nearCur) nearCur = avg;
  } else if (nearCur && !hit) {  // it fell away without firing: one near miss
    nearPeak = nearCur;
    nearReady = true;
    nearCur = 0;
  }
  if (hit) {  // ESPHome's test, in integers
    nearCur = 0;
    lastWake = slices;
    detections++;
    firedAvg = (uint8_t)(sum / winN);
    fired = true;
    memset(win, 0, sizeof(win));
    if (overHim) nBarges++;
    Serial.printf("[wake] heard \"%s\" (avg %.2f%s)\n", WAKE_MODEL_WORD, firedAvg / 255.0f,
                  overHim ? ", over his voice" : "");
  }
}

static void task(void*) {
  static int16_t frame[FRAME_SAMPLES];
  int8_t slice[mww::FEATURES];
  for (;;) {
    if (!audio::waitWakeFrame(frame, 200)) continue;
    size_t off = 0;
    while (off < FRAME_SAMPLES) {
      bool ready;
      int64_t t0 = esp_timer_get_time();
      size_t used = features.process(frame + off, FRAME_SAMPLES - off, slice, &ready);
      featUsTotal += (uint64_t)(esp_timer_get_time() - t0);
      if (!used) break;
      off += used;
      if (ready) onSlice(slice);
    }
  }
}

bool begin() {
#if !WAKE_ON_DEVICE
  failure = "off (WAKE_ON_DEVICE 0)";
  return false;
#else
  setCutoff(WAKE_CUTOFF > 0.0f ? WAKE_CUTOFF : WAKE_MODEL_CUTOFF);
  setBargeCutoff(BARGE_CUTOFF);
  if (winN > MAX_WINDOW) winN = MAX_WINDOW;
  uint32_t heap0 = ESP.getFreeHeap();
  if (!features.begin()) {
    failure = "no RAM for the frontend";
    return false;
  }
  frontendBytes = heap0 - ESP.getFreeHeap();
  if (!loadModel()) {
    features.end();
    if (varArena) heap_caps_free(varArena);
    varArena = nullptr;
    Serial.printf("[wake] no on-device wake word: %s. the relay's will be used\n", failure);
    return false;
  }
  audio::startWakeFeed();
  // TFLM's Invoke wants a few KB of stack; 8 KB leaves room for the debug printfs
  if (xTaskCreatePinnedToCore(task, "wake", 8192, nullptr, 3, &taskHandle, WAKE_TASK_CORE) != pdPASS) {
    failure = "couldn't start the task";
    return false;
  }
  ok = true;
  failure = "";
  Serial.printf("[wake] \"%s\" on the S3 (model v%d, %u KB): arena %u of %u bytes in %s, cutoff %.2f, "
                "window %d, stride %d\n",
                WAKE_MODEL_WORD, WAKE_MODEL_VERSION, (unsigned)(WAKE_MODEL_LEN / 1024),
                (unsigned)arenaUsed, (unsigned)arenaSize, arenaInternal ? "internal RAM" : "PSRAM",
                cutoffU8 / 255.0f, winN, stride);
  return true;
#endif
}

bool ready() { return ok; }

bool take(float* score) {
  if (!fired) return false;
  fired = false;
  if (score) *score = firedAvg / 255.0f;
  return true;
}

bool takeNearMiss(float* peak) {
  if (!nearReady) return false;
  nearReady = false;
  if (peak) *peak = nearPeak / 255.0f;
  return true;
}

float cutoff() { return cutoffU8 / 255.0f; }
float defaultCutoff() { return WAKE_CUTOFF > 0.0f ? WAKE_CUTOFF : WAKE_MODEL_CUTOFF; }

void setCutoff(float c) {
  c = constrain(c, 0.0f, 1.0f);
  cutoffU8 = (uint8_t)(c * 255);  // ESPHome: int(cutoff * 255)
}

float bargeCutoff() { return bargeU8 / 255.0f; }
void setBargeCutoff(float c) { bargeU8 = (c >= 0.5f && c < 1.0f) ? (uint8_t)(c * 255) : 0; }
uint32_t barges() { return nBarges; }

void setDebug(bool on) {
  dbg = on;
  secAt = millis();
}

void report() {
  if (!ok) {
    Serial.printf("wake word: not on the S3 (%s)\n", failure);
    return;
  }
  Serial.printf("wake word \"%s\" (microWakeWord v%d), cutoff %.2f, window %d, stride %d\n",
                WAKE_MODEL_WORD, WAKE_MODEL_VERSION, cutoffU8 / 255.0f, winN, stride);
  Serial.printf("  flash: model %u bytes\n", (unsigned)WAKE_MODEL_LEN);
  Serial.printf("  RAM: tensor arena %u bytes (%u used) in %s, variables %u, frontend %u, "
                "feed queue %u, task stack 8192 (%u never touched)\n",
                (unsigned)arenaSize, (unsigned)arenaUsed, arenaInternal ? "internal" : "PSRAM",
                (unsigned)VAR_ARENA, (unsigned)frontendBytes, (unsigned)(10 * FRAME_SAMPLES * 2),
                (unsigned)uxTaskGetStackHighWaterMark(taskHandle));
  float avgMs = nInfer ? (float)inferUsTotal / 1000.0f / nInfer : 0.0f;
  Serial.printf("  time: inference %.2f ms avg, %.2f max (%u runs, one per %d ms) = %.0f%% of a core; "
                "frontend %.3f ms per 10 ms slice\n",
                avgMs, inferMaxUs / 1000.0f, (unsigned)nInfer, stride * mww::STEP_MS,
                avgMs * 100.0f / (stride * mww::STEP_MS),
                slices ? (float)featUsTotal / 1000.0f / slices : 0.0f);
  Serial.printf("  %u detections, %u invoke errors, %u frames dropped (detector behind)\n",
                (unsigned)detections, (unsigned)invokeErrors, (unsigned)audio::wakeDropped());
}

const char* describe() {
  static char b[128];
  if (!ok) snprintf(b, sizeof(b), "relay (%s)", failure);
  else snprintf(b, sizeof(b), "on the S3, \"%s\" cutoff %.2f (%.2f over his voice), %u heard, %u over him",
                WAKE_MODEL_WORD, cutoffU8 / 255.0f, (bargeU8 ? bargeU8 : cutoffU8) / 255.0f,
                (unsigned)detections, (unsigned)nBarges);
  return b;
}

}  // namespace wake
