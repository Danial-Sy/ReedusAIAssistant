#!/usr/bin/env python3
"""weather api + speakable fact + looser wake checks. no mic, no gguf."""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_fails = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


print("[1] speakable fact filter")
from reedus_companion.serve import core as C
check(not C._fact_is_speakable(
    "Weather Today: Local Forecast & Live Weather Radar | WeatherBug"),
      "WeatherBug title rejected")
check(C._fact_is_speakable(
    "Right now in Woodbridge, Virginia it is clear, 78 degrees Fahrenheit, wind around 5 mph."),
      "clean weather sentence accepted")

print("\n[2] looser wake / stop")
from reedus_companion.voice.wake import classify_utterance, has_wake, is_stop, is_wake_only
from reedus_companion.voice.stt import canon_name
from reedus_companion.voice.session import WakeSession

for raw in ["Reedus?", "yo Reedus", "um dude Reedus", "Hey Reedus"]:
    check(has_wake(canon_name(raw)), f"wake: {raw!r}")
check(is_wake_only("Reedus?"), "Reedus? wake-only")
check(classify_utterance("Alright man goodbye")[0] == "stop", "Alright man goodbye stops")
check(is_stop("okay see ya Reedus"), "okay see ya Reedus stops")
check(not is_stop("I'm done with that arc"), "arc talk does not stop")

s = WakeSession()
ev = s.on_transcript("Reedus?")
check(ev.action == "arm" and not ev.text, f"Reedus? arms ({ev})")

print("\n[3] weather module (network optional)")
os.environ.setdefault("REEDUS_CITY", "Woodbridge, Virginia")
from reedus_companion.serve.weather import fetch_weather_fact, place_hint_from_message
check(place_hint_from_message("what's the weather") == "", "no place → home default")
check("Woodbridge" in place_hint_from_message("weather in Woodbridge Virginia")
      or "Woodbridge" in place_hint_from_message("weather in Woodbridge"),
      "explicit city extracted")
fact = fetch_weather_fact("what's the weather real quick")
if fact:
    print(f"  live: {fact}")
    check("degrees" in fact and C._fact_is_speakable(fact), "Open-Meteo fact speakable")
else:
    print("  SKIP live Open-Meteo (no network), module import OK")

print("\n[4] barge helpers reject numpy truth traps + adaptive thresh")
import numpy as np
from reedus_companion.voice.voice_loop import (
    _is_think_cancel, _has_barge_audio, _THINK_CANCEL,
)
from reedus_companion.voice import barge as B
check(_is_think_cancel(_THINK_CANCEL), "think-cancel sentinel")
arr = np.ones(32, dtype=np.float32)
check(not _is_think_cancel(arr), "numpy barge audio is not think-cancel")
check(_has_barge_audio(arr), "numpy barge audio detected")
check(not _has_barge_audio(None), "None is not barge audio")
t0 = B.barge_thresh_from_bleed(0.0, floor=0.038, mult=1.55, margin=0.012)
check(abs(t0 - 0.038) < 1e-6, f"zero bleed uses floor ({t0})")
t1 = B.barge_thresh_from_bleed(0.04, floor=0.038, mult=1.55, margin=0.012)
check(t1 > 0.07, f"bleed raises thresh above floor ({t1})")
check(t1 < 0.04 * 2.5, f"thresh stays in a usable band ({t1})")

print("\n[5] end-of-utterance hangover idea")
peak, hangover, abs_t = 0.08, 0.28, 0.012
end_thresh = max(abs_t * 0.55, peak * hangover)
soft = 0.03
quiet = 0.005
check(soft >= end_thresh, f"soft mid-speech stays in utterance (end={end_thresh:.3f})")
check(quiet < end_thresh, f"near-silence ends utterance (end={end_thresh:.3f})")

print("\n[4] weather ask detection + Open-Meteo short-circuit (no search-router)")
from reedus_companion.serve.weather import is_weather_ask, weather_enabled
check(is_weather_ask("What's the weather like today?"), "weather ask: like today")
check(is_weather_ask("whats the weather in woodbridge"), "weather ask: place")
check(is_weather_ask("how hot is it outside"), "weather ask: how hot")
check(is_weather_ask(
    "Yeah, could you tell me though whether in Woodbridge, Virginia today real quick"),
      "Whisper whether+place+today → weather")
check(not is_weather_ask("I feel like kebabs are pretty good"), "kebabs not weather")
check(not is_weather_ask("whether you want pizza or not"), "bare whether not weather")
check(weather_enabled(), "weather enabled by default")


class _NoRouter:
    """Any search-router / subjects call is a gate failure for weather short-circuit."""

    def __init__(self):
        self.calls = []

    def chat(self, messages, max_tokens, use_adapter=True, temperature=None,
             on_token=None, tier=None, name="", cancel=None):
        self.calls.append(name or "?")
        if name in ("search-router", "subjects", "topic-router"):
            raise AssertionError(f"{name} must not run when weather API short-circuits")
        # Echo a short weather reply for the persona call
        return "It's overcast, about 85 out."


class _NullMem:
    embedder = None

    def read_bulletin(self, *a, **k):
        return ""

    def recall(self, *a, **k):
        return ""

    def add_turn(self, *a, **k):
        return None

    def close(self, *a, **k):
        return None


# Mock the API so this test does not need network.
import reedus_companion.serve.weather as W
_orig = W.fetch_weather_fact
W.fetch_weather_fact = lambda msg="": (
    "Right now in Woodbridge, Virginia, US it is overcast, 85 degrees Fahrenheit, "
    "wind around 3 mph.")
try:
    be = _NoRouter()
    r = C.Reedus(be)
    r.mem = _NullMem()
    r._knowledge = False
    r._vision = False
    statuses = []
    r.on_status = statuses.append
    out = r.reply("What's the weather like today?")
    check(bool(out), f"got weather reply ({out!r})")
    check("search-router" not in be.calls, f"no search-router (calls={be.calls})")
    check(bool(statuses), f"ack before weather work: {statuses!r}")
    check(getattr(r, "last_search_query", "") == "weather-api", "tagged weather-api")
finally:
    W.fetch_weather_fact = _orig

print()
if _fails:
    print(f"{len(_fails)} FAILED")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
