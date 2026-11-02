# Reedus Lite

Reedus Lite is Reedus's personality in a small desk body. The Telly chassis has red cat ears, a 16x16 LED face, a head that turns toward whoever is talking, and a far-field mic array. Lite is a thin client: it streams its mics to the run box and plays back what comes down. It shares one brain with the robot: the same wake word, Whisper STT, Gemini Flash, Inworld voice, memory and barge-in.

```
 Reedus Lite (XIAO ESP32S3 + XVF3800)               run box (Xeon)
 ┌──────────────────────────────────┐   wifi   ┌──────────────────────────────────────────┐
 │ 4 mics → XVF3800 AEC/beam → I2S ─┼─ 16k ───►│ lite/hub.py ─► netaudio (fake sounddevice)│
 │ speaker ◄─ XVF3800 ◄─ I2S ◄──────┼◄─ 16k ───┤                 │                         │
 │ DoA (I2C) → eyes → head servo    │          │ voice_loop: "hey Reedus" → whisper →      │
 │ 16x16 face ◄─ state/emotion ─────┼◄─ JSON ──┤   gemini → inworld (unchanged)            │
 └──────────────────────────────────┘          │ voice/events → lite/bridge.py → face      │
                                               └──────────────────────────────────────────┘
```

## What's where

| Path | What |
|---|---|
| `firmware/` | PlatformIO project for the XIAO: audio, face, head, DoA, battery, Wi-Fi link |
| `tools/lite_sim.py` | acts as the Lite from a laptop (mic up, speaker down, face events printed) |
| `WIRING.md` | **the pin map and wiring.** It matches `firmware/src/config.h` |
| `PROTOCOL.md` | the WebSocket protocol |
| `../reedus_llm/reedus_companion/lite/` | server side: hub, sounddevice shim, face bridge, emotion picker, launcher |
| `../reedus_llm/reedus_companion/voice/events.py` | state events the voice loop emits (no-op when nothing listens) |
| `../reedus_llm/tests/test_lite_link.py` | link tests: real WebSocket, fake device, no models needed |

## How it works

**Audio.** `lite/serve.py` starts the hub, then sets `sys.modules["sounddevice"]` to `lite/netaudio.py` before the voice loop starts. Every mic and speaker call the voice stack makes (wake listener, both barge-in watchers, jingles, `PlaybackStream`) lands on the Lite with no change to those files. Speech leaves the TTS at 24 kHz and is filtered down to 16 kHz properly (`lite/resample.py`), not linear-interpolated. Writes are paced to the device's playhead, so barge-in silences it within one 40 ms chunk.

**Face.** The voice loop emits `state`, `wake`, `heard`, `reply`, `speech_abort` and `session_end` events (`voice/events.py`). `lite/bridge.py` turns them into protocol messages. `lite/emotion.py` reads the reply text before the first sentence plays and picks an emotion: laugh, love, sad, surprised, excited, smug and so on. It also reacts to what you said ("you're cute" makes it shy, "shut up" makes it sad) while it's thinking. The firmware handles everything that makes the face look alive:

- Blinks, including the occasional double blink.
- Idle fidgets: glances, a little `:3`, looking up as if thinking.
- Drowsy eyelids after 5 minutes, asleep with Zzz after 15.
- A perk-up on the wake word.
- A thinking-dots animation.
- A mouth that follows the actual speech loudness.
- Eyes that follow whoever is talking.

**Head.** The XVF3800 reports direction of arrival over I2C (10 Hz). The eyes glance immediately. When the wake word fires, the head turns to the median bearing of the last 1.5 s of speech after a 0.2 s beat, clamped to ±90°, and the eyes cover the rest. It holds still while listening, thinking and speaking, because servo whine lands right in the mics. The mics turn with the head, so world bearing = DoA + head angle. Readings taken while the head is moving are ignored.

## Run it

### Run box

```bash
pip install -r reedus_companion/requirements/lite.txt     # websockets, on top of voice + cloud
# keys in deploy/xeon.secrets.env: GEMINI_API_KEY, INWORLD_API_KEY, REEDUS_LITE_TOKEN
bash reedus_companion/deploy/run_lite.sh                  # waits for the Lite, then boots the brain
```

This defaults to `--mode cloud --wake`. If the cloud preflight fails it falls back to local. Open TCP 8767 and UDP 8769 on the run box. To run it as a service, use `deploy/reedus-lite.service.example`. Run it **instead of** `reedus-voice`, since one brain is loaded at a time.

### Laptop test (no hardware)

```bash
pip install websockets sounddevice numpy
python ReedusLite/tools/lite_sim.py --host <run box ip> --token <token>
```

Say "hey Reedus, what's the weather". You'll hear the reply on the laptop and see the face events print. `--wav file.wav` sends a recording instead of the mic. `--doa 60` fakes a voice off to the left.

### Firmware

```bash
cd ReedusLite/firmware
cp src/secrets.example.h src/secrets.h      # wifi, run box host (or "" to auto-find), token
pio run -t upload && pio device monitor
```

Before the first upload, flash the XVF3800's I2S master firmware (WIRING.md, "Before the first power-up"). Then type `help` in the serial monitor. `test`, `servo`, `doa`, `expr` and `info` cover every calibration constant tagged MEASURE in `config.h`.

## Tested so far

- `reedus_llm/tests/test_lite_link.py`: **all pass.** It runs a real WebSocket and a fake device, and covers:
  - token rejection
  - mic reads (blocking and callback)
  - fan-out to two readers at once
  - the real `PlaybackStream` and `StreamSpeaker` through the shim
  - pacing, and barge-in flush within 0.01 s
  - face event de-duplication
  - DoA and battery status
  - offline silence pacing
- Resampler: 11 kHz is rejected rather than folded down, and chunked output equals one-shot.
- The existing voice tests (`test_barge_gates`, `test_turn_flow`, `test_wake_pipeline`, `test_weather_barge_wake`) give identical results before and after the voice-loop hooks.
- `lite_sim.py` against a live hub on this laptop: mic up, speech down, state, emotion and flush all work.
- Firmware **compiles clean** for `seeed_xiao_esp32s3` in both I2S modes (22% RAM, 28% flash).
- The on-chip 48k/16k filters, checked in a Python transliteration: speech passes at unity gain, aliasing is down 77 dB, images are down 88 dB.

**Not tested** (the parts arrive in about 2 weeks):
- Anything on the real XIAO and XVF3800.
- The full brain over the link on the Xeon (Whisper, the wake model and the API keys aren't on this laptop).

## Next chats

- ~~ReedOS app support for Lite.~~ Done: ReedOS has a Lite tab (live face, expressions, head dial, volume, brightness, status). It goes through `POST /api/lite` on the control API.
- Integrations (Spotify and others).
- v2: on-device microWakeWord, timers and alarms.
