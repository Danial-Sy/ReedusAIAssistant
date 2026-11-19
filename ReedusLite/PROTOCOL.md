# Reedus Lite protocol (v2)

Reedus Lite talks to the run box over one WebSocket at `ws://<run box>:8767/lite`. The server code is `reedus_llm/reedus_companion/lite/` (constants in `protocol.py`). The firmware is `firmware/src/net.cpp`, and `tools/lite_sim.py` speaks the same protocol from a laptop.

## Finding the run box

If `LITE_HOST` is empty, the device listens on UDP **8769**. The run box broadcasts this every 2 s while no device is connected:

```json
{"reedus_lite_hub": true, "port": 8767, "path": "/lite", "proto": 1}
```

## Audio: binary frames

- Up (device to server): the mic, after the XVF3800's echo cancellation and beamforming. 16 kHz, mono, int16 little-endian, one 20 ms frame (320 samples, 640 bytes) per message. It streams all the time, because the wake word runs on the server.
- Down (server to device): speech, in the same format. The server paces it against the device's playhead and never runs more than 0.3 s ahead (`REEDUS_LITE_LEAD`). The device starts playing once 120 ms is buffered (`PLAY_PREBUFFER_MS`).

### Version 2: a music lane

A device that sends `hello` with `proto: 2` gets `welcome` with `proto: 2`. From then on, every downstream binary frame starts with one **kind byte**:

| kind | then | |
|---|---|---|
| `1` | 16 kHz mono int16 | speech, exactly as in v1 |
| `2` | 24 kHz mono int16 | music, in 40 ms frames. Only sent if `hello.caps` contains `music24` (the 48 kHz I2S build) |

Upstream mic frames are unchanged. A v1 device gets v1: raw speech and no music. The device mixes music under speech at 48 kHz and ducks it while speech plays, and while the state isn't `idle` or `sleep`. Details: `MUSIC.md`.

## Events: JSON text frames

Device to server:

| type | fields | when |
|---|---|---|
| `hello` | `proto, device, fw, token, rate, frame_ms, caps` | first message. A bad token closes with 4001, a wrong rate with 4002 |
| `doa` | `angle` (world bearing), `rel` (relative to the face), `head`, `speech` | ~4/s while someone is talking |
| `battery` | `pct, volts` | every 60 s, or when it changes |
| `status` | `rssi, heap, psram, buffered_ms, underruns, mic_drops, uptime, head` | every 10 s |
| `music` | `buffered_ms, underruns, playing` | twice a second while music is buffered or playing. The server paces music against this |

Server to device:

| type | fields | effect |
|---|---|---|
| `welcome` | `proto, volume, brightness` | reply to `hello`. `proto` is the version both sides will use |
| `music` | `op: "flush"` | drop all queued music now, with a 5 ms fade (pause, skip) |
| `state` | `state`: `idle` / `listening` / `thinking` / `speaking` / `sleep` | base face. The head holds still in listening, thinking and speaking |
| `wake` | none | the name was heard: the face perks up, and the head turns to where the voice came from |
| `emotion` | `emotion, strength, hold` | colours the face for `hold` seconds (set from the reply text) |
| `expr` | `name, hold` | one-shot expression (reacts to what you said, waves bye) |
| `flush` | none | barge-in: drop all queued speech now |
| `volume` | `level` 0-100 | speaker volume |
| `brightness` | `level` 0-100 | face brightness, under the firmware cap |
| `look` | `angle` | turn to a world bearing |
| `clock` | `t` (local wall-clock seconds), `h24` | sets the device's clock. Sent on connect and every 5 min |
| `timers` | `items: [{id, label, left, total, paused}]`, `ring: [{id, kind, label}]`, `alarm` | the running timers (seconds left, counted down on the device), anything ringing, and seconds until the next alarm within 24 h (`-1` = none). Sent on every change and once a minute |
| `glance` | `card`, `hold`, `dim?`, plus the card's fields | show an idle card for `hold` seconds (longer if a title needs to finish scrolling). `card: "none"` clears it |

Glance cards (server `lite/glance.py`, drawn by `firmware/src/glance.cpp`):

| card | fields |
|---|---|
| `clock` | none (uses the device clock) |
| `weather` | `temp, unit, icon`: `sun moon partly cloud rain snow storm fog` |
| `event` | `title, at` ("2:30"), `mins` until it starts (0 = now) |
| `music` | `title, artist` |
| `text` | `text` |

What owns the panel, highest first: a conversation (any state but `idle`), something ringing, an idle card, the soonest-ending timer, the face. Text is uppercase ASCII; the server folds anything else (`panel_text`).

Emotions: `neutral happy laugh love sad surprised angry confused sleepy excited smug shy curious`. Extra one-shots: `wink bye dizzy yawn content blink`.

Bearings are in degrees. 0 is straight out of the face, and positive is the robot's left.
