#include "store.h"

#include <Preferences.h>

namespace store {

static Preferences prefs, cal;
static bool ok = false, calOk = false;

void begin() {
  if (!ok) ok = prefs.begin("reedus", false);
  if (!calOk) calOk = cal.begin("cal", false);
}

uint32_t bulletinRev() { return ok ? prefs.getUInt("brev", 0) : 0; }

String bulletin() { return ok ? prefs.getString("btext", "") : String(); }

void saveBulletin(uint32_t rev, const char* text) {
  if (!ok) return;
  // NVS strings top out just under 4000 bytes; the relay caps the bulletin at 1500
  String t(text ? text : "");
  if (t.length() > 3800) t = t.substring(0, 3800);
  prefs.putString("btext", t);
  prefs.putUInt("brev", rev);
}

int getInt(const char* key, int def) { return calOk ? cal.getInt(key, def) : def; }

void putInt(const char* key, int value) {
  if (calOk) cal.putInt(key, value);
}

void clearCal() {
  if (calOk) cal.clear();
}

}  // namespace store
