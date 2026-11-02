// full-duplex I2S with the XVF3800: mic frames up, speech down through a jitter buffer.
// one task does both directions so they stay locked to the same clock.
#pragma once
#include <Arduino.h>

namespace audio {

bool begin();

// mic: the net loop drains 20 ms frames (FRAME_SAMPLES int16) from here
bool popMicFrame(int16_t* out);
uint32_t micDropped();

// speaker
void pushSpeech(const uint8_t* pcm, size_t bytes);  // int16 LE @ 16k, from the socket
void flush();                                       // barge-in: drop everything queued
void setVolume(int level);                          // 0-100
int volume();
uint32_t bufferedMs();
uint32_t underruns();
bool playing();

// 0..1 loudness of what's coming out right now, smoothed for the mouth
float level();

}  // namespace audio
