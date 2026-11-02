#!/usr/bin/env python3
"""pretend to be Reedus Lite from a laptop: laptop mic up, run box speech down, face events printed.

proves the whole path (wake word -> STT -> gemini -> inworld -> speaker, plus the face
events) before the hardware exists. speaks exactly the firmware's protocol (PROTOCOL.md).

    pip install websockets sounddevice numpy
    python lite_sim.py --host 192.168.1.50            # live mic, say "hey reedus ..."
    python lite_sim.py                                # find the run box by its beacon
    python lite_sim.py --wav hey_reedus_weather.wav   # 16 kHz mono wav instead of the mic
    python lite_sim.py --doa 60                       # pretend the voice is 60 deg to its left
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
import time
import wave

import numpy as np

RATE = 16000
FRAME = 320                     # 20 ms, same as the firmware
PREBUFFER = int(RATE * 0.12)
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"

FACES = {
    "idle": "( o  o )", "listening": "( O  O )", "thinking": "( ^  ^ ) ...",
    "speaking": "( o  o ) ~", "sleep": "( -  - ) z",
    "happy": "( ^  ^ )", "laugh": "( >  < ) D", "love": "( <3 <3 )", "sad": "( ;  ; )",
    "surprised": "( O  O ) o", "angry": "( >  < ) #", "confused": "( o  O ) ?",
    "sleepy": "( -  - )", "excited": "( *  * )!", "smug": "( -  o ) ", "shy": "( ^  ^ ) //",
    "curious": "( o  O )", "neutral": "( o  o )", "wink": "( ^  o )", "bye": "( ^  - ) /",
}


def discover(timeout: float = 10.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", 8769))
    s.settimeout(timeout)
    print(f"{DIM}listening for the run box beacon on udp 8769…{RESET}")
    try:
        while True:
            data, addr = s.recvfrom(512)
            try:
                m = json.loads(data)
            except ValueError:
                continue
            if m.get("reedus_lite_hub"):
                return addr[0], int(m.get("port", 8767))
    except socket.timeout:
        return None
    finally:
        s.close()


class Speaker:
    """Jitter buffer + output stream, the same play-after-prebuffer rule as the firmware."""

    def __init__(self, device=None):
        import sounddevice as sd
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.playing = False
        self.underruns = 0
        self.level = 0.0
        self.stream = sd.OutputStream(samplerate=RATE, channels=1, dtype="int16",
                                      blocksize=FRAME, device=device, callback=self._cb)
        self.stream.start()

    def push(self, pcm: bytes) -> None:
        with self.lock:
            self.buf.extend(pcm)

    def flush(self) -> None:
        with self.lock:
            self.buf.clear()
            self.playing = False

    def buffered_ms(self) -> int:
        with self.lock:
            return len(self.buf) // 2 * 1000 // RATE

    def _cb(self, out, frames, _t, _status):
        need = frames * 2
        with self.lock:
            if not self.playing and len(self.buf) >= PREBUFFER * 2:
                self.playing = True
            if self.playing and len(self.buf) >= need:
                chunk = bytes(self.buf[:need])
                del self.buf[:need]
            elif self.playing and self.buf:
                chunk = bytes(self.buf) + b"\0" * (need - len(self.buf))
                self.buf.clear()
                self.playing = False
            else:
                if self.playing:
                    self.underruns += 1
                self.playing = False
                chunk = b"\0" * need
        x = np.frombuffer(chunk, dtype="<i2")
        out[:, 0] = x
        rms = float(np.sqrt(np.mean((x / 32768.0) ** 2)))
        self.level = max(rms, self.level * 0.8)


def mic_frames(args, q: "asyncio.Queue", loop):
    """Thread: laptop mic (or a wav) -> 20 ms int16 frames on the queue, in real time."""
    if args.wav:
        with wave.open(args.wav, "rb") as w:
            if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                sys.exit(f"{args.wav}: need 16 kHz mono 16-bit")
            pcm = w.readframes(w.getnframes())
        time.sleep(1.0)
        print(f"{DIM}sending {args.wav} ({len(pcm) / 2 / RATE:.1f}s){RESET}")
        t = time.monotonic()
        for i in range(0, len(pcm), FRAME * 2):
            loop.call_soon_threadsafe(q.put_nowait, pcm[i:i + FRAME * 2].ljust(FRAME * 2, b"\0"))
            t += FRAME / RATE
            time.sleep(max(0.0, t - time.monotonic()))
        print(f"{DIM}wav done, streaming silence{RESET}")
        args._wav_done = time.monotonic()
        silence = b"\0" * FRAME * 2
        while True:
            loop.call_soon_threadsafe(q.put_nowait, silence)
            t += FRAME / RATE
            time.sleep(max(0.0, t - time.monotonic()))
    import sounddevice as sd
    with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                           device=args.mic) as s:
        while True:
            data, _ = s.read(FRAME)
            loop.call_soon_threadsafe(q.put_nowait, bytes(data))


async def run(args) -> None:
    import websockets
    host, port = args.host, args.port
    if not host:
        found = discover()
        if not found:
            sys.exit("no beacon heard, pass --host")
        host, port = found
    url = f"ws://{host}:{port}/lite"
    spk = Speaker(args.speaker)
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    loop = asyncio.get_running_loop()
    args._wav_done = None
    threading.Thread(target=mic_frames, args=(args, q, loop), daemon=True).start()
    face = {"state": "idle", "emotion": "neutral", "expr": None, "until": 0.0}

    def show(note: str = "") -> None:
        now = time.monotonic()
        key = face["expr"] if face["expr"] and now < face["until"] else None
        if key is None:
            key = face["state"]
            if face["state"] == "speaking" and face["emotion"] != "neutral":
                key = face["emotion"]
        f = FACES.get(key, FACES["idle"])
        print(f"\r{BOLD}{f:<14}{RESET} {face['state']:<10} {DIM}{note}{RESET}", flush=True)

    while True:
        try:
            print(f"{DIM}connecting {url}…{RESET}")
            async with websockets.connect(url, max_size=2 ** 20, ping_interval=10) as ws:
                await ws.send(json.dumps({"type": "hello", "proto": 1, "device": "lite-sim",
                                          "fw": "sim", "token": args.token, "rate": RATE,
                                          "frame_ms": 20}))
                first_audio = [True]

                async def up():
                    last_doa = 0.0
                    while True:
                        frame = await q.get()
                        await ws.send(frame)
                        now = time.monotonic()
                        if args.doa is not None and now - last_doa > 0.25:
                            last_doa = now
                            await ws.send(json.dumps({"type": "doa", "angle": args.doa,
                                                      "rel": args.doa, "head": 0,
                                                      "speech": True}))

                async def status():
                    while True:
                        await asyncio.sleep(10)
                        await ws.send(json.dumps({"type": "status", "rssi": -50, "heap": 0,
                                                  "buffered_ms": spk.buffered_ms(),
                                                  "underruns": spk.underruns,
                                                  "uptime": int(time.monotonic())}))

                tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(status())]
                try:
                    async for m in ws:
                        if isinstance(m, bytes):
                            if first_audio[0]:
                                first_audio[0] = False
                                if args._wav_done:
                                    show(f"first audio {time.monotonic() - args._wav_done:.2f}s "
                                         f"after the wav ended")
                            spk.push(m)
                            continue
                        msg = json.loads(m)
                        t = msg.get("type")
                        if t == "state":
                            face["state"] = msg["state"]
                            if msg["state"] != "speaking":
                                first_audio[0] = True
                            show()
                        elif t == "emotion":
                            face["emotion"] = msg["emotion"]
                            show(f"emotion {msg['emotion']} {msg.get('strength', '')} "
                                 f"for {msg.get('hold', 0):.0f}s")
                        elif t == "expr":
                            face["expr"] = msg["name"]
                            face["until"] = time.monotonic() + float(msg.get("hold", 1.5))
                            show(f"expr {msg['name']}")
                        elif t == "flush":
                            spk.flush()
                            show("flush (barge-in)")
                        elif t == "wake":
                            show("wake! (head would turn to the voice)")
                        else:
                            show(f"{t}: {msg}")
                finally:
                    for task in tasks:
                        task.cancel()
        except (OSError, websockets.ConnectionClosed) as e:
            code = getattr(e, "code", None) or getattr(getattr(e, "rcvd", None), "code", None)
            if code == 4001:
                sys.exit("server rejected the token (--token / REEDUS_LITE_TOKEN)")
            print(f"{DIM}link down ({type(e).__name__}), retrying in 2s{RESET}")
            await asyncio.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("REEDUS_LITE_HOST"))
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--token", default=os.environ.get("REEDUS_LITE_TOKEN", ""))
    ap.add_argument("--wav", default=None, help="16 kHz mono wav to send instead of the mic")
    ap.add_argument("--doa", type=int, default=None, help="fake direction of arrival, degrees")
    ap.add_argument("--mic", default=None, help="sounddevice input device")
    ap.add_argument("--speaker", default=None, help="sounddevice output device")
    args = ap.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
