// the swivel: DoA from the XVF3800 -> where the voice is -> eyes first, head ~0.2 s later.
// the mics turn with the head, so world bearing = what the array reports + head angle.
#pragma once
#include <Arduino.h>

namespace head {

void begin();
void update();  // call every loop

void setState(const char* state);  // the head holds still while listening/thinking/speaking
void setNearDb(float db);          // only follow voices within this of the loudest since the wake
void onWake();                     // turn toward whoever just said the name
void look(float worldDeg);         // server-requested turn
void center();
void setManual(float headDeg);     // serial console: point it and stay

float angle();                     // where the head is now (deg, + = its left)

// calibration (`cal`): servo pulse at center, which way + turns, the array's raw DoA angle
// that means "straight out of the face", and which way its angles run
struct Cal {
  int centerUs, dir, doaFront, doaSign;
};
Cal cal();
void setCal(const Cal& c, bool save);
bool lastRawDoa(int& raw, uint32_t& ageMs);  // the array's last raw angle while someone spoke
bool moving();
void setDoaPrint(bool on);

// for the status message
bool lastVoice(float& worldDeg, float& relDeg, uint32_t& ageMs);

}  // namespace head
