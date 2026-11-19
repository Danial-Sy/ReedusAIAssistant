#!/usr/bin/env python3
"""pretend to be a standalone Reedus Lite: connect to a relay, say something, hear the answer.
for checking a Railway deploy (keys, wake word, voice, pool) before the S3 is flashed.

    python ReedusLiteS3/tools/sim.py wss://your-relay.up.railway.app rld_<device token> hello.wav
    python ReedusLiteS3/tools/sim.py ws://localhost:8080 rld_... question.wav --out reply.wav

the WAV must be 16 kHz mono 16-bit (`ffmpeg -i in.m4a -ar 16000 -ac 1 hello.wav`). say
"hey Reedus, …" in it, or run the relay with RELAY_WAKE=gate so any speech counts. the reply
is saved to --out (default reply.wav) and every event the relay sends is printed.
needs: pip install websockets
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave

FRAME = 640  # 20 ms at 16 kHz int16


async def run(url: str, token: str, wav_path: str, out_path: str, wait: float) -> None:
    import websockets
    with wave.open(wav_path, "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            sys.exit("the WAV must be 16 kHz, mono, 16-bit: ffmpeg -i in -ar 16000 -ac 1 out.wav")
        pcm = w.readframes(w.getnframes())
    url = url.rstrip("/") + ("" if url.rstrip("/").endswith("/lite") else "/lite")
    speech = bytearray()
    t0 = time.monotonic()
    done = asyncio.Event()

    def stamp() -> str:
        return f"{time.monotonic() - t0:6.2f}s"

    async with websockets.connect(url, max_size=2 ** 20) as ws:
        await ws.send(json.dumps({"type": "hello", "proto": 3, "device": "sim", "fw": "sim",
                                  "token": token, "rate": 16000, "frame_ms": 20,
                                  "caps": ["gate", "local_ring", "bulletin"], "bulletin_rev": 0}))
        first_audio = [0.0]

        async def recv():
            try:
                async for m in ws:
                    if isinstance(m, bytes):
                        if m[:1] == b"\x01":
                            if not first_audio[0]:
                                first_audio[0] = time.monotonic()
                                print(f"{stamp()}  ♪ his voice starts")
                            speech.extend(m[1:])
                        continue
                    msg = json.loads(m)
                    t = msg.get("type")
                    if t == "bulletin":
                        print(f"{stamp()}  bulletin rev {msg['rev']}:\n{msg['text']}")
                    elif t == "state":
                        print(f"{stamp()}  state {msg['state']}")
                        if msg["state"] == "idle" and speech:
                            done.set()
                    else:
                        print(f"{stamp()}  {json.dumps(msg)[:160]}")
            except websockets.ConnectionClosed as e:
                print(f"{stamp()}  closed {e.rcvd.code if e.rcvd else ''} {e.rcvd.reason if e.rcvd else ''}")
                done.set()

        rx = asyncio.ensure_future(recv())
        await asyncio.sleep(1.0)
        print(f"{stamp()}  → saying {wav_path} ({len(pcm) / 32000:.1f}s)")
        await ws.send(json.dumps({"type": "gate", "open": True}))
        sent_end = 0.0
        for i in range(0, len(pcm), FRAME):
            await ws.send(pcm[i:i + FRAME].ljust(FRAME, b"\0"))
            await asyncio.sleep(0.02)
        # then the room goes quiet, the way the S3 keeps streaming while listen is open
        sent_end = time.monotonic()
        silence = b"\0" * FRAME
        while not done.is_set() and time.monotonic() - sent_end < wait:
            await ws.send(silence)
            await asyncio.sleep(0.02)
        if first_audio[0]:
            print(f"\nreply latency (end of your audio → his voice): {first_audio[0] - sent_end:.2f}s")
        rx.cancel()
    if speech:
        with wave.open(out_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(bytes(speech))
        print(f"saved his reply ({len(speech) / 32000:.1f}s) to {out_path}")
    else:
        print("no reply audio came back")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="wss://your-relay.up.railway.app (or ws://localhost:8080)")
    ap.add_argument("token", help="the device token (rld_…)")
    ap.add_argument("wav", help="16 kHz mono 16-bit WAV of you talking")
    ap.add_argument("--out", default="reply.wav")
    ap.add_argument("--wait", type=float, default=20.0, help="seconds to wait for the reply")
    a = ap.parse_args()
    asyncio.run(run(a.url, a.token, a.wav, a.out, a.wait))


if __name__ == "__main__":
    main()
