"""the Reedus Lite wire protocol. ReedusLite/PROTOCOL.md is the human-readable copy.

one WebSocket per device. binary frames are 16 kHz mono int16 LE PCM both ways
(up = mic after the XVF3800's AEC/beamformer, down = speech). text frames are JSON events.
"""
from __future__ import annotations

import os

PROTO = 2          # 2: downstream binary frames start with a kind byte (KIND_*)
PATH = "/lite"
PORT = int(os.environ.get("REEDUS_LITE_PORT", "8767"))
BEACON_PORT = int(os.environ.get("REEDUS_LITE_BEACON_PORT", "8769"))

RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000          # 320 samples, 640 bytes
MUSIC_RATE = 24000                               # music lane, mono int16, proto 2 + caps music24
KIND_SPEECH = 1
KIND_MUSIC = 2

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
CLOCK = "clock"            # {t: local wall-clock seconds, h24}: the device keeps time from this
TIMERS = "timers"          # {items:[{id,label,left,total,paused}], ring:[{id,kind,label}], alarm}
MUSIC = "music"            # {op: flush}: drop queued music now (pause, skip)
GLANCE = "glance"          # {card: clock|weather|event|music|text|none, hold, dim?, ...card fields}

# device -> server
HELLO = "hello"            # {proto, device, fw, token, rate, frame_ms}
DOA = "doa"                # {angle, rel, head, speech}
BATTERY = "battery"        # {pct, volts}
STATUS = "status"          # {rssi, heap, buffered_ms, underruns, uptime}
# MUSIC also goes up: {buffered_ms, underruns, playing}, twice a second while music plays

STATES = ("idle", "listening", "thinking", "speaking", "sleep")

EMOTIONS = ("neutral", "happy", "laugh", "love", "sad", "surprised", "angry",
            "confused", "sleepy", "excited", "smug", "shy", "curious")
# one-shots the firmware knows on top of the emotions (face.cpp EMO_NAMES)
EXPRESSIONS = ("wink", "bye", "dizzy", "yawn", "content", "blink")
