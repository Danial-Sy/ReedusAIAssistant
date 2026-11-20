// the little sounds Lite makes by itself, rendered once at 16 kHz and played through the speech
// path (so the volume applies and the XVF3800's echo canceller hears them as Reedus):
//   wake  the old Reedus "I'm listening" chime: C5 E5 G5 (reedus_companion/voice/jingle.py)
//   ring  two beeps, for timers and alarms
#pragma once
#include <Arduino.h>

namespace sounds {

void begin();
void wake();
void ring();

}  // namespace sounds
