// the swivel: DoA from the XVF3800 -> where the voice is -> eyes first, head ~0.2 s later.
// the mics turn with the head, so world bearing = what the array reports + head angle.
#pragma once
#include <Arduino.h>

namespace head {

void begin();
void update();  // call every loop

void setState(const char* state);  // the head holds still while listening/thinking/speaking
void onWake();                     // turn toward whoever just said the name
void look(float worldDeg);         // server-requested turn
void center();
void setManual(float headDeg);     // serial console: point it and stay

float angle();                     // where the head is now (deg, + = its left)
bool moving();
void setDoaPrint(bool on);

// for the status message
bool lastVoice(float& worldDeg, float& relDeg, uint32_t& ageMs);

}  // namespace head
