// the 16x16 face. the server says what Reedus is doing (state) and feeling (emotion,
// one-shot expressions); everything that makes it look alive (blinks, glances, idle
// fidgets, the mouth riding the speech level, drowsiness) happens here.
#pragma once
#include <Arduino.h>

namespace face {

void begin();
void update();  // call every loop; renders at FACE_FPS

void setState(const char* state);  // idle | listening | thinking | speaking | sleep
void setOnline(bool wifi, bool server);
void wake();  // the name was heard: perk up
void setEmotion(const char* name, float strength, float holdS);
void expr(const char* name, float holdS);  // any emotion name, or wink/bye/dizzy/yawn/content/blink

// where the voice is, relative to the face (deg, + = its left). call when the XVF3800 hears speech
void hearVoice(float relDeg);

void setBrightness(int level);  // 0-100 of BRIGHTNESS_CAP
void setBattery(int pct);
void testPattern(bool on);

// panel mounting (`cal`): quarter turns, then mirror left-right / top-bottom, as seen from the front
void setOrientation(int rotate, bool flipX, bool flipY, bool save);
void orientation(int& rotate, bool& flipX, bool& flipY);
const char* describe();

bool knownExpr(const char* name);

// drawings and animations from the app (relay binary kind 3). rgb = 16x16x3, rows top to
// bottom, as seen from the front. the last frame (idx == count-1) starts playback. holdS 0 =
// until cleared; anyone talking to him (wake, listening) brings the face back
void artFrame(int idx, int count, int fps, int holdS, bool loop, const uint8_t* rgb);
void artClear();
bool artActive();
bool artTakeEnded();  // true once after a drawing finished on its own (tell the relay)

// firmware update progress bar (ota.cpp): pct 0-100, <0 = off
void setUpdating(int pct);

}  // namespace face
