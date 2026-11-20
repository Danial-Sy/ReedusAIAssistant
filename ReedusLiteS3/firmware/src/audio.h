// full-duplex I2S with the XVF3800: mic frames up, speech and music down, each through its
// own jitter buffer, mixed here at 48 kHz. one task does both directions so they stay locked
// to the same clock.
#pragma once
#include <Arduino.h>

namespace audio {

bool begin();

// mic: the net loop drains 20 ms frames (FRAME_SAMPLES int16) from here
bool popMicFrame(int16_t* out);
uint32_t micDropped();

// the wake word detector's own feed (WAKE_CHANNEL), independent of the relay link
void startWakeFeed();  // frames are only copied after this
bool waitWakeFrame(int16_t* out, uint32_t timeoutMs);
uint32_t wakeDropped();

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

// music: 24 kHz mono int16, its own deeper buffer. it ducks under speech, and while
// setDuck(true) (someone is talking to Reedus). only in the 48 kHz I2S mode
bool musicSupported();
void pushMusic(const uint8_t* pcm, size_t bytes);
void musicFlush();             // pause/skip: stop now (with a 5 ms fade)
uint32_t musicBufferedMs();
uint32_t musicUnderruns();
bool musicPlaying();           // coming out of the speaker right now
float musicLevel();            // 0..1, smoothed, for the face to bop along
void setDuck(bool on);

}  // namespace audio
