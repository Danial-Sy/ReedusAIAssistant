"""music out of Lite's own speaker: librespot on the run box is a Spotify Connect speaker
called "Reedus Lite", and its audio goes down the Lite link on the music lane.

    librespot --backend pipe  ->  44.1 kHz stereo s16 on stdout
                              ->  mono, filtered down to 24 kHz (resample.py)
                              ->  hub.queue_music(), paced ~0.8 s ahead of the device

reading only as fast as the device plays means librespot blocks on the pipe when Lite is
away, so a song doesn't race ahead into nowhere. librespot's --onevent tells us about pauses
(we flush the device so pause is instant) and track changes (the face shows what's on).

the first time, pick "Reedus Lite" in the Spotify app's device list (same Wi-Fi as the run
box); librespot caches the login and shows up for "play X" from then on.
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import List, Optional

import numpy as np

from reedus_companion import config
from reedus_companion.lite.resample import Resampler

DIM, RESET = "\033[2m", "\033[0m"
SRC_RATE = 44100
CHUNK_S = 0.04                                   # 40 ms per music frame on the wire
EVENT_PORT = int(os.environ.get("REEDUS_MUSIC_EVENT_PORT", "8770"))
NAME = os.environ.get("REEDUS_LITE_SPEAKER_NAME", "Reedus Lite")
CACHE = Path(os.environ.get("REEDUS_LIBRESPOT_CACHE", str(config.MEMORY_DIR / "librespot")))


def _log(msg: str) -> None:
    print(f"{DIM}  [music] {msg}{RESET}", flush=True)


def find_librespot() -> Optional[str]:
    p = os.environ.get("REEDUS_LIBRESPOT", "").strip()
    if p:
        return p if (os.path.isfile(p) or shutil.which(p)) else None
    return shutil.which("librespot")


class MusicService:
    def __init__(self, hub, binary: Optional[str] = None, name: str = NAME,
                 event_port: int = EVENT_PORT, cache: Path = CACHE):
        self.hub = hub
        self.binary = binary if binary is not None else find_librespot()
        self.name = name
        self.event_port = int(event_port)
        self.cache = Path(cache)
        self.proc: Optional[subprocess.Popen] = None
        self.state = "off"            # off | idle | playing | paused | error
        self.error = ""
        self.track = ""
        self.started_at = 0.0
        self._stop = threading.Event()
        self._stderr: deque = deque(maxlen=20)
        self._rs = Resampler(SRC_RATE, hub_rate(), taps_per_phase=32)
        self.sent_s = 0.0             # seconds of music handed to the hub (for tests/status)
        self.on_track = None          # callable(): a new track started (the face shows it)
        # after librespot says "paused", what's still in the pipe (up to ~0.4 s) is drained and
        # dropped until it says "playing" again, so pause means silence, not one more beat
        self._muted = False
        self._publish()

    # ---------- lifecycle ----------
    @property
    def available(self) -> bool:
        return bool(self.binary)

    def start(self) -> "MusicService":
        if not self.available:
            self.state, self.error = "off", "librespot isn't installed on the run box"
            _log("librespot not found: Lite plays voice only (see ReedusLite/MUSIC.md)")
            self._publish()
            return self
        threading.Thread(target=self._events, name="music-events", daemon=True).start()
        threading.Thread(target=self._supervise, name="music", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()

    def command(self) -> List[str]:
        """The librespot command line. REEDUS_LIBRESPOT_ARGS adds or overrides flags."""
        self.cache.mkdir(parents=True, exist_ok=True)
        cmd = [self.binary, "--name", self.name, "--device-type", "speaker",
               "--backend", "pipe", "--format", "S16", "--bitrate", "160",
               "--initial-volume", "70", "--enable-volume-normalisation",
               "--cache", str(self.cache), "--onevent", str(self._onevent_script())]
        extra = os.environ.get("REEDUS_LIBRESPOT_ARGS", "").split()
        return cmd + extra

    def _onevent_script(self) -> Path:
        """librespot runs this with PLAYER_EVENT etc. in its environment; it forwards to us."""
        p = self.cache / ("onevent.cmd" if os.name == "nt" else "onevent.sh")
        code = ("import os,socket;e=os.environ;socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto("
                "(e.get('PLAYER_EVENT','')+' '+e.get('TRACK_ID','')).encode(),"
                f"('127.0.0.1',{self.event_port}))")
        if os.name == "nt":
            body = f'@"{sys.executable}" -c "{code}"\r\n'
        else:
            body = f"#!/bin/sh\nexec '{sys.executable}' -c \"{code}\"\n"
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return p

    def _supervise(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self.proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, bufsize=0)
            except OSError as e:
                self.state, self.error = "error", f"couldn't start librespot: {e}"
                _log(self.error)
                self._publish()
                self._stop.wait(30.0)
                continue
            self.state, self.error, self.started_at = "idle", "", time.time()
            _log(f"Spotify Connect speaker \"{self.name}\" is up")
            self._publish()
            threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
            self._pump(self.proc)
            code = self.proc.wait()
            if self._stop.is_set():
                break
            tail = " | ".join(list(self._stderr)[-3:])
            self.state, self.error = "error", f"librespot exited ({code}){': ' + tail if tail else ''}"
            _log(self.error)
            self._publish()
            # a crash right away is usually a bad flag or a missing audio lib: don't spin
            backoff = 2.0 if time.time() - self.started_at > 60 else min(backoff * 2, 60.0)
            self._stop.wait(backoff)

    def _drain_stderr(self, proc) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr.append(line[:200])

    # ---------- audio ----------
    def _pump(self, proc) -> None:
        """librespot stdout -> 24 kHz mono -> the hub, only as fast as Lite plays it."""
        from reedus_companion.lite import hub as hub_mod
        need = int(SRC_RATE * CHUNK_S) * 4                  # stereo s16
        buf = b""
        while not self._stop.is_set():
            # don't read ahead while Lite is away or full: librespot waits on the pipe
            while not self._stop.is_set() and not self._muted and (
                    not self.hub.music_ok or self.hub.music_ahead() > hub_mod.MUSIC_LEAD_S):
                if not self.hub.music_ok:
                    time.sleep(0.25)
                else:
                    time.sleep(0.01)
            data = proc.stdout.read(need - len(buf))
            if not data:
                return                                       # librespot exited
            buf += data
            if len(buf) < need:
                continue
            if self._muted:
                buf = b""
                continue
            frames = np.frombuffer(buf[:len(buf) & ~3], dtype="<i2").astype(np.float32) / 32768.0
            buf = b""
            mono = frames.reshape(-1, 2).mean(axis=1)
            out = self._rs.feed(mono)
            if self.state != "playing":
                self.state = "playing"
                self._publish()
            if self.hub.queue_music(out):
                self.sent_s += out.size / hub_rate()

    # ---------- librespot events ----------
    def _events(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("127.0.0.1", self.event_port))
        except OSError as e:
            _log(f"no pause/skip events (port {self.event_port}: {e}); pause will lag ~1 s")
            return
        s.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, _ = s.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                return
            self.on_event(data.decode("utf-8", "replace"))

    def on_event(self, raw: str) -> None:
        ev, _, track = (raw or "").strip().partition(" ")
        ev = ev.lower()
        if ev in ("paused", "stopped", "session_disconnected", "unavailable"):
            # pause means silence now, not after the 0.8 s already on the device
            self._muted = True
            self.hub.music_flush()
            self._rs.reset()
            self.state = "paused" if ev == "paused" else "idle"
        elif ev in ("playing", "started"):
            self._muted = False
            self.state = "playing"
        elif ev in ("track_changed", "changed") and track and track != self.track:
            self.track = track
            if self.on_track:
                threading.Timer(1.5, self._safe_track).start()  # let the Web API catch up
        self._publish()

    def _safe_track(self) -> None:
        try:
            self.on_track()
        except Exception:                                        # noqa: BLE001
            pass

    def control(self, action: str) -> None:
        """Called after a voice command reached Spotify: make the speaker react at once."""
        if action == "pause" and self.state == "playing":
            self.hub.music_flush()
            self._rs.reset()
            self.state = "paused"
            self._publish()

    def _publish(self) -> None:
        self.hub.music = {"available": self.available, "state": self.state, "name": self.name,
                          "error": self.error}

    def status(self) -> dict:
        return dict(self.hub.music)


def hub_rate() -> int:
    from reedus_companion.lite import protocol as P
    return P.MUSIC_RATE


_one: Optional[MusicService] = None


def start(hub) -> Optional[MusicService]:
    """Once per process. REEDUS_LITE_MUSIC=0 keeps Lite voice-only."""
    global _one
    if os.environ.get("REEDUS_LITE_MUSIC", "1") == "0":
        return None
    if _one is None:
        _one = MusicService(hub).start()
        _wire_spotify(_one)
    return _one


def _wire_spotify(svc: MusicService) -> None:
    """'play X' now means here, and a spoken pause silences the speaker immediately."""
    if not svc.available:
        return
    try:
        from reedus_companion.connect.manager import manager
        sp = manager().spotify
        sp.home_device = svc.name
        sp.on_control.append(svc.control)
    except Exception as e:                                       # noqa: BLE001
        _log(f"couldn't hook Spotify commands to Lite: {e}")

    def show_track():
        from reedus_companion.lite import glance
        d = glance._one
        if d is None or (d.hub.last_state or "idle") != "idle" or d.a.ringing():
            return
        card = d.build("music")
        if card:
            d.show(card, glance.CARD_HOLD.get("music", 8.0))
    svc.on_track = show_track
