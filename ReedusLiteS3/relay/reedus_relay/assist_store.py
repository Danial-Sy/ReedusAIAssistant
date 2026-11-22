"""timers, alarms, reminders and lists per device, using the run box's own assist code
(reedus_companion.assist), stored in the relay database instead of memory/assist.json.

the S3 counts timers down itself and rings on its own if the relay is gone (firmware
glance.cpp), so a wifi drop never swallows an alarm. the relay stays the source of truth.

wall-clock parsing ("7 am", "tomorrow") uses the relay process's local time: set TZ on Railway
(for example TZ=America/Chicago).
"""
from __future__ import annotations

import json
import threading

from reedus_companion.assist.store import Assistant
from reedus_companion.lite.glance import panel_text
from reedus_relay.db import db


class _DbPath:
    """Quacks like the Path the parent reads from, but reads the device's row."""

    def __init__(self, device_id: str):
        self.device_id = device_id

    def read_text(self, encoding: str = "utf-8") -> str:
        row = db().one("SELECT assist FROM devices WHERE id = %s", (self.device_id,))
        return (row or {}).get("assist") or "{}"


class DbAssistant(Assistant):
    """The run box's Assistant, loading from and saving to the relay database."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self._save_lock = threading.Lock()
        super().__init__(path=_DbPath(device_id))

    def tick(self) -> float:
        """The parent's tick, but it can never take the clock down: an exception in a tick (a
        database hiccup while saving) used to end the clock thread silently, and from then
        on no timer on that Lite would ever ring or stop at the relay."""
        try:
            return super().tick()
        except Exception as e:                                   # noqa: BLE001
            print(f"[relay] timer clock hiccup ({type(e).__name__}: {e}); retrying in 2 s", flush=True)
            self._emit("changed", {})          # whatever did change, the Lite hears about it
            return 2.0

    def save(self) -> None:
        with self.lock:
            data = {"items": [i for i in self.items if i.get("state") != "done"],
                    "lists": self.lists, "settings": self.settings}
        with self._save_lock:
            db().execute("UPDATE devices SET assist = %s WHERE id = %s",
                         (json.dumps(data, separators=(",", ":")), self.device_id))


def timers_msg(a: Assistant) -> dict:
    """The `timers` message the firmware draws (same shape as lite/glance.py sends)."""
    timers, ring, alarm_in = [], [], -1
    for it in a.active():
        left = a.left(it)
        if it["kind"] == "timer":
            timers.append({"id": it["id"], "label": panel_text(it.get("label", ""), 20),
                           "left": round(left, 1), "total": round(it.get("total") or left, 1),
                           "paused": it.get("state") == "paused"})
        elif it["kind"] == "alarm" and left < 24 * 3600 and (alarm_in < 0 or left < alarm_in):
            alarm_in = int(left)
    for it in a.ringing():
        if it.get("state") == "ringing":
            label = it.get("label", "")
            if it["kind"] == "alarm" and not label:
                label = "WAKE UP"
            ring.append({"id": it["id"], "kind": it["kind"], "label": panel_text(label, 40)})
    return {"type": "timers", "items": timers[:4], "ring": ring[:2], "alarm": alarm_in}
