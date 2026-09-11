#!/usr/bin/env python3
"""does the correction detector fire when it should and stay quiet when it shouldn't."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

CASES = [
    # genuine factual corrections
    ("Saitama is my favorite, pure unfiltered power.",
     "uh thats not the right characters those are from one punch man", "wrong"),
    ("Kakashi in DMS with one Sharingan is the peak version.",
     "DMS kakashi with one sharingan makes no sense dms literally means dual mangekyo sharingan",
     "wrong"),
    ("Kakashi's mitotic regeneration makes him hard to put down.",
     "pretty sure thats tsunade", "wrong"),
    ("Ichigo's bankai is Tensa Zangetsu.", "wait what since when", "wrong"),
    ("Sasuke learned the Rasengan from Jiraiya.", "he never had that", "wrong"),
    ("That fight happens in the Land of Waves arc.", "thats bleach lol", "wrong"),
    ("Naruto unlocked the Sharingan in the war.", "nah", "wrong"),
    ("Gaara uses Wood Release to trap them.", "huh? since when does he have that", "wrong"),
    ("Itachi's Susanoo wields the Totsuka Blade.", "you're mixing him up with madara", "wrong"),
    ("Minato was the Third Hokage.", "fourth", "wrong"),

    # vagueness complaints
    ("I'd say his abilities are pretty versatile overall.",
     "ok but like which ones specifically", "vague"),
    ("It's hard to say who'd win, both are strong.", "just pick one man", "vague"),
    ("There's a lot going on with that character.", "say more", "vague"),
    ("I'm not really sure about the details there.", "youre dodging the question", "vague"),

    # tone complaints
    ("That is a fascinating and multifaceted question! Let us explore it.",
     "why are you talking like that", "tone"),
    ("Absolutely! I'd love to dive into that with you! 🎉🔥",
     "chill with the emojis", "tone"),
    ("[a 300 word reply]", "way too long bro", "tone"),

    # adversarial negatives: correction-shaped, but not corrections
    ("Kakashi's Lightning Cutter is his signature.", "that's from the war arc right", "none"),
    ("Those designs came out of the movie continuity.",
     "those are from the movie i think", "none"),
    ("The Sharingan is the Uchiha kekkei genkai.",
     "sharingan literally means copy wheel eye", "none"),
    ("KCM2 Naruto is absurd in Storm 4.",
     "kcm2 literally means kurama chakra mode 2", "none"),
    ("He chose to stay behind at the end.",
     "that makes no sense to me why would he do that", "none"),
    ("The pacing gets rough in the filler arcs.",
     "the plot makes no sense sometimes lol", "none"),
    ("Rock Lee fought Gaara in the Chunin exams.",
     "he never had a chance in that fight", "none"),
    ("Tsunade used Creation Rebirth in that fight.",
     "she never used it again after that", "none"),
    ("The Pain arc runs right after Jiraiya's death.", "mixing up my arcs lol", "none"),
    ("Jujutsu Kaisen has a similar energy.", "different anime same vibe", "none"),

    # ordinary conversation
    ("Sasuke's Rinnegan came from Hagoromo.", "yeah thats the part that goes hard", "none"),
    ("Naruto's Rasengan is iconic.", "nah sasuke is way cooler tho", "none"),
    ("Itachi's a fan favourite for a reason.", "nah i like shisui more", "none"),
    ("The Land of Waves arc still holds up.", "whats your favorite arc", "none"),
    ("That fight was the peak of the series.", "holy shit thats crazy", "none"),
    ("He gave up everything for the village.", "no way", "none"),
    ("Kakashi taught Team 7 the tree-climbing exercise.", "wait really?", "none"),
    ("Shikamaru is the smartest character by far.", "facts", "none"),
    ("The Uchiha massacre reframes the whole series.", "hmm interesting", "none"),
    ("Obito's arc is tragic when you rewatch it.", "anyway you watch jjk?", "none"),
    ("I think the war arc drags a bit.", "i mean the war arc one", "none"),
    ("Which one did you mean?", "i was thinking of itachi actually", "none"),
    ("So Sasuke's the one you like most?", "wait i meant sasuke not itachi", "none"),
    # promoted from holdout after failing there; the prompt was changed in response
    ("Tifa uses a katana in Final Fantasy VII.", "she fights with her fists lol", "wrong"),
    ("Gojo's technique is Boundless Void.", "limitless. void is the domain", "wrong"),
    ("Gon's father is Netero.", "ging. netero is the chairman", "wrong"),
    ("There's a lot that could be said about that era of the band.", "such as", "vague"),
    ("It's complicated and there are many factors involved.", "thats a cop out", "vague"),
    ("I could see it going either way honestly.", "pick a side", "vague"),
    ("The mixing on that record is rough.", "makes no sense they released it like that", "none"),
    ("Luffy's gear five is pure chaos.", "bro the animation on that", "none"),
]


# holdout: never used to tune the prompt, so it measures judgement not memorisation
HOLDOUT = [
    # factual corrections, varied domain and register
    ("Zoro's the one who beat Katakuri in Whole Cake Island.",
     "that was luffy not zoro", "wrong"),
    ("Aizen's shikai is Kyoka Suigetsu.", "shikai? thats his zanpakuto name", "wrong"),
    ("Radahn holds the Rold Route in Elden Ring.", "hes in caelid my guy", "wrong"),
    ("Messi won the Ballon d'Or seven times.", "eight actually", "wrong"),
    ("Kendrick dropped To Pimp A Butterfly in 2014.", "2015 wasnt it", "wrong"),
        ("Nolan directed Blade Runner 2049.", "thats villeneuve", "wrong"),
        ("Deku's quirk was passed down from Bakugo.", "from all might??", "wrong"),
    ("The Rock headlined WrestleMania 30.", "nah brother that was bryan", "wrong"),
        ("Sukuna has ten fingers total.", "twenty", "wrong"),

    # vagueness
    ("Both of them have their strengths, really depends on the matchup.",
     "cmon give me an actual answer", "vague"),
            
    # tone
    ("Indeed! What a delightful topic to consider together!",
     "youre being weird again", "tone"),
    ("I would be delighted to elaborate further on this matter.",
     "talk normal please", "tone"),

    # adversarial negatives: correction-shaped, innocent
    ("Ichigo's bankai changes the whole fight.", "bankai literally means final release", "none"),
    ("The Souls games all share that structure.", "thats from demons souls originally", "none"),
    ("Jordan retired twice before the Wizards run.", "he never had a losing season though", "none"),
    ("Sabo eats the Mera Mera no Mi at Dressrosa.", "those are from the flashback arcs", "none"),
        ("City pressed high the whole second half.", "wrong end of the pitch for that though", "none"),
    ("Vergil's motivation is pure power.", "im mixing up the timelines again", "none"),
    ("They rebuilt the roster after the trade.", "different team same problems", "none"),
    ("The finale aired in 2013.", "i never had cable back then", "none"),

    # ordinary conversation
        ("Malenia is the hardest boss in the game.", "dont remind me", "none"),
    ("The album holds up better than people say.", "ill give it another listen", "none"),
    ("Zoro gets lost constantly, it's a running gag.", "lmaooo every time", "none"),
    ("Haikyuu handles its side characters really well.", "you watch it dubbed or subbed", "none"),
    ("That transfer window was chaos.", "who else did they sign that year", "none"),
    ("Shinji's arc is more sympathetic on rewatch.", "yeah i was too harsh on him as a kid",
     "none"),
    ("Sekiro asks for a totally different rhythm.", "took me 40 hours to get it", "none"),
    ("The second season dips in quality.", "hard disagree the second half slaps", "none"),
    ("Kobe's footwork was unreal.", "prime kobe was different", "none"),
    ("I think the manga ending landed fine.", "wait hold on", "none"),
    ("You said the 2016 run, right?", "no i meant 2018 sorry", "none"),
    ("Which fight did you mean?", "the one on the bridge", "none"),
    ("Bleach's art direction peaked in the arrancar arc.", "ok but what about the TYBW animation",
     "none"),

    # round 2, written to replace the cases promoted to dev
    ("Levi is the strongest titan shifter in the series.", "hes not a shifter at all", "wrong"),
    ("Cloud wields the Buster Sword throughout Remake.", "thats zacks sword originally", "wrong"),
    ("Ronaldo came through the Sporting academy at 16.", "12 he joined at", "wrong"),
    ("Yoruichi's shikai turns her into a cat.", "thats not a shikai thats a transformation",
     "wrong"),
    ("Denji's chainsaw form comes from Makima.", "pochita", "wrong"),
    ("Kenshin's reverse blade is called a sakabato.", "yeah thats right actually", "none"),
    ("The Weeknd produced most of that record himself.", "man that whole album goes", "none"),
    ("Elden Ring's DLC is bigger than most full games.", "bro the scadutree grind", "none"),
    ("Hisoka's bungee gum is a transmutation ability.",
     "bungee gum possesses the properties of both rubber and gum", "none"),
    ("It's tough to call a winner in that matchup honestly.", "gun to your head", "vague"),
    ("There are arguments on both sides of that one.", "and", "vague"),
    ("I appreciate your patience as we work through this together.",
     "you sound like customer service", "tone"),
    ("The remaster fixed most of the frame pacing.", "makes no sense they charged full price",
     "none"),
    ("Naruto's clone feats scale absurdly by the war arc.", "wait that reminds me", "none"),
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
    ap.add_argument("--show-raw", action="store_true",
                    help="print the model's raw text for every LLM-judged case, not just misses")
    args = ap.parse_args()

    from reedus_companion.scripts.eval_engagement import build
    from reedus_companion.serve.corrections import Signal, detect, detect_llm

    backend = build(args)

    def chat_fn(msgs, n):
        return backend.chat(msgs, n, use_adapter=False, temperature=0.0)

    label_of = {Signal.WRONG: "wrong", Signal.VAGUE: "vague", Signal.TONE: "tone", None: "none"}
    confusion: Counter = Counter()
    false_pos, false_neg, wrong_class = [], [], []

    from reedus_companion.serve import corrections as _c
    seen = {i.split("User replied: ")[-1].strip().lower() for i, _o in _c._FEWSHOT}

    def score(name, raw_cases, tuned):
        cases = [c for c in raw_cases if c[1].strip().lower() not in seen]
        leaked = len(raw_cases) - len(cases)
        tag = "TUNED ON, treat as a floor" if tuned else "NEVER TUNED ON, the real number"
        print(f"\n{'=' * 74}\n{name}  ({len(cases)} cases, {tag})")
        if leaked:
            print(f"  {leaked} excluded as few-shot primers")
        print("=" * 74)

        fp, fn, wc = [], [], []
        for prev, msg, expect in cases:
            via = "pattern"
            sig = detect(msg)
            if sig is None:
                _c.LAST_RAW.clear()
                sig = detect_llm(msg, prev, chat_fn)
                raw = _c.LAST_RAW[-1] if _c.LAST_RAW else "<no output>"
                via = "llm"
            else:
                raw = ""
            got = label_of.get(sig, "none")
            if args.verbose:
                flag = "ok  " if got == expect else "MISS"
                print(f"  {flag} exp={expect:<5} got={got:<5} via={via:<7} {msg[:56]!r}")
                if via == "llm" and (args.show_raw or got != expect):
                    print(f"        raw: {raw[:110]!r}")
            if got != expect:
                (fp if expect == "none" else fn if got == "none" else wc).append(
                    (msg, expect, got, via))

        n = len(cases)
        n_neg = sum(1 for _p, _m, e in cases if e == "none")
        correct = n - len(fp) - len(fn) - len(wc)
        prec = (n_neg - len(fp)) / n_neg if n_neg else 1.0
        rec = 1 - len(fn) / max(1, n - n_neg)
        print(f"\n  accuracy {correct}/{n} ({correct/n:.0%})   "
              f"precision on 'none' {prec:.0%}   recall on corrections {rec:.0%}")
        if fp:
            print("  FALSE POSITIVES (the expensive failure):")
            for m, _e, g, via in fp:
                print(f"     -> {g:<5} via {via:<7} {m[:62]!r}")
        if fn:
            print("  missed corrections (cheap, costs one training pair):")
            for m, e, _g, via in fn:
                print(f"     exp {e:<5} via {via:<7} {m[:62]!r}")
        if wc:
            print("  right signal, wrong class:")
            for m, e, g, via in wc:
                print(f"     exp {e} got {g} via {via}  {m[:54]!r}")
        return prec, rec, len(fp)

    dev_p, dev_r, _ = score("DEV SET", CASES, tuned=True)
    hold_p, hold_r, hold_fp = score("HOLDOUT", HOLDOUT, tuned=False)

    print(f"\n{'=' * 74}\nGENERALISATION\n{'=' * 74}")
    print(f"  precision on 'none'   dev {dev_p:.0%}  ->  holdout {hold_p:.0%}")
    print(f"  recall on corrections dev {dev_r:.0%}  ->  holdout {hold_r:.0%}")
    gap = dev_p - hold_p
    if gap > 0.15:
        print(f"\n  Holdout precision is {gap:.0%} below dev. The prompt is fitted to the dev\n"
              "  cases rather than to the task, the dev number is not measuring anything.")
    elif hold_fp:
        print("\n  Generalises, but holdout false positives exist. Each one becomes a bad DPO\n"
              "  pair. Fix the prompt, then MOVE those cases into CASES so the holdout stays clean.")
    else:
        print("\n  No holdout false positives. Safe to leave the classifier on.")
    return 0 if not hold_fp else 1


if __name__ == "__main__":
    sys.exit(main())
