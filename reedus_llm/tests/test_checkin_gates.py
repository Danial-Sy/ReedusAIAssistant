#!/usr/bin/env python3
"""check-ins must never pay for the search router or the corpus."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reedus_companion.serve import core as C  # noqa: E402

_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


class Boom:
    """Search-router backend. Any chat() call is a gate failure."""

    def chat(self, messages, max_tokens, use_adapter=True, temperature=None, cancel=None,
             on_token=None, tier=None, name=""):
        raise AssertionError(f"search router must not run (name={name!r})")


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


def _reedus():
    r = C.Reedus(Boom())
    r.mem = _NullMem()
    return r


CHECKINS = [
    "Come on, dude.",
    "Yo, what's going on my man?",
    "yo my guy whats goin on",              # vocative in the middle
    "YO MY BUD WHATS GOIN ON",
    "yo yo buddyyy",
    "what's happening dude",
    "how's things",
    "how we doing",
    "you good?",
    "what it do",
    "what's good",
    "whats up",
    "how's it hanging",
    "how you been",
    "how are you today",
    "how was your day",
    "what's going on with you",
    "hey man what's up",
    "sup dude",
    "yo",
    "thanks",
]

NOT_CHECKINS = [
    "come on tell me about Naruto",
    "what's going on in ukraine",
    "what's going on with the world cup final",
    "whats the weather in woodbridge va today",
    "what's going on with GTA 6",
    "who won the game earlier today?",
    "look it up for me",
    "what do you see",
    "been on elden ring",
    "what day is it",
]


print("[1/4] check-ins are token-level, not a phrase list")
for m in CHECKINS:
    check(C._is_checkin(m), f"checkin: {m!r}")
    check(C._skip_search_router(m), f"  skip router: {m!r}")
    check(C._is_personal_turn(m), f"  personal (corpus skip): {m!r}")

print("\n[2/4] real asks are not check-ins")
for m in NOT_CHECKINS:
    check(not C._is_checkin(m), f"not checkin: {m!r}")
check(not C._skip_search_router("whats the weather in woodbridge va today"),
      "weather still has live-fact signals (router would run IF called)")
from reedus_companion.serve.weather import is_weather_ask
check(is_weather_ask("whats the weather in woodbridge va today"),
      "weather ask detected for Open-Meteo short-circuit")
check(not C._skip_search_router("what's going on in ukraine"),
      "news check-in with a place still runs the router")
check(not C._is_contentless("been on elden ring"),
      "elden ring still has a retrieval subject")
check(C._skip_search_router("been on elden ring"),
      "engagement statement does not force the search router")
check(not C._is_personal_turn("been on elden ring"),
      "engagement still reaches the corpus")

print("\n[3/4] the search-router model is never called on a check-in")
r = _reedus()
for m in CHECKINS:
    try:
        d = r._route_search(m)
    except AssertionError as e:
        check(False, f"router called on {m!r}: {e}")
        continue
    check(d["search"] is False and d["query"] == "" and d["why"] == "personal",
          f"route_search silent: {m!r} ({d})")
    check(C._corpus_verdict(d, date_q=False, looked=False, look_failed=False,
                            conv_turn=True, weather_fact=False,
                            user_msg=m) == C.CORPUS_NO,
          f"check-in skips the corpus: {m!r}")

print("\n[4/4] a check-in after a factual thread still does not route")
r = _reedus()
r.history = [
    {"role": "user", "content": "who won the world cup this year"},
    {"role": "assistant", "content": "Spain won the 2026 World Cup, beating Argentina 1-0."},
]
r.last_search_query = "2026 world cup winner"
check(r._context_is_factual(), "world-cup thread is factual context")
check(not r._continues_factual_topic("Yo, what's going on my man?"),
      "check-in is not a continuation of the match")
check(not r._continues_factual_topic("what's good"),
      "'good' must not count as continuing a scoreline")
try:
    d = r._route_search("Yo, what's going on my man?")
    check(d["search"] is False and d["query"] == "" and d["why"] == "personal",
          f"check-in after a search thread still skips the router ({d})")
except AssertionError as e:
    check(False, f"router called after factual thread: {e}")
check(r._continues_factual_topic("thats real spain was better"),
      "match reaction still continues the factual thread")

print("\n" + ("ALL PASS" if not _fails else f"{len(_fails)} FAILED"))
sys.exit(0 if not _fails else 1)
