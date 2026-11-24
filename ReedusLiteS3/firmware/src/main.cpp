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
#include "sounds.h"
#include "store.h"
#include "wake.h"
#include "xvf.h"

static String line;

static void help() {
  Serial.println(
      "commands:\n"
      "  info              relay link, pairing, battery, head, face, gate\n"
      "  unpair            forget this Lite's token: it shows a new pairing code\n"
      "  wifi <ssid> <pw>  join another network (saved; the password is the last word,\n"
      "                    `-` for an open network). `wifi` alone shows what's saved\n"
      "  gate              print the speech gate opening/closing (toggle)\n"
      "  wake              wake word: live probabilities (toggle), with memory and timing\n"
      "  wake <0.5-0.99>   try another cutoff until reboot (keep it: WAKE_CUTOFF in config.h)\n"
      "  barge             talking over him: his echo level and how far a voice clears it (toggle)\n"
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
      "  ring / chime      play the ring tone / the wake chime once\n"
      "  cal               calibration (saved on the device): show it, or\n"
      "    cal rotate <0-3>  turn the face a quarter at a time      cal flipx / flipy  mirror it\n"
      "    cal center <us>   servo pulse for straight ahead (1500)  cal center +10 / -10  nudge\n"
      "    cal dir           flip which way the head turns          cal front  you're in front: set DoA\n"
      "    cal doaflip       flip the DoA direction                 cal reset  back to config.h\n"
      "  glance <what>     show a sample: clock weather event music timer ring alarm off\n"
      "  reboot");
}

// ringing: beep every 1.6 s, unless he's mid-conversation (you're telling him "stop")
static void ringTone(uint32_t now) {
  static uint32_t last = 0;
  if (!glance::ringing() || relay::listenOpen()) return;
  if (now - last < 1600 || audio::bufferedMs() > 80) return;
  last = now;
  sounds::ring();
}

static void showCal() {
  int rot;
  bool fx, fy;
  face::orientation(rot, fx, fy);
  head::Cal c = head::cal();
  int raw;
  uint32_t age;
  Serial.printf("face   rotate %d  flipx %s  flipy %s\n", rot, fx ? "on" : "off", fy ? "on" : "off");
  Serial.printf("servo  center %d us  dir %+d  (head now %.1f deg)\n", c.centerUs, c.dir, head::angle());
  Serial.printf("doa    front %d deg  sign %+d", c.doaFront, c.doaSign);
  if (head::lastRawDoa(raw, age)) Serial.printf("  (last voice raw %d deg, %u ms ago)", raw, (unsigned)age);
  Serial.println();
}

static void calCommand(String arg) {
  String what = arg, val;
  int sp = arg.indexOf(' ');
  if (sp > 0) {
    what = arg.substring(0, sp);
    val = arg.substring(sp + 1);
    val.trim();
  }
  int rot;
  bool fx, fy;
  face::orientation(rot, fx, fy);
  head::Cal c = head::cal();
  if (what == "rotate") {
    face::setOrientation(val.length() ? val.toInt() : rot + 1, fx, fy, true);
    face::testPattern(true);
  } else if (what == "flipx") {
    face::setOrientation(rot, !fx, fy, true);
    face::testPattern(true);
  } else if (what == "flipy") {
    face::setOrientation(rot, fx, !fy, true);
    face::testPattern(true);
  } else if (what == "center") {
    if (val.startsWith("+") || val.startsWith("-")) c.centerUs += val.toInt();
    else if (val.length()) c.centerUs = val.toInt();
    head::setManual(0);  // look straight ahead while you judge it
    head::setCal(c, true);
  } else if (what == "dir") {
    c.dir = -c.dir;
    head::setCal(c, true);
  } else if (what == "front") {
    int raw;
    uint32_t age;
    if (!head::lastRawDoa(raw, age) || age > 10000) {
      Serial.println("talk straight into the face first, then `cal front` within 10 s");
      return;
    }
    c.doaFront = raw;
    head::setCal(c, true);
  } else if (what == "doaflip") {
    c.doaSign = -c.doaSign;
    head::setCal(c, true);
  } else if (what == "reset") {
    store::clearCal();
    face::setOrientation(MATRIX_ROTATE, MATRIX_FLIP_X, false, false);
    head::setCal({SERVO_CENTER_US, SERVO_DIR, DOA_FRONT_DEG, DOA_SIGN}, false);
  } else if (what.length()) {
    Serial.println("? cal rotate|flipx|flipy|center|dir|front|doaflip|reset");
    return;
  }
  showCal();
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
    if (relay::paired()) Serial.println("paired: yes");
    else if (relay::pairingCode()[0]) Serial.printf("paired: no, pairing code %s\n", relay::pairingCode());
    else Serial.println("paired: no (a code shows once the relay is reachable)");
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
  } else if (c == "barge") {
    static bool bargeOn = false;
    bargeOn = !bargeOn;
    audio::setBargeDebug(bargeOn);
    Serial.printf("barge view %s (wake cutoff over his voice %.2f, %u talk-overs, %u barges)\n",
                  bargeOn ? "on: talk over him while he speaks" : "off", wake::bargeCutoff(),
                  (unsigned)audio::talkovers(), (unsigned)wake::barges());
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
    sounds::ring();
  } else if (c == "chime") {
    sounds::wake();
  } else if (c == "cal") {
    calCommand(arg);
  } else if (c == "glance") {
    glance::demo(arg.length() ? arg.c_str() : "clock");
  } else if (c == "unpair") {
    relay::unpair();
  } else if (c == "wifi") {
    int last = arg.lastIndexOf(' ');
    if (!arg.length()) {
      String s = store::wifiSsid();
      Serial.printf("wifi: %s\n", s.length() ? s.c_str() : "from secrets.h");
    } else if (last <= 0) {
      Serial.println("wifi <network name> <password>   (`-` as the password for an open network)");
    } else {
      String ssid = arg.substring(0, last), pass = arg.substring(last + 1);
      ssid.trim();
      if (pass == "-") pass = "";
      relay::setWifi(ssid.c_str(), pass.c_str());
    }
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
  store::begin();  // the calibration lives here: face and head read it in begin()
  face::begin();   // then a face, while the rest comes up
  head::begin();
  battery::begin();
  xvf::begin();
  if (!audio::begin()) Serial.println("[audio] I2S init FAILED");
  wake::begin();  // before wifi + TLS, so its arena gets internal RAM
  sounds::begin();
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
  audio::bargeReport();

  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (line.length()) command(line);
      line = "";
    } else if (line.length() < 160) {  // room for `wifi <ssid> <password>`
      line += ch;
    }
  }
  delay(1);
}
