"""the idle weather card, per account: where (a place typed in the app), in F or C.

Open-Meteo, no key. the S3 draws the card (firmware glance.cpp, card "weather"); this only finds
the numbers. also used to answer "what's the weather" without a web search.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from typing import Optional

_lock = threading.Lock()
_geo: dict = {}                 # place text -> (lat, lon, display) or None
_now: dict = {}                 # (lat, lon, unit) -> (at, data)
_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def _json(url: str) -> Optional[dict]:
    from reedus_companion.serve.weather import _http_json
    return _http_json(url, timeout=6.0)


def geocode(place: str) -> Optional[tuple]:
    """'Blacksburg, VA' -> (lat, lon, 'Blacksburg, Virginia'). Open-Meteo's search only matches
    the town name, so the part after the comma picks between towns with the same name."""
    place = (place or "").strip()
    if not place:
        return None
    key = place.lower()
    with _lock:
        if key in _geo:
            return _geo[key]
    name, _, hint = place.partition(",")
    hint = hint.strip()
    hint_full = _STATES.get(hint.upper(), hint).lower()
    data = _json("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": name.strip(), "count": 10, "language": "en", "format": "json"}))
    rows = (data or {}).get("results") or []
    pick = None
    if hint_full:
        for r in rows:
            fields = [str(r.get(k) or "").lower() for k in ("admin1", "country", "country_code", "admin2")]
            if any(hint_full == f or (len(hint_full) > 2 and hint_full in f) for f in fields):
                pick = r
                break
    if pick is None and rows:
        pick = rows[0]
    out = None
    if pick is not None:
        disp = ", ".join(x for x in (pick.get("name"), pick.get("admin1")) if x)
        out = (float(pick["latitude"]), float(pick["longitude"]), disp)
    with _lock:
        _geo[key] = out
    return out


def current(place: str, unit: str = "F", max_age: float = 600.0) -> Optional[dict]:
    """{temp, icon, unit, cond, high, low, place} for the card, cached 10 minutes."""
    from reedus_companion.serve.weather import _WMO, icon_for
    g = geocode(place)
    if g is None:
        return None
    lat, lon, disp = g
    unit = "C" if str(unit).upper().startswith("C") else "F"
    k = (round(lat, 3), round(lon, 3), unit)
    with _lock:
        hit = _now.get(k)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    data = _json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "current": "temperature_2m,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min", "forecast_days": 1,
        "temperature_unit": "celsius" if unit == "C" else "fahrenheit", "timezone": "auto"}))
    cur = (data or {}).get("current")
    if not cur:
        return hit[1] if hit else None
    try:
        code = int(cur.get("weather_code", 0))
        daily = (data or {}).get("daily") or {}
        out = {"temp": int(round(float(cur["temperature_2m"]))), "unit": unit,
               "icon": icon_for(code, bool(cur.get("is_day", 1))),
               "cond": _WMO.get(code, "unknown conditions"), "place": disp,
               "high": int(round(float(daily["temperature_2m_max"][0]))) if daily.get("temperature_2m_max") else None,
               "low": int(round(float(daily["temperature_2m_min"][0]))) if daily.get("temperature_2m_min") else None}
    except (KeyError, TypeError, ValueError, IndexError):
        return hit[1] if hit else None
    with _lock:
        _now[k] = (time.time(), out)
    return out


def spoken(place: str, unit: str = "F") -> str:
    """One line for the brain's prompt, so "what's the weather" needs no web search."""
    w = current(place, unit)
    if not w:
        return ""
    hl = f", high {w['high']} and low {w['low']} today" if w.get("high") is not None else ""
    deg = "Celsius" if w["unit"] == "C" else "Fahrenheit"
    return f"Weather right now in {w['place']}: {w['cond']}, {w['temp']} degrees {deg}{hl}."
