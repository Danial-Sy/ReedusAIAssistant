# Reedus Lite protocol (v1)

Reedus Lite talks to the run box over one WebSocket at `ws://<run box>:8767/lite`. The server code is `reedus_llm/reedus_companion/lite/` (constants in `protocol.py`). The firmware is `firmware/src/net.cpp`, and `tools/lite_sim.py` speaks the same protocol from a laptop.

## Finding the run box

If `LITE_HOST` is empty, the device listens on UDP **8769**. The run box broadcasts this every 2 s while no device is connected:

```json
{"reedus_lite_hub": true, "port": 8767, "path": "/lite", "proto": 1}
```

## Audio: binary frames

- Up (device to server): the mic, after the XVF3800's echo cancellation and beamforming. 16 kHz, mono, int16 little-endian, one 20 ms frame (320 samples, 640 bytes) per message. It streams all the time, because the wake word runs on the server.
- Down (server to device): speech, in the same format. The server paces it against the device's playhead and never runs more than 0.3 s ahead (`REEDUS_LITE_LEAD`). The device starts playing once 120 ms is buffered (`PLAY_PREBUFFER_MS`).

## Events: JSON text frames

Device to server:

| type | fields | when |
|---|---|---|
| `hello` | `proto, device, fw, token, rate, frame_ms` | first message. A bad token closes with 4001, a wrong rate with 4002 |
| `doa` | `angle` (world bearing), `rel` (relative to the face), `head`, `speech` | ~4/s while someone is talking |
| `battery` | `pct, volts` | every 60 s, or when it changes |
| `status` | `rssi, heap, psram, buffered_ms, underruns, mic_drops, uptime, head` | every 10 s |

Server to device:

| type | fields | effect |
|---|---|---|
| `welcome` | `proto, volume, brightness` | reply to `hello` |
| `state` | `state`: `idle` / `listening` / `thinking` / `speaking` / `sleep` | base face. The head holds still in listening, thinking and speaking |
| `wake` | none | the name was heard: the face perks up, and the head turns to where the voice came from |
| `emotion` | `emotion, strength, hold` | colours the face for `hold` seconds (set from the reply text) |
| `expr` | `name, hold` | one-shot expression (reacts to what you said, waves bye) |
| `flush` | none | barge-in: drop all queued speech now |
| `volume` | `level` 0-100 | speaker volume |
| `brightness` | `level` 0-100 | face brightness, under the firmware cap |
| `look` | `angle` | turn to a world bearing |

Emotions: `neutral happy laugh love sad surprised angry confused sleepy excited smug shy curious`. Extra one-shots: `wink bye dizzy yawn content blink`.

Bearings are in degrees. 0 is straight out of the face, and positive is the robot's left.
