"""ReedOS's view of the assistant layer: GET /api/assist and POST /api/assist {action, ...}."""
from __future__ import annotations

import re
import time
import urllib.parse
import uuid
from datetime import datetime

from reedus_companion.assist import when
from reedus_companion.assist.store import assistant


def _mask(url: str) -> str:
    """Calendar links are secrets (anyone with one can read the calendar): show the host only."""
    try:
        return urllib.parse.urlparse(re.sub(r"^webcals?://", "https://", url, flags=re.I)).netloc
    except ValueError:
        return ""


def status() -> dict:
    from reedus_companion.assist import briefing
    a = assistant()
    snap = a.snapshot()
    cals = briefing.calendars()
    snap["settings"]["calendars"] = [
        {"id": c.get("id"), "name": c.get("name"), "enabled": c.get("enabled", True),
         "host": _mask(c.get("url", "")), "error": cals.errors.get(c.get("id"), "")}
        for c in a.settings.get("calendars") or []]
    try:
        nowa = datetime.now().astimezone()
        today = nowa.replace(hour=0, minute=0, second=0, microsecond=0)
        evs = cals.events(today, today.replace(hour=23, minute=59)) if cals.configured() else []
        snap["today"] = [{"title": e["title"], "start": e["start"].timestamp(), "end": e["end"].timestamp(),
                          "allDay": e["all_day"], "location": e.get("location", ""), "cal": e.get("cal", "")}
                         for e in evs[:20]]
    except Exception:                                            # noqa: BLE001
        snap["today"] = []
    return snap


def act(req: dict) -> dict:
    a = assistant()
    act_ = (req.get("action") or "").strip()
    try:
        if act_ == "cancel":
            gone = a.cancel([str(req.get("id") or "")])
            return {"ok": bool(gone)}
        if act_ == "stop":
            return {"ok": True, "stopped": len(a.dismiss())}
        if act_ == "snooze":
            return {"ok": True, "snoozed": len(a.snooze(float(req.get("seconds") or 540)))}
        if act_ == "pause" or act_ == "resume":
            it = next((i for i in a.items if i["id"] == req.get("id")), None)
            if it is None:
                return {"error": "no such timer"}
            (a.pause if act_ == "pause" else a.resume)(it)
            return {"ok": True}
        if act_ == "timer":
            secs = float(req.get("seconds") or 0)
            if not 1 <= secs <= 24 * 3600:
                return {"error": "a timer is 1 second to 24 hours"}
            return {"ok": True, "item": a.add_timer(secs, str(req.get("label") or "")[:40])}
        if act_ in ("alarm", "reminder"):
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(req.get("at") or ""))
            if not m:
                return {"error": "pick a time"}
            h, mm = int(m.group(1)), int(m.group(2))
            days = [int(d) for d in req.get("days") or [] if 0 <= int(d) <= 6]
            now = datetime.now()
            if req.get("date"):
                day = datetime.strptime(req["date"], "%Y-%m-%d")
                due = day.replace(hour=h, minute=mm)
                if due <= now:
                    return {"error": "that time has already passed"}
            elif days:
                due = when.next_on_days(h, mm, days, now)
            else:
                due = when.resolve_clock(h, mm, "24", now)
            if act_ == "alarm":
                return {"ok": True, "item": a.add_alarm(due.timestamp(), str(req.get("label") or "")[:40], days or None)}
            text = str(req.get("text") or "").strip()
            if not text:
                return {"error": "what's the reminder?"}
            return {"ok": True, "item": a.add_reminder(due.timestamp(), text[:120], days or None)}
        if act_ == "say":
            from reedus_companion.assist.commands import handle
            out = handle(str(req.get("text") or ""))
            return {"reply": out or "That isn't something the assistant layer handles directly; Reedus will answer it."}
        if act_ == "list.add":
            key, added = a.list_add(str(req.get("list") or "shopping"), [str(req.get("text") or "")])
            return {"ok": True, "list": key, "added": added}
        if act_ == "list.remove":
            key, got = a.list_remove(str(req.get("list") or "shopping"), str(req.get("text") or ""))
            return {"ok": bool(got), "list": key}
        if act_ == "list.clear":
            key, n = a.list_clear(str(req.get("list") or "shopping"))
            return {"ok": True, "list": key, "cleared": n}
        if act_ == "settings":
            patch = {k: v for k, v in req.items() if k in ("glance", "units")}
            if "glance" in patch:
                g = patch["glance"]
                if "every" in g:
                    g["every"] = max(30, min(3600, int(g["every"])))
                if "cards" in g:
                    g["cards"] = [c for c in g["cards"] if c in ("clock", "weather", "event", "music")]
            a.set_settings(patch)
            _resync_lite()
            return {"ok": True}
        if act_ == "calendar.add":
            from reedus_companion.assist import briefing
            url = str(req.get("url") or "").strip()
            if not re.match(r"^(?:https?|webcals?)://", url, re.I):
                return {"error": "paste the calendar's iCal link (starts with https:// or webcal://)"}
            test = briefing.calendars().test(url)        # refuse a link that isn't a calendar
            cal = {"id": uuid.uuid4().hex[:8], "name": str(req.get("name") or "Calendar")[:40],
                   "url": url, "enabled": True}
            a.set_settings({"calendars": (a.settings.get("calendars") or []) + [cal]})
            return {"ok": True, "id": cal["id"], **test}
        if act_ == "calendar.remove":
            cid = str(req.get("id") or "")
            a.set_settings({"calendars": [c for c in a.settings.get("calendars") or [] if c.get("id") != cid]})
            return {"ok": True}
        if act_ == "calendar.test":
            from reedus_companion.assist import briefing
            cal = next((c for c in a.settings.get("calendars") or [] if c.get("id") == req.get("id")), None)
            if cal is None:
                return {"error": "no such calendar"}
            return {"ok": True, **briefing.calendars().test(cal["url"])}
        if act_ == "glance.show":
            from reedus_companion.lite import glance
            d = glance._one
            if d is None or not d.hub.connected:
                return {"error": "Lite isn't connected"}
            card = d.build(str(req.get("card") or "clock"))
            if card is None:
                return {"error": "nothing to show on that card right now (no data)"}
            d.show(card, glance.CARD_HOLD.get(card["card"], 6.0))
            return {"ok": True, "card": card}
        return {"error": f"unknown action {act_!r}"}
    except Exception as e:                                       # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:300]}


def _resync_lite() -> None:
    try:
        from reedus_companion.lite import glance
        if glance._one is not None:
            glance._one._last_clock = 0.0             # 12/24 h may have changed
            glance._one._kick.set()
    except Exception:                                            # noqa: BLE001
        pass


def snapshot_light() -> dict:
    """The bit the status push carries every few seconds: what's running and ringing."""
    a = assistant()
    return {"active": [{"id": i["id"], "kind": i["kind"], "label": i.get("label", ""),
                        "left": round(a.left(i), 1), "state": i["state"]} for i in a.active()][:8],
            "ringing": [{"id": i["id"], "kind": i["kind"], "label": i.get("label", "")} for i in a.ringing()],
            "at": time.time()}
