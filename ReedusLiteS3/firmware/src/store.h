// what the S3 keeps across reboots, in NVS flash: the bulletin (Reedus's notes about you,
// pushed by the relay, the source of truth) so an offline mode still knows who you are, and
// the calibration set with `cal` (face orientation, servo center, which way is front).
#pragma once
#include <Arduino.h>

namespace store {

void begin();  // first thing in setup(): face and head read their calibration from here
uint32_t bulletinRev();
String bulletin();
void saveBulletin(uint32_t rev, const char* text);

// calibration: `def` until `cal` saves something (keys are short, NVS caps them at 15 chars)
int getInt(const char* key, int def);
void putInt(const char* key, int value);
void clearCal();  // back to config.h

}  // namespace store
