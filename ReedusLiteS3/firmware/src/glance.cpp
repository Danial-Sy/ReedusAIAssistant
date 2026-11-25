#include "glance.h"

#include "config.h"
#include "font.h"

namespace glance {

static const CRGB MINT(0x8F, 0xF5, 0xE2);
static const CRGB GOLD(0xFF, 0xC8, 0x40);
static const CRGB SKY(0x6F, 0xA8, 0xFF);
static const CRGB SNOW(0xC8, 0xD8, 0xE8);
static const CRGB CLOUD(0x90, 0x9C, 0xA8);
static const CRGB CORAL(0xFF, 0x6A, 0x50);
static const CRGB PINK(0xFF, 0x5C, 0x9E);

struct Timer {
  char id[12];
  char label[24];
  uint32_t endsAt;   // millis
  float pausedLeft;  // seconds, when paused
  float total;
  bool paused;
};

static PxFn P = nullptr;

// ---------- state from the run box ----------
static uint32_t wallBase = 0, wallAt = 0;  // local wall-clock seconds at millis() wallAt
static bool h24 = false, haveClock = false;
static Timer timers[4];
static int nTimers = 0;
static char ringKind[12] = "", ringLabel[44] = "", ringId[24] = "";
static bool ring = false, localRing = false;
static uint32_t ringStart = 0;
// rings already stopped (silenced, or gave up): a later `timers` message or a stale countdown
// naming one of these never starts it ringing again. a few slots, oldest overwritten
static char quiet[6][24] = {"", "", "", "", "", ""};
static int quietNext = 0;
static bool ringReported = false;   // what the relay was last told
static int32_t alarmIn = -1;
static uint32_t alarmAt = 0;

static char card[10] = "";  // "" = none
static uint32_t cardStart = 0, cardUntil = 0;
static bool cardDim = false;
static int wTemp = 0;
static char wUnit[2] = "F", wIcon[8] = "sun";
// the weather is a standing card, not a passing one: while it's on (the app's idle weather
// switch) it holds the panel whenever he's idle, dozing included, until the name wakes him.
// passing cards (music, events, text) and timers still go in front of it. the relay refreshes
// it every few minutes; hold 0 takes it down (switched off, or the night hours)
static uint32_t wxUntil = 0;
static char cTitle[44] = "", cAt[8] = "", cArtist[28] = "";
static int cMins = 0;
static char desc[64] = "-";

// ---------- drawing helpers ----------
static void px(int x, int y, const CRGB& c, uint8_t v = 255) { P(x, y, c, v); }

static const font::Glyph* small(char ch) {
  if (ch >= 'a' && ch <= 'z') ch -= 32;
  for (int i = 0; i < font::N_SMALL; i++)
    if (font::SMALL[i].ch == ch) return &font::SMALL[i];
  for (int i = 0; i < font::N_SMALL; i++)
    if (font::SMALL[i].ch == ' ') return &font::SMALL[i];  // unknown -> a space
  return &font::SMALL[0];
}

static const font::Glyph* big(char ch) {
  for (int i = 0; i < font::N_BIG; i++)
    if (font::BIG[i].ch == ch) return &font::BIG[i];
  return nullptr;
}

static int smallWidth(const char* s, bool tight = false) {
  int w = 0;
  for (const char* p = s; *p; p++) {
    bool colon = *p == ':';
    w += small(*p)->w + ((tight && (colon || p[1] == ':')) ? 0 : 1);
  }
  return w > 0 ? w - 1 : 0;
}

// returns the x after the last glyph
static int drawSmall(const char* s, int x, int y, const CRGB& c, uint8_t v = 255, bool tight = false) {
  for (const char* p = s; *p; p++) {
    const font::Glyph* g = small(*p);
    if (x > -8 && x < MATRIX_W + 1) {
      for (int r = 0; r < font::SMALL_H; r++)
        for (int b = 0; b < g->w; b++)
          if (g->rows[r] & (1 << (g->w - 1 - b))) px(x + b, y + r, c, v);
    }
    bool colon = *p == ':';
    x += g->w + ((tight && (colon || p[1] == ':')) ? 0 : 1);
  }
  return x;
}

static void drawBig(char ch, int x, int y, const CRGB& c, uint8_t v = 255) {
  const font::Glyph* g = big(ch);
  if (!g) return;
  for (int r = 0; r < font::BIG_H; r++)
    for (int b = 0; b < g->w; b++)
      if (g->rows[r] & (1 << (g->w - 1 - b))) px(x + b, y + r, c, v);
}

static void drawSmallCentered(const char* s, int y, const CRGB& c, uint8_t v = 255) {
  bool tight = smallWidth(s) > MATRIX_W;
  drawSmall(s, (MATRIX_W - smallWidth(s, tight) + 1) / 2, y, c, v, tight);
}

// one line that scrolls right-to-left, looping. started at `t0`
static void scroll(const char* s, int y, const CRGB& c, uint32_t now, uint32_t t0, uint8_t v = 255) {
  int w = smallWidth(s);
  if (w <= MATRIX_W) {
    drawSmallCentered(s, y, c, v);
    return;
  }
  int span = w + MATRIX_W + 6;
  int off = (int)(((now - t0) * 18UL / 1000UL) % (uint32_t)span);  // 18 px/s
  drawSmall(s, MATRIX_W - off, y, c, v);
}

static uint32_t scrollMs(const char* s) {
  int w = smallWidth(s);
  return w <= MATRIX_W ? 0 : (uint32_t)(w + MATRIX_W + 6) * 1000UL / 18UL;
}

// "M:SS" in the big digits, exactly 16 wide
static void drawMSS(int secs, int y, const CRGB& c, uint8_t v, bool colon) {
  int m = secs / 60, s = secs % 60;
  drawBig('0' + m % 10, 0, y, c, v);
  if (colon) {
    px(5, y + 2, c, v);
    px(5, y + 4, c, v);
  }
  drawBig('0' + s / 10, 7, y, c, v);
  drawBig('0' + s % 10, 12, y, c, v);
}

// two big digits centered on a row (one digit when lead is false and n < 10)
static void drawPair(int n, int y, const CRGB& c, uint8_t v, bool lead) {
  if (n < 10 && !lead) {
    drawBig('0' + n, 6, y, c, v);
  } else {
    drawBig('0' + (n / 10) % 10, 3, y, c, v);
    drawBig('0' + n % 10, 8, y, c, v);
  }
}

static void eyes(uint32_t now, int y, const CRGB& c, bool wide) {
  // two little eyes peeking over the numbers: they blink and glance about
  uint32_t ph = now % 4200;
  bool blink = ph > 4050;
  int gx = ((now / 2600) % 3) - 1;  // -1, 0, 1
  int h = wide ? 3 : 2;
  int top = wide ? y - 1 : y;
  for (int e = 0; e < 2; e++) {
    int x = (e ? 10 : 4) + gx;
    if (blink && !wide) {
      px(x, y + 1, c, 200);
      px(x + 1, y + 1, c, 200);
    } else {
      for (int r = 0; r < h; r++) {
        px(x, top + r, c);
        px(x + 1, top + r, c);
      }
    }
  }
}

static void bell(int cx, int y, const CRGB& c, int tilt) {
  // 7 wide, 7 tall, swinging by `tilt` px at the bottom
  px(cx, y, c);
  for (int x = -1; x <= 1; x++) px(cx + x, y + 1, c);
  for (int r = 2; r <= 4; r++) {
    int t = r >= 4 ? tilt : 0;
    for (int x = -2; x <= 2; x++) px(cx + x + t, y + r, c);
  }
  for (int x = -3; x <= 3; x++) px(cx + x + tilt, y + 5, c);
  px(cx + tilt * 2, y + 6, c, 200);
}

// ---------- weather icons, drawn in a 10x8 box at (x, y) ----------
static void cloud(int x, int y, const CRGB& c, uint8_t v = 255) {
  for (int i = 3; i <= 5; i++) px(x + i, y, c, v);
  for (int i = 2; i <= 7; i++) px(x + i, y + 1, c, v);
  for (int i = 1; i <= 8; i++) px(x + i, y + 2, c, v);
  for (int i = 0; i <= 9; i++) px(x + i, y + 3, c, v);
  for (int i = 1; i <= 8; i++) px(x + i, y + 4, c, v);
}

static void sun(int x, int y, uint32_t now, uint8_t v = 255) {
  // 4x4 disc with rays that breathe
  for (int r = 0; r < 4; r++)
    for (int i = (r == 0 || r == 3) ? 1 : 0; i < ((r == 0 || r == 3) ? 3 : 4); i++) px(x + 2 + i, y + 2 + r, GOLD, v);
  uint8_t rv = (uint8_t)(v * (0.55f + 0.45f * (0.5f + 0.5f * sinf(now / 300.0f))));
  px(x + 3, y, GOLD, rv); px(x + 4, y, GOLD, rv);
  px(x + 3, y + 7, GOLD, rv); px(x + 4, y + 7, GOLD, rv);
  px(x, y + 3, GOLD, rv); px(x, y + 4, GOLD, rv);
  px(x + 7, y + 3, GOLD, rv); px(x + 7, y + 4, GOLD, rv);
  px(x + 1, y + 1, GOLD, rv / 2); px(x + 6, y + 1, GOLD, rv / 2);
  px(x + 1, y + 6, GOLD, rv / 2); px(x + 6, y + 6, GOLD, rv / 2);
}

static void weatherIcon(const char* icon, int x, int y, uint32_t now) {
  if (!strcmp(icon, "sun")) {
    sun(x + 1, y, now);
  } else if (!strcmp(icon, "moon")) {
    for (int r = 0; r < 7; r++) {
      int a = (r == 0 || r == 6) ? 3 : (r == 1 || r == 5) ? 2 : 1;
      int b = (r == 0 || r == 6) ? 5 : (r == 1 || r == 5) ? 4 : 3;
      for (int i = a; i <= b; i++) px(x + i + 1, y + r, SNOW);
    }
    if ((now / 700) % 3 == 0) px(x + 8, y + 1, SNOW, 150);
  } else if (!strcmp(icon, "partly")) {
    sun(x + 3, y - 1, now, 200);
    cloud(x, y + 3, SNOW);
  } else if (!strcmp(icon, "fog")) {
    for (int r = 0; r < 4; r++) {
      int sh = ((now / 400) + r) % 2;
      for (int i = sh; i < 10 - (1 - sh); i++) px(x + i, y + 1 + r * 2, CLOUD, 200);
    }
  } else {
    bool wet = !strcmp(icon, "rain"), snow = !strcmp(icon, "snow"), storm = !strcmp(icon, "storm");
    cloud(x, y, storm ? CLOUD : SNOW, storm ? 230 : 255);
    if (wet || snow) {
      for (int k = 0; k < 3; k++) {
        int dy = (int)((now / (snow ? 260 : 140) + k * 2) % 3);
        px(x + 2 + k * 3, y + 5 + dy, snow ? SNOW : SKY, 230);
      }
    }
    if (storm && (now / 180) % 5 != 0) {
      px(x + 5, y + 5, GOLD); px(x + 4, y + 6, GOLD); px(x + 5, y + 6, GOLD); px(x + 4, y + 7, GOLD);
    }
  }
}

static void note(int x, int y, const CRGB& c) {
  for (int r = 0; r < 5; r++) px(x + 3, y + r, c);
  px(x + 4, y, c); px(x + 5, y + 1, c);
  for (int i = 1; i <= 3; i++) px(x + i, y + 5, c);
  for (int i = 1; i <= 3; i++) px(x + i, y + 6, c);
  px(x, y + 5, c, 150); px(x + 4, y + 5, c, 150);
}

// ---------- clock ----------
static uint32_t wallNow() { return wallBase + (millis() - wallAt) / 1000; }

static void hm(int* h, int* m, int* s) {
  uint32_t t = wallNow() % 86400UL;
  *h = t / 3600;
  *m = (t / 60) % 60;
  *s = t % 60;
}

// ---------- screens ----------
static const Timer* soonest(uint32_t now) {
  const Timer* best = nullptr;
  float bestLeft = 1e9;
  for (int i = 0; i < nTimers; i++) {
    float left = timers[i].paused ? timers[i].pausedLeft
                                  : (int32_t)(timers[i].endsAt - now) / 1000.0f;
    if (left < bestLeft) {
      bestLeft = left;
      best = &timers[i];
    }
  }
  return best;
}

static void drawTimer(const Timer& t, uint32_t now) {
  float leftF = t.paused ? t.pausedLeft : (int32_t)(t.endsAt - now) / 1000.0f;
  int left = leftF <= 0 ? 0 : (int)ceilf(leftF - 0.001f);
  bool blinkOff = t.paused && (now / 600) % 2;
  uint8_t v = blinkOff ? 70 : 255;
  bool colon = t.paused || (now % 1000) < 600;
  CRGB c = left <= 10 && !t.paused ? CORAL : MINT;
  if (left < 600) {
    // eyes peek over M:SS; every 12 s the label takes their place for one pass
    bool showLabel = t.label[0] && (now / 1000) % 12 < 1 + scrollMs(t.label) / 1000;
    if (showLabel) scroll(t.label, 0, GOLD, now, now - ((now / 1000) % 12) * 1000, 200);
    else eyes(now, 2, c, left <= 10 && !t.paused);
    drawMSS(left, 6, c, v, colon);
  } else if (left < 6000) {
    // ten minutes and up: minutes over seconds
    drawPair(left / 60, 0, c, v, true);
    drawPair(left % 60, 8, c, (uint8_t)(v * 0.55f), true);
  } else {
    // past 100 minutes: hours over minutes, with a small H
    int h = left / 3600, m = (left / 60) % 60;
    drawBig('0' + h % 10, 4, 0, c, v);
    drawSmall("H", 9, 2, c, (uint8_t)(v * 0.7f));
    drawPair(m, 8, c, (uint8_t)(v * 0.55f), true);
  }
  // progress along the bottom row, shrinking toward the left
  if (t.total > 0) {
    float p = constrain(leftF / t.total, 0.0f, 1.0f);
    int n = (int)ceilf(p * MATRIX_W);
    for (int x = 0; x < n; x++) px(x, 15, c, 60);
  }
  // more than one timer: a dot per extra one, top right
  for (int k = 1; k < nTimers && k < 4; k++) px(MATRIX_W - k, 0, GOLD, 120);
}

static void drawRing(uint32_t now) {
  uint32_t t = now - ringStart;
  bool on = (t % 600) < 380;
  if (!strcmp(ringKind, "timer")) {
    CRGB c = CORAL;
    if (ringLabel[0]) scroll(ringLabel, 0, GOLD, now, ringStart);
    else eyes(now, 2, c, true);
    drawMSS(0, 6, c, on ? 255 : 50, true);
    for (int x = 0; x < MATRIX_W; x++) px(x, 15, c, on ? 160 : 30);
  } else if (!strcmp(ringKind, "alarm")) {
    int tilt = ((t / 140) % 4 == 1) ? 1 : ((t / 140) % 4 == 3) ? -1 : 0;
    bell(7, 0, GOLD, tilt);
    int h, m, s;
    hm(&h, &m, &s);
    char buf[8];
    int hh = h24 ? h : (h % 12 ? h % 12 : 12);
    snprintf(buf, sizeof(buf), "%d:%02d", hh, m);
    drawSmallCentered(buf, 9, GOLD, on ? 255 : 90);
    if (ringLabel[0] && strcmp(ringLabel, "WAKE UP")) scroll(ringLabel, 9, MINT, now, ringStart + 2500);
  } else {  // reminder
    int tilt = ((t / 200) % 4 == 1) ? 1 : ((t / 200) % 4 == 3) ? -1 : 0;
    bell(7, 0, SKY, tilt);
    scroll(ringLabel[0] ? ringLabel : "REMINDER", 9, MINT, now, ringStart);
  }
}

static void drawWeather(uint32_t now) {
  weatherIcon(wIcon, 3, 0, now);
  char buf[8];
  int n = abs(wTemp);
  snprintf(buf, sizeof(buf), "%s%d", wTemp < 0 ? "-" : "", n);
  int len = strlen(buf);
  bool deg = len <= 2;
  int w = len * 5 - 1 + (deg ? 3 : 0);
  int x = (MATRIX_W - w + 1) / 2;
  for (int i = 0; i < len; i++) drawBig(buf[i], x + i * 5, 9, MINT);
  if (deg) {
    int dx = x + len * 5;
    px(dx, 9, MINT); px(dx + 1, 9, MINT); px(dx, 10, MINT); px(dx + 1, 10, MINT);
  }
}

static void drawCard(uint32_t now) {
  if (!strcmp(card, "clock")) {
    if (!haveClock) return;
    int h, m, s;
    hm(&h, &m, &s);
    int hh = h24 ? h : (h % 12 ? h % 12 : 12);
    drawPair(hh, 0, MINT, 255, h24);
    drawPair(m, 8, MINT, 170, true);
    px(s / 4, 15, GOLD, 90);  // the seconds creep along the bottom
  } else if (!strcmp(card, "weather")) {
    drawWeather(now);
  } else if (!strcmp(card, "event")) {
    char top[10];
    if (cMins <= 0) snprintf(top, sizeof(top), "NOW");
    else if (cMins < 60) snprintf(top, sizeof(top), "%dM", cMins);
    else snprintf(top, sizeof(top), "%s", cAt);
    drawSmallCentered(top, 0, GOLD);
    for (int x = 2; x < MATRIX_W - 2; x += 2) px(x, 6, MINT, 50);
    scroll(cTitle, 9, MINT, now, cardStart + 600);
  } else if (!strcmp(card, "music")) {
    note(1, 0, PINK);
    for (int k = 0; k < 3; k++) {
      float ph = now / 170.0f + k * 1.9f;
      int hgt = 2 + (int)(2.5f + 2.5f * sinf(ph) * sinf(ph * 0.37f + k));
      for (int r = 0; r < hgt && r < 7; r++) px(9 + k * 2, 6 - r, MINT, 180);
    }
    char line[64];
    snprintf(line, sizeof(line), cArtist[0] ? "%s - %s" : "%s", cTitle, cArtist);
    scroll(line, 9, MINT, now, cardStart + 500);
  } else if (!strcmp(card, "text")) {
    bell(7, 0, GOLD, 0);
    scroll(cTitle, 9, MINT, now, cardStart + 400);
  }
}

// ---------- api ----------
// not paired yet: the 6-digit code held still in two rows ("653" over "470") so it can be read
// and typed, with "PAIR" flashed up for a second every 7
static char pairDigits[8] = "";

void showPair(const char* code) {
  strncpy(pairDigits, code ? code : "", sizeof(pairDigits) - 1);
  pairDigits[sizeof(pairDigits) - 1] = 0;
}

static void drawPair(uint32_t now) {
  if ((now / 1000) % 7 == 0) {
    drawSmallCentered("PAIR", 5, GOLD);
    return;
  }
  char top[4] = {pairDigits[0], pairDigits[1], pairDigits[2], 0};
  char bottom[4] = {pairDigits[3], pairDigits[4], pairDigits[5], 0};
  drawSmallCentered(top, 2, MINT);
  drawSmallCentered(bottom, 9, MINT);
}

bool draw(uint32_t now, bool idle, bool asleep, PxFn fn, float* dim) {
  P = fn;
  if (pairDigits[0]) {
    drawPair(now);
    snprintf(desc, sizeof(desc), "pairing %s", pairDigits);
    return true;
  }
  if (ring && idle) {
    drawRing(now);
    snprintf(desc, sizeof(desc), "ring %s %s", ringKind, ringLabel);
    return true;
  }
  if (!idle) {
    snprintf(desc, sizeof(desc), "-");
    return false;
  }
  if (card[0] && now < cardUntil && (!asleep || !strcmp(card, "clock"))) {
    drawCard(now);
    if (cardDim || asleep) *dim = min(*dim, 0.45f);
    snprintf(desc, sizeof(desc), "card %s", card);
    return true;
  }
  const Timer* t = soonest(now);
  if (t) {
    drawTimer(*t, now);
    if (asleep) *dim = min(*dim, 0.55f);
    snprintf(desc, sizeof(desc), "timer %s", t->label[0] ? t->label : t->id);
    return true;
  }
  if (wxUntil && (int32_t)(now - wxUntil) < 0) {
    drawWeather(now);
    if (asleep) *dim = min(*dim, 0.45f);
    snprintf(desc, sizeof(desc), "weather %d%s", wTemp, wUnit);
    return true;
  }
  // an alarm is armed for the next day: one dim gold pixel in the corner, like a bedside clock
  if (alarmIn >= 0 && (int32_t)(now - alarmAt) / 1000 < alarmIn) px(0, 0, GOLD, 70);
  snprintf(desc, sizeof(desc), "face");
  return false;
}

bool ringing() { return ring; }
const char* describe() { return desc; }

static void copy(char* dst, size_t n, const char* src) {
  strncpy(dst, src ? src : "", n - 1);
  dst[n - 1] = 0;
}

static bool isQuiet(const char* id) {
  if (!id || !id[0]) return false;
  for (int i = 0; i < 6; i++)
    if (!strcmp(quiet[i], id)) return true;
  return false;
}

static void markQuiet(const char* id) {
  if (!id || !id[0] || isQuiet(id)) return;
  copy(quiet[quietNext], sizeof(quiet[0]), id);
  quietNext = (quietNext + 1) % 6;
}

bool silence() {
  bool was = ring;
  if (ring) markQuiet(ringId);
  ring = localRing = false;
  return was;
}

bool pause() {
  if (localRing) return silence();
  bool was = ring;
  ring = false;
  return was;
}

bool takeRingChange(bool* on, const char** id, const char** kind) {
  if (ring == ringReported) return false;
  ringReported = ring;
  if (on) *on = ring;
  if (id) *id = ringId;
  if (kind) *kind = ringKind;
  return true;
}

void localTick(uint32_t now, bool linked) {
  if (ring) {
    uint32_t cap = !strcmp(ringKind, "timer") ? RING_CAP_TIMER_MS : RING_CAP_ALARM_MS;
    if (localRing && now - ringStart > LOCAL_RING_MAX_MS) silence();
    else if (now - ringStart > cap) silence();  // nobody stopped it: give up, whoever started it
    return;
  }
  for (int i = 0; i < nTimers; i++) {
    Timer& t = timers[i];
    if (t.paused || isQuiet(t.id)) continue;
    int32_t over = (int32_t)(now - t.endsAt);
    if (over >= 0 && (!linked || over > 2500)) {
      ring = localRing = true;
      ringStart = now;
      copy(ringId, sizeof(ringId), t.id);
      copy(ringKind, sizeof(ringKind), "timer");
      copy(ringLabel, sizeof(ringLabel), t.label);
      timers[i] = timers[--nTimers];  // it's done counting
      return;
    }
  }
  if (alarmIn >= 0) {
    int32_t over = (int32_t)((now - alarmAt) / 1000) - alarmIn;
    if (over >= 0 && (!linked || over > 2)) {
      ring = localRing = true;
      ringStart = now;
      copy(ringId, sizeof(ringId), "local-alarm");
      copy(ringKind, sizeof(ringKind), "alarm");
      copy(ringLabel, sizeof(ringLabel), "WAKE UP");
      alarmIn = -1;
    }
  }
}

bool localRinging() { return localRing; }

void onMessage(const char* type, JsonDocument& doc) {
  uint32_t now = millis();
  if (!strcmp(type, "clock")) {
    wallBase = doc["t"] | (uint32_t)0;
    wallAt = now;
    h24 = doc["h24"] | false;
    haveClock = wallBase > 0;
  } else if (!strcmp(type, "timers")) {
    nTimers = 0;
    for (JsonObject it : doc["items"].as<JsonArray>()) {
      if (nTimers >= 4) break;
      Timer& t = timers[nTimers++];
      copy(t.id, sizeof(t.id), it["id"] | "");
      copy(t.label, sizeof(t.label), it["label"] | "");
      float left = it["left"] | 0.0f;
      t.total = it["total"] | left;
      t.paused = it["paused"] | false;
      t.pausedLeft = left;
      t.endsAt = now + (uint32_t)(left * 1000.0f);
    }
    // the relay's word wins, except about a ring that was already stopped here
    JsonArray r = doc["ring"].as<JsonArray>();
    bool was = ring;
    JsonObject live;
    for (JsonObject it : r)
      if (!isQuiet(it["id"] | "")) {
        live = it;
        break;
      }
    ring = !live.isNull();
    localRing = false;
    if (ring) {
      const char* id = live["id"] | "";
      if (!was || strcmp(id, ringId)) ringStart = now;
      copy(ringId, sizeof(ringId), id);
      copy(ringKind, sizeof(ringKind), live["kind"] | "timer");
      copy(ringLabel, sizeof(ringLabel), live["label"] | "");
    }
    alarmIn = doc["alarm"] | -1;
    alarmAt = now;
  } else if (!strcmp(type, "glance")) {
    const char* c = doc["card"] | "none";
    if (!strcmp(c, "none")) {
      card[0] = 0;
      return;
    }
    if (!strcmp(c, "weather") && !(doc["once"] | false)) {  // the standing card (see wxUntil)
      wTemp = doc["temp"] | 0;
      copy(wUnit, sizeof(wUnit), doc["unit"] | "F");
      copy(wIcon, sizeof(wIcon), doc["icon"] | "sun");
      float hold = doc["hold"] | 0.0f;
      wxUntil = hold > 0 ? now + (uint32_t)(hold * 1000) : 0;
      if (!strcmp(card, "weather")) card[0] = 0;
      return;
    }
    copy(card, sizeof(card), c);
    cardStart = now;
    cardDim = doc["dim"] | false;
    if (!strcmp(c, "weather")) {
      wTemp = doc["temp"] | 0;
      copy(wUnit, sizeof(wUnit), doc["unit"] | "F");
      copy(wIcon, sizeof(wIcon), doc["icon"] | "sun");
    } else {
      copy(cTitle, sizeof(cTitle), doc["title"] | (doc["text"] | ""));
      copy(cArtist, sizeof(cArtist), doc["artist"] | "");
      copy(cAt, sizeof(cAt), doc["at"] | "");
      cMins = doc["mins"] | 0;
    }
    float hold = doc["hold"] | 6.0f;
    // let one full pass of a long title finish before the card goes
    uint32_t need = 0;
    if (!strcmp(c, "music")) {
      char line[64];
      snprintf(line, sizeof(line), cArtist[0] ? "%s - %s" : "%s", cTitle, cArtist);
      need = scrollMs(line) + 900;
    } else if (!strcmp(c, "event") || !strcmp(c, "text")) {
      need = scrollMs(cTitle) + 900;
    }
    cardUntil = now + max((uint32_t)(hold * 1000), need);
  }
}

void demo(const char* what) {
  JsonDocument d;
  uint32_t fakeWall = 14 * 3600 + 32 * 60;
  if (!haveClock) {
    wallBase = fakeWall;
    wallAt = millis();
    haveClock = true;
  }
  if (!strcmp(what, "timer")) {
    deserializeJson(d, "{\"items\":[{\"id\":\"demo\",\"label\":\"PASTA\",\"left\":272,\"total\":600}],\"ring\":[],\"alarm\":-1}");
    onMessage("timers", d);
  } else if (!strcmp(what, "ring")) {
    deserializeJson(d, "{\"items\":[],\"ring\":[{\"id\":\"demo\",\"kind\":\"timer\",\"label\":\"PASTA\"}],\"alarm\":-1}");
    onMessage("timers", d);
  } else if (!strcmp(what, "alarm")) {
    deserializeJson(d, "{\"items\":[],\"ring\":[{\"id\":\"demo\",\"kind\":\"alarm\",\"label\":\"WAKE UP\"}],\"alarm\":-1}");
    onMessage("timers", d);
  } else if (!strcmp(what, "off")) {
    deserializeJson(d, "{\"items\":[],\"ring\":[],\"alarm\":-1}");
    onMessage("timers", d);
    card[0] = 0;
    wxUntil = 0;
  } else {
    char buf[200];
    if (!strcmp(what, "weather")) snprintf(buf, sizeof(buf), "{\"card\":\"weather\",\"temp\":72,\"unit\":\"F\",\"icon\":\"partly\",\"hold\":8}");
    else if (!strcmp(what, "event")) snprintf(buf, sizeof(buf), "{\"card\":\"event\",\"title\":\"CHEM 101 LECTURE\",\"at\":\"2:30\",\"mins\":25,\"hold\":9}");
    else if (!strcmp(what, "music")) snprintf(buf, sizeof(buf), "{\"card\":\"music\",\"title\":\"MIDNIGHT CITY\",\"artist\":\"M83\",\"hold\":8}");
    else snprintf(buf, sizeof(buf), "{\"card\":\"clock\",\"hold\":8}");
    deserializeJson(d, buf);
    onMessage("glance", d);
  }
}

}  // namespace glance
