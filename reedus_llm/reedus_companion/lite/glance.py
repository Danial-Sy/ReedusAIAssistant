"""the face as something you look at: timers counting down, and while nobody's talking a short
rotation of cards (time, weather, next class, what's playing) before the face comes back.

the device does the drawing and the ticking (firmware glance.cpp); this decides what's worth
showing and when, and keeps the device's clock and timer list current. protocol: PROTOCOL.md.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from datetime import datetime
from typing import List, Optional

from reedus_companion.lite import protocol as P

IDLE_BEFORE_S = 20.0        # quiet this long after a conversation before cards start
CARD_HOLD = {"clock": 6.0, "weather": 6.0, "event": 9.0, "music": 8.0, "text": 8.0}
RESYNC_S = 60.0             # timers drift by a few ms a minute on the device; correct it anyway
CLOCK_EVERY_S = 300.0


def panel_text(s: str, n: int = 48) -> str:
    """What the 3x5 font can draw: uppercase ASCII, accents folded, the rest dropped."""
    t = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9 .,:!'?\-+/&%#()@*=]", " ", t).upper()
    return re.sub(r"\s+", " ", t).strip()[:n]


class GlanceDirector:
    def __init__(self, hub, store=None, clock=time.time):
        from reedus_companion.assist.store import assistant
        self.hub = hub
        self.a = store or assistant()
        self.clock = clock
        self._kick = threading.Event()
        self._stop = threading.Event()
        self._was_connected = False
        self._last_busy = clock()
        self._next_round = clock() + 30.0
        self._last_timers = 0.0
        self._last_clock = 0.0
        self._timers_sig = ""
        self.card: Optional[dict] = None             # what's on the face now, for ReedOS's preview
        self.a.on_change(lambda kind, it: self._kick.set())
        hub.on_message(self._on_device)

    def start(self) -> "GlanceDirector":
        threading.Thread(target=self._run, name="lite-glance", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    def _on_device(self, msg: dict) -> None:
        pass                                         # reserved: the device may ask for a card later

    # ---------- the loop ----------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:                               # noqa: BLE001
                print(f"\033[2m  [glance] {type(e).__name__}: {e}\033[0m", flush=True)
            self._kick.wait(timeout=1.0)
            self._kick.clear()

    def step(self) -> None:
        hub, now = self.hub, self.clock()
        if not hub.connected:
            self._was_connected = False
            return
        if not self._was_connected:                  # fresh link: tell it the time and the timers
            self._was_connected = True
            self._timers_sig = ""
            self._last_clock = 0.0
        if now - self._last_clock > CLOCK_EVERY_S:
            self.send_clock()
        self.send_timers(force=now - self._last_timers > RESYNC_S)

        state = hub.last_state or "idle"
        if state != "idle":
            self._last_busy = now
            self._next_round = max(self._next_round, now + IDLE_BEFORE_S)
            return
        g = self.a.settings.get("glance") or {}
        if not g.get("enabled", True) or self.a.ringing():
            return
        if any(i["kind"] == "timer" for i in self.a.active()):
            return                                   # the countdown owns the face
        if now < self._next_round or now - self._last_busy < IDLE_BEFORE_S:
            return
        self._next_round = now + max(30.0, float(g.get("every", 120)))
        self.play_round(g)

    # ---------- what goes out ----------
    def send_clock(self) -> None:
        now = datetime.now()
        wall = int(now.timestamp() + (now.astimezone().utcoffset() or 0).total_seconds())
        h24 = bool((self.a.settings.get("glance") or {}).get("h24"))
        self.hub.send({"type": P.CLOCK, "t": wall, "h24": h24})
        self.hub.display = {**getattr(self.hub, "display", {}), "h24": h24}
        self._last_clock = self.clock()

    def timers_msg(self) -> dict:
        timers, ring, alarm_in = [], [], -1
        for it in self.a.active():
            left = self.a.left(it)
            if it["kind"] == "timer":
                timers.append({"id": it["id"], "label": panel_text(it.get("label", ""), 20),
                               "left": round(left, 1), "total": round(it.get("total") or left, 1),
                               "paused": it.get("state") == "paused"})
            elif it["kind"] == "alarm" and left < 24 * 3600 and (alarm_in < 0 or left < alarm_in):
                alarm_in = int(left)
        for it in self.a.ringing():
            if it.get("state") == "ringing":
                label = it.get("label", "")
                if it["kind"] == "alarm" and not label:
                    label = "WAKE UP"
                ring.append({"id": it["id"], "kind": it["kind"], "label": panel_text(label, 40)})
        return {"type": P.TIMERS, "items": timers[:4], "ring": ring[:2], "alarm": alarm_in}

    def send_timers(self, force: bool = False) -> None:
        msg = self.timers_msg()
        sig = repr(([(t["id"], t["paused"], round(t["total"])) for t in msg["items"]], msg["ring"],
                    msg["alarm"] >= 0))
        if not force and sig == self._timers_sig:
            return
        self._timers_sig = sig
        self._last_timers = self.clock()
        self.hub.send(msg)
        self.hub.display = {**getattr(self.hub, "display", {}), "timers": msg, "timers_at": time.time()}

    def show(self, card: dict, hold: float) -> None:
        msg = {"type": P.GLANCE, "hold": hold, **card}
        self.hub.send(msg)
        self.card = {**card, "start": time.time(), "until": time.time() + hold}
        self.hub.display = {**getattr(self.hub, "display", {}), "card": self.card}

    # ---------- the round ----------
    def play_round(self, g: dict) -> None:
        cards = list(g.get("cards") or ["clock", "weather", "event", "music"])
        night = g.get("night") or [22, 7]
        h = datetime.now().hour
        if len(night) == 2 and (h >= night[0] or h < night[1] if night[0] > night[1] else night[0] <= h < night[1]):
            cards = [c for c in cards if c == "clock"]   # at night: just the time, dim
            dim = True
        else:
            dim = False
        for name in cards:
            card = self.build(name)
            if card is None:
                continue
            if dim:
                card["dim"] = True
            hold = CARD_HOLD.get(card["card"], 6.0)
            self.show(card, hold)
            if not self._wait_idle(hold + 0.4):
                return                               # someone started talking: the face is back

    def _wait_idle(self, secs: float) -> bool:
        end = time.time() + secs
        while time.time() < end:
            if self._stop.is_set() or (self.hub.last_state or "idle") != "idle" or self.a.ringing():
                return False
            time.sleep(0.2)
        return True

    def build(self, name: str) -> Optional[dict]:
        if name == "clock":
            return {"card": "clock"}
        if name == "weather":
            try:
                from reedus_companion.serve.weather import current
                w = current()
            except Exception:                                    # noqa: BLE001
                w = None
            if not w:
                return None
            return {"card": "weather", "temp": w["temp"], "unit": w["unit"], "icon": w["icon"]}
        if name == "event":
            try:
                from reedus_companion.assist import briefing
                if not briefing.calendars().configured():
                    return None
                ev = briefing.next_event(within_h=12)
            except Exception:                                    # noqa: BLE001
                return None
            if ev is None:
                return None
            nowa = datetime.now().astimezone()
            mins = int((ev["start"] - nowa).total_seconds() // 60)
            h24 = bool((self.a.settings.get("glance") or {}).get("h24"))
            st = ev["start"].astimezone()
            at = st.strftime("%H:%M") if h24 else f"{st.hour % 12 or 12}:{st.minute:02d}"
            return {"card": "event", "title": panel_text(ev["title"], 40), "at": at, "mins": max(mins, 0)}
        if name == "music":
            try:
                from reedus_companion.connect.manager import manager
                sp = manager().spotify
                if not sp.connected:
                    return None
                n = sp.now()
            except Exception:                                    # noqa: BLE001
                return None
            if not n or not n.get("playing") or not n.get("track"):
                return None
            return {"card": "music", "title": panel_text(n["track"], 32), "artist": panel_text(n.get("artist", ""), 24)}
        return None


_one: Optional[GlanceDirector] = None


def start(hub) -> GlanceDirector:
    global _one
    if _one is None:
        _one = GlanceDirector(hub).start()
    return _one
