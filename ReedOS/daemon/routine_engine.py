"""routine_engine, executes routine JSON built in the ReedOS UI."""

import asyncio
import logging

log = logging.getLogger("routines")

TURN_SECONDS_PER_DEG = 0.008
TURN_DUTY = 0.35


class RoutineEngine:
    def __init__(self, bridge, chat):
        self.bridge = bridge
        self.chat = chat
        self._abort = False
        self._running = False

    def abort(self):
        self._abort = True

    async def run(self, routine: dict, hub):
        if self._running:
            await hub.broadcast("log", {"level": "warn", "msg": "a routine is already running"})
            return
        self._running, self._abort = True, False
        name = routine.get("name", "routine")
        steps = routine.get("steps", [])
        rid = routine.get("id")
        speed_cap = 1.0

        repeat = next((s for s in steps if s.get("type") == "repeat"), None)
        passes = max(1, int(repeat.get("times", 1))) if repeat else 1
        seq = [(i, s) for i, s in enumerate(steps) if s.get("type") != "repeat"]

        mode = "closed-loop" if self.bridge.closed_loop else "open-loop"
        await hub.broadcast("log", {"level": "info", "msg": f'routine "{name}" started ({mode})'})
        try:
            for _ in range(passes):
                for ix, s in seq:
                    if self._abort:
                        raise asyncio.CancelledError
                    await hub.broadcast("routine.progress", {"id": rid, "stepIx": ix, "done": False})
                    speed_cap = await self._step(s, speed_cap, hub)
        except asyncio.CancelledError:
            await hub.broadcast("log", {"level": "warn", "msg": f'routine "{name}" aborted'})
        finally:
            await self.bridge.stop()
            self._running = False
            await hub.broadcast("routine.progress", {"id": rid, "done": True})
            await hub.broadcast("log", {"level": "info", "msg": f'routine "{name}" finished'})

    async def _step(self, s: dict, speed_cap: float, hub) -> float:
        t = s.get("type")

        if t == "move":
            await self._move(s, speed_cap, hub)

        elif t == "turn":
            await self._turn(s, hub)

        elif t == "wait":
            await self._sleep(s.get("seconds", 1))

        elif t == "say":
            text = s.get("text", "")
            await hub.broadcast("log", {"level": "info", "msg": f'Reedus says: "{text}"'})
            await self.chat.speak(text)

        elif t == "sound":
            await hub.broadcast("log", {"level": "info", "msg": f"*{s.get('name', 'chirp')}*"})
            await self.chat.play_sound(s.get("name", "chirp"))

        elif t == "expression":
            await hub.broadcast("log", {"level": "info", "msg": f"expression -> {s.get('mood')}"})

        elif t == "speed":
            speed_cap = s.get("limit", 60) / 100.0

        return speed_cap

    async def _move(self, s, speed_cap, hub):
        back = s.get("dir") == "back"

        if self.bridge.closed_loop:
            if "cm" in s:
                mm = float(s["cm"]) * 10.0
            else:
                rpm = min(speed_cap, s.get("speed", 40) / 100.0) * 60.0
                mm = (rpm / 60.0) * 204.0 * s.get("seconds", 1)   # 204mm per wheel rev
            await hub.broadcast("log", {"level": "info",
                                        "msg": f"move {'back' if back else 'forward'} {mm:.0f} mm"})
            await self.bridge.move_mm(-mm if back else mm)
            await self._await_maneuver()
        else:
            duty = min(speed_cap, s.get("speed", 40) / 100.0)
            if back:
                duty = -duty
            await self.bridge.drive(duty, duty)
            await self._sleep(s.get("seconds", 1))
            await self.bridge.stop()

    async def _turn(self, s, hub):
        deg = float(s.get("degrees", 90))
        right = s.get("dir") == "right"

        if self.bridge.closed_loop:
            await hub.broadcast("log", {"level": "info",
                                        "msg": f"turn {'right' if right else 'left'} {deg:.0f} deg"})
            await self.bridge.turn_deg(deg if right else -deg)
            await self._await_maneuver()
        else:
            spin = TURN_DUTY if right else -TURN_DUTY
            await self.bridge.drive(spin, -spin)
            await self._sleep(max(0.2, deg * TURN_SECONDS_PER_DEG))
            await self.bridge.stop()

    async def _await_maneuver(self):
        """Wait for the Pico to report the maneuver finished, abortably."""
        while not self.bridge.maneuver_done.is_set():
            if self._abort:
                await self.bridge.stop()
                raise asyncio.CancelledError
            try:
                await asyncio.wait_for(self.bridge.maneuver_done.wait(), 0.1)
            except asyncio.TimeoutError:
                pass

    async def _sleep(self, seconds: float):
        """Abortable sleep in 50 ms slices."""
        end = asyncio.get_event_loop().time() + float(seconds)
        while asyncio.get_event_loop().time() < end:
            if self._abort:
                raise asyncio.CancelledError
            await asyncio.sleep(0.05)
