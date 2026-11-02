#include "net.h"

#include <ArduinoJson.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#include "audio.h"
#include "battery.h"
#include "config.h"
#include "face.h"
#include "head.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "no src/secrets.h: copy secrets.example.h and fill it in. building with placeholders"
#include "secrets.example.h"
#endif

namespace net {

static const uint16_t BEACON_PORT = 8769;
static const char* PATH = "/lite";

static WebSocketsClient ws;
static WiFiUDP udp;
static bool connected = false, wsStarted = false, udpOpen = false;
static String host = LITE_HOST;
static uint16_t port = LITE_PORT;
static uint32_t lastStatus = 0, lastBatt = 0, lastDoa = 0, wifiSince = 0, failSince = 0;
static int lastSentBatt = -2;
static char desc[48] = "-";

static void handleText(const char* txt, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, txt, len)) return;
  const char* t = doc["type"] | "";
  if (!strcmp(t, "state")) {
    const char* s = doc["state"] | "idle";
    face::setState(s);
    head::setState(s);
  } else if (!strcmp(t, "wake")) {
    face::wake();
    head::onWake();
  } else if (!strcmp(t, "emotion")) {
    face::setEmotion(doc["emotion"] | "neutral", doc["strength"] | 1.0f, doc["hold"] | 3.0f);
  } else if (!strcmp(t, "expr")) {
    face::expr(doc["name"] | "blink", doc["hold"] | 1.5f);
  } else if (!strcmp(t, "flush")) {
    audio::flush();
  } else if (!strcmp(t, "volume")) {
    audio::setVolume(doc["level"] | DEFAULT_VOLUME);
  } else if (!strcmp(t, "brightness")) {
    face::setBrightness(doc["level"] | 100);
  } else if (!strcmp(t, "look")) {
    head::look(doc["angle"] | 0.0f);
  } else if (!strcmp(t, "welcome")) {
    audio::setVolume(doc["volume"] | DEFAULT_VOLUME);
    face::setBrightness(doc["brightness"] | 100);
    Serial.printf("[net] linked to the run box (%s)\n", desc);
  }
}

static void onEvent(WStype_t type, uint8_t* payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED: {
      connected = true;
      failSince = 0;
      JsonDocument hello;
      hello["type"] = "hello";
      hello["proto"] = 1;
      hello["device"] = DEVICE_NAME;
      hello["fw"] = FW_VERSION;
      hello["token"] = LITE_TOKEN;
      hello["rate"] = AUDIO_RATE;
      hello["frame_ms"] = FRAME_SAMPLES * 1000 / AUDIO_RATE;
      String out;
      serializeJson(hello, out);
      ws.sendTXT(out);
      lastSentBatt = -2;
      break;
    }
    case WStype_DISCONNECTED:
      if (connected) Serial.println("[net] link down");
      connected = false;
      audio::flush();
      if (!failSince) failSince = millis();
      break;
    case WStype_TEXT:
      handleText((const char*)payload, len);
      break;
    case WStype_BIN:
      audio::pushSpeech(payload, len);
      break;
    default:
      break;
  }
}

static void startSocket() {
  snprintf(desc, sizeof(desc), "%s:%u", host.c_str(), port);
  Serial.printf("[net] connecting ws://%s%s\n", desc, PATH);
  ws.begin(host, port, PATH);
  ws.onEvent(onEvent);
  ws.setReconnectInterval(2000);
  ws.enableHeartbeat(10000, 4000, 2);  // notice a dead AP in ~18 s, not minutes
  wsStarted = true;
  failSince = millis();
}

// the run box broadcasts {"reedus_lite_hub":true,"port":8767} on udp 8769 every 2 s
static void listenForBeacon() {
  if (!udpOpen) {
    udp.begin(BEACON_PORT);
    udpOpen = true;
    Serial.println("[net] no LITE_HOST set, listening for the run box beacon");
  }
  int n = udp.parsePacket();
  if (n <= 0) return;
  char buf[160];
  n = udp.read(buf, sizeof(buf) - 1);
  if (n <= 0) return;
  buf[n] = 0;
  JsonDocument doc;
  if (deserializeJson(doc, buf) || !(doc["reedus_lite_hub"] | false)) return;
  host = udp.remoteIP().toString();
  port = doc["port"] | LITE_PORT;
  udp.stop();
  udpOpen = false;
  startSocket();
}

void begin() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // modem sleep adds 100+ ms of jitter to every packet
  WiFi.setHostname(DEVICE_NAME);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[net] wifi: joining %s\n", WIFI_SSID);
}

void update() {
  bool up = WiFi.status() == WL_CONNECTED;
  if (!up) {
    wifiSince = 0;
    connected = false;
    return;
  }
  if (!wifiSince) {
    wifiSince = millis();
    Serial.printf("[net] wifi up, %s rssi %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }

  if (!wsStarted) {
    if (host.length()) startSocket();
    else listenForBeacon();
    return;
  }
  ws.loop();

  // discovered host gone quiet for 20 s (run box moved IP?): go back to the beacon
  if (!connected && failSince && millis() - failSince > 20000 && !strlen(LITE_HOST)) {
    ws.disconnect();
    wsStarted = false;
    host = "";
    return;
  }
  if (!connected) return;

  // mic up: everything the audio task has queued, one 20 ms frame per binary message
  int16_t frame[FRAME_SAMPLES];
  int sent = 0;
  while (sent < 8 && audio::popMicFrame(frame)) {
    ws.sendBIN((uint8_t*)frame, sizeof(frame));
    sent++;
  }

  uint32_t now = millis();
  // where the voice is, a few times a second while someone's talking
  float world, rel;
  uint32_t age;
  if (now - lastDoa > 250 && head::lastVoice(world, rel, age) && age < 300) {
    lastDoa = now;
    char b[112];
    snprintf(b, sizeof(b),
             "{\"type\":\"doa\",\"angle\":%.0f,\"rel\":%.0f,\"head\":%.0f,\"speech\":true}", world,
             rel, head::angle());
    ws.sendTXT(b);
  }
  if (now - lastBatt > 60000 || (battery::percent() != lastSentBatt && now - lastBatt > 5000)) {
    lastBatt = now;
    lastSentBatt = battery::percent();
    if (battery::available()) {
      char b[80];
      snprintf(b, sizeof(b), "{\"type\":\"battery\",\"pct\":%d,\"volts\":%.2f}",
               battery::percent(), battery::volts());
      ws.sendTXT(b);
    }
  }
  if (now - lastStatus > 10000) {
    lastStatus = now;
    char b[200];
    snprintf(b, sizeof(b),
             "{\"type\":\"status\",\"rssi\":%d,\"heap\":%u,\"psram\":%u,\"buffered_ms\":%u,"
             "\"underruns\":%u,\"mic_drops\":%u,\"uptime\":%u,\"head\":%.0f}",
             WiFi.RSSI(), (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram(),
             (unsigned)audio::bufferedMs(), (unsigned)audio::underruns(),
             (unsigned)audio::micDropped(), (unsigned)(now / 1000), head::angle());
    ws.sendTXT(b);
  }
}

bool wifiUp() { return WiFi.status() == WL_CONNECTED; }
bool linked() { return connected; }
void sendJson(const char* json) {
  if (connected) ws.sendTXT(json);
}
const char* serverDesc() { return desc; }

}  // namespace net
