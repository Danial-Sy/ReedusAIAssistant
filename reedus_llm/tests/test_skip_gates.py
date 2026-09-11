#!/usr/bin/env python3
"""Regression suite for the cheap-turn skip gates."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reedus_companion.serve import core as C   # noqa: E402

_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


# --- router: MUST NOT skip ---
print("[1/5] search router MUST still run, anything factual")
MUST_ROUTE = [
    "whats the score in the game",
    "who won the match last night",
    "who is the president",
    "what's the capital of France",
    "how much does a 3090 cost",
    "look it up for me",
    "google that",
    "search for the latest news",
    "can you find out when BG3 patch 8 drops",
    "what's the weather tomorrow",
    "tell me about the Eiffel Tower",
    "is GTA6 out yet",
    "what happened in the election",
    "whats todays date",
    "did the Lakers win",
    "what's the price of bitcoin right now",
    "hey can you check the standings for me",
    "i think the score was four to one",
    "what year did the Berlin Wall fall",
    "whats the current version of Python",
]
for m in MUST_ROUTE:
    check(not C._skip_search_router(m), f"routes: {m!r}")

# --- router: MUST NOT skip (eyes) ---
print("\n[2/5] search router MUST still run, anything visual (vision path must stay live)")
MUST_ROUTE_VISUAL = [
    "what do you see",
    "what you see right now",
    "Awesome, and can you tell me what you see right now?",
    "tell me what you see",
    "what is this",
    "what am I holding",
    "what am I pointing at",
    "can you see what I'm pointing at right now",
    "look at this",
    "take a look",
    "check this out",
    "what colour is this",
    "read this label",
    "whats in front of you",
    "can you see me",
    "identify this thing",
    "show me what you see",
    "point the camera over here",
]
for m in MUST_ROUTE_VISUAL:
    check(not C._skip_search_router(m), f"visual cue preserved: {m!r}")
    # and the look path itself must still classify it as a look turn
    check(bool(C._LOOK_Q.search(m) or C._LOOK_HINT.search(m)),
          f"  ...still reaches look routing: {m!r}")
for m in ("what you see right now",
          "Awesome, and can you tell me what you see right now?",
          "tell me what you see",
          "show me what you see"):
    check(bool(C._LOOK_Q.search(m)), f"LOOK_Q fast-path: {m!r}")
    check(C._wants_look(m), f"_wants_look: {m!r}")

# --- router: SAFE to skip ---
print("\n[3/5] search router safely skipped, pure conversation")
MAY_SKIP = [
    "hey whats up",
    "hi",
    "hello",
    "yo",
    "how was your day",
    "i had a rough day at work",
    "im tired",
    "thanks",
    "lol",
    "ok cool",
    "goodnight",
    "that sucks",
    "i feel kinda down today",
]
for m in MAY_SKIP[:-1]:
    check(C._skip_search_router(m), f"skips: {m!r}")
check(C._skip_search_router(MAY_SKIP[-1]),
      f"feelings + today still skippable: {MAY_SKIP[-1]!r}")

# --- draft check ---
print("\n[4/5] draft check MUST still run on anything checkable")
MUST_CHECK = [
    ("The Eiffel Tower is in Paris.", "where is it"),
    ("It was released last year.", "when did it come out"),
    ("They won 3-1.", "what was the score"),
    ("That's the largest one ever built.", "how big is it"),
    ("It was founded in Seattle.", "where did it start"),
    ("Python 3.13 is the current version.", "what version"),
    ("Not much really.", "whats the score"),          # ask was factual, verify anyway
    ("Sure, about 40 percent.", "how many"),
]
for draft, ask in MUST_CHECK:
    check(C._draft_needs_llm_check(draft, ask), f"checks draft: {draft!r}")

print("\n[5/5] draft check safely skipped, nothing checkable")
MAY_SKIP_CHECK = [
    ("Not much. just hanging out, you know what i mean", "hey whats up"),
    ("It was okay, I just went to work and came home. How about you?", "how was your day"),
    ("I'm sorry to hear that. What was the problem?", "i had a rough day at work"),
    ("haha fair enough", "lol"),
    ("yeah I get that. wanna talk about it?", "im tired"),
]
for draft, ask in MAY_SKIP_CHECK:
    check(not C._draft_needs_llm_check(draft, ask), f"skips draft: {draft!r}")

# --- kill switches ---
# --- conversation context beats message shape ---
print("\n[6/6] a casual turn INSIDE a factual thread must still route")


class _Stub:
    def chat(self, m, mx, use_adapter=True, temperature=None, on_token=None, cancel=None,
             tier=None, name=""):
        return "ok"


class _NullMem:
    embedder = None
    def read_bulletin(self, *a, **k): return ""
    def recall(self, *a, **k): return ""
    def add_turn(self, *a, **k): return None
    def close(self, *a, **k): return None


_r = C.Reedus(_Stub())
_r.mem = _NullMem()

check(not _r._context_is_factual(), "empty history is not factual context")

_r.history = [
    {"role": "user", "content": "who won the world cup this year"},
    {"role": "assistant", "content": "Spain won the 2026 World Cup, beating Argentina 1-0."},
]
check(_r._context_is_factual(), "World Cup thread reads as factual context")
check(C._skip_search_router("thats real spain was better"),
      "  ...the message ALONE still looks like chit-chat (why this regressed)")

check(_r._continues_factual_topic("thats real spain was better"),
      "a reaction to the topic -> router runs")
check(_r._continues_factual_topic("ok thats pretty neat. spain was REALLY good huh"),
      "commentary on the topic -> router runs")
check(not _r._continues_factual_topic("anywho how you doing?"),
      "a pivot to small talk -> router still skipped")
check(not _r._continues_factual_topic("Im doing ok, ive been feeling down lately"),
      "an emotional pivot -> router still skipped")

_r.history = [
    {"role": "user", "content": "i had a rough day at work"},
    {"role": "assistant", "content": "Oh no, what happened? That sounds draining."},
]
check(not _r._context_is_factual(), "an emotional thread is NOT factual context")
check(not _r._continues_factual_topic("thats real it was better"),
      "continuation words alone, with no factual thread, do not force the router")

print("\n[7/7] unsourced entity+outcome claims force verification without asking an LLM")
FORCE = [
    "Yep. And the USA? They got crushed in their group, lost to Costa Rica and Mexico.",
    "The Lakers beat the Celtics last night.",
    "Python 3.13 was released recently.",
]
for d in FORCE:
    check(bool(C._ENTITY.search(d) and C._TIME_SENSITIVE.search(d)), f"forces: {d[:46]!r}")
NO_FORCE = [
    "Not much, just hanging out.",
    "Ha, that's fair. How's your week going?",
    "I'm sorry to hear that. Work can be rough.",
]
for d in NO_FORCE:
    check(not (C._ENTITY.search(d) and C._TIME_SENSITIVE.search(d)),
          f"leaves alone: {d[:46]!r}")

# --- caps-lock is not a named entity ---
print("\n[8/8] SHOUTING must not be mistaken for an acronym")
for m in ["WHAT does that mean bro", "that shit is PEAK", "THAT WAS CRAZY",
          "bro NO WAY that happened", "OMG really", "this game is SO GOOD"]:
    check(C._entity_search(m) is None, f"not an entity: {m!r}")

# ...while real acronyms and proper nouns still are.
for m, want in [("is BG3 patch 8 out yet", "BG3"), ("who won the NBA finals", "NBA"),
                ("what's new with the PS5", "PS5"), ("did the USA win", "USA"),
                ("hows the FIFA ranking look", "FIFA"),
                ("tell me about the Eiffel Tower", "Eiffel Tower")]:
    got = C._entity_search(m)
    check(got is not None and want in got.group(0), f"still an entity: {want} in {m!r}")

check(C._skip_search_router("that shit is PEAK"),
      "a shouted adjective is skippable chit-chat, not a search")
check(not C._kw_wants_search("WHAT does that mean bro"),
      "a shouted question word no longer forces a search")

# --- verification must not vandalise a good conversation ---
print("\n[9/9] a curious reply is not a factual claim")
# live failure: a warm reply full of questions got force-verified (entity + "current"), searched,
# and replaced with regional release dates. 43 seconds for a worse answer.
GOOD = ("Aight, that's peak energy right there. Naruto: Ultimate Ninja Storm 4? That game still "
        "got the heat. What's your current grind? Boss fights or runnin through the villages?")
check(not C._draft_needs_llm_check(GOOD, "im playing naruto ultimate ninja storm 4 its so PEAK"),
      "an enthusiastic, question-heavy reply is left alone")
check("Ultimate Ninja" not in C._declarative_only(GOOD),
      "entities inside questions don't count as assertions")

for d, u in [("The Lakers beat the Celtics last night.", "who won"),
             ("Naruto Ultimate Ninja Storm 4 was released in 2016.", "when did it come out"),
             ("They won 3-1 in the final.", "whats the score"),
             ("Yep. And the USA? They got crushed in their group, lost to Costa Rica and Mexico.",
              "spain was better")]:
    check(C._draft_needs_llm_check(d, u), f"still verifies: {d[:46]!r}")

for d, u in [("That sounds rough. What happened?", "been feeling down"),
             ("Ha, no way. What got you into it?", "im playing elden ring")]:
    check(not C._draft_needs_llm_check(d, u), f"leaves alone: {d!r}")

check("\U0001F525" not in C.scrub("nice one \U0001F525 love that"), "emoji are stripped from text")

print("\n[extra] kill switches restore the old behaviour")
import os  # noqa: E402
os.environ["REEDUS_ROUTER_SKIP"] = "0"
os.environ["REEDUS_DRAFT_CHECK_SKIP"] = "0"
check(not C._skip_search_router("hey whats up"), "REEDUS_ROUTER_SKIP=0 forces the router")
check(C._draft_needs_llm_check("haha fair enough", "lol"),
      "REEDUS_DRAFT_CHECK_SKIP=0 forces the draft check")
os.environ.pop("REEDUS_ROUTER_SKIP")
os.environ.pop("REEDUS_DRAFT_CHECK_SKIP")

print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("ALL PASS")
