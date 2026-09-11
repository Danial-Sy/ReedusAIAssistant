#!/usr/bin/env python3
"""persona latency: sticky knowledge, chunked history, stable volatile slots."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reedus_companion.serve import core as C                          # noqa: E402
from reedus_companion.serve import persona as P                       # noqa: E402
from reedus_companion.knowledge import retrieve as R                  # noqa: E402

_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


class StubBackend:
    def chat(self, messages, max_tokens, use_adapter=True, temperature=None, cancel=None,
             on_token=None, tier=None, name=""):
        return "Sure."


class FakeKB:
    def __init__(self, weak: bool = False):
        self.calls = 0
        self.last_topics = ["Naruto: Ultimate Ninja Storm"]
        self.last_weak = weak
        self._weak = weak

    def context_for(self, q, context="", hint=""):
        self.calls += 1
        self.last_weak = self._weak
        return f"[Naruto] passage call#{self.calls} q={q[:30]!r}"


def make():
    r = C.Reedus(StubBackend())
    r.mem.recall = lambda *_a, **_k: ""
    r.mem.read_bulletin = lambda *_a, **_k: ""
    r.topics["Naruto: Ultimate Ninja Storm"] = "a fighting game"
    return r


# --- history window ---
print("[1/5] chunked history window")
hist = [{"role": "user", "content": f"m{i}"} for i in range(20)]
w12 = C._history_window(hist[:12], capacity=12, chunk=4)
check(len(w12) == 12 and w12[0]["content"] == "m0", "n<=capacity keeps all from the start")
w13 = C._history_window(hist[:13], capacity=12, chunk=4)
check(len(w13) == 13 and w13[0]["content"] == "m0",
      "excess<chunk keeps early messages (no one-message slide)")
w16 = C._history_window(hist[:16], capacity=12, chunk=4)
check(len(w16) == 12 and w16[0]["content"] == "m4",
      "excess==chunk drops exactly one chunk")
w17 = C._history_window(hist[:17], capacity=12, chunk=4)
check(w17[0]["content"] == "m4", "first message stays stable until next chunk boundary")
w20 = C._history_window(hist[:20], capacity=12, chunk=4)
check(w20[0]["content"] == "m8", "second chunk drop advances start by 8")
plain13 = hist[:13][-12:]
check(plain13[0]["content"] == "m1", "sanity: plain slide WOULD move first msg at n=13")

# --- sticky knowledge ---
print("\n[2/6] sticky knowledge + session lock")
r = make()
kb = FakeKB(weak=False)
r._knowledge = kb
r._skip_corpus = False
r._learned_topic = "Naruto: Ultimate Ninja Storm"
r._update_session_subject("Naruto: Ultimate Ninja Storm")
a = r._knowledge_for("the five kage summit was wild")
check(kb.calls == 1, f"first turn hits corpus (calls={kb.calls})")
check(a.startswith("[Naruto] passage call#1"), "first inject returned")
check(r._sticky_subject == "naruto: ultimate ninja storm", f"sticky subject set ({r._sticky_subject!r})")
check(r._session_subject == "naruto: ultimate ninja storm", "session locked to engagement")
check(not r._sticky_weak, "strong inject is not marked weak")

b = r._knowledge_for("sasuke was cold in that arc")
check(kb.calls == 1, f"same subject reuses sticky, no second corpus call (calls={kb.calls})")
check(b == a, "sticky returns identical bytes")
check(r._knowledge_weak is False, "reuse clears weak flag")

r._learned_topic = ""
r._last_anchor = "Five Kage Summit Arc"
kb.last_topics = ["Five Kage Summit Arc"]
b2 = r._knowledge_for("sasuke was cold as hell in that arc")
check(kb.calls == 1, f"arc anchor does not rekey sticky (calls={kb.calls})")
check(b2 == a, "session lock still returns engagement brief")
check(r._session_subject == "naruto: ultimate ninja storm", "session unchanged by arc chatter")

r._last_anchor = "Edo Madara"
b3 = r._knowledge_for("what about madara though")
check(kb.calls == 1, f"character pivot keeps sticky (calls={kb.calls})")
check(b3 == a, "madara follow-up reuses engagement brief")

# new titled engagement retargets session and refreshes
r._update_session_subject("Elden Ring")
r._learned_topic = "Elden Ring"
r._last_anchor = "Elden Ring"
kb.last_topics = ["Elden Ring"]
check(r._sticky_knowledge == "", "engagement pivot clears sticky bytes")
c = r._knowledge_for("elden ring bosses are hard")
check(kb.calls == 2, f"new engagement refreshes corpus (calls={kb.calls})")
check(c != a, "new inject differs from sticky naruto block")
check(r._session_subject == "elden ring", f"session retargeted ({r._session_subject!r})")

# weak hits must not enter the persona prompt: they bust the prefix cache and license fabrication
print("\n[3/6] weak hits inject nothing")
r2 = make()
kb2 = FakeKB(weak=True)
r2._knowledge = kb2
r2._skip_corpus = False
r2._learned_topic = "Naruto: Ultimate Ninja Storm"
r2._update_session_subject("Naruto: Ultimate Ninja Storm")
w1 = r2._knowledge_for("naruto talk")
check(w1 == "", "weak inject discarded")
check(r2._knowledge_weak, "turn still flagged ungrounded")
check(not r2._sticky_knowledge, "weak junk is not sticky-stored")
w2 = r2._knowledge_for("more naruto talk")
check(kb2.calls == 2, f"no sticky of weak junk so retrieve runs again (calls={kb2.calls})")
check(w2 == "", "still discarded while weak")

r2b = make()
kb2b = FakeKB(weak=True)
r2b._knowledge = kb2b
r2b._skip_corpus = False
r2b._learned_topic = "Naruto: Ultimate Ninja Storm"
_ = r2b._knowledge_for("naruto talk")
check(r2b._session_subject == "", "weak first hit does not lock session")
kb2b._weak = False
_ = r2b._knowledge_for("more naruto talk")
check(kb2b.calls == 2, f"unlocked refresh after weak (calls={kb2b.calls})")
check(not r2b._sticky_weak, "strong hit can store sticky")
check(r2b._session_subject == "",
      "strong refresh without engagement does not lock session")

# --- stable volatile slots ---
print("\n[4/6] always-present bulletin / weak slots + knowledge chars default")
empty = P.build_volatile_block(bulletin="", recalled="", topics="", weak_knowledge=False)
weak = P.build_volatile_block(bulletin="", recalled="", topics="[x]", weak_knowledge=True)
check(P._BULLETIN_EMPTY in empty, "empty bulletin still emits placeholder")
check(P._WEAK_GROUNDED in empty, "grounded weak-slot always present")
check(P._WEAK_UNGROUNDED in weak, "ungrounded warning still fires when weak")
check(weak.startswith(empty[: len(P._BULLETIN_EMPTY)]),
      "bulletin header shared across empty/weak builds")
# common weak prefix
check(P._WEAK_GROUNDED.startswith(P._WEAK_PREFIX)
      and P._WEAK_UNGROUNDED.startswith(P._WEAK_PREFIX),
      "grounded/ungrounded share a common prefix")
check(R.MAX_INJECT_CHARS == 900, f"default MAX_INJECT_CHARS is 900 (got {R.MAX_INJECT_CHARS})")
check(C.PERSONA_SPLIT is True, "PERSONA_SPLIT defaults on")

print("\n[5/6] session subject helper is a no-op on same title")
r3 = make()
r3._update_session_subject("Elden Ring")
r3._sticky_knowledge = "keep-me"
r3._sticky_subject = "elden ring"
r3._update_session_subject("Elden Ring")
check(r3._sticky_knowledge == "keep-me", "same-title update does not clear sticky")
r3._update_session_subject("  ")
check(r3._session_subject == "elden ring", "blank topic leaves session alone")
r3._update_session_subject("sometimes")
check(r3._session_subject == "elden ring", "junk adverb does not retarget session")
check(r3._sticky_knowledge == "keep-me", "junk update does not clear sticky")
check(C.WEAK_STICKY_CHARS == 320, f"weak sticky default cap 320 (got {C.WEAK_STICKY_CHARS})")

# weak hits are discarded entirely (persona cache + fabrication)
print("\n[5b/6] weak inject discarded, not capped-and-kept")
r4 = make()
kb4 = FakeKB(weak=True)
kb4.context_for = lambda q, context="", hint="": ("[x] " + ("word " * 200))
r4._knowledge = kb4
r4._skip_corpus = False
r4._update_session_subject("Breaking Bad")
r4._learned_topic = "Breaking Bad"
w = r4._knowledge_for("been watching breaking bad")
check(w == "", f"weak inject discarded (len={len(w)})")
check(r4._knowledge_weak, "still marked ungrounded")


# --- live-fact demotion ---
print("\n[6/6] encyclopedia chat is not a live-fact web search")
for m, q in [
    ("been playing naruto ultimate ninja storm 4", "Naruto Ultimate Ninja Storm 4 details"),
    ("what about madara though", "what is madara uchiha's role in the five kage summit arc"),
    ("the five kage summit arc was crazy", ""),
]:
    check(not C._is_live_fact_search(m, q), f"corpus path: {m!r}")
for m in ["whats the weather tomorrow", "who won the match last night",
          "google that for me", "whats the price of bitcoin"]:
    check(C._is_live_fact_search(m, ""), f"web path: {m!r}")
check(C._skip_search_router("been playing naruto ultimate ninja storm 4"),
      "engagement title skips search router")
check(C._skip_search_router("what about madara though"),
      "character pivot skips search router")

print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("ALL PASS")
