#include "face.h"

#include <FastLED.h>

#include "audio.h"
#include "config.h"

namespace face {

// ---------- palette ----------
static const CRGB MINT(0x8F, 0xF5, 0xE2);  // the chassis mockup's default
static const CRGB PINK(0xFF, 0x5C, 0x9E);
static const CRGB BLUSH(0xFF, 0x4F, 0x78);
static const CRGB RED(0xFF, 0x40, 0x28);
static const CRGB SKY(0x6F, 0xA8, 0xFF);
static const CRGB GOLD(0xFF, 0xC8, 0x40);
static const CRGB AMBER(0xFF, 0x90, 0x10);
static const CRGB DIMWHITE(0x60, 0x70, 0x70);

enum Eye : uint8_t { E_OPEN, E_WIDE, E_HAPPY, E_CLOSED, E_HALF, E_SQUINT, E_HEART, E_STAR,
                     E_X, E_SAD, E_ANGRY, E_RING };
enum Mouth : uint8_t { M_NONE, M_SMILE, M_GRIN, M_FLAT, M_SHORT, M_SMALL_O, M_BIG_O, M_FROWN,
                       M_WAVY, M_SMIRK, M_CAT, M_TALK };
enum Extra : uint16_t { X_NONE = 0, X_BLUSH = 1, X_TEAR = 2, X_ZZZ = 4, X_DOTS = 8,
                        X_QUESTION = 16, X_HEARTS = 32, X_SPARKLE = 64, X_BOUNCE = 128 };

enum Emo : uint8_t { NEUTRAL, HAPPY, LAUGH, LOVE, SAD, SURPRISED, ANGRY, CONFUSED, SLEEPY,
                     EXCITED, SMUG, SHY, CURIOUS,
                     // expression-only
                     WINK, BYE, DIZZY, YAWN, CONTENT, BLINK, N_EMO };
static const char* EMO_NAMES[N_EMO] = {
    "neutral", "happy", "laugh", "love", "sad", "surprised", "angry", "confused", "sleepy",
    "excited", "smug", "shy", "curious", "wink", "bye", "dizzy", "yawn", "content", "blink"};

enum St : uint8_t { S_IDLE, S_LISTENING, S_THINKING, S_SPEAKING, S_SLEEP };
static const char* ST_NAMES[] = {"idle", "listening", "thinking", "speaking", "sleep"};

struct Spec {
  Eye eyeL = E_OPEN, eyeR = E_OPEN;
  Mouth mouth = M_SMILE;
  CRGB color = MINT;
  uint16_t extras = X_NONE;
  float gx = 0, gy = 0;  // gaze, pixels
  float dim = 1.0f;
  bool blinkable = true;
};

// ---------- state ----------
static CRGB leds[MATRIX_W * MATRIX_H];
static CRGB frame[MATRIX_H][MATRIX_W];   // what we want
static CRGB shown[MATRIX_H][MATRIX_W];   // what's lit, eased toward `frame`
static St st = S_IDLE;
static bool wifiUp = false, serverUp = false, test = false;
static uint32_t bootAt = 0, lastFrame = 0, lastActive = 0, wakeAt = 0;
static Emo emo = NEUTRAL;
static uint32_t emoUntil = 0;
static Emo ex = NEUTRAL;
static uint32_t exUntil = 0, exStart = 0;
static float voiceRel = 0;
static uint32_t voiceAt = 0;
static float gazeX = 0, gazeY = 0;      // eased
static uint32_t nextBlink = 0, blinkStart = 0, blinkLen = 130;
static bool doubleBlink = false;
static bool drowsyNow = false;  // slow, heavy blinks
static uint32_t nextFidget = 0, fidgetUntil = 0;
static uint8_t fidget = 0;
static float fidgetGx = 0;
static int battPct = -1;
static int bright = 100;
static char desc[64];

static float now_s() { return millis() / 1000.0f; }

// ---------- pixels ----------
static uint16_t xyToIndex(int x, int y) {
  // logical (x right, y down, seen from the front) -> strip index
  int rx = x, ry = y;
  for (int r = 0; r < MATRIX_ROTATE; r++) {
    int t = rx;
    rx = MATRIX_W - 1 - ry;
    ry = t;
  }
  if (MATRIX_FLIP_X) rx = MATRIX_W - 1 - rx;
  if (MATRIX_SERPENTINE && (ry & 1)) rx = MATRIX_W - 1 - rx;
  return ry * MATRIX_W + rx;
}

static void px(int x, int y, const CRGB& c, uint8_t v = 255) {
  if (x < 0 || x >= MATRIX_W || y < 0 || y >= MATRIX_H) return;
  CRGB p = c;
  p.nscale8_video(v);
  CRGB& d = frame[y][x];
  d.r = max(d.r, p.r);
  d.g = max(d.g, p.g);
  d.b = max(d.b, p.b);
}

static void row(int y, int x0, int x1, const CRGB& c, uint8_t v = 255) {
  for (int x = x0; x <= x1; x++) px(x, y, c, v);
}

// ---------- eyes ----------
// eye box: 3 wide (cx..cx+2), rows cy..cy+3. `outer` = +1 for the right eye, -1 for the left
static void drawEye(Eye e, int cx, int cy, const CRGB& c, float open, int side) {
  const uint8_t EDGE = 115;
  int outerX = side < 0 ? cx : cx + 2, innerX = side < 0 ? cx + 2 : cx;
  if (open < 0.2f && e != E_HAPPY && e != E_HEART && e != E_STAR && e != E_X) e = E_CLOSED;
  switch (e) {
    case E_OPEN:
    case E_WIDE: {
      int top = e == E_WIDE ? cy - 1 : cy, h = e == E_WIDE ? 5 : 4;
      // eyelids close toward the lower middle, so a half-open eye still looks like an eye
      int vis = max(1, (int)roundf(h * open));
      int y0 = top + (h - vis + 1) / 2 + (h - vis) / 4;
      for (int y = y0; y < y0 + vis; y++) {
        bool edge = (y == y0 || y == y0 + vis - 1) && vis > 2;
        px(cx, y, c, edge ? EDGE : 255);
        px(cx + 1, y, c);
        px(cx + 2, y, c, edge ? EDGE : 255);
      }
      break;
    }
    case E_HAPPY:
      px(cx + 1, cy + 1, c);
      px(cx, cy + 2, c);
      px(cx + 2, cy + 2, c);
      break;
    case E_CLOSED:
      row(cy + 2, cx, cx + 2, c);
      break;
    case E_HALF:
      row(cy + 2, cx, cx + 2, c, 130);
      row(cy + 3, cx, cx + 2, c);
      break;
    case E_SQUINT:
      row(cy + 1, cx, cx + 2, c, 150);
      row(cy + 2, cx, cx + 2, c);
      break;
    case E_HEART: {
      // 5x4 heart centered on the eye, beating
      float beat = 0.75f + 0.25f * sinf(now_s() * 7.0f);
      uint8_t v = (uint8_t)(255 * beat);
      int l = cx - 1;
      px(l + 1, cy, c, v); px(l + 3, cy, c, v);
      row(cy + 1, l, l + 4, c, v);
      row(cy + 2, l + 1, l + 3, c, v);
      px(l + 2, cy + 3, c, v);
      break;
    }
    case E_STAR: {
      bool plus = ((millis() / 220) & 1) == 0;
      px(cx + 1, cy + 2, c);
      if (plus) {
        px(cx + 1, cy + 1, c, 200); px(cx + 1, cy + 3, c, 200);
        px(cx, cy + 2, c, 200); px(cx + 2, cy + 2, c, 200);
        px(cx + 1, cy, c, 90);
      } else {
        px(cx, cy + 1, c, 200); px(cx + 2, cy + 1, c, 200);
        px(cx, cy + 3, c, 200); px(cx + 2, cy + 3, c, 200);
      }
      break;
    }
    case E_X:
      px(cx, cy + 1, c); px(cx + 2, cy + 1, c);
      px(cx + 1, cy + 2, c);
      px(cx, cy + 3, c); px(cx + 2, cy + 3, c);
      break;
    case E_SAD:  // the outer top corner drops, brow rises toward the middle: / \ reversed
      row(cy + 1, min(cx + 1, innerX), max(cx + 1, innerX), c, 150);
      row(cy + 2, cx, cx + 2, c);
      row(cy + 3, cx, cx + 2, c);
      px(innerX, cy - 1, c, 150);
      px(cx + 1, cy - 1, c, 90);
      px(outerX, cy, c, 150);
      break;
    case E_ANGRY:  // the inner top corner drops, brow slants down toward the middle: \ /
      row(cy + 1, min(cx + 1, outerX), max(cx + 1, outerX), c, 170);
      row(cy + 2, cx, cx + 2, c);
      row(cy + 3, cx, cx + 2, c);
      px(outerX, cy - 1, c, 170);
      px(cx + 1, cy - 1, c, 110);
      px(innerX, cy, c, 170);
      break;
    case E_RING:
      row(cy - 1, cx, cx + 2, c);
      for (int y = cy; y <= cy + 2; y++) { px(cx, y, c); px(cx + 2, y, c); }
      row(cy + 3, cx, cx + 2, c);
      break;
  }
}

// ---------- mouths ----------
static void drawMouth(Mouth m, const CRGB& c, float level, int dy) {
  int y = 10 + dy;
  switch (m) {
    case M_NONE:
      break;
    case M_SMILE:
      px(5, y, c); px(10, y, c);
      row(y + 1, 6, 9, c);
      break;
    case M_GRIN:  // open D
      row(y - 1, 5, 10, c);
      px(5, y, c); px(10, y, c);
      row(y, 6, 9, c, 60);
      row(y + 1, 6, 9, c);
      break;
    case M_FLAT:
      row(y + 1, 6, 9, c);
      break;
    case M_SHORT:
      row(y + 1, 7, 8, c);
      break;
    case M_SMALL_O:
      row(y, 7, 8, c);
      px(6, y + 1, c); px(9, y + 1, c);
      row(y + 2, 7, 8, c);
      break;
    case M_BIG_O:
      row(y - 1, 6, 9, c);
      px(5, y, c); px(10, y, c);
      px(5, y + 1, c); px(10, y + 1, c);
      row(y + 2, 6, 9, c);
      break;
    case M_FROWN:
      row(y, 6, 9, c);
      px(5, y + 1, c); px(10, y + 1, c);
      break;
    case M_WAVY: {
      int ph = (millis() / 300) & 1;
      for (int x = 5; x <= 10; x++) px(x, y + (((x + ph) & 1) ? 0 : 1), c);
      break;
    }
    case M_SMIRK:
      row(y + 1, 6, 8, c);
      px(9, y, c);
      px(10, y - 1, c, 160);
      break;
    case M_CAT:  // two little u's: the cat-ear robot's ω
      px(5, y, c); px(7, y, c); px(8, y, c); px(10, y, c);
      px(6, y + 1, c); px(9, y + 1, c);
      break;
    case M_TALK: {
      int f = level < 0.10f ? 0 : level < 0.32f ? 1 : level < 0.62f ? 2 : 3;
      if (f == 0) {
        row(y + 1, 6, 9, c);
      } else if (f == 1) {
        row(y, 7, 8, c); px(6, y + 1, c); px(9, y + 1, c); row(y + 2, 7, 8, c);
      } else if (f == 2) {
        row(y, 6, 9, c); px(5, y + 1, c); px(10, y + 1, c); row(y + 2, 6, 9, c);
      } else {
        row(y - 1, 6, 9, c); px(5, y, c); px(10, y, c); px(5, y + 1, c); px(10, y + 1, c);
        row(y + 2, 6, 9, c);
      }
      break;
    }
  }
}

// ---------- extras ----------
static void drawExtras(uint16_t x, const CRGB& c) {
  float t = now_s();
  if (x & X_BLUSH) {
    row(8, 1, 2, BLUSH, 150);
    row(8, 13, 14, BLUSH, 150);
  }
  if (x & X_TEAR) {
    int y = 7 + (int)(fmodf(t * 2.5f, 3.0f));
    px(3, y, SKY, 200);
  }
  if (x & X_ZZZ) {
    int dy = sinf(t * 1.6f) > 0 ? 1 : 0;
    row(0 + dy, 12, 14, DIMWHITE, 170);
    px(13, 1 + dy, DIMWHITE, 170);
    row(2 + dy, 12, 14, DIMWHITE, 170);
  }
  if (x & X_DOTS) {
    int i = (int)(t * 3.0f) % 3;
    for (int k = 0; k < 3; k++) px(6 + k * 2, 14, c, k == i ? 255 : 70);
  }
  if (x & X_QUESTION) {
    CRGB q = GOLD;
    px(14, 0, q); px(13, 1, q); px(15, 1, q); px(15, 2, q); px(14, 3, q);
    if (((millis() / 400) & 1) == 0) px(14, 5, q);
  }
  if (x & X_HEARTS) {  // little pink motes rising up the sides
    for (int k = 0; k < 2; k++) {
      float p = fmodf(t * 0.9f + k * 0.5f, 1.0f);
      int y = 13 - (int)(p * 13);
      uint8_t v = (uint8_t)(220 * (1.0f - p));
      px(k ? 14 : 1, y, PINK, v);
    }
  }
  if (x & X_SPARKLE) {
    static const uint8_t spots[][2] = {{0, 1}, {15, 2}, {1, 13}, {14, 12}, {7, 0}, {0, 8}, {15, 8}};
    for (int k = 0; k < 7; k++) {
      float s = sinf(t * 5.0f + k * 1.7f);
      if (s > 0.6f) px(spots[k][0], spots[k][1], GOLD, (uint8_t)(255 * (s - 0.6f) / 0.4f));
    }
  }
}

static void drawBattery() {
  if (battPct < 0 || battPct > BATT_LOW_PCT) return;
  if (((millis() / 700) & 1) == 0) return;
  CRGB c = battPct <= 5 ? RED : AMBER;
  row(13, 12, 14, c, 160);
  row(15, 12, 14, c, 160);
  px(12, 14, c, 160);
  px(14, 14, c, 160);
  px(15, 14, c, 120);
  px(13, 14, c, battPct > 5 ? 160 : 0);
}

// ---------- emotion -> parts ----------
static void applyEmo(Spec& s, Emo e, bool speaking) {
  Mouth m = s.mouth;
  switch (e) {
    case NEUTRAL: break;
    case HAPPY: s.eyeL = s.eyeR = E_HAPPY; m = M_SMILE; break;
    case LAUGH: s.eyeL = s.eyeR = E_HAPPY; m = M_GRIN; s.extras |= X_BOUNCE; break;
    case LOVE: s.eyeL = s.eyeR = E_HEART; s.color = PINK; m = M_SMILE; s.extras |= X_HEARTS | X_BLUSH; break;
    case SAD: s.eyeL = s.eyeR = E_SAD; s.color = blend(MINT, SKY, 150); m = M_FROWN; s.extras |= X_TEAR; s.gy = 1; break;
    case SURPRISED: s.eyeL = s.eyeR = E_RING; m = M_BIG_O; break;
    case ANGRY: s.eyeL = s.eyeR = E_ANGRY; s.color = blend(MINT, RED, 190); m = M_FROWN; break;
    case CONFUSED: s.eyeL = E_OPEN; s.eyeR = E_SQUINT; m = M_WAVY; s.extras |= X_QUESTION; break;
    case SLEEPY: s.eyeL = s.eyeR = E_HALF; m = M_FLAT; s.dim *= 0.7f; break;
    case EXCITED: s.eyeL = s.eyeR = E_STAR; s.color = blend(MINT, GOLD, 120); m = M_GRIN; s.extras |= X_SPARKLE; break;
    case SMUG: s.eyeL = E_HALF; s.eyeR = E_SQUINT; m = M_SMIRK; break;
    case SHY: s.eyeL = s.eyeR = E_HAPPY; m = M_CAT; s.extras |= X_BLUSH; s.gy = 1; break;
    case CURIOUS: s.eyeL = E_WIDE; s.eyeR = E_OPEN; m = M_SMALL_O; s.gx += 1; break;
    case WINK: s.eyeL = E_OPEN; s.eyeR = E_HAPPY; m = M_SMIRK; break;
    case BYE: s.eyeL = E_HAPPY; s.eyeR = (millis() / 450) & 1 ? E_CLOSED : E_HAPPY; m = M_SMILE; break;
    case DIZZY: s.eyeL = s.eyeR = E_X; m = M_WAVY; break;
    case YAWN: {
      float p = (millis() - exStart) / 1000.0f;
      s.eyeL = s.eyeR = p < 0.4f ? E_HALF : E_CLOSED;
      m = p < 0.3f ? M_SMALL_O : M_BIG_O;
      s.dim *= 0.85f;
      break;
    }
    case CONTENT: s.eyeL = s.eyeR = E_HAPPY; m = M_CAT; break;
    case BLINK: break;
    default: break;
  }
  if (e == HAPPY || e == LAUGH || e == LOVE || e == SAD || e == SURPRISED || e == ANGRY ||
      e == CONFUSED || e == EXCITED || e == SHY || e == WINK || e == BYE || e == CONTENT)
    s.blinkable = false;
  if (!speaking) s.mouth = m;
}

static Emo emoFromName(const char* name) {
  for (int i = 0; i < N_EMO; i++)
    if (strcasecmp(name, EMO_NAMES[i]) == 0) return (Emo)i;
  return N_EMO;
}

bool knownExpr(const char* name) { return emoFromName(name) != N_EMO; }

// ---------- liveliness ----------
static void scheduleBlink(uint32_t now, bool drowsy) {
  nextBlink = now + (drowsy ? random(3500, 8000) : random(2200, 6000));
  blinkLen = drowsy ? 320 : 130;
  doubleBlink = !drowsy && random(100) < 18;
}

static float blinkOpen(uint32_t now) {
  if (now >= nextBlink && blinkStart == 0) blinkStart = now;
  if (blinkStart == 0) return 1.0f;
  uint32_t t = now - blinkStart;
  if (t >= blinkLen) {
    if (doubleBlink) {
      doubleBlink = false;
      blinkStart = now + 90;  // a beat, then again
      nextBlink = blinkStart;
      blinkStart = 0;
      return 1.0f;
    }
    blinkStart = 0;
    scheduleBlink(now, drowsyNow);
    return 1.0f;
  }
  float p = t / (float)blinkLen;  // 0..1, closed in the middle
  return fabsf(p * 2.0f - 1.0f);
}

static void idleFidget(uint32_t now, Spec& s) {
  if (now >= nextFidget) {
    fidget = random(6);
    uint32_t dur = 900 + random(1400);
    fidgetUntil = now + dur;
    fidgetGx = random(2) ? 2 : -2;
    nextFidget = fidgetUntil + random(6000, 16000);
  }
  if (now >= fidgetUntil) return;
  float p = 1.0f - (fidgetUntil - now) / 2300.0f;
  switch (fidget) {
    case 0: s.gx = fidgetGx; break;                                  // glance
    case 1: s.gx = p < 0.5f ? -2 : 2; break;                          // look around
    case 2: applyEmo(s, CONTENT, false); break;                       // little :3
    case 3: s.gy = -1; s.gx = 1; break;                               // ponder the ceiling
    case 4: if (blinkStart == 0) { nextBlink = now; doubleBlink = true; } break;
    case 5: s.mouth = M_SHORT; s.gx = fidgetGx * 0.5f; break;         // "hm"
  }
}

// ---------- compose ----------
static void compose(uint32_t now, Spec& s) {
  bool speaking = st == S_SPEAKING || audio::playing();
  uint32_t idleFor = (now - lastActive) / 1000;
  bool drowsy = st == S_IDLE && idleFor > IDLE_DROWSY_S;
  bool asleep = st == S_SLEEP || (st == S_IDLE && idleFor > IDLE_SLEEP_S);

  // voice tracking: eyes go to the voice for a couple of seconds after it's heard
  bool voiceFresh = voiceAt && now - voiceAt < 2000;
  if (voiceFresh) s.gx = constrain(voiceRel / 45.0f * 2.0f, -2.0f, 2.0f);

  switch (st) {
    case S_IDLE:
      s.mouth = M_SMILE;
      if (asleep) {
        s.eyeL = s.eyeR = E_CLOSED;
        s.mouth = M_SHORT;
        s.extras |= X_ZZZ;
        s.dim = 0.45f + 0.1f * sinf(now_s() * 0.9f);  // slow breathing
        s.blinkable = false;
        s.gx = 0;
      } else if (drowsy) {
        s.eyeL = s.eyeR = E_HALF;
        s.mouth = M_FLAT;
        s.dim = 0.75f;
      } else if (!voiceFresh) {
        idleFidget(now, s);
      }
      break;
    case S_LISTENING:
      s.eyeL = s.eyeR = E_WIDE;
      s.mouth = M_SHORT;
      if (now - wakeAt < 350) s.eyeL = s.eyeR = E_RING;  // the perk-up
      break;
    case S_THINKING:
      s.eyeL = s.eyeR = E_OPEN;
      s.gy = -1;
      s.gx = 1.5f * sinf(now_s() * 0.8f);
      s.mouth = M_FLAT;
      s.extras |= X_DOTS;
      break;
    case S_SPEAKING:
      s.eyeL = s.eyeR = E_OPEN;
      s.mouth = M_TALK;
      break;
    case S_SLEEP:
      s.eyeL = s.eyeR = E_CLOSED;
      s.mouth = M_SHORT;
      s.extras |= X_ZZZ;
      s.dim = 0.45f;
      s.blinkable = false;
      break;
  }
  if (speaking) s.mouth = M_TALK;

  // what he feels colours the reply and lingers a moment after
  if (emo != NEUTRAL && now < emoUntil && st != S_THINKING && !asleep) applyEmo(s, emo, speaking);
  // one-shots win over everything but the speech mouth
  if (ex != NEUTRAL && now < exUntil) {
    if (ex == BLINK) {
      if (blinkStart == 0) nextBlink = now;
      ex = NEUTRAL;
    } else {
      s.extras &= ~X_DOTS;
      applyEmo(s, ex, speaking);
    }
  }
  if (drowsy != drowsyNow) {
    drowsyNow = drowsy;
    scheduleBlink(now, drowsy);
  }
}

static void render(uint32_t now) {
  memset(frame, 0, sizeof(frame));
  if (test) {
    // corners: red top-left, green top-right, blue bottom-left, white bottom-right, as seen
    // from the front. use it to set MATRIX_ROTATE / MATRIX_FLIP_X
    px(0, 0, CRGB::Red); px(1, 0, CRGB::Red);
    px(15, 0, CRGB::Green);
    px(0, 15, CRGB::Blue);
    px(15, 15, CRGB::White);
    row(7, 0, (millis() / 100) % 16, MINT, 120);  // sweeps left to right
    return;
  }
  Spec s;
  float bootP = (now - bootAt) / 900.0f;
  if (!wifiUp || !serverUp) {
    // searching: dim eyes scanning, a status pixel (amber = no wifi, red = no run box)
    s.eyeL = s.eyeR = E_OPEN;
    s.mouth = M_FLAT;
    s.gx = 2.0f * sinf(now_s() * 1.3f);
    s.dim = 0.55f;
    if ((millis() / 600) & 1) px(15, 15, wifiUp ? RED : AMBER, 180);
  } else {
    compose(now, s);
  }
  float open = 1.0f;
  if (bootP < 1.0f) {
    open = bootP;  // eyes open for the first time
    s.mouth = M_NONE;
  } else if (s.blinkable) {
    open = blinkOpen(now);
  }

  // ease the gaze: quick saccade, a little overshoot-free settle
  gazeX += (s.gx - gazeX) * 0.35f;
  gazeY += (s.gy - gazeY) * 0.35f;
  int gx = (int)roundf(gazeX), gy = (int)roundf(gazeY);
  int bounce = (s.extras & X_BOUNCE) && ((millis() / 160) & 1) ? 1 : 0;

  // eye left in its own wink states
  float openL = open, openR = open;
  drawEye(s.eyeL, 3 + gx, 3 + gy - bounce, s.color, openL, -1);
  drawEye(s.eyeR, 10 + gx, 3 + gy - bounce, s.color, openR, +1);
  drawMouth(s.mouth, s.color, audio::level(), bounce ? -1 : 0);
  drawExtras(s.extras, s.color);
  drawBattery();

  if (s.dim < 1.0f) {
    uint8_t d = (uint8_t)(255 * constrain(s.dim, 0.0f, 1.0f));
    for (int y = 0; y < MATRIX_H; y++)
      for (int x = 0; x < MATRIX_W; x++) frame[y][x].nscale8_video(d);
  }
  snprintf(desc, sizeof(desc), "%s emo=%s ex=%s gaze=%d,%d", ST_NAMES[st],
           now < emoUntil ? EMO_NAMES[emo] : "-", now < exUntil ? EMO_NAMES[ex] : "-", gx, gy);
}

// ---------- api ----------
void begin() {
  FastLED.addLeds<WS2812B, PIN_LED_DATA, GRB>(leds, MATRIX_W * MATRIX_H);
  FastLED.setMaxPowerInVoltsAndMilliamps(5, LED_POWER_MA);
  FastLED.setBrightness(BRIGHTNESS_CAP);
  FastLED.clear(true);
  bootAt = lastActive = millis();
  randomSeed(esp_random());
  scheduleBlink(millis(), false);
  nextFidget = millis() + 5000;
}

void update() {
  uint32_t now = millis();
  if (now - lastFrame < 1000 / FACE_FPS) return;
  lastFrame = now;
  render(now);
  // ease every pixel toward the target: soft transitions instead of hard frame cuts
  for (int y = 0; y < MATRIX_H; y++) {
    for (int x = 0; x < MATRIX_W; x++) {
      CRGB& a = shown[y][x];
      a = blend(a, frame[y][x], test ? 255 : 150);
      leds[xyToIndex(x, y)] = a;
    }
  }
  FastLED.setBrightness((uint8_t)(BRIGHTNESS_CAP * bright / 100));
  FastLED.show();
}

void setState(const char* name) {
  for (int i = 0; i < 5; i++) {
    if (strcmp(name, ST_NAMES[i]) == 0) {
      St next = (St)i;
      if (next != S_IDLE) lastActive = millis();
      if (st != S_IDLE && next == S_IDLE) lastActive = millis();  // drowsiness counts from here
      st = next;
      return;
    }
  }
}

void setOnline(bool wifi, bool server) {
  if (server && !serverUp) lastActive = millis();
  wifiUp = wifi;
  serverUp = server;
}

void wake() {
  wakeAt = lastActive = millis();
  emoUntil = 0;  // a fresh conversation starts with a fresh face
  exUntil = 0;
}

void setEmotion(const char* name, float strength, float holdS) {
  Emo e = emoFromName(name);
  if (e == N_EMO) return;
  if (strength < 0.3f && e != NEUTRAL) e = NEUTRAL;
  emo = e;
  emoUntil = millis() + (uint32_t)(holdS * 1000);
}

void expr(const char* name, float holdS) {
  Emo e = emoFromName(name);
  if (e == N_EMO) return;
  ex = e;
  exStart = millis();
  exUntil = exStart + (uint32_t)(holdS * 1000);
  lastActive = exStart;
}

void hearVoice(float relDeg) {
  voiceRel = relDeg;
  voiceAt = millis();
}

void setBrightness(int level) { bright = constrain(level, 0, 100); }
void setBattery(int pct) { battPct = pct; }
void testPattern(bool on) { test = on; }
const char* describe() { return desc; }

}  // namespace face
