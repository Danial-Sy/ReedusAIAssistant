#include "store.h"

#include <Preferences.h>

namespace store {

static Preferences prefs;
static bool ok = false;

void begin() { ok = prefs.begin("reedus", false); }

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

}  // namespace store
