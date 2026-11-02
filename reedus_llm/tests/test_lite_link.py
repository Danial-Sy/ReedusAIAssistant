#!/usr/bin/env python3
"""Reedus Lite link: resampler, emotion picker, and hub + sounddevice shim against a fake
device over a real localhost WebSocket. no models, no sound card."""
from __future__ import annotations
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

_fails = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


def tone(freq, sr, secs, amp=0.5):
    t = np.arange(int(sr * secs)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def peak_hz(x, sr):
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    return np.argmax(spec) * sr / x.size


# [1] resampler
print("[1] resampler")
from reedus_companion.lite.resample import Resampler, from_int16, to_int16

src = tone(1000, 24000, 1.0)
one = Resampler(24000, 16000).feed(src)
rs = Resampler(24000, 16000)
chunked = np.concatenate([rs.feed(src[i:i + 777]) for i in range(0, src.size, 777)])
check(abs(one.size - 16000) <= 1, f"24k->16k keeps duration ({one.size} samples)")
check(one.size == chunked.size and np.allclose(one, chunked, atol=1e-5),
      "chunked == one-shot (no seams between sentences)")
check(abs(peak_hz(one[2000:], 16000) - 1000) < 5, "1 kHz stays 1 kHz")
check(abs(np.sqrt(np.mean(one[2000:] ** 2)) - 0.5 / np.sqrt(2)) < 0.02, "passband gain ~1")
alias = Resampler(24000, 16000).feed(tone(11000, 24000, 1.0))
check(np.sqrt(np.mean(alias[2000:] ** 2)) < 0.01, "11 kHz is filtered, not folded to 5 kHz")
up = Resampler(16000, 24000).feed(tone(500, 16000, 0.5))
check(abs(up.size - 12000) <= 1 and abs(peak_hz(up[1000:], 24000) - 500) < 5, "16k->24k works too")
rt = from_int16(to_int16(np.array([0.0, 0.5, -0.5, 1.2], dtype=np.float32)))
check(np.allclose(rt, [0, 0.5, -0.5, 0.99997], atol=1e-3), "int16 round trip clips")

# [2] emotion
print("[2] emotion")
from reedus_companion.lite import emotion

cases = [
    ("Haha, that's hilarious. You got me.", "laugh"),
    ("Oh no, I'm really sorry. That sounds rough.", "sad"),
    ("Whoa, no way! That actually happened?", "surprised"),
    ("That's awesome! Let's go!", "excited"),
    ("Glad you liked it.", "happy"),
    ("Hmm, I'm not sure about that one.", "confused"),
    ("Goodnight. Sweet dreams.", "sleepy"),
    ("Obviously. I told you.", "smug"),
    ("The capital of France is Paris.", "neutral"),
]
for text, want in cases:
    got, s = emotion.classify(text)
    check(got == want, f"{text!r} -> {got} ({s})")
check(emotion.react("you're so cute") == "shy", "compliment -> shy")
check(emotion.react("I love you Reedus") == "love", "i love you -> love")
check(emotion.react("shut up") == "sad", "shut up -> sad")
check(emotion.react("what's the weather") is None, "plain question -> no reaction")

# [3] hub + shim with a fake device
print("[3] hub + shim + fake device")
import websockets
from reedus_companion.lite import hub as hub_mod
from reedus_companion.lite import netaudio
from reedus_companion.lite.hub import LiteHub

PORT = 18767
hub = LiteHub(host="127.0.0.1", port=PORT, token="sekrit", beacon=False).start()
netaudio.install(hub)
import sounddevice as sd                                        # noqa: E402  (the shim)
check(sd is netaudio, "import sounddevice -> the shim")


class FakeLite:
    """The firmware's side of the protocol, enough to test against."""

    def __init__(self, token="sekrit"):
        self.token = token
        self.texts, self.audio = [], bytearray()
        self.closed_code = None
        self.loop = asyncio.new_event_loop()
        self.ws = None
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        try:
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/lite") as ws:
                self.ws = ws
                await ws.send(json.dumps({"type": "hello", "proto": 1, "device": "fake-lite",
                                          "fw": "test", "token": self.token, "rate": 16000,
                                          "frame_ms": 20}))
                self.ready.set()
                async for m in ws:
                    if isinstance(m, bytes):
                        self.audio.extend(m)
                    else:
                        self.texts.append(json.loads(m))
        except websockets.ConnectionClosed as e:
            self.closed_code = getattr(e, "code", None) or getattr(getattr(e, "rcvd", None), "code", None)
        except Exception as e:                                   # noqa: BLE001
            self.closed_code = repr(e)
        finally:
            self.ready.set()

    def send_mic(self, x, realtime=True):
        """Stream float32 16k as 20 ms frames, like the firmware does."""
        async def go():
            for i in range(0, x.size, 320):
                await self.ws.send(to_int16(x[i:i + 320]))
                if realtime:
                    await asyncio.sleep(0.02)
        return asyncio.run_coroutine_threadsafe(go(), self.loop)

    def send_text(self, msg):
        return asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(msg)), self.loop)

    def types(self):
        return [t.get("type") for t in self.texts]


bad = FakeLite(token="wrong")
time.sleep(0.5)
check(bad.closed_code == 4001 and not hub.connected, f"bad token rejected ({bad.closed_code})")

dev = FakeLite()
check(hub.wait_connected(3), "device connects with the right token")
time.sleep(0.2)
check("welcome" in dev.types(), "device gets welcome")

# mic: the blocking read path the wake listener uses
mic = tone(440, 16000, 1.0, amp=0.3)
with sd.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=1280) as stream:
    sending = dev.send_mic(mic)
    got = []
    for _ in range(int(16000 * 0.9 / 1280)):
        block, _of = stream.read(1280)
        got.append(block[:, 0])
heard = np.concatenate(got)
sending.result(10)          # let the tail land before the next readers subscribe
check(heard.shape[0] > 10000 and abs(peak_hz(heard, 16000) - 440) < 10,
      "InputStream.read hears the device's mic")

# mic: callback path (_MicTap in barge.py) and fan-out to two readers at once
blocks = []
cb_stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=800,
                           callback=lambda indata, f, t, status: blocks.append(indata[:, 0].copy()))
cb_stream.start()
with sd.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=800) as s2:
    dev.send_mic(tone(880, 16000, 0.6, amp=0.3)).result(5)
    b2, _ = s2.read(800 * 8)
time.sleep(0.2)
cb_stream.stop()
cb_stream.close()
check(len(blocks) >= 8 and abs(peak_hz(np.concatenate(blocks), 16000) - 880) < 15,
      f"callback InputStream gets blocks ({len(blocks)})")
check(abs(peak_hz(b2[:, 0], 16000) - 880) < 15, "two mic readers both hear it")

# speaker: PlaybackStream (the real one from voice/tts.py) through the shim
from reedus_companion.voice.tts import PlaybackStream, playback_rate

check(playback_rate(None) == 24000, "the shim reports a 24 kHz output")
dev.audio.clear()
speech = tone(300, 24000, 1.0, amp=0.4)
t0 = time.monotonic()
pb = PlaybackStream(24000)
pb.write(speech)
t_write = time.monotonic() - t0
pb.close()
t_close = time.monotonic() - t0
time.sleep(0.3)
got = from_int16(bytes(dev.audio))
check(abs(got.size - 16000 * 1.1) < 1600, f"device receives ~1.1 s at 16k ({got.size} samples)")
check(abs(peak_hz(got[3000:], 16000) - 300) < 6, "and it's the right tone")
check(t_write > 0.5, f"write is paced to the device ({t_write:.2f}s for 1 s of audio)")
check(t_close >= 1.0, f"close waits for the device to finish ({t_close:.2f}s)")

# barge-in: speech_abort from StreamSpeaker.stop -> flush, and the shim stops writing
from reedus_companion.lite.bridge import FaceBridge
from reedus_companion.voice import events

events.subscribe(FaceBridge(hub))
dev.audio.clear()
dev.texts.clear()


class FakeTTS:
    sample_rate = 24000

    def synth(self, text):
        return tone(220, 24000, 1.5, amp=0.3)


from reedus_companion.voice.stream_speaker import StreamSpeaker

spk = StreamSpeaker(FakeTTS())
spk.reset()
done = threading.Event()
threading.Thread(target=lambda: (spk.finish("This is a long reply that keeps going. And going."),
                                 done.set()), daemon=True).start()
time.sleep(0.8)
t_stop = time.monotonic()
spk.stop()
stop_s = time.monotonic() - t_stop
done.wait(3)
time.sleep(0.2)
check("flush" in dev.types(), "barge-in sends flush")
check(stop_s < 0.6, f"stop() returns fast ({stop_s:.2f}s)")
got_s = len(dev.audio) / 2 / 16000
check(got_s < 1.6, f"audio stopped flowing at the barge ({got_s:.2f}s sent of 3 s)")

# face events
dev.texts.clear()
events.emit("state", state="thinking")
events.emit("state", state="thinking")
events.emit("wake")
events.emit("wake")
events.emit("reply", text="Haha, nice one!")
events.emit("heard", text="you're so cute")
time.sleep(0.3)
ty = dev.types()
check(ty.count("state") == 1, "repeated state is sent once")
check(ty.count("wake") == 1, "wake de-duplicated")
emo = [t for t in dev.texts if t["type"] == "emotion"]
check(emo and emo[0]["emotion"] == "laugh" and emo[0]["hold"] > 2, f"reply -> emotion {emo}")
check(any(t["type"] == "expr" and t["name"] == "shy" for t in dev.texts), "user compliment -> shy")

# device -> server status
dev.send_text({"type": "doa", "angle": 42, "rel": 12, "head": 30, "speech": True}).result(2)
dev.send_text({"type": "battery", "pct": 81, "volts": 3.98}).result(2)
time.sleep(0.2)
snap = hub.snapshot()
check(snap["doa"]["angle"] == 42 and snap["battery"]["pct"] == 81, "doa + battery land in snapshot")
check(snap["face"] and snap["face"]["name"] == "shy", f"snapshot carries the live face {snap['face']}")

# ReedOS knobs through the control API hook
from reedus_companion.serve.control_api import Hooks, VoiceLoopHooks
n0 = len(dev.texts)
out = VoiceLoopHooks({"lite": hub}).lite({"volume": 140, "brightness": 40, "look": 200,
                                          "expr": "wink"})
time.sleep(0.3)
sent = {t["type"]: t for t in dev.texts[n0:]}
check(out["applied"] == {"volume": 100, "brightness": 40, "look": 90.0, "expr": "wink"},
      f"lite knobs clamp ({out['applied']})")
check(sent.get("volume", {}).get("level") == 100 and sent.get("look", {}).get("angle") == 90.0
      and sent.get("expr", {}).get("name") == "wink", "knobs reach the device")
check(out["lite"]["volume"] == 100 and out["lite"]["face"]["name"] == "wink",
      "reply carries the new snapshot")
check("error" in hub.control({"expr": "moonwalk"}), "unknown expression refused")
check("error" in VoiceLoopHooks({}).lite({"volume": 5}) and "error" in Hooks().lite({}),
      "no Lite on the brain is an error, not a crash")

# offline: reads keep pace with silence instead of hanging the loop
asyncio.run_coroutine_threadsafe(dev.ws.close(), dev.loop)
for _ in range(30):
    if not hub.connected:
        break
    time.sleep(0.1)
check(not hub.connected, "disconnect noticed")
t0 = time.monotonic()
with sd.InputStream(samplerate=16000, channels=1, dtype="float32") as s3:
    x, _ = s3.read(1600)
dt = time.monotonic() - t0
check(0.08 < dt < 0.6 and not np.any(x), f"offline read returns silence in real time ({dt:.2f}s)")

hub.stop()
print()
if _fails:
    print(f"{len(_fails)} FAILED")
    for f in _fails:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
