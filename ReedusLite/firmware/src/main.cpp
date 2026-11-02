// Reedus Lite: ears, mouth and face for the Reedus brain on the run box.
// the XIAO only streams audio and animates; wake word, STT, Gemini and Inworld all run on
// the run box (reedus_companion/lite). bring-up: open the serial monitor and type `help`.
#include <Arduino.h>

#include "audio.h"
#include "battery.h"
#include "config.h"
#include "face.h"
#include "head.h"
#include "net.h"
#include "xvf.h"

static String line;

static void help() {
  Serial.println(
      "commands:\n"
      "  info              link, battery, head, face\n"
      "  test              LED corner test (toggle): red=top-left green=top-right\n"
      "                    blue=bottom-left white=bottom-right, as seen from the front\n"
      "  doa               print direction of arrival live (toggle)\n"
      "  servo <deg>       point the head (+ = its left); `servo off` = back to center\n"
      "  state <name>      idle|listening|thinking|speaking|sleep\n"
      "  expr <name> [s]   happy laugh love sad surprised angry confused sleepy excited\n"
      "                    smug shy curious wink bye dizzy yawn content blink\n"
      "  vol <0-100>       speaker volume\n"
      "  bright <0-100>    face brightness (under the cap)\n"
      "  wake              pretend the wake word fired\n"
      "  reboot");
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
  static bool testOn = false, doaOn = false;
  if (c == "help" || c == "?") {
    help();
  } else if (c == "info") {
    Serial.printf("fw %s  wifi %s  link %s (%s)\n", FW_VERSION, net::wifiUp() ? "up" : "down",
                  net::linked() ? "up" : "down", net::serverDesc());
    Serial.printf("xvf3800 %s  head %.1f deg  vol %d  buffered %u ms  underruns %u  mic drops %u\n",
                  xvf::present() ? "ok" : "MISSING", head::angle(), audio::volume(),
                  (unsigned)audio::bufferedMs(), (unsigned)audio::underruns(),
                  (unsigned)audio::micDropped());
    Serial.printf("battery %s %.2f V %d%%  heap %u  psram %u\n",
                  battery::available() ? "ok" : "n/a", battery::volts(), battery::percent(),
                  (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram());
    Serial.printf("face %s\n", face::describe());
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
  } else if (c == "wake") {
    face::wake();
    head::onWake();
    face::setState("listening");
  } else if (c == "reboot") {
    ESP.restart();
  } else if (c.length()) {
    Serial.println("? (type help)");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\nReedus Lite %s\n", FW_VERSION);
  face::begin();  // first, so there's a face while the rest comes up
  head::begin();
  battery::begin();
  xvf::begin();
  if (!audio::begin()) Serial.println("[audio] I2S init FAILED");
  net::begin();
  face::setBattery(battery::percent());
}

void loop() {
  net::update();
  head::update();
  battery::update();
  face::setOnline(net::wifiUp(), net::linked());
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
