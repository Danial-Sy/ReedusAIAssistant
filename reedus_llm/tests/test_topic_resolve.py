#!/usr/bin/env python3
"""topic-learn / corpus-resolve regressions from freeform naruto chat."""
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


class StubBackend:
    def __init__(self):
        self.calls: list[str] = []
        self.topic_json = '{"topic": "Sage Mode Naruto", "query": "Sage Mode Naruto anime"}'

    def chat(self, messages, max_tokens, use_adapter=True, temperature=None, cancel=None,
             on_token=None, tier=None, name=""):
        self.calls.append(name or "")
        if name == "topic-router":
            return self.topic_json
        if name == "topic-brief":
            return "Sage Mode is a thing. NOT a good brief path."
        return "yeah"


class FakeKB:
    def __init__(self):
        self.learned: list[str] = []

    def has_topic(self, t: str) -> bool:
        t = (t or "").strip().lower()
        return t in {"sage mode", "naruto", "amaterasu", "naruto uzumaki",
                     "five kage summit arc", "senjutsu"}

    def learn(self, topic: str, **k):
        self.learned.append(topic)
        return 0  # not ingested as that exact string


print("[1/6] bare 'on' must not cue topic learning")
msg = ("literally NOTHIHNG man like hes got nothing on it! its kinda crazy how insanely "
       "cracked he is. I mean the thing is sage mode naruto might have a chance, but idk "
       "if naruto could get over amaterasu")
check(not C._TOPIC_CUE.search(msg), "TOPIC_CUE misses 'nothing on it'")
check(bool(C._TOPIC_CUE.search("been watching naruto lately")),
      "real watch cue still matches")
check(bool(C._TOPIC_CUE.search("been playing storm 4")),
      "real play cue still matches")
check(bool(C._TOPIC_CUE.search("been on elden ring all week")),
      "'been on' still matches")

print("\n[2/6] franchise name must not resolve to a more-specific game topic")
be = StubBackend()
r = C.Reedus(be)
r.topics = {
    "Naruto Shippuden: Ultimate Ninja Storm 4": "a fighting game",
    "Five Kage Summit Arc": "an anime arc",
}
check(r._topic_already_known("Naruto Shippuden") is None,
      "Naruto Shippuden does NOT map onto Storm 4")
check(r._topic_already_known("naruto ultimate ninja storm 4")
      == "Naruto Shippuden: Ultimate Ninja Storm 4",
      "near-complete game title still maps to Storm 4")
check(r._topic_already_known("five kage summit") == "Five Kage Summit Arc",
      "short arc name still maps to stored arc")

print("\n[3/6] Sage Mode Naruto resolves to corpus Sage Mode")
r2 = C.Reedus(StubBackend())
r2._knowledge = FakeKB()
got = r2._corpus_resolve_topic("Sage Mode Naruto")
check(got == "Sage Mode", f"resolves compound -> Sage Mode (got {got!r})")
check(r2._corpus_resolve_topic("Amaterasu") == "Amaterasu", "exact known topic unchanged")
check(r2._corpus_resolve_topic("Totally Fake Technique XYZ") is None,
      "unknown stays unresolved")

print("\n[4/6] _maybe_learn_topic skips web brief when corpus resolves")
be3 = StubBackend()
r3 = C.Reedus(be3)
r3.mem.recall = lambda *_a, **_k: ""
r3.mem.read_bulletin = lambda *_a, **_k: ""
r3._knowledge = FakeKB()
r3.topics = {}
# Force the learn path with a real cue + compound router title
out = r3._maybe_learn_topic("i got into sage mode naruto lately")
check(out and out.lower() == "sage mode", f"learn returns resolved corpus topic (got {out!r})")
check("topic-brief" not in be3.calls, f"no topic-brief call (calls={be3.calls})")
check("topic-router" not in be3.calls,
      f"corpus already had it, topic-router skipped (calls={be3.calls})")
be4 = StubBackend()
r4 = C.Reedus(be4)
r4._knowledge = FakeKB()
out4 = r4._maybe_learn_topic(msg)
check(out4 is None, "no TOPIC_CUE -> learn returns None without router")
check(be4.calls == [], f"no model calls without cue (calls={be4.calls})")

print("\n[5/6] retrieval anchor must not poison on one word of a compound topic")
r5 = C.Reedus(StubBackend())
r5.topics = {
    "Naruto": "franchise",
    "Sage Mode Naruto": "bad compound learn",
    "Naruto Shippuden": "anime",
}
r5.history = [{"role": "user", "content": "yo"}]
q = r5._retrieval_query("yeah i been watching naruto its so fun")
check(not q.startswith("Sage Mode Naruto"),
      f"compound does not win on bare naruto (q={q!r})")
check(q.startswith("Naruto"), f"single-word Naruto can still anchor (q={q!r})")
r5.history = [{"role": "user", "content": "yo"},
              {"role": "assistant", "content": "nice"},
              {"role": "user", "content": "watching naruto"}]
q2 = r5._retrieval_query("naruto shippuden five kage summit")
check(q2.startswith("Naruto Shippuden"),
      f"longer title preferred over Naruto (q={q2!r})")

print("\n[6/6] junk topic titles rejected (session-sticky eval Persona failure)")
check(not C._is_plausible_topic_title("sometimes"), "sometimes is not a title")
check(not C._is_plausible_topic_title("always"), "always is not a title")
check(not C._is_plausible_topic_title(""), "empty is not a title")
check(C._is_plausible_topic_title("Persona 5"), "Persona 5 is a title")
check(C._topic_candidate("been watching one piece lately its so long but worth it")
      == "one piece", "One Piece keeps leading 'one'")
check(C._is_plausible_topic_title("one piece"), "one piece is a title")
check(bool(C._TOPIC_CUE.search("mementos grinding is a slog sometimes")),
      "grinding still cues (validation is the gate)")
check(not C._NEW_ENGAGE.search("mementos grinding is a slog sometimes"),
      "mid-sentence grinding is not a new engagement")
check(bool(C._NEW_ENGAGE.search("anyway been watching breaking bad instead")),
      "anyway been watching is a new engagement")
be5 = StubBackend()
be5.topic_json = '{"topic": "sometimes", "query": "sometimes"}'
r6 = C.Reedus(be5)
r6._knowledge = FakeKB()
r6._session_subject = "persona 5"
r6._sticky_subject = "persona 5"
r6._sticky_knowledge = "p5-brief"
out6 = r6._maybe_learn_topic("mementos grinding is a slog sometimes")
check(out6 is None, f"locked session ignores buried grind cue (got {out6!r})")
check(be5.calls == [], f"no topic-router when session blocks cue (calls={be5.calls})")
check(r6._session_subject == "persona 5", "session untouched after junk learn attempt")

print("\n[7/7] 'watching Naruto' skips the topic-router (one-word title already in corpus)")
be7 = StubBackend()
r7 = C.Reedus(be7)
r7._knowledge = FakeKB()
out7 = r7._maybe_learn_topic(
    "I mean, I've just been chilling and watching Naruto. It's so fun")
check(out7 == "Naruto", f"cheap corpus title (got {out7!r})")
check("topic-router" not in be7.calls, f"no router call (calls={be7.calls})")
check(C._topic_candidate(
    "I've just been chilling and watching Naruto. It's so fun", min_words=1) == "Naruto",
      "one-word candidate after watch cue")

print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("ALL PASS")
