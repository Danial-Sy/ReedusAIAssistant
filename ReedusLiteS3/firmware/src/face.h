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
const char* describe();

bool knownExpr(const char* name);

}  // namespace face
