#include "battery.h"

#include "config.h"

namespace battery {

static float v = 0;
static int pct = -1;
static uint32_t last = 0;

// resting Li-ion (Molicel P28A, two in parallel) volts -> percent
static const float CURVE[][2] = {{4.20f, 100}, {4.10f, 90}, {4.00f, 79}, {3.90f, 65},
                                 {3.80f, 50},  {3.70f, 33}, {3.60f, 18}, {3.50f, 9},
                                 {3.40f, 4},   {3.30f, 1},  {3.00f, 0}};

static int toPct(float volts) {
  if (volts >= CURVE[0][0]) return 100;
  for (size_t i = 1; i < sizeof(CURVE) / sizeof(CURVE[0]); i++) {
    if (volts >= CURVE[i][0]) {
      float t = (volts - CURVE[i][0]) / (CURVE[i - 1][0] - CURVE[i][0]);
      return (int)roundf(CURVE[i][1] + t * (CURVE[i - 1][1] - CURVE[i][1]));
    }
  }
  return 0;
}

static float sample() {
  uint32_t acc = 0;
  for (int i = 0; i < 16; i++) acc += analogReadMilliVolts(PIN_BATT_ADC);
  return acc / 16.0f / 1000.0f * BATT_DIVIDER * BATT_CAL;
}

void begin() {
  if (PIN_BATT_ADC < 0) return;
  analogSetPinAttenuation(PIN_BATT_ADC, ADC_11db);  // full ~0-3.1 V range, divider gives ~2.1 V
  v = sample();
  pct = v > 2.5f ? toPct(v) : -1;  // nothing on the pin: report "no battery reading"
}

void update() {
  if (PIN_BATT_ADC < 0 || millis() - last < 5000) return;
  last = millis();
  float s = sample();
  if (s < 2.5f) {
    pct = -1;
    return;
  }
  v = v < 2.5f ? s : v * 0.8f + s * 0.2f;  // speech peaks sag the cell, smooth them out
  pct = toPct(v);
}

bool available() { return pct >= 0; }
float volts() { return v; }
int percent() { return pct; }

}  // namespace battery
