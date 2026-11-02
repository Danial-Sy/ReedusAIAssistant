#include "xvf.h"

#include <Wire.h>

#include "config.h"

namespace xvf {

static const uint8_t RESID_GPO = 20;   // GPO servicer: DoA + the LED ring
static const uint8_t RESID_APP = 48;   // application servicer
static const uint8_t CMD_DOA = 18;     // DOA_VALUE: uint16 angle, uint16 speech
static const uint8_t CMD_LED_EFFECT = 12;
static const uint8_t CMD_LED_COLOR = 16;
static const uint8_t CMD_VERSION = 0;
static const uint8_t STATUS_RETRY = 64;

static bool ok = false;

static bool read(uint8_t resid, uint8_t cmd, uint8_t* out, uint8_t len) {
  for (int attempt = 0; attempt < 3; attempt++) {
    Wire.beginTransmission(XVF_ADDR);
    Wire.write(resid);
    Wire.write(cmd | 0x80);
    Wire.write(len + 1);  // +1 for the status byte
    if (Wire.endTransmission() != 0) return false;
    if (Wire.requestFrom((uint8_t)XVF_ADDR, (uint8_t)(len + 1)) != len + 1) return false;
    uint8_t status = Wire.read();
    for (uint8_t i = 0; i < len; i++) out[i] = Wire.read();
    if (status == 0) return true;
    if (status != STATUS_RETRY) return false;
    delayMicroseconds(300);
  }
  return false;
}

static bool write(uint8_t resid, uint8_t cmd, const uint8_t* data, uint8_t len) {
  Wire.beginTransmission(XVF_ADDR);
  Wire.write(resid);
  Wire.write(cmd);
  Wire.write(len);
  for (uint8_t i = 0; i < len; i++) Wire.write(data[i]);
  return Wire.endTransmission() == 0;
}

bool begin() {
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 100000);
  Wire.setTimeOut(20);
  uint8_t a, b, c;
  ok = version(a, b, c);
  if (ok) {
    Serial.printf("[xvf] XVF3800 firmware %u.%u.%u\n", a, b, c);
    setRingEffect(XVF_RING_EFFECT);
  } else {
    Serial.println("[xvf] no reply at 0x2C: DoA off (is the I2S firmware flashed?)");
  }
  return ok;
}

bool present() { return ok; }

bool readDoa(uint16_t& deg, bool& speech) {
  uint8_t b[4];
  if (!read(RESID_GPO, CMD_DOA, b, 4)) return false;
  deg = (uint16_t)(b[0] | (b[1] << 8)) % 360;
  speech = (b[2] | (b[3] << 8)) != 0;
  return true;
}

bool version(uint8_t& major, uint8_t& minor, uint8_t& patch) {
  uint8_t b[3];
  if (!read(RESID_APP, CMD_VERSION, b, 3)) return false;
  major = b[0];
  minor = b[1];
  patch = b[2];
  return true;
}

bool setRingEffect(uint8_t effect) { return write(RESID_GPO, CMD_LED_EFFECT, &effect, 1); }

bool setRingColor(uint32_t rgb) {
  uint8_t b[4] = {(uint8_t)rgb, (uint8_t)(rgb >> 8), (uint8_t)(rgb >> 16), 0};
  return write(RESID_GPO, CMD_LED_COLOR, b, 4);
}

}  // namespace xvf
