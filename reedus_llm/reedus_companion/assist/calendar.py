"""calendars from iCal feeds: Google ("secret address in iCal format"), iCloud, Outlook, and
school feeds like Canvas. one URL, no OAuth, read-only, which is all "what's my day" needs.

stdlib only: a small RFC 5545 reader with the recurrence rules class schedules and meetings
actually use (DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY, EXDATE,
moved instances via RECURRENCE-ID).
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:                                              # pragma: no cover
    ZoneInfo = None  # type: ignore

CACHE_S = 600.0
UA = "reedus-companion/1.0"
_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
# Outlook/Exchange feeds name zones the Windows way
_WIN_TZ = {
    "eastern standard time": "America/New_York", "central standard time": "America/Chicago",
    "mountain standard time": "America/Denver", "us mountain standard time": "America/Phoenix",
    "pacific standard time": "America/Los_Angeles", "alaskan standard time": "America/Anchorage",
    "hawaiian standard time": "Pacific/Honolulu", "gmt standard time": "Europe/London",
    "w. europe standard time": "Europe/Berlin", "romance standard time": "Europe/Paris",
    "central europe standard time": "Europe/Budapest", "india standard time": "Asia/Kolkata",
    "tokyo standard time": "Asia/Tokyo", "china standard time": "Asia/Shanghai",
    "aus eastern standard time": "Australia/Sydney", "utc": "UTC",
}


def local_tz():
    return datetime.now().astimezone().tzinfo


def _zone(tzid: Optional[str]):
    if not tzid:
        return None
    name = tzid.strip().strip('"')
    if ZoneInfo is not None:
        for cand in (name, _WIN_TZ.get(name.lower(), ""), name.split("/", 1)[-1] if name.count("/") > 1 else ""):
            if not cand:
                continue
            try:
                return ZoneInfo(cand)
            except Exception:                                    # noqa: BLE001
                continue
    return None


# ---------- parsing ----------
def _lines(text: str) -> Iterator[str]:
    buf = ""
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t"):
            buf += raw[1:]
            continue
        if buf:
            yield buf
        buf = raw
    if buf:
        yield buf


def _prop(line: str) -> Tuple[str, Dict[str, str], str]:
    # NAME;PARAM=a;PARAM="b:c":VALUE  (a colon inside quotes isn't the separator)
    i, q = 0, False
    while i < len(line):
        ch = line[i]
        if ch == '"':
            q = not q
        elif ch == ":" and not q:
            break
        i += 1
    head, value = line[:i], line[i + 1:]
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _dt(value: str, params: Dict[str, str]) -> Tuple[datetime, bool]:
    """(aware datetime, all_day)."""
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        d = datetime.strptime(v[:8], "%Y%m%d")
        return d.replace(tzinfo=local_tz()), True
    utc = v.endswith("Z")
    d = datetime.strptime(v.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return d.replace(tzinfo=timezone.utc), False
    tz = _zone(params.get("TZID")) or local_tz()
    return d.replace(tzinfo=tz), False


def _duration(v: str) -> timedelta:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return timedelta(hours=1)
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse_ics(text: str) -> List[dict]:
    """VEVENTs as dicts: uid, title, location, start, end, all_day, rrule, exdates, rid, cancelled."""
    out, cur, depth = [], None, 0
    for line in _lines(text):
        name, params, value = _prop(line)
        if name == "BEGIN":
            depth += 1
            if value.upper() == "VEVENT":
                cur = {"exdates": [], "title": "", "location": ""}
            continue
        if name == "END":
            depth -= 1
            if value.upper() == "VEVENT" and cur is not None:
                if "start" in cur:
                    if "end" not in cur:
                        cur["end"] = cur["start"] + (cur.pop("dur", None) or
                                                     (timedelta(days=1) if cur.get("all_day") else timedelta(0)))
                    out.append(cur)
                cur = None
            continue
        if cur is None:
            continue
        try:
            if name == "UID":
                cur["uid"] = value.strip()
            elif name == "SUMMARY":
                cur["title"] = _unescape(value)
            elif name == "LOCATION":
                cur["location"] = _unescape(value)
            elif name == "DTSTART":
                cur["start"], cur["all_day"] = _dt(value, params)
            elif name == "DTEND":
                cur["end"], _ = _dt(value, params)
            elif name == "DURATION":
                cur["dur"] = _duration(value)
            elif name == "RRULE":
                cur["rrule"] = {k.upper(): v for k, v in (p.split("=", 1) for p in value.split(";") if "=" in p)}
            elif name == "EXDATE":
                for v in value.split(","):
                    cur["exdates"].append(_dt(v, params)[0])
            elif name == "RECURRENCE-ID":
                cur["rid"] = _dt(value, params)[0]
            elif name == "STATUS":
                cur["cancelled"] = value.strip().upper() == "CANCELLED"
        except (ValueError, IndexError):
            continue
    return out


# ---------- recurrence ----------
def _byday(spec: str) -> List[Tuple[int, int]]:
    """'MO,-1FR,2TU' -> [(0, 0), (4, -1), (1, 2)]: (weekday, ordinal or 0)."""
    out = []
    for part in spec.split(","):
        m = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", part.strip().upper())
        if m:
            out.append((_DAYS.index(m.group(2)), int(m.group(1) or 0)))
    return out


def _nth_weekday(year: int, month: int, wd: int, n: int) -> Optional[int]:
    days = [d for d in range(1, monthrange(year, month)[1] + 1) if date(year, month, d).weekday() == wd]
    if not days or n == 0:
        return None
    try:
        return days[n - 1] if n > 0 else days[n]
    except IndexError:
        return None


def expand(ev: dict, lo: datetime, hi: datetime, limit: int = 2000) -> List[datetime]:
    """Start times of `ev` that overlap [lo, hi). Wall-clock math, so DST doesn't drift classes."""
    start: datetime = ev["start"]
    length = ev["end"] - start
    rule = ev.get("rrule")
    if not rule:
        return [start] if start < hi and start + max(length, timedelta(seconds=1)) > lo else []
    tz = start.tzinfo
    wall = start.replace(tzinfo=None)
    freq = rule.get("FREQ", "WEEKLY")
    step = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT") else None
    until = None
    if rule.get("UNTIL"):
        try:
            until = _dt(rule["UNTIL"], {"TZID": getattr(tz, "key", None)} if not rule["UNTIL"].endswith("Z") else {})[0]
            if len(rule["UNTIL"]) == 8:
                until += timedelta(days=1) - timedelta(seconds=1)
        except ValueError:
            until = None
    byday = _byday(rule.get("BYDAY", ""))
    bymd = [int(x) for x in rule.get("BYMONTHDAY", "").split(",") if x.strip().lstrip("-").isdigit()]
    ex = {e.astimezone(timezone.utc).replace(second=0) for e in ev.get("exdates", [])}

    def candidates() -> Iterator[datetime]:
        k = 0
        while k < limit:
            if freq == "DAILY":
                d = wall + timedelta(days=k * step)
                if not byday or d.weekday() in [w for w, _ in byday]:
                    yield d
            elif freq == "WEEKLY":
                week0 = wall - timedelta(days=wall.weekday())
                wk = week0 + timedelta(weeks=k * step)
                days = sorted({w for w, _ in byday}) if byday else [wall.weekday()]
                for wd in days:
                    yield wk + timedelta(days=wd)
            elif freq == "MONTHLY":
                mi = wall.month - 1 + k * step
                y, mo = wall.year + mi // 12, mi % 12 + 1
                days = []
                if byday:
                    for wd, n in byday:
                        if n:
                            dd = _nth_weekday(y, mo, wd, n)
                            if dd:
                                days.append(dd)
                        else:
                            days += [d for d in range(1, monthrange(y, mo)[1] + 1) if date(y, mo, d).weekday() == wd]
                elif bymd:
                    last = monthrange(y, mo)[1]
                    days = [d if d > 0 else last + 1 + d for d in bymd]
                else:
                    days = [wall.day]
                for dd in sorted(set(days)):
                    if 1 <= dd <= monthrange(y, mo)[1]:
                        yield wall.replace(year=y, month=mo, day=dd)
            elif freq == "YEARLY":
                y = wall.year + k * step
                try:
                    yield wall.replace(year=y)
                except ValueError:                               # feb 29
                    pass
            else:
                return
            k += 1

    out, n = [], 0
    for c in candidates():
        if c < wall:
            continue
        aware = c.replace(tzinfo=tz)
        if until is not None and aware > until:
            break
        n += 1
        if count is not None and n > count:
            break
        if aware >= hi:
            break
        if aware.astimezone(timezone.utc).replace(second=0) in ex:
            continue
        if aware + max(length, timedelta(seconds=1)) > lo:
            out.append(aware)
    return out


def occurrences(events: List[dict], lo: datetime, hi: datetime, cal: str = "") -> List[dict]:
    """Every event instance overlapping [lo, hi), moved instances applied, sorted by start."""
    moved: Dict[Tuple[str, datetime], dict] = {}
    for ev in events:
        if ev.get("rid") is not None and ev.get("uid"):
            moved[(ev["uid"], ev["rid"].astimezone(timezone.utc))] = ev
    out = []
    for ev in events:
        if ev.get("rid") is not None:
            continue
        length = ev["end"] - ev["start"]
        for st in expand(ev, lo, hi):
            key = (ev.get("uid", ""), st.astimezone(timezone.utc))
            src, s0, ln = ev, st, length
            if key in moved:
                src = moved[key]
                s0, ln = src["start"], src["end"] - src["start"]
                if not (s0 < hi and s0 + ln > lo):
                    continue
            if src.get("cancelled"):
                continue
            out.append({"title": src.get("title") or "(busy)", "location": src.get("location", ""),
                        "start": s0, "end": s0 + ln, "all_day": bool(ev.get("all_day")), "cal": cal})
    # moved instances whose original slot was outside the window
    for (uid, _), ev in moved.items():
        if ev["start"] < hi and ev["end"] > lo and not ev.get("cancelled") and \
                not any(o["start"] == ev["start"] and o["title"] == ev.get("title") for o in out):
            out.append({"title": ev.get("title") or "(busy)", "location": ev.get("location", ""),
                        "start": ev["start"], "end": ev["end"], "all_day": bool(ev.get("all_day")), "cal": cal})
    return sorted(out, key=lambda o: (not o["all_day"], o["start"]))


# ---------- feeds ----------
def fetch(url: str, timeout: float = 12.0) -> str:
    u = re.sub(r"^webcals?://", "https://", url.strip(), flags=re.I)
    req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "text/calendar, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(8 * 1024 * 1024)
    text = raw.decode("utf-8", "replace")
    if "BEGIN:VCALENDAR" not in text[:4096].upper():
        raise ValueError("that link didn't return a calendar (no BEGIN:VCALENDAR)")
    return text


class Calendars:
    """The feeds in settings["calendars"], cached. events() never raises."""

    def __init__(self, get_feeds, fetcher=fetch):
        self.get_feeds = get_feeds                   # () -> [{id, name, url, enabled}]
        self.fetcher = fetcher
        self.cache: Dict[str, Tuple[float, List[dict]]] = {}
        self.errors: Dict[str, str] = {}
        self.lock = threading.Lock()

    def configured(self) -> bool:
        return any(f.get("enabled", True) and f.get("url") for f in self.get_feeds())

    def _events(self, feed: dict, force: bool = False) -> List[dict]:
        url = feed["url"]
        with self.lock:
            hit = self.cache.get(url)
        if hit and not force and time.time() - hit[0] < CACHE_S:
            return hit[1]
        try:
            evs = parse_ics(self.fetcher(url))
            with self.lock:
                self.cache[url] = (time.time(), evs)
                self.errors.pop(feed.get("id", url), None)
            return evs
        except Exception as e:                                   # noqa: BLE001
            with self.lock:
                self.errors[feed.get("id", url)] = f"{type(e).__name__}: {e}"[:200]
            return hit[1] if hit else []

    def events(self, lo: datetime, hi: datetime, force: bool = False) -> List[dict]:
        out = []
        for f in self.get_feeds():
            if not f.get("enabled", True) or not f.get("url"):
                continue
            out += occurrences(self._events(f, force), lo, hi, f.get("name") or "")
        return sorted(out, key=lambda o: (not o["all_day"], o["start"]))

    def test(self, url: str) -> dict:
        evs = parse_ics(self.fetcher(url))
        now = datetime.now().astimezone()
        upcoming = occurrences(evs, now, now + timedelta(days=14))
        return {"events": len(evs), "upcoming": len(upcoming),
                "sample": [f"{o['start'].strftime('%a %H:%M')} {o['title']}" for o in upcoming[:5]]}
