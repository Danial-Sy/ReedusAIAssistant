// Reedus Lite, standalone: the XIAO ESP32-S3 talks straight to the Reedus relay in the cloud
// (ReedusLiteS3/relay on Railway). no run box. the S3 runs the body (face, head, audio, battery),
// listens for "Reedus" itself (wake.cpp) so nothing streams until the name is said, keeps a
// copy of Reedus's notes about you, and rings timers and alarms on its own if the link drops.
// the relay does speech-to-text, the LLM, memory and Inworld (and the wake word, for a relay
// that doesn't trust the S3's or when the model can't load).
// bring-up: open the serial monitor and type `help`.
#include <Arduino.h>

#include "audio.h"
#include "battery.h"
#include "config.h"
#include "face.h"
#include "glance.h"
#include "head.h"
#include "relay.h"
#include "store.h"
#include "wake.h"
#include "xvf.h"

static String line;

static void help() {
  Serial.println(
      "commands:\n"
      "  info              relay link, battery, head, face, gate\n"
      "  gate              print the speech gate opening/closing (toggle)\n"
      "  wake              wake word: live probabilities (toggle), with memory and timing\n"
      "  wake <0.5-0.99>   try another cutoff until reboot (keep it: WAKE_CUTOFF in config.h)\n"
      "  bulletin          what Reedus remembers about you (the copy on this device)\n"
      "  test              LED corner test (toggle): red=top-left green=top-right\n"
      "                    blue=bottom-left white=bottom-right, as seen from the front\n"
      "  doa               print direction of arrival live (toggle)\n"
      "  servo <deg>       point the head (+ = its left); `servo off` = back to center\n"
      "  state <name>      idle|listening|thinking|speaking|sleep\n"
      "  expr <name> [s]   happy laugh love sad surprised angry confused sleepy excited\n"
      "                    smug shy curious wink bye dizzy yawn content blink\n"
      "  vol <0-100>       speaker volume\n"
      "  bright <0-100>    face brightness (under the cap)\n"
      "  ring              play the ring tone once\n"
      "  glance <what>     show a sample: clock weather event music timer ring alarm off\n"
      "  reboot");
}

// two short beeps, 16 kHz, made once. played through the speech path so volume applies
static int16_t* ringPcm = nullptr;
static size_t ringSamples = 0;

static void makeRing() {
  const float beep = 0.11f, gap = 0.07f;
  ringSamples = (size_t)(AUDIO_RATE * (beep * 2 + gap));
  ringPcm = (int16_t*)ps_malloc(ringSamples * 2);
  if (!ringPcm) ringPcm = (int16_t*)malloc(ringSamples * 2);
  if (!ringPcm) return;
  for (size_t i = 0; i < ringSamples; i++) {
    float t = (float)i / AUDIO_RATE;
    bool second = t > beep + gap;
    bool on = t < beep || second;
    float lt = second ? t - beep - gap : t;
    float env = on ? fminf(1.0f, fminf(lt, beep - lt) / 0.008f) : 0.0f;
    float f = second ? 1175.0f : 880.0f;
    ringPcm[i] = (int16_t)(env * 0.3f * 32767.0f * sinf(2.0f * PI * f * t));
  }
}

static void playRing() {
  if (ringPcm) audio::pushSpeech((const uint8_t*)ringPcm, ringSamples * 2);
}

// ringing: beep every 1.6 s, unless he's mid-conversation (you're telling him "stop")
static void ringTone(uint32_t now) {
  static uint32_t last = 0;
  if (!glance::ringing() || relay::listenOpen()) return;
  if (now - last < 1600 || audio::bufferedMs() > 80) return;
  last = now;
  playRing();
}

static void command(String c) {
  c.trim();
  String arg;
  int sp = c.indexOf(' ');
  if (sp > 0) {
    arg = c.substring(sp + 1);
    arg.trim();
    c = c.substring(0, sp);
  }
  static bool testOn = false, doaOn = false, gateOn = false, wakeOn = false;
  if (c == "help" || c == "?") {
    help();
  } else if (c == "info") {
    Serial.printf("fw %s  wifi %s  relay %s (%s)  mic %s%s\n", FW_VERSION,
                  relay::wifiUp() ? "up" : "down", relay::linked() ? "linked" : "down",
                  relay::serverDesc(), relay::streaming() ? "streaming" : "gated",
                  relay::listenOpen() ? " (conversation open)" : "");
    Serial.printf("xvf3800 %s  head %.1f deg  vol %d  buffered %u ms  underruns %u  mic drops %u\n",
                  xvf::present() ? "ok" : "MISSING", head::angle(), audio::volume(),
                  (unsigned)audio::bufferedMs(), (unsigned)audio::underruns(),
                  (unsigned)audio::micDropped());
    Serial.printf("battery %s %.2f V %d%%  heap %u  psram %u  bulletin rev %u\n",
                  battery::available() ? "ok" : "n/a", battery::volts(), battery::percent(),
                  (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram(),
                  (unsigned)store::bulletinRev());
    Serial.printf("face %s  glance %s\n", face::describe(), glance::describe());
    Serial.printf("wake word %s%s\n", wake::describe(),
                  wake::ready() && relay::linked() && !relay::deviceWakeOn()
                      ? " (the relay runs its own: speech gate)" : "");
  } else if (c == "gate") {
    gateOn = !gateOn;
    relay::setGateDebug(gateOn);
  } else if (c == "wake") {
    if (arg.length()) {
      float v = arg.toFloat();
      if (v >= 0.3f && v < 1.0f) wake::setCutoff(v);
      Serial.printf("wake word cutoff %.2f\n", wake::cutoff());
    } else {
      wakeOn = !wakeOn;
      wake::report();
      wake::setDebug(wakeOn);
      Serial.printf("wake word live view %s\n", wakeOn ? "on (type wake again to stop)" : "off");
    }
  } else if (c == "bulletin") {
    String b = store::bulletin();
    Serial.printf("rev %u:\n%s\n", (unsigned)store::bulletinRev(), b.length() ? b.c_str() : "(nothing yet)");
  } else if (c == "test") {
    testOn = !testOn;
    face::testPattern(testOn);
  } else if (c == "doa") {
    doaOn = !doaOn;
    head::setDoaPrint(doaOn);
  } else if (c == "servo") {
    if (arg == "off") head::center();
    else head::setManual(arg.toFloat());
  } else if (c == "state") {
    face::setState(arg.c_str());
    head::setState(arg.c_str());
  } else if (c == "expr") {
    String name = arg;
    float hold = 3.0f;
    int s2 = arg.indexOf(' ');
    if (s2 > 0) {
      name = arg.substring(0, s2);
      hold = arg.substring(s2 + 1).toFloat();
    }
    if (!face::knownExpr(name.c_str())) Serial.println("unknown expression");
    face::expr(name.c_str(), hold);
  } else if (c == "vol") {
    audio::setVolume(arg.toInt());
  } else if (c == "bright") {
    face::setBrightness(arg.toInt());
  } else if (c == "ring") {
    playRing();
  } else if (c == "glance") {
    glance::demo(arg.length() ? arg.c_str() : "clock");
  } else if (c == "reboot") {
    ESP.restart();
  } else if (c.length()) {
    Serial.println("? (type help)");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\nReedus Lite (standalone) %s\n", FW_VERSION);
  face::begin();  // first, so there's a face while the rest comes up
  head::begin();
  battery::begin();
  xvf::begin();
  store::begin();
  if (!audio::begin()) Serial.println("[audio] I2S init FAILED");
  wake::begin();  // before wifi + TLS, so its arena gets internal RAM
  makeRing();
  relay::begin();
  face::setBattery(battery::percent());
}

void loop() {
  uint32_t now = millis();
  relay::update();
  head::update();
  battery::update();
  glance::localTick(now, relay::linked());
  ringTone(now);
  face::setOnline(relay::wifiUp(), relay::linked());
  face::setBattery(battery::percent());
  face::update();

  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (line.length()) command(line);
      line = "";
    } else if (line.length() < 64) {
      line += ch;
    }
  }
  delay(1);
}
