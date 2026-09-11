#!/usr/bin/env python3
"""corpus gate regressions: arithmetic skips, unresolved non-target turns probe."""
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


class StubBackend:
    def chat(self, messages, max_tokens, use_adapter=True, temperature=None, cancel=None,
             on_token=None, tier=None, name=""):
        return "Sure."


class BoomBackend:
    def chat(self, *args, **kwargs):
        raise AssertionError("arithmetic fast path must not call a model")


class NullMemory:
    embedder = None

    def read_bulletin(self, *_args, **_kwargs): return ""
    def recall(self, *_args, **_kwargs): return ""
    def add_turn(self, *_args, **_kwargs): return None
    def close(self, *_args, **_kwargs): return None


class FakeKB:
    def __init__(self, score: float):
        self.score = score
        self.probes: list[tuple[str, str]] = []
        self.context_calls = 0
        self.last_weak = False
        self.last_topics = ["Sage Mode"]

    def probe(self, query: str, context: str = "") -> float:
        self.probes.append((query, context))
        return self.score

    def context_for(self, query: str, context: str = "", hint: str = "") -> str:
        self.context_calls += 1
        return "[Sage Mode] grounded passage"


def make(score: float) -> tuple[C.Reedus, FakeKB, list[str]]:
    reedus = C.Reedus(StubBackend())
    kb = FakeKB(score)
    reedus._knowledge = kb
    reedus._skip_corpus = False
    reedus._corpus_unsure = True
    reedus._learned_topic = ""
    reedus._last_anchor = ""
    statuses: list[str] = []
    reedus.on_status = statuses.append
    return reedus, kb, statuses


print("[1/5] arithmetic is a closed-form hard skip")
for message in [
    "What's two plus two, bro?",
    "I'm not catching up. Just want to know what two plus two.",
    "what is 17 times 23",
    "can you work out 144 divided by 12",
    "what is the square root of 81",
    "7*8",
]:
    check(C._is_arithmetic_turn(message), f"arithmetic detected: {message!r}")
    check(C._is_pure_arithmetic_turn(message), f"pure arithmetic: {message!r}")
    decision = {"search": False, "query": "", "why": C.R_STATEMENT}
    verdict = C._corpus_verdict(
        decision, date_q=False, looked=False, look_failed=False,
        conv_turn=False, weather_fact=False, user_msg=message,
    )
    check(verdict == C.CORPUS_NO, f"corpus skipped: {message!r}")

for message in [
    "been playing naruto ultimate ninja storm 4",
    "tell me about Apollo 11",
    "what happened in World War 2",
    "over 2 years I played Naruto Ultimate Ninja Storm 4",
    "the five kage summit arc was crazy",
]:
    check(not C._is_arithmetic_turn(message), f"not arithmetic: {message!r}")
check(C._corpus_has_target("whats sage mode"),
      "ASR-style 'whats' still preserves a real named subject")
mixed = "tell me 2 plus 2 and search Naruto"
check(C._is_arithmetic_turn(mixed) and not C._is_pure_arithmetic_turn(mixed),
      "a second lookup instruction is not swallowed as pure arithmetic")
check(C._arithmetic_answer(mixed) is None and not C._skip_search_router(mixed),
      "mixed arithmetic/search request still reaches routing")


print("\n[2/5] conversational preamble cannot authorize retrieval")
live_regression = "I'm not catching up. Just want to know what two plus two."
check(not C._corpus_has_target(live_regression),
      "rare `catching` lead-in is outside the ask clause")
r1, kb1, statuses1 = make(0.10)
out1 = r1._knowledge_for(live_regression)
check(len(kb1.probes) == 1, "ambiguous turn reaches the evidence probe")
check(kb1.context_calls == 0, "low evidence never starts full corpus retrieval")
check(out1 == "", "low-evidence turn injects no corpus text")
check(statuses1 == [], "low-evidence turn never speaks a retrieval acknowledgement")

r_social = C.Reedus(StubBackend())
r_social.mem = NullMemory()
kb_social = FakeKB(1.0)
r_social._knowledge = kb_social
social_statuses: list[str] = []
r_social.on_status = social_statuses.append
_ = r_social.reply("Come on, dude.")
check(r_social._skip_corpus is True, "live social interjection hard-skips the corpus")
check(kb_social.probes == [] and kb_social.context_calls == 0,
      "social interjection performs neither probe nor recall")
check(social_statuses == [], "social interjection speaks no retrieval acknowledgement")


print("\n[3/5] a strong probe still permits real corpus retrieval")
r2, kb2, statuses2 = make(0.90)
out2 = r2._knowledge_for("the five kage summit arc was crazy")
check(len(kb2.probes) == 1, "real ambiguous subject is probed")
check(kb2.context_calls == 1, "strong evidence starts full retrieval")
check(out2.startswith("[Sage Mode]"), "grounded corpus block is returned")
check(len(statuses2) == 1, "retrieval acknowledgement is preserved for real retrieval")


print("\n[4/5] certain corpus turns do not pay for the probe")
r3, kb3, _statuses3 = make(0.01)
r3._corpus_unsure = False
out3 = r3._knowledge_for("what about madara though")
check(kb3.probes == [], "CORPUS_YES path bypasses the uncertainty probe")
check(kb3.context_calls == 1, "CORPUS_YES still retrieves")
check(bool(out3), "certain corpus turn returns context")


print("\n[5/5] live arithmetic turns call no model, router, probe, or recall")
for message, expected in [
    (live_regression, "4"),
    ("What's four plus four?", "8"),
]:
    r4 = C.Reedus(BoomBackend())
    r4.mem = NullMemory()
    kb4 = FakeKB(1.0)
    r4._knowledge = kb4
    statuses4: list[str] = []
    r4.on_status = statuses4.append
    answer4 = r4.reply(message)
    check(expected in answer4, f"fast arithmetic answer is correct: {message!r} -> {answer4!r}")
    check(r4._skip_corpus is True, "reply marks the corpus as skipped")
    check(kb4.probes == [] and kb4.context_calls == 0, "neither probe nor recall is called")
    check(statuses4 == [], "no retrieval acknowledgement is spoken")
    decision4 = r4._route_search(message)
    check(decision4["search"] is False and decision4["why"] == "arithmetic",
          "search router is deterministically bypassed")
    check(C._skip_search_router(message), "cheap router gate also recognizes arithmetic")


print("\n" + ("ALL PASS" if not _fails else f"{len(_fails)} FAILED"))
if _fails:
    for failure in _fails:
        print("  -", failure)
sys.exit(0 if not _fails else 1)
