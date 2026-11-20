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
#include "sounds.h"
#include "store.h"
#include "wake.h"

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
// nothing goes up while the room is quiet. with the wake word on the S3 (wake.cpp, and the
// relay said wake: "device" in welcome) the gate opens only on "Reedus": the last
// WAKE_PREROLL_MS goes up right behind the wake message, then live audio. with the relay doing
// the wake word, it opens when the XVF3800 flags speech (or the beam gets loud) and the last
// GATE_PREROLL_MS goes up first so the start of the name isn't clipped. either way it stays
// open until GATE_HOLD_MS after the voice stops, and while the relay has a conversation open
// (`listen`) everything goes up.
static const int PREROLL = (GATE_PREROLL_MS > WAKE_PREROLL_MS ? GATE_PREROLL_MS : WAKE_PREROLL_MS) / 20;
static int16_t* pre = nullptr;  // PREROLL frames, PSRAM
static int preHead = 0, preCount = 0;
static bool gateOpen = false, listening = false, gateDebug = false;
static uint32_t lastSpeech = 0;
static bool deviceWake = false;  // the relay trusts our wake word (welcome)
static uint32_t wokeAt = 0;      // our last wake: the relay's echo of it doesn't replay the face

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

static void sendPreroll(int ms) {
  if (!pre) return;
  int n = min(preCount, ms / 20);
  int start = (preHead - n + PREROLL) % PREROLL;
  for (int i = 0; i < n; i++) {
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
      if (v && !deviceWake) {
        setGate(true);
        sendPreroll(GATE_PREROLL_MS);
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

// "Reedus", heard here: react now, tell the relay, then the pre-roll and live audio (micUp).
// the relay answers with wake + listen like it does for its own wake word
static void onDeviceWake(float score) {
  wokeAt = millis();
  face::wake();
  head::onWake();
  audio::setDuck(true);
  if (audio::playing()) audio::flush();  // barge-in: quiet now, the relay cancels the reply
  sounds::wake();                        // the old Reedus chime: I heard you
  char b[48];
  snprintf(b, sizeof(b), "{\"type\":\"wake\",\"score\":%.2f}", score);
  ws.sendTXT(b);
  if (!gateOpen) {
    setGate(true);
    sendPreroll(WAKE_PREROLL_MS);
  }
  lastSpeech = millis();  // hold the gate while the relay switches to listening
}

// "Reedus" heard with no relay to answer (offline, or before it's deployed): still react, so
// you can tell it heard you and the head and face can be checked on the bench
static void onLocalWake(float score) {
  wokeAt = millis();
  face::wake();
  head::onWake();
  sounds::wake();
  Serial.printf("[wake] heard Reedus (%.2f), no relay linked so nobody answers\n", score);
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
    if (millis() - wokeAt > 1500) {  // not just the echo of ours: the relay heard the name
      face::wake();
      head::onWake();
      sounds::wake();
    }
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
    // an older relay says nothing about it, and runs the wake word itself
    deviceWake = wake::ready() && !strcmp(doc["wake"] | "relay", "device");
    Serial.printf("[relay] linked to the relay (%s) as %s, wake word %s\n", desc,
                  (const char*)(doc["device"] | "?"),
                  deviceWake ? "on the S3" : "on the relay (speech gate)");
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
      if (wake::ready()) hello["caps"].add("wake");  // we can say {"type":"wake"} ourselves
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
      gateOpen = listening = deviceWake = false;
      preCount = 0;
      break;
    }
    case WStype_DISCONNECTED:
      if (connected) Serial.println("[relay] relay link down");
      connected = false;
      gateOpen = listening = deviceWake = false;
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
  float score;
  bool heard = wake::take(&score);  // always drained, so a stale one never fires later
  if (heard && !connected) onLocalWake(score);
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

  if (heard && deviceWake) onDeviceWake(score);
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
    char b[256];
    snprintf(b, sizeof(b),
             "{\"type\":\"status\",\"rssi\":%d,\"heap\":%u,\"psram\":%u,\"buffered_ms\":%u,"
             "\"underruns\":%u,\"mic_drops\":%u,\"uptime\":%u,\"head\":%.0f,\"gate\":%s,"
             "\"wake\":\"%s\"}",
             WiFi.RSSI(), (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram(),
             (unsigned)audio::bufferedMs(), (unsigned)audio::underruns(),
             (unsigned)audio::micDropped(), (unsigned)(now / 1000), head::angle(),
             gateOpen ? "true" : "false", deviceWake ? "device" : "relay");
    ws.sendTXT(b);
  }
}

bool wifiUp() { return WiFi.status() == WL_CONNECTED; }
bool linked() { return connected; }
bool streaming() { return connected && gateOpen; }
bool listenOpen() { return listening; }
bool deviceWakeOn() { return connected && deviceWake; }
void sendJson(const char* json) {
  if (connected) ws.sendTXT(json);
}
const char* serverDesc() { return desc; }
void setGateDebug(bool on) { gateDebug = on; }

}  // namespace relay
