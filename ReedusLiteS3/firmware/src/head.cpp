#include "head.h"

#include "audio.h"
#include "config.h"
#include "face.h"
#include "store.h"
#include "xvf.h"

namespace head {

static const int SERVO_CH = 0;    // LEDC channel
static const int SERVO_BITS = 14;
static const uint32_t SERVO_PERIOD_US = 20000;

enum Mode : uint8_t { M_IDLE, M_LISTEN, M_THINK, M_SPEAK, M_SLEEP };

static Mode mode = M_IDLE;
static float cur = 0, target = 0, vel = 0;  // deg, deg, deg/s
static uint32_t lastUpdate = 0, arrivedAt = 0, settleUntil = 0, pendingAt = 0;
static float pendingTarget = 0;
static bool pending = false, attached = false, printDoa = false, manual = false;
static uint32_t lastPoll = 0, lastIdleTurn = 0, lastTrack = 0;
// how fast the current move may go: a wake turn is brisk, following you is slow
static float speedCap = HEAD_MAX_DPS, accelCap = HEAD_ACCEL_DPS2;

// recent speech bearings (world frame) for "where was that voice"
static const int HIST = 16;
static float histDeg[HIST];
static uint32_t histAt[HIST];
static int histN = 0, histI = 0;
static float lastWorld = 0, lastRel = 0;
static uint32_t lastVoiceAt = 0, lastPlayAt = 0;
static int sustained = 0;

// calibration: config.h's defaults, overridden by `cal` (saved in NVS, see store.cpp)
static int centerUs = SERVO_CENTER_US, dir = SERVO_DIR, doaFront = DOA_FRONT_DEG, doaSign = DOA_SIGN;
static int lastRaw = -1;
static uint32_t lastRawAt = 0;

static float wrap180(float a) {
  while (a > 180) a -= 360;
  while (a < -180) a += 360;
  return a;
}

static void writeServo(float deg) {
  float us = centerUs + dir * deg * SERVO_US_PER_DEG;
  uint32_t duty = (uint32_t)(us / SERVO_PERIOD_US * ((1 << SERVO_BITS) - 1));
  ledcWrite(SERVO_CH, duty);
  attached = true;
}

static void release() {
  ledcWrite(SERVO_CH, 0);  // no pulses: an SG90 holding still still whines into the mics
  attached = false;
}

static void turnTo(float deg, uint32_t delayMs, bool slow = false) {
  deg = constrain(deg, -HEAD_LIMIT_DEG, HEAD_LIMIT_DEG);
  speedCap = slow ? HEAD_TRACK_DPS : HEAD_MAX_DPS;
  accelCap = slow ? HEAD_TRACK_ACCEL_DPS2 : HEAD_ACCEL_DPS2;
  if (fabsf(deg - target) < 1.0f && !pending) return;
  pendingTarget = deg;
  pendingAt = millis() + delayMs;
  pending = true;
}

// the bearing of whoever is talking: in the last `ms`, the biggest group of readings that agree
// within HEAD_CLUSTER_DEG, and only if that group is at least HEAD_CLUSTER_SHARE of them. a
// median of two people (you on one side, your mom on the other) lands in the empty middle and
// the head goes nowhere useful; this picks the one who's actually talking, or waits
static bool recentBearing(uint32_t ms, float& out, int minN = 1, float share = HEAD_CLUSTER_SHARE) {
  float v[HIST];
  int n = 0;
  uint32_t now = millis();
  for (int i = 0; i < histN; i++)
    if (now - histAt[i] <= ms) v[n++] = histDeg[i];
  if (n < minN || !n) return false;
  int best = -1, bestCount = 0;
  for (int i = 0; i < n; i++) {
    int c = 0;
    for (int j = 0; j < n; j++)
      if (fabsf(wrap180(v[j] - v[i])) <= HEAD_CLUSTER_DEG) c++;
    if (c > bestCount) {
      bestCount = c;
      best = i;
    }
  }
  if (best < 0 || bestCount < minN || bestCount < share * n) return false;
  // circular mean of that group, around its seed
  float sum = 0;
  int c = 0;
  for (int j = 0; j < n; j++) {
    float d = wrap180(v[j] - v[best]);
    if (fabsf(d) <= HEAD_CLUSTER_DEG) {
      sum += d;
      c++;
    }
  }
  out = wrap180(v[best] + sum / c);
  return true;
}

static void pollDoa(uint32_t now) {
  if (!xvf::present() || now - lastPoll < XVF_DOA_POLL_MS) return;
  lastPoll = now;
  uint16_t raw;
  bool speech;
  if (!xvf::readDoa(raw, speech)) return;
  if (speech) {
    lastRaw = raw;
    lastRawAt = now;
  }
  float rel = wrap180(doaSign * ((float)raw - doaFront));
  // his own voice: the echo canceller takes most of it out, but what's left still trips the
  // speech flag and points at the speaker. those readings used to fill the history, so after he
  // talked, a wake or a new voice was outvoted by himself
  bool talking = audio::playing();
  if (talking) lastPlayAt = now;
  bool ownVoice = talking || now - lastPlayAt < HEAD_ECHO_GUARD_MS;
  if (printDoa)
    Serial.printf("[doa] raw=%3u rel=%+6.1f head=%+6.1f world=%+6.1f %s%s\n", raw, rel, cur,
                  wrap180(rel + cur), speech ? "SPEECH" : "", speech && ownVoice ? " (his own voice, ignored)" : "");
  // the mics ride on the head: while it's turning, the array is spinning under the voice
  if (!speech || now < settleUntil) {
    if (!speech) sustained = 0;
    return;
  }
  float world = wrap180(rel + cur);
  // while he talks, his speaker (in the base, firing straight out the front) is a voice at
  // world ~0. keep only readings well away from it: that's someone talking over him (usually
  // saying "Reedus"), which is exactly who the head should turn to
  if (ownVoice && fabsf(world) < HEAD_SPEAKER_CONE_DEG) return;
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
      turnTo(cur + wrap180(b - cur) * HEAD_TURN_SCALE, HEAD_FOLLOW_DELAY_MS);
    }
  }
#endif

#if HEAD_TRACK_IN_CONVO
  // a conversation is open and he's listening: look at whoever is talking. someone new across
  // the room (a big jump) gets a proper turn right away; you shifting in your seat gets a slow
  // drift. it needs HEAD_TRACK_MIN_N readings that agree, so noise and crosstalk don't move it
  if (mode == M_LISTEN && !manual && !pending && now - lastTrack > HEAD_TRACK_EVERY_MS) {
    float b;
    if (recentBearing(HEAD_TRACK_WINDOW_MS, b, HEAD_TRACK_MIN_N)) {
      float off = wrap180(b - cur);
      if (fabsf(off) > HEAD_TRACK_DEADBAND_DEG) {
        lastTrack = now;
        bool newPerson = fabsf(off) > HEAD_NEW_SPEAKER_DEG;
        turnTo(cur + off * HEAD_TURN_SCALE, 0, !newPerson);
      }
    }
  }
#endif
}

void begin() {
  centerUs = constrain(store::getInt("srv_c", SERVO_CENTER_US), 800, 2200);
  dir = store::getInt("srv_dir", SERVO_DIR) < 0 ? -1 : 1;
  doaFront = store::getInt("doa_f", DOA_FRONT_DEG);
  // "doa_s2": a sign saved under the old "doa_s" key was backwards, so it's ignored now
  doaSign = store::getInt("doa_s2", DOA_SIGN) < 0 ? -1 : 1;
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
  if (fabsf(err) > 0.3f || fabsf(vel) > 1.0f) {
    // ramp up, cruise, ease out: the speed it wants falls off as it nears the target (so it
    // can always stop in time), and the speed it has can only change by HEAD_ACCEL_DPS2. no
    // instant full-speed starts, no hard stops: that's what made the SG90 look twitchy
    float stopDist = vel * vel / (2.0f * accelCap);
    float want = 0;
    if (fabsf(err) > 0.3f) {
      want = sqrtf(2.0f * accelCap * fabsf(err)) * 0.9f;  // the most it can stop from
      want = min(want, speedCap);
      if (fabsf(err) <= stopDist) want = 0;
      if (err < 0) want = -want;
    }
    float dv = constrain(want - vel, -accelCap * dt, accelCap * dt);
    vel += dv;
    float step = vel * dt;
    if ((err > 0 && step > err) || (err < 0 && step < err)) {  // would pass it: land exactly
      step = err;
      vel = 0;
    }
    cur += step;
    if (fabsf(target - cur) < 0.3f && fabsf(vel) < 8.0f) {
      cur = target;
      vel = 0;
    }
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
  lastTrack = millis();  // the wake turn first; following starts after it
  float b;
  // whoever said the name: the readings during the last ~1.2 s that agree (his own voice is
  // already left out). the share is lower than in conversation: the name is short
  if (!recentBearing(1200, b, 2, 0.5f) && !recentBearing(2000, b, 1, 0.4f)) return;
  face::hearVoice(wrap180(b - cur));
  // the array's bearing runs a little wide, so turn most of the way and let the eyes
  // (already looking at the voice) cover the rest. better short than past you
  float want = constrain(cur + wrap180(b - cur) * HEAD_TURN_SCALE, -HEAD_LIMIT_DEG, HEAD_LIMIT_DEG);
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

Cal cal() { return {centerUs, dir, doaFront, doaSign}; }

void setCal(const Cal& c, bool save) {
  bool moved = c.centerUs != centerUs || c.dir != dir;
  centerUs = constrain(c.centerUs, 800, 2200);
  dir = c.dir < 0 ? -1 : 1;
  doaFront = ((c.doaFront % 360) + 360) % 360;
  doaSign = c.doaSign < 0 ? -1 : 1;
  if (save) {
    store::putInt("srv_c", centerUs);
    store::putInt("srv_dir", dir);
    store::putInt("doa_f", doaFront);
    store::putInt("doa_s2", doaSign);
  }
  if (moved) writeServo(cur);  // show the new center right away
  arrivedAt = millis();
}

bool lastRawDoa(int& raw, uint32_t& ageMs) {
  if (lastRaw < 0) return false;
  raw = lastRaw;
  ageMs = millis() - lastRawAt;
  return true;
}
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
