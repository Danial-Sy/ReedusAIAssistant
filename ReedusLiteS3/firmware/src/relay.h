// wifi + the encrypted WebSocket to the Reedus relay in the cloud (ReedusLiteS3/PROTOCOL.md).
// replaces ReedusLite's net.cpp: no run box, no LAN discovery, and the mic only goes up after
// "Reedus" (wake.cpp; or, with a relay doing the wake word, while someone is talking: the
// speech gate) or while the relay has a conversation open.
#pragma once
#include <Arduino.h>

namespace relay {

void begin();
void update();  // call every loop: socket, gated mic upload, periodic status

bool wifiUp();
bool linked();
bool streaming();     // mic frames are going up right now
bool listenOpen();    // the relay asked for the mic (a conversation is going)
bool deviceWakeOn();  // the relay trusts the S3's wake word: the mic waits for "Reedus"
void sendJson(const char* json);
const char* serverDesc();
void setGateDebug(bool on);

bool paired();              // has a device token (from pairing, or LITE_TOKEN)
const char* pairingCode();  // the code the relay gave us while unpaired, "" otherwise
void unpair();              // forget the token: show a pairing code again
void setWifi(const char* ssid, const char* pass);  // saved in NVS, beats secrets.h

}  // namespace relay
