// Reedus Lite (standalone S3) build constants. forked from ReedusLite/firmware/src/config.h:
// the hardware sections are identical (same board, same wiring); the link is the cloud relay.
// anything tagged MEASURE is a guess until the parts are on the bench; the serial console
// (type `help`) has a command to check each one.
#pragma once
#include <Arduino.h>

#ifdef FW_BUILD                    // CI builds add the commit: 0.3.0-s3.<sha>
#define FW_VERSION "0.3.0-s3." FW_BUILD
#else
#define FW_VERSION "0.3.0-s3"
#endif
#define DEVICE_NAME "reedus-lite-s3"

// ---------- the speech gate (relay.cpp) ----------
// the mic only streams to the relay while someone's talking, or while a conversation is open
#define GATE_PREROLL_MS 400        // sent first when the gate opens, so "hey" isn't clipped
#define GATE_HOLD_MS 1500          // stay open this long after the voice stops
#define GATE_RMS 700.0f            // MEASURE with `gate`: beam level (int16 RMS) that counts as a
                                   // voice when the XVF3800's own speech flag hasn't fired

// ---------- the wake word, on the S3 (wake.cpp) ----------
// "Reedus" (microWakeWord, ReedusLiteS3/wakeword/models). when the relay agrees (welcome says
// wake: "device"), the mic stays here until the name is heard, and the speech gate above is
// only the fallback for a relay that runs openWakeWord itself
#define WAKE_ON_DEVICE 1           // 0: don't load the model; the relay does the wake word
#define WAKE_CHANNEL MIC_CHANNEL   // MEASURE: the I2S slot it listens to. on the 48 kHz master
                                   // firmware slot 1 is the XVF3800's wake-word beam
#define WAKE_CUTOFF 0.0f           // 0 = the manifest's probability_cutoff (0.90). higher: fewer
                                   // false wakes, more misses. try values live with `wake 0.95`
#define WAKE_PREROLL_MS 250        // audio from just before the detection that goes up with it:
                                   // the detector fires ~0.2 s after the name ends, so "Reedus
                                   // what time is it" in one breath keeps its first word
#define WAKE_REFRACTORY_MS 2000    // one "Reedus" = one wake
#define WAKE_WARMUP_SLICES 100     // ignore the first second (the noise estimate settling)
#define WAKE_TASK_CORE 0           // wifi's core has the idle time; audio + the loop are on 1
#define WAKE_CHIME 1               // the old Reedus "listening" chime when the name is heard
#define CHIME_GAIN 0.20f           // its loudness before the volume setting (jingle.py uses 0.20)

// ---------- ringing on its own (glance.cpp localTick) ----------
#define LOCAL_RING_MAX_MS 60000    // a timer/alarm the relay never rang: ring here for a minute

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
#define MUSIC_BUFFER_MS 2000       // music jitter buffer (24 kHz, PSRAM): music can afford latency
#define MUSIC_PREBUFFER_MS 300     // starts once this much is queued
#define MUSIC_DUCK 0.22f           // music level under speech / while listening (~-13 dB)

// ---------- XVF3800 control (I2C) ----------
#define XVF_ADDR 0x2C
#define XVF_DOA_POLL_MS 100
#define XVF_RING_EFFECT 0          // the board's own LED ring: 0 off (it's inside the head), 4 = DoA

// direction of arrival -> bearing. bearing is degrees, 0 = straight out of the face,
// + = the robot's left (same as the chassis mockup). MEASURE both: stand in front, run
// `doa`, put the reported angle here; then step to its left, the bearing must go positive.
#define DOA_FRONT_DEG 280          // measured: talking straight into the face reads raw 280
#define DOA_SIGN -1                // flipped 2026-11-20: with 1 a voice on its left turned the head
                                   // right. `cal doaflip` flips it live if it's ever backwards again

// ---------- LED matrix ----------
#define MATRIX_W 16
#define MATRIX_H 16
#define MATRIX_SERPENTINE 1        // BTF 16x16 panels zig-zag
#define MATRIX_ROTATE 1            // measured with `test`: the panel is mounted transposed, so a
#define MATRIX_FLIP_X 1            // quarter turn + mirror. `cal rotate/flipx/flipy` overrides live
#define BRIGHTNESS_CAP 46          // of 255 (~18%): behind the diffuser this is plenty
#define LED_POWER_MA 1200          // FastLED dims the frame if it would draw more
#define FACE_FPS 40

// ---------- head servo ----------
#define SERVO_CENTER_US 1500       // MEASURE: pulse that points the face straight ahead
#define SERVO_US_PER_DEG 10.0f     // SG90 is roughly 500-2400 us over 180 deg
#define SERVO_DIR 1                // checked: `servo 30` turns toward its left (your right, facing it)
#define HEAD_LIMIT_DEG 90.0f       // +/-90 per the chassis; the eyes cover the rest
#define HEAD_MAX_DPS 120.0f        // top speed (deg/s). lower = calmer
#define HEAD_ACCEL_DPS2 360.0f     // how fast it gets up to speed and slows down (deg/s^2). lower = softer
#define HEAD_TURN_SCALE 0.85f      // turn this share of the way to the voice; the eyes cover the rest
#define HEAD_FOLLOW_DELAY_MS 200   // eyes glance first, the head follows
#define HEAD_DEADBAND_DEG 12.0f    // closer than this, the eyes alone handle it
#define HEAD_IDLE_FOLLOW 0         // 0: the head only turns when it hears "Reedus" (1: also toward
                                   // anyone who keeps talking while it's idle)
// after "Reedus", while the conversation is open and you're the one talking, the head keeps
// drifting after your voice: slowly, and only once you've clearly moved. it stops when the
// conversation ends (quiet for long enough, or goodbye) and stays where it is
#define HEAD_TRACK_IN_CONVO 1
#define HEAD_TRACK_DPS 45.0f       // top speed for a small drift after you (deg/s)
#define HEAD_TRACK_ACCEL_DPS2 120.0f  // and a gentle start and stop
#define HEAD_TRACK_EVERY_MS 500    // at most one adjustment this often
#define HEAD_TRACK_WINDOW_MS 900   // who's talking = readings from this long ago to now
#define HEAD_TRACK_MIN_N 4         // ...at least this many that agree (DoA is polled every 100 ms)
#define HEAD_TRACK_DEADBAND_DEG 18.0f  // smaller moves are left to the eyes
#define HEAD_NEW_SPEAKER_DEG 45.0f // further than this: someone else is talking, turn at full speed
#define HEAD_CLUSTER_DEG 20.0f     // readings within this of each other are the same voice
#define HEAD_CLUSTER_SHARE 0.6f    // the voice must be this share of recent readings to count
#define HEAD_ECHO_GUARD_MS 350     // while he talks and this long after, his own echo is around...
#define HEAD_SPEAKER_CONE_DEG 35.0f  // ...so voices within this of the base's front are ignored
#define HEAD_DETACH_MS 500         // stop pulsing once there, an idle SG90 buzzes

// ---------- face timing ----------
#define IDLE_DROWSY_S 300          // no conversation for 5 min: heavy eyelids
#define IDLE_SLEEP_S 900           // 15 min: asleep, dim, Zzz

// ---------- battery ----------
#define BATT_DIVIDER 2.0f          // (R1+R2)/R2
#define BATT_CAL 1.0f              // MEASURE: multimeter volts / reported volts
#define BATT_LOW_PCT 12
