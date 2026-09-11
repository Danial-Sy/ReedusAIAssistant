#!/usr/bin/env python3
"""does he know when he doesn't know."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from reedus_companion.serve.core import (            # noqa: E402
    _content_words,
    _extract_noncompliant,
    _has_invented_speech,
    _strip_ungrounded_speech,
)
from reedus_companion.serve.corrections import detect, detect_contradiction  # noqa: E402
from reedus_companion.serve.persona import build_runtime_system              # noqa: E402

# the drafts, verbatim from the session
D_SCROLL = ("That scene where Kakashi walks Naruto back from the battle, silent, slow, full of "
            "unspoken understanding. He doesn't rush, doesn't explain. Just keeps walking. "
            "Feels like home. And when he stops, hands Naruto the last scroll piece in the "
            "rain? That quiet moment? Pure heart.")
D_SASUKE = ('Peak Sasuke. Stillness. Silence after "I will not kill." Cold cut: "He\'s already '
            'dead." No rage, decided cruelty. Control. Looks at Itachi\'s face in the mirror, not '
            'anger, recognition. Seen the future. Chooses to live in it. Not just power. '
            'Evolution.')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    fails: list[str] = []
    npass = 0

    def check(group: str, ok: bool, label: str, detail: str = "") -> None:
        nonlocal npass
        npass += ok
        if not ok:
            fails.append(f"[{group}] {label}" + (f", {detail}" if detail else ""))
        if args.verbose or not ok:
            print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))

    # 1. the ungrounded block
    print("\n=== prompt: weak grounding must be visible to the model ===")
    miss = build_runtime_system(today="Sunday", topics="", weak_knowledge=True)
    weak = build_runtime_system(today="Sunday", topics="a loosely related passage",
                                weak_knowledge=True)
    strong = build_runtime_system(today="Sunday", topics="a real passage", weak_knowledge=False)
    quiet = build_runtime_system(today="Sunday", topics="", weak_knowledge=False)
    MARK = "never quote a character"
    check("prompt", MARK in miss, "fires when nothing was injected")
    check("prompt", MARK in weak, "fires when WEAK passages WERE injected",
          "this is the case that produced the invented Sasuke death arc")
    check("prompt", MARK not in strong, "silent on a confidently anchored turn",
          "a real retrieval must not be second-guessed")
    check("prompt", MARK not in quiet, "absent when retrieval never ran",
          "chit-chat turns must not pay for it")

    # 1b. extractor contract
    # a false NOT FOUND buys a wide re-search (~6-8s) and a worse answer
    print("\n=== extractor: clipped prose is an answer, not deliberation ===")
    weather = ("Generally sunny despite a few afternoon clouds. Hot. High near 95F. "
               "Winds SW at 5 to 10 mph.")
    check("extract", not _extract_noncompliant(weather),
          "verbatim forecast line accepted", f"{len(weather.split())} words, 4 sentences")
    check("extract", not _extract_noncompliant("Spain beat England 2-1 in the final."),
          "ordinary one-sentence answer accepted")
    # still rejects what the rule was written for
    for bad, why in [
        ("The results are mixed, one says Argentina won the 2026 final, another that they won "
         "in 2022. Since your question is about 'recently,' and 2026 hasn't happened yet, I'll "
         "resolve this carefully.", "deliberation (meta vocabulary)"),
        ("The high is 95. But the second source disagrees with the first one here. It is hard "
         "to say which is correct given both were published today and neither is definitive.",
         "long multi-sentence explaining"),
    ]:
        check("extract", _extract_noncompliant(bad), f"still rejected: {why}")
    cost = len(miss) - len(quiet)
    check("prompt", cost < 300, f"block costs {cost} chars (~{cost // 4} tokens)",
          "keep it small, this is CPU prefill")

    # 2. invented dialogue
    print("\n=== draft: quoted dialogue with nothing behind it ===")
    check("speech", _has_invented_speech(D_SASUKE), "invented Sasuke lines detected")
    stripped = _strip_ungrounded_speech(D_SASUKE)
    check("speech", "I will not kill" not in stripped and "already dead" not in stripped,
          "invented lines removed", stripped[:90] + "...")
    check("speech", len(stripped.split()) >= 6, "reply survives the removal")
    # quotation marks that are not speech must be left alone
    for text, why in [('He said the movie was "mid" honestly', "scare quotes"),
                      ('I have been reading "Norwegian Wood" lately', "a title"),
                      ("Nothing quoted here, just a warm take.", "no quotes at all")]:
        check("speech", not _has_invented_speech(text), f"not flagged: {why}", repr(text))
    check("speech", _strip_ungrounded_speech(D_SCROLL) == D_SCROLL,
          "quote-free draft passes through unchanged")

    # 3. content corrections
    print("\n=== corrections: denying a detail IS a correction ===")
    check("corr", detect("i mean he never handed him a scroll but yeah i get what you saying")
          is None, "old detector still misses it (unchanged)")
    check("corr", detect_contradiction(
        "i mean he never handed him a scroll but yeah i get what you saying", D_SCROLL) == "wrong",
        "contradiction detector catches it")
    check("corr", detect_contradiction("she doesnt use a katana she fights with her fists",
                                       "Tifa uses a katana in the remake.") == "wrong",
          "catches a flat denial of a stated fact")
    print("\n=== corrections: the documented false positives stay negative ===")
    prev = "Gaara fought Rock Lee in the final round. That fight was a masterpiece."
    for msg in ["he never had a chance in that fight",
                "that's from the war arc right",
                "sharingan literally means copy wheel eye",
                "the plot makes no sense sometimes lol",
                "mixing up my arcs lol",
                "thats not the right vibe for it",
                "yooo that fight was insane",
                "wait really? i dont remember that"]:
        check("corr-fp", detect_contradiction(msg, prev) is None, f"not a correction: {msg!r}")

    # 4. retrieval query
    print("\n=== retrieval: a query, not a transcript ===")
    q = _content_words("maybe the chills that five kage summit arc sasuke gave man "
                       "he was pure evil there")
    check("retr", "sasuke" in [w.lower() for w in q] and "summit" in [w.lower() for w in q],
          "subject terms survive", " ".join(q))
    for junk in ("man", "he", "was", "that", "there"):
        check("retr", junk not in [w.lower() for w in q], f"conversational filler dropped: {junk!r}")
    cat = [w.lower() for w in _content_words("been watching some anime and playing that game")]
    for keep in ("anime", "game", "watching", "playing"):
        check("retr", keep in cat, f"routing term kept: {keep!r}", " ".join(cat))

    # 5. cost
    print("\n=== cost: none of this may add a model call ===")
    t0 = time.perf_counter()
    for _ in range(2000):
        _has_invented_speech(D_SASUKE)
        _content_words(D_SCROLL)
        detect_contradiction("he never handed him a scroll", D_SCROLL)
    per = (time.perf_counter() - t0) / 2000 * 1e6
    check("cost", per < 500, f"all three guards: {per:.0f} us/turn",
          "budget is microseconds; a model call here would be seconds")

    total = npass + len(fails)
    print(f"\n{npass}/{total} passed")
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
