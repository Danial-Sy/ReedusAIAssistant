// the face as a display: a timer you can read across the room, alarms and reminders ringing,
// and the idle cards (time, weather, next class, now playing). the run box decides what to
// show (lite/glance.py); the drawing and the ticking happen here so a wifi hiccup doesn't
// freeze the countdown. ReedOS's js/face.js draws the same thing for the app preview.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <FastLED.h>

namespace glance {

typedef void (*PxFn)(int x, int y, const CRGB& c, uint8_t v);

// clock | timers | glance messages from the run box
void onMessage(const char* type, JsonDocument& doc);

// draw if there's something to show. idle: no conversation is going on. asleep: the face has
// dozed off. returns true when the glance owns the panel (the face isn't drawn), and may lower
// *dim for night cards. even when it returns false it may add the armed-alarm dot.
bool draw(uint32_t now, bool idle, bool asleep, PxFn px, float* dim);

bool ringing();                     // something is ringing right now: wakes the face up

void showPair(const char* code);    // hold a pairing code on the face; "" puts the face back

// standalone S3: a countdown that runs out with no ring from the relay (link down, or the
// relay more than 2.5 s late) rings here anyway, for up to LOCAL_RING_MAX_MS. the relay's next
// `timers` message is the truth again. call every loop.
void localTick(uint32_t now, bool linked);
bool localRinging();
// stop ringing now (his name was heard, "stop", the app's Stop button) and never ring that
// timer/alarm again from stale data. false if nothing was ringing
bool silence();
// the ring changed (on or off) since the last call: report it to the relay so the app can
// show a Stop button. id/kind describe the current ring
bool takeRingChange(bool* on, const char** id, const char** kind);
const char* describe();
void demo(const char* what);        // serial `glance <clock|weather|event|music|timer|ring|off>`

}  // namespace glance
