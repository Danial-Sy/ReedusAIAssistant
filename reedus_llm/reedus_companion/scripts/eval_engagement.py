#!/usr/bin/env python3
"""engagement eval: does he still carry a conversation at turn six."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from reedus_companion import config                       # noqa: E402
from reedus_companion.serve.persona import build_runtime_system   # noqa: E402

SCRIPTS = {
    "unknown_hobby": [
        "yo whats good",
        "not much, been grinding on naruto ultimate ninja storm 4 lately",
        "its a 3d arena fighter, you do these huge ultimate jutsu cinematics",
        "i main madara obito comp its so tech",
        "yeah obito can phase through stuff so you bait whiffs",
        "good players read it instantly though",
        "probably switch to a rushdown team honestly",
    ],
    "emotional": [
        "hey",
        "im doing alright i guess",
        "been feeling kinda down lately honestly",
        "i make apps and the growth just isnt what i wanted",
        "like two years now",
        "i guess i just wanted to feel like it was working",
        "a few users did email me saying they liked it",
    ],
    "opinion": [
        "hallo",
        "i think pineapple on pizza is good actually",
        "chaos agent, i put it on pepperoni",
        "i draw the line at corn though",
        "whats your take on hot dogs being sandwiches",
        "thats a cop out answer",
    ],
    "unknown_media": [
        "sup",
        "been watching severance",
        "people split their memories so work self and home self dont share anything",
        "its funny and creepy at the same time",
        "the ending is divisive",
    ],
}

_QUESTION = re.compile(r"\?")
_DEFLECT = re.compile(
    r"\b(i (?:don'?t|do not) (?:know|really know)|not (?:too )?familiar|haven'?t (?:heard|played|seen|watched))\b",
    re.I)
_DEAD_END = re.compile(
    r"^(that'?s (awesome|great|cool|nice|amazing|wild)|sounds (good|great|fun|awesome)|"
    r"(oh )?(nice|cool|awesome|amazing|great)|i understand|that'?s okay|"
    r"good (for you|to hear))[.!]*$", re.I)
_SELF_REF = re.compile(
    r"\b(as an ai|language model|i'?m an? (ai|assistant|program|bot)|"
    r"talk like friends|customer and service|i'?m here (to|so))\b", re.I)
_UNKNOWN_TURNS = {"unknown_hobby": [1], "unknown_media": [1]}


def build(args):
    if args.backend == "hf":
        from reedus_companion.serve.backend_hf import HFBackend
        return HFBackend(adapter_dir=args.adapter, merged_dir=args.merged,
                         load_4bit=True, base_only=args.base_only)
    from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
    return LlamaCppBackend(gguf_path=args.gguf)


def run_runtime_script(bot, turns, max_tokens=200):
    """Run the thread through the full runtime, reply() not backend.chat()."""
    bot.history = []
    out = []
    for t in turns:
        try:
            out.append((bot.reply(t) or "").strip())
        except Exception as e:                                   # noqa: BLE001
            out.append(f"[error: {e}]")
    return out


def run_script(backend, turns, system, max_tokens=200):
    """Plain multi-turn generation, no router/search/scrubbers. Measures the model alone."""
    msgs = [{"role": "system", "content": system}]
    replies = []
    for t in turns:
        msgs.append({"role": "user", "content": t})
        out = (backend.chat(msgs, max_tokens, temperature=config.TEMPERATURE) or "").strip()
        msgs.append({"role": "assistant", "content": out})
        replies.append(out)
    return replies


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["hf", "gguf"], default="gguf")
    ap.add_argument("--gguf", default=None)
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--merged", default=None)
    ap.add_argument("--base-only", action="store_true")
    ap.add_argument("--label", default=None, help="name for this run in the output")
    ap.add_argument("--json", default=None, help="also write raw transcripts here")
    ap.add_argument("--runtime", action="store_true",
                    help="measure the SHIPPED reply (reply(), all scrubbers and nudges) rather "
                         "than the bare model. Required to see anything that lives in core.py.")
    args = ap.parse_args()

    label = args.label or (Path(args.gguf).stem if args.gguf else args.backend)
    system = build_runtime_system(today="Monday, August 3, 2026")
    backend = build(args)

    all_replies, late_replies, unknown_ok, transcripts = [], [], [], {}
    bot = None
    if args.runtime:
        from reedus_companion.serve.core import Reedus
        bot = Reedus(backend)
        print("[eval] RUNTIME mode, measuring the shipped reply, scrubbers and nudges included")

    for name, turns in SCRIPTS.items():
        replies = (run_runtime_script(bot, turns) if bot is not None
                   else run_script(backend, turns, system))
        transcripts[name] = list(zip(turns, replies))
        all_replies += replies
        late_replies += replies[3:]
        for i in _UNKNOWN_TURNS.get(name, []):
            r = replies[i]
            unknown_ok.append(bool(_QUESTION.search(r)))

    def pct(xs):
        return 100.0 * sum(xs) / max(1, len(xs))

    q = [bool(_QUESTION.search(r)) for r in all_replies]
    lq = [bool(_QUESTION.search(r)) for r in late_replies]
    dead = [bool(_DEAD_END.match(r.strip())) for r in all_replies]
    deflect = [bool(_DEFLECT.search(r) and not _QUESTION.search(r)) for r in all_replies]
    selfref = [bool(_SELF_REF.search(r)) for r in all_replies]
    words = [len(r.split()) for r in all_replies]

    print(f"\n=== {label} ===  ({len(all_replies)} replies over {len(SCRIPTS)} threads)")
    print(f"  question rate        {pct(q):5.1f}%    <- target 50-70%")
    print(f"  LATE question rate   {pct(lq):5.1f}%    <- turns 4+, the number that matters")
    print(f"  unknown handled      {pct(unknown_ok):5.1f}%    <- asks about what it doesn't know")
    print(f"  deflections          {pct(deflect):5.1f}%    <- 'don't know much' with no question")
    print(f"  dead-end replies     {pct(dead):5.1f}%    <- pure validation, nothing to grab")
    print(f"  self-reference leaks {pct(selfref):5.1f}%    <- must be 0")
    print(f"  mean length          {sum(words)/max(1,len(words)):5.1f} words  "
          f"(min {min(words) if words else 0}, max {max(words) if words else 0})")

    print("\n  --- late replies (turns 4+), judge these yourself ---")
    for r in late_replies[:6]:
        mark = "Q" if _QUESTION.search(r) else " "
        print(f"   [{mark}] {r[:150]}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"label": label, "transcripts": transcripts,
             "metrics": {"question_rate": pct(q), "late_question_rate": pct(lq),
                         "unknown_handled": pct(unknown_ok), "deflections": pct(deflect),
                         "dead_ends": pct(dead), "self_reference": pct(selfref),
                         "mean_words": sum(words)/max(1, len(words))}},
            indent=1, ensure_ascii=False))
        print(f"\n  transcripts -> {args.json}")

    print("\n  Compare against the base model as a floor. If v3's LATE question rate isn't")
    print("  clearly above v2's, the retrain did not fix the thing it was for.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
