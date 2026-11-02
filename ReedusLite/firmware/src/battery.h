// cell voltage through a 100k/100k divider on D3. the UPS module hides it from the 5V side,
// so this is the only way to know how much is left.
#pragma once
#include <Arduino.h>

namespace battery {

void begin();
void update();
bool available();
float volts();
int percent();  // -1 if not wired

}  // namespace battery
