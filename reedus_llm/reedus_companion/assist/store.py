"""timers, alarms, reminders and lists: the state, the clock that fires them, persistence.

one Assistant per brain (assistant()). it never speaks: when something is due it flips the
item to `ringing` and tells its listeners (voice/ringer.py rings it, lite/glance.py shows it).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from reedus_companion import config
from reedus_companion.assist import when

PATH = Path(os.environ.get("REEDUS_ASSIST_FILE", str(config.MEMORY_DIR / "assist.json")))

SNOOZE_S = int(os.environ.get("REEDUS_SNOOZE_S", str(9 * 60)))      # Alexa's 9 minutes
RING_MAX_S = {"timer": 300.0, "alarm": 600.0, "reminder": 45.0}       # then it gives up
MISSED_REMINDER_S = 12 * 3600       # a reminder that came due while the brain was off still counts
ACK_WINDOW_S = 12.0                 # after the wake word hushes a ring, this long to say stop/snooze

DEFAULT_SETTINGS = {
    "glance": {"enabled": True, "every": 120, "cards": ["clock", "weather", "event", "music"],
               "h24": False, "night": [22, 7]},
    "calendars": [],                # [{id, name, url, enabled}]
    "units": "F",
}

_LIST_ALIASES = {"grocery": "shopping", "groceries": "shopping", "shopping": "shopping",
                 "todo": "to-do", "to do": "to-do", "to-do": "to-do", "task": "to-do",
                 "tasks": "to-do", "chore": "chores", "chores": "chores"}


def list_key(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").lower().strip()).removesuffix(" list").strip()
    n = re.sub(r"^(?:my|the|our)\s+", "", n)
    return _LIST_ALIASES.get(n, n or "shopping")


class Assistant:
    # after the wake word hushes a ring and nobody says stop/snooze in ACK_WINDOW_S: False ends
    # it there (the run box), True rings again until RING_MAX_S (the Lite, where a false wake
    # used to swallow a timer whole)
    HUSH_RESUMES = False

    def __init__(self, path: Path = PATH, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self.lock = threading.RLock()
        self.items: List[dict] = []
        self.lists: Dict[str, List[dict]] = {}
        self.settings: dict = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.pending: Optional[dict] = None          # half-asked reminder: {"text", "at"}
        self._listeners: List[Callable[[str, dict], None]] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.missed: List[dict] = []
        self._load()

    # ---------- storage ----------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            raw = {}
        self.lists = raw.get("lists") or {}
        for k, v in (raw.get("settings") or {}).items():
            if isinstance(v, dict) and isinstance(self.settings.get(k), dict):
                self.settings[k].update(v)
            else:
                self.settings[k] = v
        now = self.clock()
        for it in raw.get("items") or []:
            if it.get("state") == "ringing":
                it["state"] = "active"
            due = it.get("due") or 0
            if it.get("state") == "paused" or due > now:
                self.items.append(it)
            elif it.get("repeat"):
                self._advance(it, now)
                self.items.append(it)
            elif it["kind"] == "reminder" and now - due < MISSED_REMINDER_S:
                it["missed"] = True
                self.items.append(it)                # fires on the next tick, late
            else:
                self.missed.append(it)               # a timer or alarm whose moment passed

    def save(self) -> None:
        with self.lock:
            data = {"items": [i for i in self.items if i.get("state") != "done"],
                    "lists": self.lists, "settings": self.settings}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(self.path)

    # ---------- events ----------
    def on_change(self, fn: Callable[[str, dict], None]) -> None:
        """fn(kind, item): kind is added | removed | ringing | stopped | snoozed | changed."""
        self._listeners.append(fn)

    def _emit(self, kind: str, item: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, item)
            except Exception:                                    # noqa: BLE001
                pass

    # ---------- the clock ----------
    def start(self) -> "Assistant":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="assist-clock", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            wait = self.tick()
            self._wake.wait(timeout=max(0.05, min(wait, 30.0)))
            self._wake.clear()

    def tick(self) -> float:
        """Fire what's due, expire what's rung too long. Seconds until the next thing."""
        now = self.clock()
        fired, expired = [], []
        with self.lock:
            nxt = 3600.0
            for it in self.items:
                st = it.get("state")
                if st == "active" and it["due"] <= now:
                    it["state"], it["rang_at"] = "ringing", now
                    fired.append(it)
                elif st in ("ringing", "hushed"):
                    limit = it.get("hushed_at", 0) + ACK_WINDOW_S if st == "hushed" else \
                        it.get("rang_at", now) + RING_MAX_S.get(it["kind"], 300)
                    gave_up = now >= it.get("rang_at", now) + RING_MAX_S.get(it["kind"], 300)
                    if st == "hushed" and now >= limit and self.HUSH_RESUMES and not gave_up:
                        # the name quieted it but nobody said stop or snooze: ring again
                        it["state"] = "ringing"
                        it.pop("hushed_at", None)
                        fired.append(it)
                        nxt = min(nxt, it.get("rang_at", now) + RING_MAX_S.get(it["kind"], 300) - now)
                    elif now >= limit:
                        expired.append(it)
                    else:
                        nxt = min(nxt, limit - now)
                if it.get("state") == "active":
                    nxt = min(nxt, it["due"] - now)
            if fired or expired:
                for it in expired:
                    self._finish(it, now)
                self.save()
        for it in fired:
            self._emit("ringing", it)
        for it in expired:
            self._emit("stopped", it)
        return nxt

    def _finish(self, it: dict, now: float) -> None:
        """Done ringing: repeats roll to the next day, the rest go away."""
        it.pop("rang_at", None)
        it.pop("hushed_at", None)
        it.pop("missed", None)
        if it.get("repeat"):
            self._advance(it, now)
            it["state"] = "active"
        else:
            it["state"] = "done"
            self.items = [i for i in self.items if i is not it]

    def _advance(self, it: dict, now: float) -> None:
        dt = datetime.fromtimestamp(it.get("orig") or it["due"])
        nxt = when.next_on_days(dt.hour, dt.minute, it["repeat"], datetime.fromtimestamp(now))
        it["due"] = nxt.timestamp()
        it["orig"] = it["due"]

    def _poke(self) -> None:
        self._wake.set()

    # ---------- adding ----------
    def _add(self, kind: str, due: float, label: str = "", **extra) -> dict:
        it = {"id": uuid.uuid4().hex[:8], "kind": kind, "label": label.strip(), "due": float(due),
              "created": self.clock(), "state": "active", **extra}
        with self.lock:
            self.items.append(it)
            self.save()
        self._emit("added", it)
        self._poke()
        return it

    def add_timer(self, seconds: float, label: str = "") -> dict:
        return self._add("timer", self.clock() + seconds, label, total=float(seconds))

    def add_alarm(self, due: float, label: str = "", repeat: Optional[List[int]] = None) -> dict:
        return self._add("alarm", due, label, repeat=repeat or None, orig=float(due))

    def add_reminder(self, due: float, text: str, repeat: Optional[List[int]] = None) -> dict:
        return self._add("reminder", due, text, repeat=repeat or None, orig=float(due))

    # ---------- queries ----------
    def active(self, kind: Optional[str] = None) -> List[dict]:
        with self.lock:
            out = [i for i in self.items if i.get("state") in ("active", "paused")
                   and (kind is None or i["kind"] == kind)]
        return sorted(out, key=self.left)

    def ringing(self) -> List[dict]:
        with self.lock:
            return [i for i in self.items if i.get("state") in ("ringing", "hushed")]

    def left(self, it: dict) -> float:
        if it.get("state") == "paused":
            return float(it.get("paused_left") or 0)
        return max(0.0, it["due"] - self.clock())

    def find(self, kind: str, label: str = "", seconds: Optional[int] = None,
             clock: Optional[tuple] = None) -> List[dict]:
        """Items of `kind` matching a spoken description; all of them if nothing narrows it."""
        cands = self.active(kind)
        if label:
            words = set(re.findall(r"[a-z0-9]+", label.lower())) - {"the", "my", "a", "an", "for", "to"}
            hit = [i for i in cands if words & set(re.findall(r"[a-z0-9]+", i.get("label", "").lower()))]
            if hit:
                return hit
        if seconds:
            hit = [i for i in cands if abs(i.get("total", -1) - seconds) < 1.5]
            if hit:
                return hit
        if clock:
            h, m = clock
            hit = [i for i in cands if datetime.fromtimestamp(i["due"]).minute == m and
                   datetime.fromtimestamp(i["due"]).hour % 12 == h % 12]
            if hit:
                return hit
        return cands if not (label or seconds or clock) else []

    # ---------- changing ----------
    def cancel(self, ids: List[str]) -> List[dict]:
        with self.lock:
            gone = [i for i in self.items if i["id"] in ids]
            self.items = [i for i in self.items if i["id"] not in ids]
            if gone:
                self.save()
        for it in gone:
            self._emit("removed", it)
        self._poke()
        return gone

    def pause(self, it: dict) -> None:
        with self.lock:
            if it.get("state") == "active":
                it["paused_left"] = self.left(it)
                it["state"] = "paused"
                self.save()
        self._emit("changed", it)
        self._poke()

    def resume(self, it: dict) -> None:
        with self.lock:
            if it.get("state") == "paused":
                it["due"] = self.clock() + float(it.pop("paused_left", 0))
                it["state"] = "active"
                self.save()
        self._emit("changed", it)
        self._poke()

    def extend(self, it: dict, seconds: float) -> None:
        with self.lock:
            if it.get("state") == "paused":
                it["paused_left"] = float(it.get("paused_left", 0)) + seconds
            else:
                it["due"] += seconds
            it["total"] = float(it.get("total", 0)) + seconds
            self.save()
        self._emit("changed", it)
        self._poke()

    def hush(self) -> List[dict]:
        """The wake word was heard while ringing: quiet, but wait a moment for stop/snooze."""
        now = self.clock()
        with self.lock:
            out = [i for i in self.items if i.get("state") == "ringing"]
            for it in out:
                it["state"], it["hushed_at"] = "hushed", now
        for it in out:
            self._emit("changed", it)
        self._poke()
        return out

    def dismiss(self, ids: Optional[List[str]] = None) -> List[dict]:
        now = self.clock()
        with self.lock:
            out = [i for i in self.items if i.get("state") in ("ringing", "hushed")
                   and (ids is None or i["id"] in ids)]
            for it in out:
                self._finish(it, now)
            if out:
                self.save()
        for it in out:
            self._emit("stopped", it)
        self._poke()
        return out

    def snooze(self, seconds: float = SNOOZE_S) -> List[dict]:
        now = self.clock()
        with self.lock:
            out = [i for i in self.items if i.get("state") in ("ringing", "hushed")]
            for it in out:
                it.pop("rang_at", None)
                it.pop("hushed_at", None)
                it["due"], it["state"] = now + seconds, "active"
                it["snoozed"] = int(it.get("snoozed", 0)) + 1
            if out:
                self.save()
        for it in out:
            self._emit("snoozed", it)
        self._poke()
        return out

    # ---------- lists ----------
    def list_add(self, name: str, things: List[str]) -> tuple[str, List[str]]:
        key = list_key(name)
        added = []
        with self.lock:
            lst = self.lists.setdefault(key, [])
            have = {x["text"].lower() for x in lst if not x.get("done")}
            for t in things:
                t = t.strip(" .,")
                if t and t.lower() not in have:
                    lst.append({"text": t, "at": self.clock()})
                    have.add(t.lower())
                    added.append(t)
            self.save()
        self._emit("changed", {"kind": "list", "list": key})
        return key, added

    def list_items(self, name: str) -> tuple[str, List[str]]:
        key = list_key(name)
        with self.lock:
            return key, [x["text"] for x in self.lists.get(key, []) if not x.get("done")]

    def list_remove(self, name: str, thing: str) -> tuple[str, Optional[str]]:
        key = list_key(name)
        t = thing.strip(" .,").lower()
        with self.lock:
            lst = self.lists.get(key, [])
            hit = next((x for x in lst if x["text"].lower() == t), None) or \
                next((x for x in lst if t and (t in x["text"].lower() or x["text"].lower() in t)), None)
            if hit is None:
                return key, None
            lst.remove(hit)
            self.save()
        self._emit("changed", {"kind": "list", "list": key})
        return key, hit["text"]

    def list_clear(self, name: str) -> tuple[str, int]:
        key = list_key(name)
        with self.lock:
            n = len(self.lists.get(key, []))
            self.lists[key] = []
            self.save()
        self._emit("changed", {"kind": "list", "list": key})
        return key, n

    # ---------- for ReedOS / the face ----------
    def snapshot(self) -> dict:
        with self.lock:
            items = []
            for it in self.items:
                if it.get("state") == "done":
                    continue
                items.append({**it, "left": round(self.left(it), 1)})
            return {"items": sorted(items, key=lambda i: i["due"]),
                    "lists": {k: [x["text"] for x in v if not x.get("done")] for k, v in self.lists.items()},
                    "settings": json.loads(json.dumps(self.settings)), "now": self.clock()}

    def set_settings(self, patch: dict) -> dict:
        with self.lock:
            for k, v in (patch or {}).items():
                if k not in DEFAULT_SETTINGS:
                    continue
                if isinstance(v, dict) and isinstance(self.settings.get(k), dict):
                    self.settings[k].update(v)
                else:
                    self.settings[k] = v
            self.save()
        self._emit("changed", {"kind": "settings"})
        return self.settings


_one: Optional[Assistant] = None
_one_lock = threading.Lock()


def assistant() -> Assistant:
    global _one
    with _one_lock:
        if _one is None:
            _one = Assistant().start()
        return _one
