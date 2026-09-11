#!/usr/bin/env python3
"""Companion chitchat must not pay for corpus retrieval."""
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


class BoomSubjects:
    """Any subjects/router/persona call is a gate failure for chitchat turns."""

    def __init__(self):
        self.calls: list[str] = []

    def chat(self, messages, max_tokens, use_adapter=True, temperature=None,
             on_token=None, tier=None, name="", cancel=None):
        self.calls.append(name or "?")
        if name in ("subjects", "search-router", "topic-router"):
            raise AssertionError(f"{name} must not run on companion chitchat")
        return "Ha. Same, talking to you is the fun part."


class TrackingKB:
    """Corpus must not be consulted on companion chitchat."""

    def __init__(self):
        self.calls = 0

    def context_for(self, *a, **k):
        self.calls += 1
        raise AssertionError("corpus context_for must not run on companion chitchat")


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


CHITCHAT = [
    "Not in particular, I'm just talking to you because I think it's fun to talk to you, my guy.",
    "just chilling talking to you",
    "I like talking to you man",
    "I'm just here to talk to you",
    "talking to you is fun",
]

# must still reach the corpus (engagement / character / title talk)
KEEP_CORPUS = [
    "been playing naruto",
    "what about madara though",
    "been on elden ring",
    "the five kage summit arc was crazy",
]

# must NOT be swallowed as chitchat (lookups / asks)
NOT_CHITCHAT = [
    "hey can you check the standings for me",
    "tell me about the Eiffel Tower",
    "you watch naruto?",
]


print("[1/4] companion chitchat is personal → corpus skip")
for m in CHITCHAT:
    check(C._is_personal_turn(m), f"personal: {m[:56]!r}")
    check(not C._is_checkin(m) or C._is_personal_turn(m),
          f"not relying on check-in alone: {m[:40]!r}")
    # Structural: no encyclopedia subject in the utterance
    check(not C._entity_search(m), f"no entity: {m[:40]!r}")

print("\n[2/4] encyclopedia engagement still NOT personal (corpus allowed)")
for m in KEEP_CORPUS:
    check(not C._is_personal_turn(m), f"not personal: {m!r}")
    check(not C._is_checkin(m), f"not checkin: {m!r}")

print("\n[2b/4] real asks are not companion chitchat")
for m in NOT_CHITCHAT:
    check(not C._is_personal_turn(m) or C._entity_question(m) or C._OPINIONY.search(m),
          f"not chitchat-personal: {m!r} (personal={C._is_personal_turn(m)})")
check(not C._is_personal_turn("hey can you check the standings for me"),
      "standings ask is not personal")

print("\n[3/4] live turn never hits corpus or subjects")
XEON = CHITCHAT[0]
be = BoomSubjects()
r = C.Reedus(be)
r.mem = _NullMem()
r._knowledge = TrackingKB()
r._vision = False
out = r.reply(XEON)
check(bool(out), f"got a reply ({out!r})")
check(r._knowledge.calls == 0, f"corpus never called (calls={r._knowledge.calls})")
check("subjects" not in be.calls, f"subjects never called (calls={be.calls})")
check(getattr(r, "_skip_corpus", False) is True, "_skip_corpus set for the turn")
# persona may run, that's the whole point of a chitchat turn
check("persona" in be.calls or any(c == "persona" for c in be.calls),
      f"persona still answers (calls={be.calls})")

print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("ALL PASS")
