// what the S3 keeps across reboots, in NVS flash: the bulletin (Reedus's notes about you,
// pushed by the relay, the source of truth) so an offline mode still knows who you are.
#pragma once
#include <Arduino.h>

namespace store {

void begin();
uint32_t bulletinRev();
String bulletin();
void saveBulletin(uint32_t rev, const char* text);

}  // namespace store
