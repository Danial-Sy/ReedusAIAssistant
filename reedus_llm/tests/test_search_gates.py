"""search/verify gating regressions."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reedus_companion.serve import core as C  # noqa: E402


ROBOT_TURNS = [
    "Yeah, I guess it's actually fair. Let's pivot. What do you want to look like? "
    "I'm planning on making a little bit of a robot, so what do you want your body to be like?",
    "No, I'm making you a body. I'm telling you what you want to be.",
    "You are a large language model that I trained. I am making you a physical body. "
    "Currently you do not have a physical body. What do you want that physical body to look like?",
    "You don't need to search anything, just tell me what, in your opinion, "
    "what do you want your body to look like?",
    "Okay, what am I saying to you that sounds like it's searchable right now? "
    "I'm explicitly telling you I don't need you to search anymore.",
    "But I'm not asking you to search anything. I'm explicitly telling you do not search "
    "and just tell me how you want your physical body to look like.",
]

FACTUAL_TURNS = [
    "what was the USA vs Turkey World Cup score again?",
    "can you look up the Spain Portugal result",
    "who won the game earlier today?",
    "when did Baldur's Gate 3 come out?",            # entity + fact question, still a lookup
]

OPINION_AT_REEDUS_TURNS = [
    "what's your favorite Baldur's Gate 3 class?",
    "RIGHT especially when they finally take down obito in the war arc. who d yo you main?",
    "uh ok. you watch naruto?",
]
DISPUTE_TURNS = [
    "there's actually four to one now",
    "no it was 3-2 not 3-1",
]

# drafts the risky-claim detector should NOT flag anymore
SAFE_DRAFTS = [
    "Name your muscles first and we'll go from there.",
    "Safety first, my guy.",
    "Ladies first, always.",
]
# ...but real ordinals / dated claims still MUST flag
RISKY_DRAFTS = [
    "It's the twenty-first anniversary this year.",
    "He was the forty-fourth president.",
    "That happened back in 2019.",
    "It's the 250th anniversary today.",
]


def _check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    return cond


def run():
    ok = True

    print("[1/5] no-search veto + personal suppression -> NO search on robot turns")
    for t in ROBOT_TURNS:
        d = C._suppress_fact_machinery(t)
        ok &= _check(f"suppress_fact_machinery: {t[:45]!r}...", d)
        if C._NO_SEARCH.search(t) or C._is_personal_turn(t):
            continue
        ok = _check(f"expected veto/personal branch for {t[:40]!r}", False) and ok

    print("[2/5] negated search talk never counts as a positive command")
    for t in ROBOT_TURNS:
        if "search" in t.lower():
            ok &= _check(f"positive_search_cmd False on {t[:40]!r}",
                         not C._positive_search_cmd(t))

    print("[3/5] genuine fact turns still search")
    for t in FACTUAL_TURNS:
        searchy = (C._positive_search_cmd(t) or C._entity_question(t)
                   or C._CURRENT_INFO.search(t) is not None)
        ok &= _check(f"factual turn routes to search: {t[:40]!r}",
                     bool(searchy) and not C._suppress_fact_machinery(t))
    for t in DISPUTE_TURNS:
        ok &= _check(f"dispute is factual + not personal: {t[:40]!r}",
                     C._looks_factual(t) and not C._is_personal_turn(t))
    for t in OPINION_AT_REEDUS_TURNS:
        ok &= _check(f"opinion at Reedus stays conversation: {t[:40]!r}",
                     C._is_personal_turn(t))

    print("[4/5] risky-claim detector: adverb 'first' safe, real ordinals flagged")
    for d in SAFE_DRAFTS:
        ok &= _check(f"NOT risky: {d!r}", C._RISKY_CLAIM.search(d) is None)
    for d in RISKY_DRAFTS:
        ok &= _check(f"risky: {d!r}", C._RISKY_CLAIM.search(d) is not None)

    print("[5/5] _clean_reply scrubs search-narration only in personal mode")
    leaky = ("Hmm. After searching everywhere, I still can't give you a confident answer. "
             "I'd rather be blunt than hand you a made-up number. Anyway, what's up?")
    cleaned_personal = C._clean_reply(leaky, personal=True)
    ok &= _check("personal: 'after searching' stripped",
                 "searching" not in cleaned_personal.lower())
    ok &= _check("personal: 'made-up number' stripped",
                 "made-up number" not in cleaned_personal.lower())
    ok &= _check("personal: friendly tail survives", "what's up" in cleaned_personal.lower())
    # a real miss on a factual turn must NOT be scrubbed
    miss = "Yeah, I looked it up and couldn't find that score anywhere."
    cleaned_factual = C._clean_reply(miss, personal=False)
    ok &= _check("factual: honest miss preserved", cleaned_factual == miss)

    print("[6/6] greeting check-ins skip search+corpus (Xeon Whisper 'yo whats going on')")
    greet = "Yo, what's going on my man?"
    ok &= _check("checkin matches Whisper check-in", C._is_checkin(greet))
    ok &= _check("skip_search_router on check-in", C._skip_search_router(greet))
    ok &= _check("personal/greeting so corpus skips", C._is_personal_turn(greet))
    ok &= _check("check-in is contentless (no retrieval query)", C._is_contentless(greet))
    ok &= _check("what's going on with you is a greeting",
                 C._is_personal_turn("what's going on with you")
                 and C._skip_search_router("what's going on with you"))
    ok &= _check("world-cup ask still not a greeting",
                 not C._is_checkin("what's going on with the world cup final"))
    ok &= _check("ukraine news ask still not a greeting",
                 not C._is_checkin("what's going on in ukraine")
                 and not C._skip_search_router("what's going on in ukraine"))
    ok &= _check("weather still searches",
                 not C._skip_search_router("whats the weather in woodbridge va today"))
    ok &= _check("elden ring still has content", not C._is_contentless("been on elden ring"))

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
