#include "ota.h"

#include <HTTPClient.h>
#include <Update.h>
#include <WiFiClientSecure.h>

#include "audio.h"
#include "face.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#include "secrets.example.h"
#endif
#if RELAY_TLS && __has_include("ca_cert.h")
#include "ca_cert.h"
#define OTA_HAVE_CA 1
#else
#define OTA_HAVE_CA 0
#endif

namespace ota {

static bool fail(char* err, size_t n, const char* what) {
  snprintf(err, n, "%s", what);
  Update.abort();
  face::setUpdating(-1);
  Serial.printf("[ota] failed: %s\n", what);
  return false;
}

bool run(const char* url, size_t size, const char* md5, char* err, size_t errLen) {
  Serial.printf("[ota] updating from %s (%u bytes)\n", url, (unsigned)size);
  audio::flush();
  audio::musicFlush();
  face::setUpdating(0);
  face::update();

  WiFiClientSecure tls;
  WiFiClient plain;
  bool https = !strncmp(url, "https://", 8);
#if OTA_HAVE_CA
  tls.setCACert(RELAY_CA_PEM);
#else
  tls.setInsecure();  // the MD5 from the relay (over the pinned link, when there is one) still checks it
#endif
  HTTPClient http;
  http.setTimeout(15000);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!(https ? http.begin(tls, url) : http.begin(plain, url))) return fail(err, errLen, "bad url");
  int code = http.GET();
  if (code != 200) {
    char b[48];
    snprintf(b, sizeof(b), "download answered HTTP %d", code);
    http.end();
    return fail(err, errLen, b);
  }
  int len = http.getSize();
  if (len > 0 && (size_t)len != size) {
    http.end();
    return fail(err, errLen, "download size doesn't match");
  }
  if (!Update.begin(size, U_FLASH)) {
    http.end();
    return fail(err, errLen, Update.errorString());
  }
  if (md5 && strlen(md5) == 32) Update.setMD5(md5);

  static uint8_t buf[4096];
  WiFiClient* s = http.getStreamPtr();
  size_t got = 0;
  uint32_t last = millis();
  int shown = -1;
  while (got < size) {
    size_t avail = s->available();
    if (avail) {
      int n = s->readBytes(buf, min(avail, sizeof(buf)));
      if (n > 0) {
        if (Update.write(buf, n) != (size_t)n) {
          http.end();
          return fail(err, errLen, Update.errorString());
        }
        got += n;
        last = millis();
        int pct = (int)(got * 100 / size);
        if (pct != shown) {
          shown = pct;
          face::setUpdating(pct);
          face::update();
          if (pct % 10 == 0) Serial.printf("[ota] %d%%\n", pct);
        }
      }
    } else {
      if (!http.connected()) break;
      if (millis() - last > 15000) {
        http.end();
        return fail(err, errLen, "download stalled");
      }
      face::update();
      delay(2);
    }
  }
  http.end();
  if (got != size) return fail(err, errLen, "download ended early");
  if (!Update.end()) return fail(err, errLen, Update.errorString());  // MD5 mismatch lands here
  Serial.println("[ota] done, rebooting into the new firmware");
  face::setUpdating(100);
  face::update();
  delay(300);
  ESP.restart();
  return true;
}

}  // namespace ota
