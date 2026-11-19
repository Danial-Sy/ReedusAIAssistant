// XVF3800 control over I2C (Seeed's protocol: [resid][cmd|0x80][len+1], then [status][data]).
#pragma once
#include <Arduino.h>

namespace xvf {

bool begin();
bool present();

// direction of arrival in the array's own frame: 0-359 deg, and the board's speech flag.
// false if the read failed.
bool readDoa(uint16_t& deg, bool& speech);

bool version(uint8_t& major, uint8_t& minor, uint8_t& patch);
bool setRingEffect(uint8_t effect);  // 0 off, 1 breathing, 2 rainbow, 3 solid, 4 DoA, 5 ring
bool setRingColor(uint32_t rgb);

}  // namespace xvf
