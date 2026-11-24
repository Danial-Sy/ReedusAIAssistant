// the wake word on the S3: "Reedus" with microWakeWord. its own task listens to its own copy
// of the mic (audio::waitWakeFrame), makes a spectrogram slice every 10 ms (the TFLite Micro
// frontend, lib/microfrontend) and runs the streaming model every 3 slices. a detection is
// the average of the last 5 probabilities over the manifest's cutoff; relay.cpp picks it up.
#pragma once
#include <Arduino.h>

namespace wake {

bool begin();   // loads the model and starts the detector. false: no on-device wake word
bool ready();   // the model is loaded and running

bool take(float* score);  // true once per detection; score = the window's average probability

bool takeNearMiss(float* peak);  // true once per near miss (almost heard it); peak = its best average
float cutoff();
float defaultCutoff();  // this build's own: config.h WAKE_CUTOFF, else the model's manifest
void setCutoff(float c);  // 0..1, live (not saved: put it in config.h WAKE_CUTOFF)

// while he talks (and 1 s after) the name only has to clear this, since his echo blurs it.
// 0 = the normal cutoff. config.h BARGE_CUTOFF, or the app (wake_cfg barge_cutoff)
float bargeCutoff();
void setBargeCutoff(float c);
uint32_t barges();        // detections while he was talking, since boot

void setDebug(bool on);   // `wake`: live probabilities, once a second and on every near miss
void report();            // memory, flash, timing
const char* describe();   // one line for `info`

}  // namespace wake
