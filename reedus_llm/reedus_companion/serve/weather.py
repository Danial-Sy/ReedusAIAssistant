"""weather via open-meteo, no api key. bypasses the search router entirely."""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Optional, Tuple

_WMO = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}

_CITY_IN_QUERY = re.compile(
    r"\b(?:in|for|near|around)\s+([A-Z][\w'.]*(?:\s+[A-Z][\w'.]*){0,3})"
    r"|\b([A-Z][\w']+(?:\s+[A-Z][\w']+){0,2},\s*[A-Z]{2})\b"
)

_WEATHER_ASK = re.compile(
    r"\b(weather|forecast)\b"
    r"|\btemperature\b"
    r"|\btemp(?:erature)?\s+outside\b"
    r"|\bhow\s+(?:hot|cold|warm)\s+(?:is\s+it|out(?:side)?)\b"
    r"|\bis\s+it\s+(?:gonna|going\s+to)\s+(?:rain|snow)\b"
    r"|\bwhether\b.{0,48}\b(?:today|tomorrow|outside|tonight|"
    r"woodbridge|virginia|degrees?|hot|cold|rain|humid)\b"
    r"|\b(?:today|tomorrow|outside|tonight|woodbridge|virginia).{0,48}\bwhether\b",
    re.I)


def is_weather_ask(msg: str) -> bool:
    """This turn should use the weather API and skip the search router."""
    if not weather_enabled() or not (msg or "").strip():
        return False
    return bool(_WEATHER_ASK.search(msg))


def weather_enabled() -> bool:
    return os.environ.get("REEDUS_WEATHER", "1") != "0"


def _http_json(url: str, timeout: float = 6.0) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Reedus/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


_geo_cache: dict[str, Tuple[float, float, str]] = {}


def resolve_place(hint: str = "") -> Optional[Tuple[float, float, str]]:
    """(lat, lon, display_name). Explicit hint first, else env defaults."""
    hint = (hint or "").strip()
    lat_e = os.environ.get("REEDUS_LAT", "").strip()
    lon_e = os.environ.get("REEDUS_LON", "").strip()
    city_e = os.environ.get("REEDUS_CITY", "").strip()

    query = hint or city_e
    if not query and lat_e and lon_e:
        try:
            return float(lat_e), float(lon_e), f"{lat_e},{lon_e}"
        except ValueError:
            return None
    if not query:
        return None

    key = query.lower()
    if key in _geo_cache:
        return _geo_cache[key]

    url = ("https://geocoding-api.open-meteo.com/v1/search?"
           + urllib.parse.urlencode({"name": query, "count": 1, "language": "en",
                                     "format": "json"}))
    data = _http_json(url)
    results = (data or {}).get("results") or []
    if not results:
        # geocode failed, fall back to env lat/lon
        if lat_e and lon_e and not hint:
            try:
                return float(lat_e), float(lon_e), city_e or f"{lat_e},{lon_e}"
            except ValueError:
                return None
        return None
    r = results[0]
    lat, lon = float(r["latitude"]), float(r["longitude"])
    parts = [r.get("name") or query]
    if r.get("admin1"):
        parts.append(r["admin1"])
    if r.get("country_code"):
        parts.append(r["country_code"])
    display = ", ".join(parts)
    _geo_cache[key] = (lat, lon, display)
    return _geo_cache[key]


def place_hint_from_message(user_msg: str) -> str:
    """An explicit place from the user message, else '' for home."""
    t = user_msg or ""
    m = _CITY_IN_QUERY.search(t)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").strip()


def fetch_weather_fact(user_msg: str = "") -> Optional[str]:
    """One clean spoken sentence, or None if the weather path is unavailable."""
    if not weather_enabled():
        return None
    hint = place_hint_from_message(user_msg)
    place = resolve_place(hint)
    if place is None:
        return None
    lat, lon, display = place
    url = ("https://api.open-meteo.com/v1/forecast?"
           + urllib.parse.urlencode({
               "latitude": lat,
               "longitude": lon,
               "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                          "weather_code,wind_speed_10m",
               "temperature_unit": "fahrenheit",
               "wind_speed_unit": "mph",
               "timezone": "auto",
           }))
    data = _http_json(url)
    cur = (data or {}).get("current")
    if not cur:
        return None
    try:
        temp = int(round(float(cur["temperature_2m"])))
        feels = int(round(float(cur.get("apparent_temperature", temp))))
        wind = int(round(float(cur.get("wind_speed_10m", 0))))
        code = int(cur.get("weather_code", 0))
        cond = _WMO.get(code, "unknown conditions")
    except (KeyError, TypeError, ValueError):
        return None
    # digits are intentional, the grounding digit check wants them
    if feels != temp:
        return (f"Right now in {display} it is {cond}, {temp} degrees Fahrenheit "
                f"(feels like {feels}), wind around {wind} mph.")
    return (f"Right now in {display} it is {cond}, {temp} degrees Fahrenheit, "
            f"wind around {wind} mph.")
