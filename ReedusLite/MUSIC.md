# Music on Reedus Lite

Lite plays Spotify out of its own speaker. On the run box, `librespot` acts as a Spotify Connect speaker called **Reedus Lite**. The audio goes down the same WebSocket as speech, on its own music lane. Lite mixes the two itself and turns the music down whenever Reedus talks or listens.

```
Spotify app / "play X"  ->  librespot (run box, Connect speaker "Reedus Lite")
                            44.1 kHz stereo on a pipe
                         -> lite/music.py: mono, filtered to 24 kHz, paced ~0.8 s ahead
                         -> WebSocket, music frames (kind 2)
                         -> firmware: 2 s music buffer -> 2x up to 48 kHz -> ducked -> mixed
                            with speech -> XVF3800 -> speaker
```

## Setup (once)

1. **Premium.** librespot, like every Spotify Connect speaker, needs Spotify Premium.
2. On the run box: `bash reedus_companion/deploy/setup_librespot.sh`. It installs Rust if it's missing, then builds librespot (a few minutes). If `run_lite.sh` can't find it, put `export REEDUS_LIBRESPOT=$HOME/.cargo/bin/librespot` in `xeon.env`.
3. Flash the new firmware (protocol v2). Old firmware keeps working, but voice only.
4. Start `run_lite.sh`. The log should say `Spotify Connect speaker "Reedus Lite" is up`, and the Lite connect line should end in `(music lane)`.
5. On your phone, on the same Wi-Fi as the run box: open Spotify, tap the devices button, pick **Reedus Lite**. librespot caches the login in `memory/librespot/` (gitignored), so this only happens once.

After that, saying "play Midnight City" to Lite plays on Lite. "Play it here" pulls music over from your phone, and "move it to my phone" sends it back. ReedOS → Connections → Spotify shows the speaker's state and has a **Play on Lite** button.

If the phone doesn't list Reedus Lite, the run box's firewall is probably blocking discovery (mDNS, UDP 5353, plus one TCP port). Either pin the port and open it with `REEDUS_LIBRESPOT_ARGS="--zeroconf-port 5354"`, or sign in another way. Newer librespot builds can do an OAuth login: run librespot by hand with its OAuth flag once, using the same `--cache` folder.

## How it behaves

- **Ducking:** music drops to about −13 dB (`MUSIC_DUCK` in `config.h`) the moment the wake word fires, stays down while Reedus listens, thinks and talks, and glides back up over about half a second afterwards. The XVF3800's echo cancellation already removes the music from the mics, and the ducking makes STT more reliable on top of that.
- **Pause is instant.** A spoken "pause", or the pause button in Spotify, flushes Lite's buffer with a 5 ms fade. Whatever librespot still has in its pipe is dropped until it reports playing again.
- **Wi-Fi hiccups:** Lite buffers 2 s of music and starts once 0.3 s is in. The run box stays about 0.8 s ahead. The device reports its buffer twice a second, so the two clocks can't drift apart over a long album. A gap restarts after a fresh 0.3 s rather than stuttering.
- **Lite unplugged mid-song:** the run box stops reading librespot's pipe, so the song waits instead of playing to nobody.
- **Volume:** the Spotify slider (and "turn it up") sets the music level. Lite's own volume knob in ReedOS scales everything.
- **The face:** while music plays and nobody's talking, Lite's eyes go happy and he bobs on the beat. He doesn't doze off mid-song. A new track shows its title as a card.
- **Quality:** 24 kHz mono, so content up to about 11 kHz. That's more than a small speaker reproduces, at 48 KB/s on Wi-Fi. Bass depends on the speaker and its box: a sealed chamber in the base is the biggest free improvement.

## Settings

| | |
|---|---|
| `REEDUS_LITE_MUSIC=0` | voice only, don't start librespot |
| `REEDUS_LIBRESPOT` | path to the librespot binary (else `PATH`) |
| `REEDUS_LIBRESPOT_ARGS` | extra librespot flags, e.g. `--zeroconf-port 5354 --bitrate 320` |
| `REEDUS_LITE_SPEAKER_NAME` | the name in Spotify's device list (default `Reedus Lite`) |
| `REEDUS_LITE_MUSIC_LEAD` | seconds the run box stays ahead (default 0.8) |
| `REEDUS_MUSIC_EVENT_PORT` | local UDP port librespot's events come back on (default 8770) |
| firmware `MUSIC_BUFFER_MS` / `MUSIC_PREBUFFER_MS` / `MUSIC_DUCK` | in `config.h` |

## Tested

- `reedus_llm/tests/test_music.py`, all pass. It uses a real WebSocket, a fake Lite and a fake librespot streaming a 1 kHz tone, and covers:
  - v2 framing (the speech/music kind byte), and that old proto-1 firmware still gets raw speech and no music
  - the music clock, and its correction from the device's reports
  - pacing to real time
  - the tone arriving at 1 kHz on the 24 kHz lane at the right level
  - pause by event, with the pipe drained and dropped
  - resume, and holding the song while Lite is unplugged
  - "play X" and "play it here" going to Lite
- The firmware's real `audio.cpp`, compiled on a laptop against I2S/FreeRTOS stubs:
  - music passes at unity gain, and images are 78 to 115 dB down
  - music ducks to exactly 0.22 under speech and comes back
  - a flush makes no click
  - playback waits for the prebuffer
  - underruns are counted only when the network was late
- `face.cpp`, `glance.cpp` and `audio.cpp` compile together on the host, and the bop runs.
- **Not tested:** a PlatformIO build for the ESP32, real librespot on the Xeon, and the real speaker.
