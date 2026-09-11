#!/usr/bin/env python3
"""user stance / everyday opinion must skip the corpus."""
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


def must_skip_corpus(msg: str) -> bool:
    """What reply() uses for `_skip_corpus` on the personal branch."""
    return C._is_personal_turn(msg) or C._is_checkin(msg)


SKIP = [
    # Live kebab failure + close cousins
    "Well for sure, I feel like kebabs are pretty good.",
    "I feel like kebabs are pretty good",
    "I like kebabs",
    "kebabs are pretty good",
    "pizza is better than burgers",
    "my favorite food is tacos",
    "I like rainy days",
    "nah that movie was mid",
    "that show was so peak",
    "burgers are pretty good honestly",
    # Reedus-directed (already personal)
    "What's your favorite food?",
    "If you had to pick just one food, what would you pick?",
    "do you like kebabs?",
    # discourse repair, an interrupt apology was still paying subjects
    "Sorry, I didn't mean to interrupt, do you continue?",
    "sorry for interrupting, please continue",
    "my bad, go on",
    # Companion chitchat still covered
    "I'm just talking to you because it's fun",
]

# --- MUST keep corpus / lookups available ---
KEEP = [
    "been playing naruto",
    "been watching Naruto recently, it's so peak",
    "what about madara though",
    "the five kage summit arc was crazy",
    "sasuke was cold in that arc",
    "hey can you check the standings for me",
    "tell me about the Eiffel Tower",
    "when did Baldur's Gate 3 come out?",
    "what's the weather in woodbridge",
    # Live / score claims that look like opinions
    "I feel like the Lakers won last night",
    "I think the score was 3-2",
    "I think Spain won 1-0",
]


print("[1] user-stance / everyday opinion → personal → corpus skip")
for m in SKIP:
    check(must_skip_corpus(m), f"skip: {m[:64]!r}")
    check(C._is_user_stance(m) or C._is_personal_turn(m) or C._is_checkin(m),
          f"classified: {m[:48]!r}")

print("\n[2] engagement / live / titled → NOT stance-skipped")
for m in KEEP:
    check(not C._is_user_stance(m), f"not stance: {m!r}")
    if "favorite" in m.lower() or m.lower().startswith("do you"):
        continue
    check(not must_skip_corpus(m) or C._entity_question(m) or C._CURRENT_INFO.search(m)
          or C._SCOREISH.search(m) or C._TOPIC_CUE.search(m) or C._CANON_STRUCTURE.search(m)
          or C._STANCE_LIVE_CLAIM.search(m) or C._entity_search(m),
          f"keep-path: {m!r}")

print("\n[3] hard negatives: stance helper alone")
check(C._is_user_stance("I feel like kebabs are pretty good"), "stance: kebabs feel-like")
check(C._is_user_stance("kebabs are pretty good"), "stance: bare eval")
check(not C._is_user_stance("been playing naruto"), "not stance: engagement")
check(not C._is_user_stance("I think the score was 3-2"), "not stance: score claim")
check(not C._is_user_stance("I feel like the Lakers won last night"),
      "not stance: live win claim")
check(not C._is_user_stance("the five kage summit arc was crazy"),
      "not stance: canon structure")

print("\n[4] live turn: kebab opinion never hits corpus/subjects")


class Boom:
    def __init__(self):
        self.calls = []

    def chat(self, messages, max_tokens, use_adapter=True, temperature=None,
             on_token=None, tier=None, name="", cancel=None):
        self.calls.append(name or "?")
        if name in ("subjects", "search-router", "topic-router", "knowledge-brief"):
            raise AssertionError(f"{name} must not run on user-stance turn")
        return "Kebabs slap, for real."


class TrackingKB:
    def __init__(self):
        self.calls = 0

    def context_for(self, *a, **k):
        self.calls += 1
        raise AssertionError("corpus must not run on user-stance turn")


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


be = Boom()
r = C.Reedus(be)
r.mem = _NullMem()
r._knowledge = TrackingKB()
r._vision = False
out = r.reply("Well for sure, I feel like kebabs are pretty good.")
check(bool(out), f"got reply ({out!r})")
check(r._knowledge.calls == 0, f"corpus never called (calls={r._knowledge.calls})")
check("subjects" not in be.calls, f"subjects never called (calls={be.calls})")
check(getattr(r, "_skip_corpus", False) is True, "_skip_corpus set")

print("\n[5] discourse repair also skips corpus")
be2 = Boom()
r2 = C.Reedus(be2)
r2.mem = _NullMem()
r2._knowledge = TrackingKB()
r2._vision = False
out2 = r2.reply("Sorry, I didn't mean to interrupt, do you continue?")
check(bool(out2), f"got reply ({out2!r})")
check(r2._knowledge.calls == 0, "corpus never called on discourse repair")
check(getattr(r2, "_skip_corpus", False) is True, "_skip_corpus on discourse repair")

print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print(f"   - {f}")
    sys.exit(1)
print("ALL PASS")
