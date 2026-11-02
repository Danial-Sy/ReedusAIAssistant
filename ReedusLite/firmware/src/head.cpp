#include "head.h"

#include "config.h"
#include "face.h"
#include "xvf.h"

namespace head {

static const int SERVO_CH = 0;    // LEDC channel
static const int SERVO_BITS = 14;
static const uint32_t SERVO_PERIOD_US = 20000;

enum Mode : uint8_t { M_IDLE, M_LISTEN, M_THINK, M_SPEAK, M_SLEEP };

static Mode mode = M_IDLE;
static float cur = 0, target = 0;
static uint32_t lastUpdate = 0, arrivedAt = 0, settleUntil = 0, pendingAt = 0;
static float pendingTarget = 0;
static bool pending = false, attached = false, printDoa = false, manual = false;
static uint32_t lastPoll = 0, lastIdleTurn = 0;

// recent speech bearings (world frame) for "where was that voice"
static const int HIST = 16;
static float histDeg[HIST];
static uint32_t histAt[HIST];
static int histN = 0, histI = 0;
static float lastWorld = 0, lastRel = 0;
static uint32_t lastVoiceAt = 0;
static int sustained = 0;

static float wrap180(float a) {
  while (a > 180) a -= 360;
  while (a < -180) a += 360;
  return a;
}

static void writeServo(float deg) {
  float us = SERVO_CENTER_US + SERVO_DIR * deg * SERVO_US_PER_DEG;
  uint32_t duty = (uint32_t)(us / SERVO_PERIOD_US * ((1 << SERVO_BITS) - 1));
  ledcWrite(SERVO_CH, duty);
  attached = true;
}

static void release() {
  ledcWrite(SERVO_CH, 0);  // no pulses: an SG90 holding still still whines into the mics
  attached = false;
}

static void turnTo(float deg, uint32_t delayMs) {
  deg = constrain(deg, -HEAD_LIMIT_DEG, HEAD_LIMIT_DEG);
  if (fabsf(deg - target) < 1.0f && !pending) return;
  pendingTarget = deg;
  pendingAt = millis() + delayMs;
  pending = true;
}

// median of speech bearings heard in the last `ms`: the name is ~0.6 s, so this is the
// direction the wake word came from even if the next reading is the speaker's echo
static bool recentBearing(uint32_t ms, float& out) {
  float v[HIST];
  int n = 0;
  uint32_t now = millis();
  for (int i = 0; i < histN; i++)
    if (now - histAt[i] <= ms) v[n++] = histDeg[i];
  if (!n) return false;
  for (int i = 1; i < n; i++)
    for (int j = i; j > 0 && v[j] < v[j - 1]; j--) {
      float t = v[j];
      v[j] = v[j - 1];
      v[j - 1] = t;
    }
  out = v[n / 2];
  return true;
}

static void pollDoa(uint32_t now) {
  if (!xvf::present() || now - lastPoll < XVF_DOA_POLL_MS) return;
  lastPoll = now;
  uint16_t raw;
  bool speech;
  if (!xvf::readDoa(raw, speech)) return;
  float rel = wrap180(DOA_SIGN * ((float)raw - DOA_FRONT_DEG));
  if (printDoa)
    Serial.printf("[doa] raw=%3u rel=%+6.1f head=%+6.1f world=%+6.1f %s\n", raw, rel, cur,
                  wrap180(rel + cur), speech ? "SPEECH" : "");
  // the mics ride on the head: while it's turning, the array is spinning under the voice
  if (!speech || now < settleUntil) {
    if (!speech) sustained = 0;
    return;
  }
  float world = wrap180(rel + cur);
  histDeg[histI] = world;
  histAt[histI] = now;
  histI = (histI + 1) % HIST;
  if (histN < HIST) histN++;
  lastWorld = world;
  lastRel = rel;
  lastVoiceAt = now;
  sustained++;
  face::hearVoice(rel);  // eyes first, always

#if HEAD_IDLE_FOLLOW
  // idle and someone keeps talking well off to one side: look at them (not during a
  // conversation, where the head holds still for the mics)
  if (mode == M_IDLE && !manual && sustained >= 8 && fabsf(rel) > 35.0f &&
      now - lastIdleTurn > 4000) {
    float b;
    if (recentBearing(1200, b)) {
      lastIdleTurn = now;
      turnTo(b, HEAD_FOLLOW_DELAY_MS);
    }
  }
#endif
}

void begin() {
  ledcSetup(SERVO_CH, 1000000 / SERVO_PERIOD_US, SERVO_BITS);
  ledcAttachPin(PIN_SERVO, SERVO_CH);
  cur = target = 0;
  writeServo(0);  // start centered, then let go
  arrivedAt = millis();
  settleUntil = millis() + 600;
  lastUpdate = millis();
}

void update() {
  uint32_t now = millis();
  pollDoa(now);
  if (pending && now >= pendingAt) {
    pending = false;
    target = pendingTarget;
  }
  float dt = (now - lastUpdate) / 1000.0f;
  lastUpdate = now;
  if (dt <= 0 || dt > 0.2f) dt = 0.02f;

  float err = target - cur;
  if (fabsf(err) > 0.3f) {
    // ease in/out: proportional with a speed cap, so big turns are quick and small ones soft
    float step = constrain(err * 8.0f * dt, -HEAD_MAX_DPS * dt, HEAD_MAX_DPS * dt);
    if (fabsf(step) < 0.15f) step = err > 0 ? 0.15f : -0.15f;
    cur += step;
    if (fabsf(target - cur) < 0.3f) cur = target;
    writeServo(cur);
    arrivedAt = now;
    settleUntil = now + 300;
  } else if (attached && now - arrivedAt > HEAD_DETACH_MS) {
    release();
  }
}

void setState(const char* s) {
  Mode prev = mode;
  if (!strcmp(s, "listening")) mode = M_LISTEN;
  else if (!strcmp(s, "thinking")) mode = M_THINK;
  else if (!strcmp(s, "speaking")) mode = M_SPEAK;
  else if (!strcmp(s, "sleep")) mode = M_SLEEP;
  else mode = M_IDLE;
  if (mode == M_SLEEP && prev != M_SLEEP && !manual) turnTo(0, 400);  // settle in, face forward
}

void onWake() {
  manual = false;
  float b;
  if (!recentBearing(1500, b)) return;  // didn't catch it: the eyes stay put too
  face::hearVoice(wrap180(b - cur));
  float want = constrain(b, -HEAD_LIMIT_DEG, HEAD_LIMIT_DEG);
  if (fabsf(want - cur) > HEAD_DEADBAND_DEG) turnTo(want, HEAD_FOLLOW_DELAY_MS);
}

void look(float worldDeg) {
  manual = false;
  turnTo(wrap180(worldDeg), 0);
}

void center() { turnTo(0, 0); }

void setManual(float headDeg) {
  manual = true;
  turnTo(headDeg, 0);
}

float angle() { return cur; }
bool moving() { return fabsf(target - cur) > 0.3f || pending; }
void setDoaPrint(bool on) { printDoa = on; }

bool lastVoice(float& worldDeg, float& relDeg, uint32_t& ageMs) {
  if (!lastVoiceAt) return false;
  worldDeg = lastWorld;
  relDeg = lastRel;
  ageMs = millis() - lastVoiceAt;
  return true;
}

}  // namespace head
