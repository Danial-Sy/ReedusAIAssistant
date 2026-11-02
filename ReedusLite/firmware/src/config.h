// Reedus Lite build constants. anything tagged MEASURE is a guess until the parts are on
// the bench; the serial console (type `help`) has a command to check each one.
#pragma once
#include <Arduino.h>

#define FW_VERSION "0.1.0"
#define DEVICE_NAME "reedus-lite"

// ---------- XIAO pins ----------
// verified against Seeed's pinout photo of this board (pads XIAO_D0-D3, I2C, I2S, MCLK on the
// back) and the formatBCE ESPHome config ("Do not change the pins. They are fixed by the
// board layout."). ReedusLite/WIRING.md is the same table with sources.
//
//   D4  GPIO5   I2C SDA      board: XVF3800 (0x2C) + AIC3104 DAC (0x18)
//   D5  GPIO6   I2C SCL      board
//   D8  GPIO7   I2S LRCK     board
//   D9  GPIO8   I2S BCLK     board
//   D6  GPIO43  I2S DIN      board, XVF3800 -> XIAO (mics)
//   D7  GPIO44  I2S DOUT     board, XIAO -> XVF3800 (speaker + AEC reference)
//   D10 GPIO9   MCLK net     board, leave alone
//   D1  GPIO2   pad only     possibly the XVF3800 reset line (unconfirmed), leave unconnected
//   D0  GPIO1   LED data     ours
//   D2  GPIO3   servo        ours. strapping pin (JTAG select, ignored unless an eFuse is
//                            burned); an output that's just an input at reset is fine here
//   D3  GPIO4   battery ADC  ours. ADC1_CH3, not a strapping pin, so the divider can't
//                            upset boot
#define PIN_I2S_BCK 8
#define PIN_I2S_WS 7
#define PIN_I2S_DIN 43
#define PIN_I2S_DOUT 44
#define PIN_I2C_SDA 5
#define PIN_I2C_SCL 6
#define PIN_LED_DATA 1   // D0 -> SN74AHCT125 pin 2 (1A); pin 3 (1Y) -> 330R -> matrix DIN
#define PIN_SERVO 3      // D2 -> SG90 signal (orange)
#define PIN_BATT_ADC 4   // D3 <- cell+ through 100k/100k divider. -1 = no battery reading

// ---------- audio ----------
// the XVF3800 runs Seeed's I2S *master* firmware (respeaker_xvf3800_i2s_master_..._48k.bin,
// the one the ESPHome integration uses): it makes BCLK/LRCK itself at 48 kHz and the XIAO is
// the slave, so nobody has to produce MCLK. we resample 48k <-> 16k here; the wire is 16k.
// set XVF_I2S_MASTER_48K 0 for Seeed's other I2S firmware with the XIAO as 16 kHz master.
#ifndef XVF_I2S_MASTER_48K
#define XVF_I2S_MASTER_48K 1
#endif
#define AUDIO_RATE 16000           // wire + server rate
#define FRAME_SAMPLES 320          // 20 ms at 16k, matches lite/protocol.py
#define MIC_CHANNEL 0              // master firmware: slot 0 = ASR beam, slot 1 = wake word beam
#define MIC_GAIN 1.0f              // digital gain after the XVF3800's own AGC
#define PLAY_BUFFER_MS 2000        // jitter buffer capacity
#define PLAY_PREBUFFER_MS 120      // same as REEDUS_LITE_PREBUFFER on the server
#define DEFAULT_VOLUME 70          // 0-100, the server sends its own on connect

// ---------- XVF3800 control (I2C) ----------
#define XVF_ADDR 0x2C
#define XVF_DOA_POLL_MS 100
#define XVF_RING_EFFECT 0          // the board's own LED ring: 0 off (it's inside the head), 4 = DoA

// direction of arrival -> bearing. bearing is degrees, 0 = straight out of the face,
// + = the robot's left (same as the chassis mockup). MEASURE both: stand in front, run
// `doa`, put the reported angle here; then step to its left, the bearing must go positive.
#define DOA_FRONT_DEG 0
#define DOA_SIGN 1

// ---------- LED matrix ----------
#define MATRIX_W 16
#define MATRIX_H 16
#define MATRIX_SERPENTINE 1        // BTF 16x16 panels zig-zag
#define MATRIX_ROTATE 0            // MEASURE with `test`: 0-3 quarter turns
#define MATRIX_FLIP_X 0            // MEASURE with `test`: mirror if it reads backwards
#define BRIGHTNESS_CAP 46          // of 255 (~18%): behind the diffuser this is plenty
#define LED_POWER_MA 1200          // FastLED dims the frame if it would draw more
#define FACE_FPS 40

// ---------- head servo ----------
#define SERVO_CENTER_US 1500       // MEASURE: pulse that points the face straight ahead
#define SERVO_US_PER_DEG 10.0f     // SG90 is roughly 500-2400 us over 180 deg
#define SERVO_DIR 1                // MEASURE: flip to -1 if `servo 30` turns it right
#define HEAD_LIMIT_DEG 90.0f       // +/-90 per the chassis; the eyes cover the rest
#define HEAD_MAX_DPS 220.0f        // top speed
#define HEAD_FOLLOW_DELAY_MS 200   // eyes glance first, the head follows
#define HEAD_DEADBAND_DEG 12.0f    // closer than this, the eyes alone handle it
#define HEAD_IDLE_FOLLOW 1         // turn toward sustained talking while idle
#define HEAD_DETACH_MS 500         // stop pulsing once there, an idle SG90 buzzes

// ---------- face timing ----------
#define IDLE_DROWSY_S 300          // no conversation for 5 min: heavy eyelids
#define IDLE_SLEEP_S 900           // 15 min: asleep, dim, Zzz

// ---------- battery ----------
#define BATT_DIVIDER 2.0f          // (R1+R2)/R2
#define BATT_CAL 1.0f              // MEASURE: multimeter volts / reported volts
#define BATT_LOW_PCT 12
