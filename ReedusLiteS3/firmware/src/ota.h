// firmware updates from the app, over Wi-Fi: the relay says {"type":"ota", url, size, md5}, this
// downloads the image into the other app slot, checks its MD5 and reboots into it. a failed or
// interrupted download leaves the running firmware alone. (the first build with this in it
// still has to go on over USB.)
#pragma once
#include <Arduino.h>

namespace ota {

// blocking: returns only on failure (err filled in). on success it reboots
bool run(const char* url, size_t size, const char* md5, char* err, size_t errLen);

}  // namespace ota
