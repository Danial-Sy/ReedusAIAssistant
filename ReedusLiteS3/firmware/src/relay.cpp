#include "relay.h"

#include <ArduinoJson.h>
#include <WebSocketsClient.h>
#include <WiFi.h>

#include "audio.h"
#include "battery.h"
#include "config.h"
#include "face.h"
#include "glance.h"
#include "head.h"
#include "store.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "no src/secrets.h: copy secrets.example.h and fill it in. building with placeholders"
#include "secrets.example.h"
#endif

// the relay's root certificate (tools/fetch_ca.py writes it). without it the link is still
// encrypted but the relay's identity isn't checked
#if RELAY_TLS && __has_include("ca_cert.h")
#include "ca_cert.h"
#define HAVE_CA 1
#else
#define HAVE_CA 0
#endif

namespace relay {

static const char* PATH = "/lite";

static WebSocketsClient ws;
static bool connected = false, wsStarted = false;
static uint32_t lastStatus = 0, lastBatt = 0, lastDoa = 0, wifiSince = 0;
static int lastSentBatt = -2;
static int proto = 1;  // what the relay agreed to in welcome
static uint32_t lastMusic = 0, musicIdleAt = 0;
static char desc[64] = "-";

// ---------- the speech gate ----------
// nothing goes up while the room is quiet. when the XVF3800 flags speech (or the beam gets
// loud), the last GATE_PREROLL_MS goes up first so the start of "hey reedus" isn't clipped,
// then live audio until GATE_HOLD_MS after the voice stops. while the relay has a
// conversation open (`listen`), everything goes up.
static const int PREROLL = GATE_PREROLL_MS / 20;
static int16_t* pre = nullptr;  // PREROLL frames, PSRAM
static int preHead = 0, preCount = 0;
static bool gateOpen = false, listening = false, gateDebug = false;
static uint32_t lastSpeech = 0;

static bool voiced(const int16_t* f) {
  uint32_t age;
  float w, r;
  if (head::lastVoice(w, r, age) && age < 200) return true;  // the XVF3800's own speech flag
  int64_t acc = 0;
  for (int i = 0; i < FRAME_SAMPLES; i += 2) acc += (int32_t)f[i] * f[i];
  float rms = sqrtf((float)acc / (FRAME_SAMPLES / 2));
  return rms > GATE_RMS;
}

static void setGate(bool open) {
  if (open == gateOpen) return;
  gateOpen = open;
  ws.sendTXT(open ? "{\"type\":\"gate\",\"open\":true}" : "{\"type\":\"gate\",\"open\":false}");
  if (gateDebug) Serial.printf("[gate] %s\n", open ? "open" : "closed");
}

static void pushPreroll(const int16_t* f) {
  if (!pre) return;
  memcpy(pre + preHead * FRAME_SAMPLES, f, FRAME_SAMPLES * 2);
  preHead = (preHead + 1) % PREROLL;
  if (preCount < PREROLL) preCount++;
}

static void sendPreroll() {
  if (!pre) return;
  int start = (preHead - preCount + PREROLL) % PREROLL;
  for (int i = 0; i < preCount; i++) {
    ws.sendBIN((uint8_t*)(pre + ((start + i) % PREROLL) * FRAME_SAMPLES), FRAME_SAMPLES * 2);
  }
  preCount = 0;
}

static void micUp() {
  int16_t frame[FRAME_SAMPLES];
  int n = 0;
  uint32_t now = millis();
  while (n < 8 && audio::popMicFrame(frame)) {
    n++;
    bool v = voiced(frame);
    if (v) lastSpeech = now;
    if (listening) {
      setGate(true);
      ws.sendBIN((uint8_t*)frame, sizeof(frame));
      continue;
    }
    if (!gateOpen) {
      if (v) {
        setGate(true);
        sendPreroll();
        ws.sendBIN((uint8_t*)frame, sizeof(frame));
      } else {
        pushPreroll(frame);
      }
      continue;
    }
    ws.sendBIN((uint8_t*)frame, sizeof(frame));
    if (now - lastSpeech > GATE_HOLD_MS) {
      setGate(false);
      preCount = 0;
    }
  }
}

// ---------- events from the relay ----------
static void handleText(const char* txt, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, txt, len)) return;
  const char* t = doc["type"] | "";
  if (!strcmp(t, "state")) {
    const char* s = doc["state"] | "idle";
    face::setState(s);
    head::setState(s);
    audio::setDuck(strcmp(s, "idle") != 0 && strcmp(s, "sleep") != 0);
  } else if (!strcmp(t, "listen")) {
    listening = doc["open"] | false;
    if (!listening) lastSpeech = 0;  // let the gate close on its own schedule
  } else if (!strcmp(t, "wake")) {
    face::wake();
    head::onWake();
    audio::setDuck(true);
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
  } else if (!strcmp(t, "clock") || !strcmp(t, "timers") || !strcmp(t, "glance")) {
    glance::onMessage(t, doc);
  } else if (!strcmp(t, "music")) {
    const char* op = doc["op"] | "";
    if (!strcmp(op, "flush")) audio::musicFlush();
  } else if (!strcmp(t, "bulletin")) {
    uint32_t rev = doc["rev"] | 0;
    store::saveBulletin(rev, doc["text"] | "");
    char b[64];
    snprintf(b, sizeof(b), "{\"type\":\"bulletin_ack\",\"rev\":%u}", (unsigned)rev);
    ws.sendTXT(b);
    Serial.printf("[relay] bulletin rev %u saved (%u chars)\n", (unsigned)rev,
                  (unsigned)strlen(doc["text"] | ""));
  } else if (!strcmp(t, "welcome")) {
    proto = doc["proto"] | 1;
    audio::setVolume(doc["volume"] | DEFAULT_VOLUME);
    face::setBrightness(doc["brightness"] | 100);
    Serial.printf("[relay] linked to the relay (%s) as %s\n", desc, (const char*)(doc["device"] | "?"));
  }
}

static void onEvent(WStype_t type, uint8_t* payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED: {
      connected = true;
      JsonDocument hello;
      hello["type"] = "hello";
      hello["proto"] = 3;  // 3: the cloud relay. gated mic, listen, bulletin, local ringing
      hello["caps"].add("gate");
      hello["caps"].add("local_ring");
      hello["caps"].add("bulletin");
      if (audio::musicSupported()) hello["caps"].add("music24");
      hello["device"] = DEVICE_NAME;
      hello["fw"] = FW_VERSION;
      hello["token"] = LITE_TOKEN;
      hello["rate"] = AUDIO_RATE;
      hello["frame_ms"] = FRAME_SAMPLES * 1000 / AUDIO_RATE;
      hello["bulletin_rev"] = store::bulletinRev();
      String out;
      serializeJson(hello, out);
      ws.sendTXT(out);
      lastSentBatt = -2;
      proto = 1;
      gateOpen = listening = false;
      preCount = 0;
      break;
    }
    case WStype_DISCONNECTED:
      if (connected) Serial.println("[relay] relay link down");
      connected = false;
      gateOpen = listening = false;
      audio::flush();
      audio::musicFlush();
      audio::setDuck(false);
      face::setState("idle");
      break;
    case WStype_TEXT:
      handleText((const char*)payload, len);
      break;
    case WStype_BIN:
      if (proto >= 2) {
        if (len < 1) break;
        if (payload[0] == 1) audio::pushSpeech(payload + 1, len - 1);
        else if (payload[0] == 2) audio::pushMusic(payload + 1, len - 1);
      } else {
        audio::pushSpeech(payload, len);
      }
      break;
    default:
      break;
  }
}

static void startSocket() {
  snprintf(desc, sizeof(desc), "%s://%s:%u", RELAY_TLS ? "wss" : "ws", RELAY_HOST, (unsigned)RELAY_PORT);
  Serial.printf("[relay] connecting %s%s\n", desc, PATH);
#if RELAY_TLS
#if HAVE_CA
  ws.beginSslWithCA(RELAY_HOST, RELAY_PORT, PATH, RELAY_CA_PEM);
#else
  Serial.println("[relay] WARNING: no src/ca_cert.h, the relay's certificate isn't checked. "
                 "Run tools/fetch_ca.py");
  ws.beginSSL(RELAY_HOST, RELAY_PORT, PATH);
#endif
#else
  ws.begin(RELAY_HOST, RELAY_PORT, PATH);
#endif
  ws.onEvent(onEvent);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 5000, 2);
  wsStarted = true;
}

void begin() {
  pre = (int16_t*)ps_malloc(PREROLL * FRAME_SAMPLES * 2);
  if (!pre) pre = (int16_t*)malloc(PREROLL * FRAME_SAMPLES * 2);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // modem sleep adds 100+ ms of jitter to every packet
  WiFi.setHostname(DEVICE_NAME);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[relay] wifi: joining %s\n", WIFI_SSID);
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
    Serial.printf("[relay] wifi up, %s rssi %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }
  if (!wsStarted) {
    startSocket();
    return;
  }
  ws.loop();
  if (!connected) {
    // keep the mic queue from backing up while there's nowhere to send it
    int16_t frame[FRAME_SAMPLES];
    while (audio::popMicFrame(frame)) {
    }
    return;
  }

  micUp();

  uint32_t now = millis();
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
  bool mActive = audio::musicPlaying() || audio::musicBufferedMs() > 0;
  if (mActive) musicIdleAt = 0;
  else if (!musicIdleAt) musicIdleAt = now;
  bool recent = mActive || (musicIdleAt && now - musicIdleAt < 1500);
  if (recent && now - lastMusic > 500) {
    lastMusic = now;
    char b[96];
    snprintf(b, sizeof(b), "{\"type\":\"music\",\"buffered_ms\":%u,\"underruns\":%u,\"playing\":%s}",
             (unsigned)audio::musicBufferedMs(), (unsigned)audio::musicUnderruns(),
             audio::musicPlaying() ? "true" : "false");
    ws.sendTXT(b);
  }
  if (now - lastStatus > 10000) {
    lastStatus = now;
    char b[220];
    snprintf(b, sizeof(b),
             "{\"type\":\"status\",\"rssi\":%d,\"heap\":%u,\"psram\":%u,\"buffered_ms\":%u,"
             "\"underruns\":%u,\"mic_drops\":%u,\"uptime\":%u,\"head\":%.0f,\"gate\":%s}",
             WiFi.RSSI(), (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram(),
             (unsigned)audio::bufferedMs(), (unsigned)audio::underruns(),
             (unsigned)audio::micDropped(), (unsigned)(now / 1000), head::angle(),
             gateOpen ? "true" : "false");
    ws.sendTXT(b);
  }
}

bool wifiUp() { return WiFi.status() == WL_CONNECTED; }
bool linked() { return connected; }
bool streaming() { return connected && gateOpen; }
bool listenOpen() { return listening; }
void sendJson(const char* json) {
  if (connected) ws.sendTXT(json);
}
const char* serverDesc() { return desc; }
void setGateDebug(bool on) { gateDebug = on; }

}  // namespace relay
