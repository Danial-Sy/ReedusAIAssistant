#!/usr/bin/env python3
"""does the subject detector name the right thing and stay silent when there isn't one."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

CASES = [
    # must find nothing: chat with no lookupable subject
    ("(none)", "yo yo yo wsg", None),
    ("(none)", "yo", None),
    ("discussing Naruto", "honestly im just tired today", None),
    ("discussing Naruto", "lmaooo thats crazy", None),
    ("discussing Naruto", "anyway what you been up to", None),
    ("discussing Elden Ring", "nah im good but that makes sense for sure though", None),
    ("(none)", "you there?", None),
    ("discussing Naruto", "it was great man excellent conclusion", None),

    # must find the subject the rarity path got wrong
    ("talking about Naruto", "it was great man excellent conclusion. Lets pivot either way, "
     "you play elden ring?", {"elden ring"}),
    ("(none)", "uh thats not quite right... theres not dragon of the deep in elden ring",
     {"elden ring"}),
    ("(none)", "whats your favorite naruto team comp", {"naruto"}),
    ("(none)", "im just rewatching naruto shippuden god its so good",
     {"naruto: shippuden", "naruto shippuden", "naruto"}),
    ("(none)", "yo you ever play hollow knight silksong",
     {"hollow knight: silksong", "hollow knight silksong", "hollow knight"}),

    # ordinary subject finding
    ("(none)", "ooo im currently at five kage summit arc. the evilness of sasuke here is just "
     "crazy chills yk", {"five kage summit", "sasuke uchiha", "sasuke"}),
    ("(none)", "man i love rinnegan sasuke", {"sasuke uchiha", "sasuke", "rinnegan"}),
    ("(none)", "kakashi vs itachi who takes it",
     {"kakashi hatake", "kakashi", "itachi uchiha", "itachi"}),
    ("(none)", "hows tsunade stack up against orochimaru",
     {"tsunade", "orochimaru"}),

    # pronoun follow-ups: the subject must be carried from context
    ("discussing Kakashi Hatake", "ok but is he actually stronger than the captain guy or nah",
     {"kakashi hatake", "kakashi"}),
    ("discussing Elden Ring", "man that boss took me 40 tries lmao", {"elden ring"}),
    ("discussing Sasuke Uchiha", "what about his brother though",
     {"sasuke uchiha", "sasuke", "itachi uchiha", "itachi"}),
]
HOLDOUT = [
    # reactions and asides mid-topic -> nothing to look up
    ("discussing Bleach", "yooo no way", None),
    ("discussing Elden Ring", "bro im dead lmao", None),
    ("discussing One Piece", "yeah true honestly", None),
    ("discussing Naruto", "ok that actually makes sense now", None),
    ("discussing Elden Ring", "fair enough", None),
    # explicit pivots away
    ("discussing Bleach", "anyway how was your day", None),
    ("discussing Naruto", "different question tho", None),
    ("discussing One Piece", "ok forget that, whats your deal", None),
    # real subjects, unseen titles
    ("(none)", "you ever mess with baldurs gate 3",
     {"baldur's gate 3", "baldurs gate 3", "baldur's gate"}),
    ("(none)", "bro luffy gear 5 is insane", {"monkey d. luffy", "luffy", "gear 5"}),
    ("(none)", "whats the deal with aizens shikai", {"sosuke aizen", "aizen", "kyoka suigetsu"}),
    ("(none)", "i been grinding sekiro again", {"sekiro: shadows die twice", "sekiro"}),
    ("(none)", "gojo vs sukuna who takes it",
     {"satoru gojo", "gojo", "ryomen sukuna", "sukuna"}),
    # pronoun follow-ups that DO need the subject resolved
    ("discussing Itachi Uchiha", "was he always planning that though",
     {"itachi uchiha", "itachi"}),
    ("discussing Malenia in Elden Ring", "how many phases does she have",
     {"malenia", "malenia blade of miquella", "elden ring"}),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["hf", "gguf"], default="hf")
    ap.add_argument("--gguf", default=None)
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--merged", default=None)
    ap.add_argument("--base-only", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    from reedus_companion.scripts.eval_engagement import build
    from reedus_companion.knowledge.retrieve import Knowledge
    from reedus_companion.knowledge.entity_index import EntityIndex

    backend = build(args)
    kb = Knowledge.__new__(Knowledge)
    kb._ei = EntityIndex()
    kb.debug = False
    kb.llm_chat = lambda m, n: backend.chat(m, n, use_adapter=False, temperature=0.0)

    def score(name, cases, tuned):
        tag = "TUNED ON, a floor" if tuned else "NEVER TUNED ON, the real number"
        print(f"\n{'=' * 72}\n{name}  ({len(cases)} cases, {tag})\n{'=' * 72}")
        false_subj, missed, wrong = [], [], []
        for ctx, msg, want in cases:
            got = kb.llm_subjects(msg, ctx) or []
            gotset = {g.lower() for g in got}
            if want is None:
                ok = not gotset
                if not ok:
                    false_subj.append((msg, got))
            elif not gotset:
                ok = False
                missed.append((msg, want))
            else:
                ok = bool(gotset & {w.lower() for w in want})
                if not ok:
                    wrong.append((msg, got, want))
            if args.verbose:
                print(f"  {'ok  ' if ok else 'MISS'} got={got!s:<42} "
                      f"want={'(none)' if want is None else sorted(want)}")
                if not ok:
                    print(f"        ctx={ctx!r}  msg={msg[:60]!r}")

        n = len(cases)
        n_none = sum(1 for _c, _m, w in cases if w is None) or 1
        correct = n - len(false_subj) - len(missed) - len(wrong)
        prec = (n_none - len(false_subj)) / n_none
        print(f"\n  accuracy {correct}/{n} ({correct/n:.0%})   "
              f"silence precision {prec:.0%}   wrong {len(wrong)}   missed {len(missed)}")
        for m, g in false_subj:
            print(f"     FALSE SUBJECT -> {g} for {m[:54]!r}")
        for m, g, w in wrong:
            print(f"     WRONG -> {g} want {sorted(w)}  {m[:44]!r}")
        for m, w in missed:
            print(f"     missed (falls back to _entities) {sorted(w)}  {m[:44]!r}")
        return prec, len(wrong), len(false_subj)

    dev_p, dev_w, _ = score("DEV SET", CASES, True)
    h_p, h_w, h_fs = score("HOLDOUT", HOLDOUT, False)

    print(f"\n{'=' * 72}\nGENERALISATION\n{'=' * 72}")
    print(f"  silence precision   dev {dev_p:.0%}  ->  holdout {h_p:.0%}")
    print(f"  wrong subjects      dev {dev_w}     ->  holdout {h_w}")
    print("\n  verdict:")
    if h_w:
        print("    A WRONG subject is the expensive one, retrieval fetches confident nonsense.")
        print("    Tighten SUBJECT_SYSTEM, or set REEDUS_LLM_SUBJECT=0 to keep the old path.")
    elif h_fs:
        print("    No wrong subjects; the misses are it repeating the running topic on a turn\n"
              "    that needed nothing. Costs a shard search, does not corrupt the reply.\n"
              "    Acceptable to ship; worth another prompt pass if turns feel slow.")
    else:
        print("    Clean. Leave it on, then time a turn on the Xeon to confirm it is cheaper\n"
              "    than the entity passes it replaces.")
    return 0 if not h_w else 1


if __name__ == "__main__":
    sys.exit(main())
