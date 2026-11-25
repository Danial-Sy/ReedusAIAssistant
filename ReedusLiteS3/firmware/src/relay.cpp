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
#include "ota.h"
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
static uint32_t lastPb = 0, pbIdleAt = 0;
static char desc[64] = "-";

// a firmware update the relay asked for (ota.cpp runs it from update(), outside the socket
// callback), and the reason the last one failed, reported once we're back online
static bool otaPending = false;
static char otaUrl[200] = "", otaMd5[40] = "", otaErr[96] = "";
static size_t otaSize = 0;

// ---------- pairing ----------
// a Lite with no token asks the relay to pair: the relay sends a 6-digit code (shown on the face
// as a scrolling card, and printed here), you type it into the website, and the relay hands
// back this Lite's token, which lives in NVS from then on. "unpaired" (removed in the app)
// forgets it and goes back to showing a code. LITE_TOKEN in secrets.h is only a fallback.
static String token;
static char pairCode[8] = "";
static bool compiledTokenOk = true;  // cleared once the relay refuses it

static bool realToken(const char* t) { return t && strlen(t) > 8 && strncmp(t, "rld_...", 7) != 0; }

static void loadToken() {
  token = store::token();
  if (!token.length() && compiledTokenOk && realToken(LITE_TOKEN)) token = LITE_TOKEN;
}

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
  glance::pause();                       // his name quiets a ring right here; the relay ends
                                         // it on "stop" or brings it back if nobody says
  face::wake();
  head::onWake();
  audio::setDuck(true);
  bool over = audio::sinceSpeechMs() < 1000;  // said over him: a barge-in
  if (audio::playing()) audio::flush();  // barge-in: quiet now, the relay cancels the reply
  sounds::wake();                        // the old Reedus chime: I heard you
  char b[80];
  snprintf(b, sizeof(b), "{\"type\":\"wake\",\"score\":%.2f,\"over\":%s}", score,
           over ? "true" : "false");
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
  glance::silence();
  face::wake();
  head::onWake();
  sounds::wake();
  Serial.printf("[wake] heard Reedus (%.2f), no relay linked so nobody answers\n", score);
}

// the app's wake sensitivity: a cutoff from the relay (welcome / wake_cfg), 0 = this build's
// default (config.h WAKE_CUTOFF, else the model's manifest)
static void applyWakeCutoff(float c) {
  if (!wake::ready()) return;
  float want = (c >= 0.3f && c < 1.0f) ? c : wake::defaultCutoff();
  if (fabsf(want - wake::cutoff()) > 0.004f) {
    wake::setCutoff(want);
    Serial.printf("[wake] cutoff %.2f (from the app)\n", wake::cutoff());
  }
}

// ---------- events from the relay ----------
static void handleText(const char* txt, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, txt, len)) return;
  const char* t = doc["type"] | "";
  if (!strcmp(t, "pair")) {
    const char* code = doc["code"] | "";
    if (strcmp(code, pairCode)) {
      strncpy(pairCode, code, sizeof(pairCode) - 1);
      pairCode[sizeof(pairCode) - 1] = 0;
      glance::showPair(pairCode);
      Serial.printf("[relay] not paired yet. Pairing code: %s (type it in the website: Pair a Lite)\n",
                    pairCode);
    }
    return;
  }
  if (!strcmp(t, "paired")) {
    const char* tok = doc["token"] | "";
    if (realToken(tok)) {
      store::saveToken(tok);
      token = tok;
      pairCode[0] = 0;
      glance::showPair("");
      Serial.printf("[relay] paired as \"%s\". Reconnecting with the new token\n",
                    (const char*)(doc["name"] | "Reedus Lite"));
      face::expr("happy", 2.0f);
      sounds::wake();
    }
    return;
  }
  if (!strcmp(t, "unpaired")) {
    store::saveToken("");
    compiledTokenOk = false;
    token = "";
    Serial.println("[relay] this Lite was removed from its account: showing a pairing code again");
    return;
  }
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
  } else if (!strcmp(t, "chime")) {  // "done": he stopped listening (after what he's saying)
    const char* n = doc["name"] | "done";
    if (!strcmp(n, "wake")) sounds::wake();
    else sounds::done();
  } else if (!strcmp(t, "ring_off")) {  // "stop", or the app's Stop button
    glance::silence();
  } else if (!strcmp(t, "wake_cfg")) {
    applyWakeCutoff(doc["cutoff"] | 0.0f);
    if (doc["barge_cutoff"].is<float>()) {  // 0 = this build's BARGE_CUTOFF
      float bc = doc["barge_cutoff"].as<float>();
      wake::setBargeCutoff(bc >= 0.5f && bc < 1.0f ? bc : BARGE_CUTOFF);
    }
    if (doc["head_near_db"].is<float>()) head::setNearDb(doc["head_near_db"].as<float>());
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
  } else if (!strcmp(t, "cal")) {
    // the app's hearing calibration (the serial console's `cal front` / `cal doaflip`):
    // "front" = the voice he heard last (within 10 s) is straight ahead
    head::Cal c = head::cal();
    const char* op = doc["op"] | "";
    const char* err = "";
    if (!strcmp(op, "front")) {
      int raw;
      uint32_t age;
      if (head::lastRawDoa(raw, age) && age < 10000) c.doaFront = raw;
      else err = "talk to him from straight in front first, then tap it within 10 s";
    } else if (!strcmp(op, "flip")) {
      c.doaSign = -c.doaSign;
    } else if (!strcmp(op, "mirror") || !strcmp(op, "mirror_on") || !strcmp(op, "mirror_off")) {
      // which way the servo turns for a given angle (the serial console's servo dir). hearing
      // stays as is: use this when the dot lands on you but the head swings the other way
      int mirrored = -SERVO_DIR;
      if (!strcmp(op, "mirror")) c.dir = -c.dir;
      else c.dir = !strcmp(op, "mirror_on") ? mirrored : -mirrored;
    } else if (!strcmp(op, "reset")) {
      c.doaFront = DOA_FRONT_DEG;
      c.doaSign = DOA_SIGN;
      c.dir = SERVO_DIR;
    } else if (op[0]) {
      err = "unknown calibration step";
    }
    if (!err[0] && op[0]) head::setCal(c, true);
    c = head::cal();
    char b[200];
    snprintf(b, sizeof(b),
             "{\"type\":\"cal\",\"front\":%d,\"sign\":%d,\"dir\":%d,\"mirror\":%s,\"error\":\"%s\"}",
             c.doaFront, c.doaSign, c.dir, c.dir != SERVO_DIR ? "true" : "false", err);
    ws.sendTXT(b);
    Serial.printf("[head] calibration %s: front %d sign %+d servo dir %+d %s\n", op, c.doaFront,
                  c.doaSign, c.dir, err);
  } else if (!strcmp(t, "draw")) {
    if (!strcmp(doc["op"] | "", "clear")) face::artClear();
  } else if (!strcmp(t, "ota")) {
    const char* url = doc["url"] | "";
    otaSize = doc["size"] | 0;
    if (strlen(url) < sizeof(otaUrl) && otaSize > 0) {
      strcpy(otaUrl, url);
      strncpy(otaMd5, doc["md5"] | "", sizeof(otaMd5) - 1);
      otaMd5[sizeof(otaMd5) - 1] = 0;
      otaPending = true;
      Serial.printf("[relay] firmware update %s requested from the app\n", (const char*)(doc["version"] | ""));
    }
  } else if (!strcmp(t, "clock") || !strcmp(t, "timers") || !strcmp(t, "glance")) {
    // the relay also scrolls "PAIR CODE …" for older firmware; ours holds the code still instead
    if (!strcmp(t, "glance") && !strncmp(doc["text"] | "", "PAIR CODE", 9)) return;
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
    applyWakeCutoff(doc["wake_cutoff"] | 0.0f);
    {
      float bc = doc["barge_cutoff"] | 0.0f;  // 0 / missing = this build's BARGE_CUTOFF
      wake::setBargeCutoff(bc >= 0.5f && bc < 1.0f ? bc : BARGE_CUTOFF);
    }
    head::setNearDb(doc["head_near_db"] | HEAD_NEAR_DB);
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
      hello["caps"].add("draw");  // drawings / animations from the app (binary kind 3)
      hello["caps"].add("ota");   // firmware updates from the app
      hello["caps"].add("cal");   // hearing calibration from the app
      hello["device"] = DEVICE_NAME;
      hello["fw"] = FW_VERSION;
      hello["caps"].add("pair");  // no token: the relay shows a code and hands one over
      if (token.length()) hello["token"] = token;
      else hello["pair"] = true;
      hello["rate"] = AUDIO_RATE;
      hello["frame_ms"] = FRAME_SAMPLES * 1000 / AUDIO_RATE;
      hello["bulletin_rev"] = store::bulletinRev();
      String out;
      serializeJson(hello, out);
      ws.sendTXT(out);
      if (otaErr[0]) {  // the last update failed before we could say so
        char b[160];
        snprintf(b, sizeof(b), "{\"type\":\"ota\",\"state\":\"error\",\"error\":\"%s\"}", otaErr);
        ws.sendTXT(b);
        otaErr[0] = 0;
      }
      lastSentBatt = -2;
      proto = 1;
      gateOpen = listening = deviceWake = false;
      preCount = 0;
      break;
    }
    case WStype_DISCONNECTED:
      if (connected) Serial.println("[relay] relay link down");
      connected = false;
      pairCode[0] = 0;  // a code only means something while this link is up
      glance::showPair("");
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
        else if (payload[0] == 3 && len == 6 + 768)  // [3, index, count, fps, hold s, loop] + RGB
          face::artFrame(payload[1], payload[2], payload[3], payload[4], payload[5] != 0, payload + 6);
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

static void joinWifi() {
  String ssid = store::wifiSsid(), pass = store::wifiPass();
  if (!ssid.length()) {
    ssid = WIFI_SSID;
    pass = WIFI_PASS;
  }
  WiFi.begin(ssid.c_str(), pass.c_str());
  Serial.printf("[relay] wifi: joining %s\n", ssid.c_str());
}

void begin() {
  pre = (int16_t*)ps_malloc(PREROLL * FRAME_SAMPLES * 2);
  if (!pre) pre = (int16_t*)malloc(PREROLL * FRAME_SAMPLES * 2);
  loadToken();
  if (!token.length()) Serial.println("[relay] not paired: Lite will show a code once it's online");
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // modem sleep adds 100+ ms of jitter to every packet
  WiFi.setHostname(DEVICE_NAME);
  joinWifi();
}

void setWifi(const char* ssid, const char* pass) {
  store::saveWifi(ssid, pass);
  WiFi.disconnect();
  joinWifi();
}

void unpair() {
  store::saveToken("");
  compiledTokenOk = false;
  token = "";
  pairCode[0] = 0;
  if (wsStarted) ws.disconnect();  // reconnects on its own and asks for a code
  Serial.println("[relay] token forgotten: Lite will show a pairing code");
}

bool paired() { return token.length() > 0; }
const char* pairingCode() { return pairCode; }

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
    // keep the network that works in flash: a firmware update built without your secrets.h
    // (the app's "latest build") then still knows your Wi-Fi
    if (!store::wifiSsid().length() && strcmp(WIFI_SSID, "your-network-name") != 0)
      store::saveWifi(WIFI_SSID, WIFI_PASS);
  }
  if (!wsStarted) {
    startSocket();
    return;
  }
  ws.loop();
  // the library takes one websocket message per loop() call. one per pass of the main loop
  // (face, head, mic upload in between) fell behind his speech in a long reply, so drain
  // what's waiting. an idle call is cheap
  for (int i = 0; i < 8 && connected; i++) ws.loop();
  if (!connected) {
    // keep the mic queue from backing up while there's nowhere to send it
    int16_t frame[FRAME_SAMPLES];
    while (audio::popMicFrame(frame)) {
    }
    return;
  }

  if (!token.length()) {  // waiting to be paired: nobody's listening yet, keep the mic home
    int16_t frame[FRAME_SAMPLES];
    while (audio::popMicFrame(frame)) {
    }
    return;
  }
  if (heard && deviceWake) onDeviceWake(score);
  bool ringOn;
  const char *ringIdNow, *ringKindNow;
  if (glance::takeRingChange(&ringOn, &ringIdNow, &ringKindNow) && connected) {
    char b[96];
    snprintf(b, sizeof(b), "{\"type\":\"ring\",\"on\":%s,\"id\":\"%s\",\"kind\":\"%s\"}",
             ringOn ? "true" : "false", ringIdNow, ringKindNow);
    ws.sendTXT(b);
  }
  float nearP;
  if (wake::takeNearMiss(&nearP) && connected && deviceWake) {
    char b[80];
    snprintf(b, sizeof(b), "{\"type\":\"wake_near\",\"p\":%.2f,\"over\":%s}", nearP,
             audio::sinceSpeechMs() < 1000 ? "true" : "false");
    ws.sendTXT(b);
  }
  if (audio::takeTalkover()) {  // someone started talking over him: he's ducked; the relay logs
    ws.sendTXT("{\"type\":\"talkover\"}");  // it and listens for the name / "stop" in it
  }
  micUp();

  if (otaPending && !listening) {
    otaPending = false;
    // tell the app, then drop the link: the download gets the Wi-Fi (and the RAM for its own
    // TLS session) to itself. on success we reboot; on failure the socket reconnects on its
    // own and the error goes up right after hello
    ws.sendTXT("{\"type\":\"ota\",\"state\":\"downloading\",\"pct\":0}");
    for (int i = 0; i < 5; i++) {
      ws.loop();
      delay(20);
    }
    ws.disconnect();
    connected = false;
    face::setOnline(true, false);
    if (!ota::run(otaUrl, otaSize, otaMd5, otaErr, sizeof(otaErr))) {
      Serial.printf("[relay] firmware update failed: %s (still on %s)\n", otaErr, FW_VERSION);
      face::expr("confused", 3.0f);
    }
    return;
  }
  if (face::artTakeEnded()) ws.sendTXT("{\"type\":\"draw\",\"op\":\"done\"}");

  uint32_t now = millis();
  float world, rel;
  uint32_t age;
  if (now - lastDoa > 250 && head::lastVoice(world, rel, age) && age < 300) {
    lastDoa = now;
    char b[140];
    int raw = -1;
    uint32_t rawAge;
    if (!head::lastRawDoa(raw, rawAge)) raw = -1;
    snprintf(b, sizeof(b),
             "{\"type\":\"doa\",\"angle\":%.0f,\"rel\":%.0f,\"head\":%.0f,\"raw\":%d,\"speech\":true}",
             world, rel, head::angle(), raw);
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
  // while he talks: how much speech is queued here, so the relay paces against the real
  // playhead (not its own clock) and logs dropouts per reply
  bool pbActive = audio::playing() || audio::bufferedMs() > 0;
  if (pbActive) pbIdleAt = 0;
  else if (!pbIdleAt) pbIdleAt = now;
  if ((pbActive || now - pbIdleAt < 600) && now - lastPb > 250) {
    lastPb = now;
    char b[112];
    snprintf(b, sizeof(b), "{\"type\":\"pb\",\"buf\":%u,\"under\":%u,\"over\":%u,\"playing\":%s}",
             (unsigned)audio::bufferedMs(), (unsigned)audio::underruns(),
             (unsigned)audio::overflowMs(), audio::playing() ? "true" : "false");
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
