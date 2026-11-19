#!/usr/bin/env python3
"""music on Lite's speaker: protocol v2 framing (speech/music kind byte, old firmware still
fine), the music lane's pacing and its correction from the device's buffer reports, a fake
librespot streamed end to end (filtered to 24 kHz, paced, paused, held while Lite is away),
and Spotify sending "play X" to Lite. a real WebSocket, no Spotify account, no network."""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = Path(tempfile.mkdtemp())
os.environ["REEDUS_CONNECTIONS_FILE"] = str(TMP / "connections.json")

import websockets                                                   # noqa: E402
from reedus_companion.lite import hub as hub_mod                    # noqa: E402
from reedus_companion.lite import protocol as P                     # noqa: E402
from reedus_companion.lite.hub import LiteHub                       # noqa: E402

_fails = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


PORT = 18777
hub = LiteHub(host="127.0.0.1", port=PORT, token="", beacon=False).start()


class FakeLite:
    def __init__(self, proto=2, caps=("music24",)):
        self.proto, self.caps = proto, list(caps)
        self.texts, self.speech, self.music, self.raw = [], bytearray(), bytearray(), []
        self.music_at = []                     # (time, total bytes) as music arrives
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.ws = None
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)
        wait_for(lambda: any(t.get("type") == "welcome" for t in self.texts))

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        try:
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/lite") as ws:
                self.ws = ws
                await ws.send(json.dumps({"type": "hello", "proto": self.proto, "caps": self.caps,
                                          "device": "fake", "fw": "t", "token": "", "rate": 16000}))
                self.ready.set()
                async for m in ws:
                    if isinstance(m, bytes):
                        self.raw.append(m)
                        if self.proto >= 2 and self.welcome_proto() >= 2:
                            (self.speech if m[0] == P.KIND_SPEECH else self.music).extend(m[1:])
                            if m[0] == P.KIND_MUSIC:
                                self.music_at.append((time.time(), len(self.music)))
                        else:
                            self.speech.extend(m)
                    else:
                        self.texts.append(json.loads(m))
        except Exception:                                        # noqa: BLE001
            pass
        finally:
            self.ready.set()

    def welcome_proto(self):
        return next((t.get("proto", 1) for t in self.texts if t.get("type") == "welcome"), 1)

    def send(self, msg):
        asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(msg)), self.loop).result(2)

    def close(self):
        asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)   # the hub notices at once


print("[1] protocol v2 framing")
dev = FakeLite()
check(wait_for(lambda: hub.connected), "device connected")
check(dev.welcome_proto() == 2 and hub.music_ok, "a proto-2 device with music24 gets the music lane")
hub.queue_speech(np.zeros(1600, np.float32))
check(wait_for(lambda: len(dev.speech) >= 3200), "speech arrives under kind 1")
check(all(m[0] in (P.KIND_SPEECH, P.KIND_MUSIC) for m in dev.raw), "every downstream frame has a kind byte")
tone = np.sin(2 * np.pi * 1000 * np.arange(2400) / 24000).astype(np.float32) * 0.5
check(hub.queue_music(tone), "queue_music accepts 24 kHz audio")
check(wait_for(lambda: len(dev.music) >= 4800), "music arrives under kind 2")
check(abs(hub.music_ahead() - (0.1 + hub_mod.MUSIC_PREBUFFER_S)) < 0.05, "the music clock counts prebuffer + audio")
before = hub.mgen
hub.music_flush()
check(hub.mgen == before + 1 and hub.music_ahead() == 0.0, "flush bumps the generation and empties the clock")
check(wait_for(lambda: any(t.get("type") == "music" and t.get("op") == "flush" for t in dev.texts)),
      "the device is told to flush")
hub.queue_music(np.zeros(24000, np.float32))
dev.send({"type": "music", "buffered_ms": 200, "underruns": 0, "playing": True})
check(wait_for(lambda: hub.music_ahead() < 0.95), "a device report pulls the estimate toward what it holds")
check(hub.snapshot().get("music", {}).get("lane") is True, "snapshot shows the music lane")
dev.close()
check(wait_for(lambda: not hub.connected), "device gone")
check(not hub.music_ok and not hub.queue_music(tone), "no lane, nothing queued, while Lite is away")

old = FakeLite(proto=1, caps=())
check(wait_for(lambda: hub.connected) and old.welcome_proto() == 1, "old firmware gets proto 1")
hub.queue_speech(np.zeros(1600, np.float32))
check(wait_for(lambda: len(old.speech) >= 3200) and all(len(m) % 2 == 0 for m in old.raw),
      "old firmware still gets raw speech, no kind byte")
check(not hub.music_ok and not hub.queue_music(tone), "and no music")
old.close()
wait_for(lambda: not hub.connected)

nomusic = FakeLite(proto=2, caps=())
check(wait_for(lambda: hub.connected) and not hub.music_ok, "proto 2 without music24 (16 kHz I2S mode): no music")
nomusic.close()
wait_for(lambda: not hub.connected)

print("[2] a fake librespot, end to end")
FAKE = TMP / "fake_librespot.py"
FAKE.write_text(
    "import sys, numpy as np\n"
    "t = np.arange(4410) / 44100.0\n"
    "n = 0\n"
    "out = sys.stdout.buffer\n"
    "while True:\n"
    "    s = (0.5 * np.sin(2 * np.pi * 1000 * (t + n * 0.1)) * 32767).astype('<i2')\n"
    "    out.write(np.repeat(s, 2).tobytes())   # stereo\n"
    "    out.flush()\n"
    "    n += 1\n", encoding="utf-8")
from reedus_companion.lite import music as music_mod                 # noqa: E402
svc = music_mod.MusicService(hub, binary=sys.executable, event_port=18778, cache=TMP / "ls")
svc.command = lambda: [sys.executable, str(FAKE)]
svc.start()
time.sleep(0.6)
check(svc.sent_s == 0.0, "nothing is read while no Lite is connected (librespot waits on the pipe)")
dev = FakeLite()
wait_for(lambda: hub.connected)
t0 = time.time()
check(wait_for(lambda: len(dev.music) > 24000 * 2, 4), "music flows once Lite connects")
time.sleep(2.0)
got_s = len(dev.music) / 2 / 24000
wall = time.time() - t0
check(got_s <= wall + hub_mod.MUSIC_LEAD_S + hub_mod.MUSIC_PREBUFFER_S + 0.15,
      f"paced to real time: {got_s:.2f} s of audio in {wall:.2f} s (lead {hub_mod.MUSIC_LEAD_S})")
check(got_s >= wall - 0.3, f"and not starved: {got_s:.2f} s")
x = np.frombuffer(bytes(dev.music[-48000:]), dtype="<i2").astype(np.float32) / 32768
spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
freqs = np.fft.rfftfreq(x.size, 1 / 24000)
check(abs(freqs[spec.argmax()] - 1000) < 5, f"a 1 kHz tone comes out at 1 kHz on the 24 kHz lane ({freqs[spec.argmax()]:.0f})")
check(0.3 < np.sqrt(np.mean(x ** 2)) * np.sqrt(2) < 0.6, "level kept (0.5 in, ~0.5 out)")
check(svc.state == "playing" and hub.snapshot()["music"]["state"] == "playing", "state is playing")

import socket                                                       # noqa: E402
socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"paused abc", ("127.0.0.1", 18778))
check(wait_for(lambda: svc.state == "paused"), "librespot's pause event arrives over UDP")
check(wait_for(lambda: sum(1 for t in dev.texts if t.get("op") == "flush") >= 1),
      "pause flushes Lite at once (no 0.8 s tail)")
time.sleep(0.3)
n_paused = len(dev.music)
time.sleep(0.8)
check(len(dev.music) == n_paused, "and what's left in librespot's pipe is dropped, not played")
socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"playing abc", ("127.0.0.1", 18778))
check(wait_for(lambda: len(dev.music) > n_paused + 9600) and svc.state == "playing", "'playing' resumes it")
cmd = music_mod.MusicService(hub, binary="librespot", cache=TMP / "ls2").command()
script = (TMP / "ls2" / ("onevent.cmd" if os.name == "nt" else "onevent.sh"))
check(script.exists() and "PLAYER_EVENT" in script.read_text(), "the --onevent forwarder is written")
check(cmd[:3] == ["librespot", "--name", "Reedus Lite"] and "--backend" in cmd and "pipe" in cmd
      and "--onevent" in cmd, "real command line: named speaker, pipe backend, events")

dev.close()
wait_for(lambda: not hub.connected)
time.sleep(0.5)
held = svc.sent_s
time.sleep(1.0)
check(svc.sent_s == held, "Lite unplugged mid-song: reading stops, the song waits")
svc.stop()
missing = music_mod.MusicService(hub, binary="", cache=TMP / "ls3").start()
check(missing.state == "off" and "isn't installed" in missing.error and not missing.available,
      "no librespot: voice-only, with a reason ReedOS can show")

print("[3] Spotify plays on Lite")
from reedus_companion.connect.spotify import Spotify, SpotifyError    # noqa: E402
calls = []


class FakeSpotify(Spotify):
    def __init__(self, devices):
        super().__init__({"refresh_token": "x"}, lambda: None)
        self._devs = devices

    def api(self, method, path, query=None, body=None):
        calls.append((method, path, dict(query or {}), body))
        if path == "/me/player/devices":
            return {"devices": self._devs}
        if path == "/search":
            return {"tracks": {"items": [{"name": "Midnight City", "uri": "spotify:track:1",
                                          "artists": [{"name": "M83"}]}]}}
        return {}


sp = FakeSpotify([{"id": "phone", "name": "Danial's iPhone", "is_active": True},
                  {"id": "lite", "name": "Reedus Lite", "is_active": False}])
sp.play("midnight city", "track")
play = [c for c in calls if c[1] == "/me/player/play"][-1]
check(play[2].get("device_id") is None, "no home device: plays wherever Spotify already is")
sp.home_device = "Reedus Lite"
calls.clear()
sp.play("midnight city", "track")
play = [c for c in calls if c[1] == "/me/player/play"][-1]
check(play[2].get("device_id") == "lite", "with Lite as home: 'play X' plays on Lite, even if the phone was active")
calls.clear()
check(sp.here() == "Reedus Lite" and any(c[1] == "/me/player" and c[3]["device_ids"] == ["lite"] for c in calls),
      "'play it here' moves playback to Lite")
gone = FakeSpotify([{"id": "phone", "name": "Danial's iPhone", "is_active": True}])
gone.home_device = "Reedus Lite"
try:
    gone.here()
    check(False, "here() with Lite not signed in should explain")
except SpotifyError as e:
    check("pick Reedus Lite once" in str(e), "Lite not signed in yet: says how to fix it")
heard = []
sp.on_control.append(heard.append)
sp.control("pause")
check(heard == ["pause"], "a spoken pause tells the speaker (so it can go quiet at once)")
from reedus_companion.connect import intents                        # noqa: E402
check(intents.parse("play it here") == ("here", {}), "'play it here' parses")
check(intents.parse("move the music to the kitchen speaker")[0] == "transfer", "other devices still transfer")
check(intents.run(sp, "here", {}) in ("Playing it here.", "Moved it over to me.", "Okay, it's coming out of me now."),
      "and it answers")

print()
hub.stop()
if _fails:
    print(f"{len(_fails)} FAILED")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("all passed")
