"""the server end of the Reedus Lite link: one WebSocket, mic in, speech + face events out.

runs its own asyncio loop on a daemon thread so the voice loop (plain threads, blocking
sounddevice-style reads) never has to know about asyncio.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import socket
import threading
import time
from collections import deque
from typing import Callable, Deque, List, Optional

import numpy as np

from reedus_companion.lite import protocol as P
from reedus_companion.lite.resample import from_int16, to_int16

DIM, RESET = "\033[2m", "\033[0m"

# device-side jitter buffer: it starts playing once this much is queued, and we never run
# more than LEAD_S ahead of its playhead. wifi jitter on a home AP is ~20-80 ms, the lead
# is also the worst-case barge-in tail if a flush ever gets lost.
PREBUFFER_S = float(os.environ.get("REEDUS_LITE_PREBUFFER", "0.12"))
LEAD_S = float(os.environ.get("REEDUS_LITE_LEAD", "0.30"))


def _log(msg: str) -> None:
    print(f"{DIM}  [lite] {msg}{RESET}", flush=True)


class MicSubscriber:
    """One reader's view of the mic stream. Each open InputStream gets its own."""

    def __init__(self, max_sec: float = 30.0):
        self._q: Deque[np.ndarray] = deque()
        self._have = 0
        self._max = int(P.RATE * max_sec)
        self._cv = threading.Condition()
        self.overflowed = False

    def push(self, x: np.ndarray) -> None:
        with self._cv:
            self._q.append(x)
            self._have += x.size
            while self._have > self._max and self._q:
                self._have -= self._q.popleft().size
                self.overflowed = True
            self._cv.notify_all()

    def read(self, n: int, timeout: float) -> tuple[np.ndarray, bool]:
        """Exactly n samples. Pads with silence if the device goes quiet for `timeout`."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._have < n:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
            out = np.zeros(n, dtype=np.float32)
            got = 0
            while got < n and self._q:
                head = self._q[0]
                take = min(n - got, head.size)
                out[got:got + take] = head[:take]
                got += take
                if take == head.size:
                    self._q.popleft()
                else:
                    self._q[0] = head[take:]
                self._have -= take
            of, self.overflowed = self.overflowed, False
            return out, of


class LiteHub:
    def __init__(self, host: str = "0.0.0.0", port: int = P.PORT,
                 token: Optional[str] = None, beacon: bool = True):
        self.host, self.port = host, int(port)
        self.token = (token if token is not None
                      else os.environ.get("REEDUS_LITE_TOKEN", "")).strip()
        self.beacon = beacon
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._outbox: Optional[asyncio.Queue] = None
        self._subs: List[MicSubscriber] = []
        self._subs_lock = threading.Lock()
        self._listeners: List[Callable[[dict], None]] = []
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._clock_lock = threading.Lock()
        self._play_end = 0.0                # monotonic time the device runs dry
        self.gen = 0                        # bumped by flush(); stale speech is dropped
        self.volume = int(os.environ.get("REEDUS_LITE_VOLUME", "70"))
        self.brightness = int(os.environ.get("REEDUS_LITE_BRIGHTNESS", "100"))
        self.last_state: Optional[str] = None
        self.status: dict = {"connected": False}
        self.error: Optional[str] = None

    # --- lifecycle ---
    def start(self, timeout: float = 5.0) -> "LiteHub":
        threading.Thread(target=self._run, name="lite-hub", daemon=True).start()
        self._ready.wait(timeout)
        if self.error:
            raise RuntimeError(self.error)
        if self.beacon:
            threading.Thread(target=self._beacon_loop, name="lite-beacon", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def wait_connected(self, timeout: Optional[float] = None) -> bool:
        return self._connected.wait(timeout)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def on_message(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    # --- mic side ---
    def subscribe(self, max_sec: float = 30.0) -> MicSubscriber:
        sub = MicSubscriber(max_sec)
        with self._subs_lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: MicSubscriber) -> None:
        with self._subs_lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def _fan_out(self, pcm: bytes) -> None:
        if len(pcm) < 2:
            return
        x = from_int16(pcm[: len(pcm) & ~1])
        with self._subs_lock:
            subs = list(self._subs)
        for s in subs:
            s.push(x)

    # --- speaker side ---
    def ahead(self) -> float:
        """Seconds of speech the device still has queued (by our clock)."""
        with self._clock_lock:
            return max(0.0, self._play_end - time.monotonic())

    def queue_speech(self, pcm16k: np.ndarray, gen: Optional[int] = None) -> None:
        """Send 16 kHz float32 speech. Advances the playback clock even when offline."""
        if gen is not None and gen != self.gen:
            return
        n = int(np.asarray(pcm16k).size)
        if n == 0:
            return
        with self._clock_lock:
            now = time.monotonic()
            if self._play_end < now:
                self._play_end = now + PREBUFFER_S
            self._play_end += n / float(P.RATE)
        self._post(("audio", self.gen, to_int16(pcm16k)))

    def flush(self) -> None:
        """Barge-in: stop talking now."""
        with self._clock_lock:
            self.gen += 1
            self._play_end = 0.0
        self.send({"type": P.FLUSH})

    def send(self, msg: dict) -> None:
        self._post(("text", None, json.dumps(msg, separators=(",", ":"))))

    def set_state(self, state: str) -> None:
        if state == self.last_state:
            return
        self.last_state = state
        self.send({"type": P.STATE, "state": state})

    def set_volume(self, level: int) -> None:
        self.volume = max(0, min(100, int(level)))
        self.send({"type": P.VOLUME, "level": self.volume})

    def set_brightness(self, level: int) -> None:
        self.brightness = max(0, min(100, int(level)))
        self.send({"type": P.BRIGHTNESS, "level": self.brightness})

    def snapshot(self) -> dict:
        snap = dict(self.status)
        snap.update(connected=self.connected, state=self.last_state,
                    volume=self.volume, brightness=self.brightness)
        return snap

    def _post(self, item) -> None:
        loop, box = self.loop, self._outbox
        if loop is None or box is None or not self.connected:
            return
        try:
            loop.call_soon_threadsafe(box.put_nowait, item)
        except RuntimeError:
            pass                            # loop shutting down

    # --- asyncio side ---
    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve_start())
        except Exception as e:                                   # noqa: BLE001
            self.error = f"lite hub could not listen on :{self.port}: {type(e).__name__}: {e}"
            self._ready.set()
            return
        self._ready.set()
        _log(f"listening on ws://{self.host}:{self.port}{P.PATH}"
             f"{' (token required)' if self.token else ''}")
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    async def _serve_start(self):
        try:
            from websockets.asyncio.server import serve           # websockets >= 13
        except ImportError:                                      # pragma: no cover
            from websockets import serve                         # legacy API
        self._server = await serve(self._handler, self.host, self.port,
                                   max_size=2 ** 20, ping_interval=10, ping_timeout=20)

    async def _handler(self, ws, *legacy_path):
        req = getattr(ws, "request", None)
        path = getattr(req, "path", None) or getattr(ws, "path", None) or \
            (legacy_path[0] if legacy_path else P.PATH)
        if path.split("?")[0].rstrip("/") != P.PATH:
            await ws.close(4004, "wrong path")
            return
        peer = getattr(ws, "remote_address", None)
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        except Exception:                                        # noqa: BLE001
            await ws.close(4000, "expected hello")
            return
        if hello.get("type") != P.HELLO:
            await ws.close(4000, "expected hello")
            return
        if self.token and not hmac.compare_digest(str(hello.get("token") or ""), self.token):
            _log(f"rejected {peer}: bad token")
            await ws.close(4001, "bad token")
            return
        if int(hello.get("rate") or P.RATE) != P.RATE:
            await ws.close(4002, f"rate must be {P.RATE}")
            return

        old = self._ws
        if old is not None:
            _log("a second Lite connected, dropping the first")
            try:
                await old.close(4003, "replaced")
            except Exception:                                    # noqa: BLE001
                pass
        self._ws = ws
        self._outbox = asyncio.Queue()
        self.status = {"connected": True, "device": hello.get("device"),
                       "fw": hello.get("fw"), "peer": str(peer), "since": time.time()}
        self._connected.set()
        _log(f"{hello.get('device', 'device')} fw {hello.get('fw', '?')} connected from {peer}")
        await ws.send(json.dumps({"type": P.WELCOME, "proto": P.PROTO, "volume": self.volume,
                                  "brightness": self.brightness}))
        if self.last_state:
            await ws.send(json.dumps({"type": P.STATE, "state": self.last_state}))
        sender = asyncio.ensure_future(self._sender(ws, self._outbox))
        try:
            async for frame in ws:
                if isinstance(frame, (bytes, bytearray, memoryview)):
                    self._fan_out(bytes(frame))
                else:
                    self._on_text(frame)
        except Exception as e:                                   # noqa: BLE001
            _log(f"link dropped: {type(e).__name__}: {e}")
        finally:
            sender.cancel()
            if self._ws is ws:
                self._ws = None
                self._outbox = None
                self._connected.clear()
                self.status = {"connected": False, "last_seen": time.time()}
                _log("Lite disconnected")

    async def _sender(self, ws, box: asyncio.Queue) -> None:
        while True:
            kind, gen, payload = await box.get()
            if kind == "audio" and gen != self.gen:
                continue                    # flushed while queued
            try:
                await ws.send(payload)
            except Exception:                                    # noqa: BLE001
                return

    def _on_text(self, frame: str) -> None:
        try:
            msg = json.loads(frame)
        except ValueError:
            return
        kind = msg.get("type")
        if kind in (P.DOA, P.BATTERY, P.STATUS):
            self.status[kind] = {k: v for k, v in msg.items() if k != "type"}
            self.status[kind]["at"] = time.time()
        for fn in list(self._listeners):
            try:
                fn(msg)
            except Exception:                                    # noqa: BLE001
                pass

    # --- discovery ---
    def _beacon_loop(self) -> None:
        """Same idea as the drive link's beacon: the device finds us without a hardcoded IP."""
        beacon = json.dumps({"reedus_lite_hub": True, "port": self.port,
                             "path": P.PATH, "proto": P.PROTO}).encode()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError as e:
            _log(f"beacon off: {e}")
            return
        while not self._stop.is_set():
            if not self.connected:
                try:
                    s.sendto(beacon, ("255.255.255.255", P.BEACON_PORT))
                except OSError:
                    pass
            self._stop.wait(2.0)
