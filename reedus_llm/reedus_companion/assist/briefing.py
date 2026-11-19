"""'what's my day': calendar + reminders + alarms + to-dos (+ weather) in a few spoken lines."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import List, Optional

from reedus_companion.assist import when

_cals = None


def calendars():
    global _cals
    if _cals is None:
        from reedus_companion.assist.calendar import Calendars
        from reedus_companion.assist.store import assistant
        _cals = Calendars(lambda: assistant().settings.get("calendars") or [])
    return _cals


def _clock(dt: datetime, h24: bool) -> str:
    return when.say_clock(dt.astimezone().replace(tzinfo=None), h24)


def say_event(ev: dict, h24: bool = False, now: Optional[datetime] = None) -> str:
    title = ev["title"]
    if ev["all_day"]:
        return f"{title}, all day"
    now = now or datetime.now().astimezone()
    if ev["start"] <= now < ev["end"]:
        return f"{title}, going on now until {_clock(ev['end'], h24)}"
    loc = f" in {ev['location'].split(',')[0]}" if ev.get("location") and len(ev["location"]) < 40 else ""
    return f"{title} at {_clock(ev['start'], h24)}{loc}"


def _join(parts: List[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def day_events(day: datetime) -> List[dict]:
    lo = day.astimezone() if day.tzinfo else day.astimezone()
    lo = lo.replace(hour=0, minute=0, second=0, microsecond=0)
    return calendars().events(lo, lo + timedelta(days=1))


def next_event(now: Optional[datetime] = None, within_h: float = 18.0, kind: str = "") -> Optional[dict]:
    """The next timed event that hasn't ended (an in-progress one counts), optionally a class/meeting."""
    now = now or datetime.now().astimezone()
    evs = calendars().events(now, now + timedelta(hours=within_h))
    evs = [e for e in evs if not e["all_day"] and e["end"] > now]
    if kind:
        k = kind.lower()
        keyed = [e for e in evs if k in e["title"].lower()]
        if keyed:
            evs = keyed
    upcoming = [e for e in evs if e["start"] >= now]
    return upcoming[0] if upcoming else (evs[0] if evs else None)


def day_summary(day: datetime, now: Optional[datetime] = None, weather: bool = True,
                apps=None) -> str:
    """The spoken briefing for `day` (midnight, naive local)."""
    from reedus_companion.assist.store import assistant
    a = assistant()
    now = now or datetime.now()
    h24 = bool(a.settings.get("glance", {}).get("h24"))
    is_today = day.date() == now.date()
    label = "today" if is_today else "tomorrow" if (day.date() - now.date()).days == 1 else \
        when.WEEKDAYS[day.weekday()].capitalize()
    out: List[str] = []

    if weather and is_today:
        try:
            from reedus_companion.serve.weather import current
            w = current()
            if w:
                hi = f", high of {w['high']}" if w.get("high") is not None else ""
                out.append(f"It's {w['temp']} and {w['cond']} outside{hi}.")
        except Exception:                                        # noqa: BLE001
            pass

    cals = calendars()
    if cals.configured():
        evs = day_events(day)
        nowa = now.astimezone()
        if is_today:
            evs = [e for e in evs if e["all_day"] or e["end"] > nowa]
        if not evs:
            out.append(f"Nothing on your calendar {label if label in ('today', 'tomorrow') else 'on ' + label}"
                       f"{' for the rest of the day' if is_today and now.hour >= 12 else ''}.")
        else:
            said = [say_event(e, h24, nowa) for e in evs[:6]]
            more = f" Plus {len(evs) - 6} more." if len(evs) > 6 else ""
            n = len(evs)
            lead = f"You've got {n} thing{'s' if n != 1 else ''} {label if label in ('today', 'tomorrow') else 'on ' + label}: "
            out.append(lead + _join(said) + "." + more)
    elif apps is not None:
        got = apps(f"List my calendar events for {day.strftime('%A %B %d')} with their start times. "
                   "Answer with just the events, or say there are none.")
        if got and not got.lower().startswith(("no apps", "the apps helper failed")):
            out.append(got.rstrip(".") + ".")

    lo, hi = day.timestamp(), (day + timedelta(days=1)).timestamp()
    rems = [i for i in a.active("reminder") if lo <= i["due"] < hi]
    if rems:
        bits = [f"{r['label']} at {when.say_clock(datetime.fromtimestamp(r['due']), h24)}" for r in rems[:4]]
        out.append(("Reminders: " if len(rems) > 1 else "One reminder: ") + _join(bits) + ".")
    if is_today:
        alarms = [i for i in a.active("alarm") if i["due"] - now.timestamp() < 24 * 3600]
        if alarms:
            first = datetime.fromtimestamp(alarms[0]["due"])
            out.append(f"Your next alarm is {when.say_when(first, now, h24)}.")
    _, todo = a.list_items("to-do")
    if todo:
        if len(todo) <= 3:
            out.append(f"On your to-do list: {_join(todo)}.")
        else:
            out.append(f"You have {len(todo)} things on your to-do list, starting with {todo[0]}.")
    if not out:
        if not cals.configured():
            return ("Your day's wide open as far as I know. I can't see a calendar yet: add one in "
                    "ReedOS, Settings, Connections, Calendars, and I'll keep track of it.")
        return f"Your {label} looks wide open."
    return " ".join(out)


def greeting(now: Optional[datetime] = None) -> str:
    h = (now or datetime.now()).hour
    name = os.environ.get("REEDUS_USER_NAME", "").strip()
    who = f", {name}" if name else ""
    return ("Good morning" if h < 12 else "Good afternoon" if h < 17 else "Good evening") + who + "."
