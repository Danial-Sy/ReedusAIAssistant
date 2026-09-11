"""robot_bridge, asyncio TCP client for the Pico W link agent (reedus_link.py)."""

import asyncio
import json
import logging
import time

log = logging.getLogger("bridge")

BEACON_PORT = 8267


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_beacon):
        self.on_beacon = on_beacon

    def datagram_received(self, data, addr):
        try:
            m = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return
        if m.get("reedus"):
            self.on_beacon(m.get("ip") or addr[0], m.get("port", 8266))


class RobotBridge:
    def __init__(self, host: str, port: int = 8266):
        self.host = host                 # configured fallback
        self.port = port
        self.discovered = None           # (ip, port) from the beacon
        self.reader = None
        self.writer = None
        self.connected = False
        self.closed_loop = False
        self._telemetry = None
        self._latency_ms = 0.0
        self._pps = 0.0
        self._start = time.time()
        self._hold = None                # latched continuous drive command
        self.maneuver_done = asyncio.Event()
        self.maneuver_done.set()

    # the Pico stops the motors if no command arrives within watchdog_ms, but a held key
    # or a still joystick produces no events. so we latch the last continuous command and
    # resend it at ~7 Hz until cancelled. done here so every client gets it for free.
    async def drive(self, l: float, r: float):
        cmd = {"cmd": "drive", "l": round(l, 3), "r": round(r, 3)}
        self._hold = None if (l == 0 and r == 0) else cmd
        await self._send(cmd)

    async def twist(self, fwd: float, turn: float):
        """Joystick / arrow keys. Both -1..1. Speed is held by the PID."""
        cmd = {"cmd": "twist", "fwd": round(fwd, 3), "turn": round(turn, 3)}
        self._hold = None if (fwd == 0 and turn == 0) else cmd
        await self._send(cmd)

    async def _hold_loop(self):
        """Resend the latched command so the robot's watchdog stays fed."""
        while self.connected:
            if self._hold is not None:
                await self._send(self._hold)
            await asyncio.sleep(0.15)

    async def move_mm(self, mm: float):
        """Closed-loop straight run. Await wait_maneuver() for completion."""
        self._hold = None                # maneuvers are not continuous commands
        self.maneuver_done.clear()
        await self._send({"cmd": "move", "mm": round(mm, 1)})

    async def turn_deg(self, deg: float):
        """Closed-loop spin. Positive = right. Real degrees, not timing."""
        self._hold = None
        self.maneuver_done.clear()
        await self._send({"cmd": "turn", "deg": round(deg, 1)})

    async def wait_maneuver(self, timeout: float = 40.0):
        try:
            await asyncio.wait_for(self.maneuver_done.wait(), timeout)
        except asyncio.TimeoutError:
            log.warning("maneuver timed out")

    async def stop(self):
        self._hold = None
        self.maneuver_done.set()
        await self._send({"cmd": "stop"})

    async def push_config(self, settings: dict):
        await self._send({
            "cmd": "cfg",
            "watchdog_ms": settings.get("watchdogMs", 400),
            "speed_limit": settings.get("speedLimit", 100),
        })

    def latest_telemetry(self):
        if not self.connected or self._telemetry is None:
            return None
        t = self._telemetry
        return {
            "rpm": {"l": t.get("rpm_l", 0), "r": t.get("rpm_r", 0)},
            "target": {"l": t.get("tgt_l", 0), "r": t.get("tgt_r", 0)},
            "counts": {"l": t.get("count_l", 0), "r": t.get("count_r", 0)},
            "duty": {"l": t.get("duty_l", 0), "r": t.get("duty_r", 0)},
            "headingDeg": t.get("head_deg", 0),
            "maneuver": t.get("man"),
            "maneuverPct": t.get("man_pct", 0),
            "latencyMs": self._latency_ms,
            "pps": self._pps,
            "uptimeS": int(time.time() - self._start),
            "battery": t.get("battery"),
            "closedLoop": self.closed_loop,
        }

    async def _send(self, obj: dict):
        if not self.connected or self.writer is None:
            return
        try:
            self.writer.write((json.dumps(obj) + "\n").encode())
            await self.writer.drain()
        except Exception:
            self.connected = False

    def _note_beacon(self, ip, port):
        if self.discovered != (ip, port):
            log.info("discovered Reedus at %s:%s via beacon", ip, port)
        self.discovered = (ip, port)

    async def _listen_beacon(self):
        loop = asyncio.get_running_loop()
        try:
            await loop.create_datagram_endpoint(
                lambda: _BeaconProtocol(self._note_beacon),
                local_addr=("0.0.0.0", BEACON_PORT),
                allow_broadcast=True,
            )
        except Exception as e:
            log.warning("beacon listener unavailable (%s), using configured host", e)

    async def run_forever(self, hub):
        await self._listen_beacon()
        while True:
            target = self.discovered or (self.host, self.port)
            try:
                self.reader, self.writer = await asyncio.wait_for(
                    asyncio.open_connection(*target), timeout=4.0
                )
                self.connected = True
                log.info("linked to Reedus at %s:%s", *target)
                await hub.broadcast("log", {"level": "info",
                                            "msg": "Reedus linked (%s)" % target[0]})
                await asyncio.gather(self._read_loop(), self._ping_loop(), self._hold_loop())
            except Exception:
                if self.connected:
                    await hub.broadcast("log", {"level": "err", "msg": "lost link to Reedus"})
                self.connected = False
                self.closed_loop = False
                self._hold = None
                self.maneuver_done.set()
            await asyncio.sleep(2.0)

    async def _read_loop(self):
        win_t = time.time()
        win_n = 0
        while self.connected:
            line = await self.reader.readline()
            if not line:
                self.connected = False
                break
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt = m.get("evt")
            if evt == "telemetry":
                self._telemetry = m
                win_n += 1
                now = time.time()
                if now - win_t >= 1.0:
                    self._pps = win_n / (now - win_t)
                    win_t, win_n = now, 0
            elif evt == "pong":
                self._latency_ms = (time.time() * 1000) - m.get("t", 0)
            elif evt == "hello":
                self.closed_loop = bool(m.get("closed_loop"))
                log.info("Reedus fw %s, closed_loop=%s", m.get("fw"), self.closed_loop)
            elif evt == "maneuver" and m.get("state") == "done":
                self.maneuver_done.set()

    async def _ping_loop(self):
        while self.connected:
            await self._send({"cmd": "ping", "t": time.time() * 1000})
            await asyncio.sleep(1.0)
