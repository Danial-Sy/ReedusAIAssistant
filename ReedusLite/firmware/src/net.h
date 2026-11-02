// wifi + the WebSocket to the run box (protocol: ReedusLite/PROTOCOL.md).
#pragma once
#include <Arduino.h>

namespace net {

void begin();
void update();  // call every loop: socket, mic upload, periodic status

bool wifiUp();
bool linked();
void sendJson(const char* json);
const char* serverDesc();

}  // namespace net
