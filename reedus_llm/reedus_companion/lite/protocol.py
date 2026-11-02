"""the Reedus Lite wire protocol. ReedusLite/PROTOCOL.md is the human-readable copy.

one WebSocket per device. binary frames are 16 kHz mono int16 LE PCM both ways
(up = mic after the XVF3800's AEC/beamformer, down = speech). text frames are JSON events.
"""
from __future__ import annotations

import os

PROTO = 1
PATH = "/lite"
PORT = int(os.environ.get("REEDUS_LITE_PORT", "8767"))
BEACON_PORT = int(os.environ.get("REEDUS_LITE_BEACON_PORT", "8769"))

RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000          # 320 samples, 640 bytes

# server -> device
WELCOME = "welcome"        # {proto, volume, brightness}
STATE = "state"            # {state: idle|listening|thinking|speaking|sleep}
WAKE = "wake"              # wake word heard: perk up, turn to the voice
EMOTION = "emotion"        # {emotion, hold}: colours the face for `hold` seconds
EXPR = "expr"              # {name, hold}: one-shot expression (wink, laugh, ...)
FLUSH = "flush"            # barge-in: drop every queued speech sample now
VOLUME = "volume"          # {level: 0-100}
BRIGHTNESS = "brightness"  # {level: 0-100}, still capped by the firmware
LOOK = "look"              # {angle}: turn to a world bearing (deg, + = its left)

# device -> server
HELLO = "hello"            # {proto, device, fw, token, rate, frame_ms}
DOA = "doa"                # {angle, rel, head, speech}
BATTERY = "battery"        # {pct, volts}
STATUS = "status"          # {rssi, heap, buffered_ms, underruns, uptime}

STATES = ("idle", "listening", "thinking", "speaking", "sleep")

EMOTIONS = ("neutral", "happy", "laugh", "love", "sad", "surprised", "angry",
            "confused", "sleepy", "excited", "smug", "shy", "curious")
