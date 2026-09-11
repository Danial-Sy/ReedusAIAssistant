#!/usr/bin/env python3
"""grounding eval: the acceptance criteria, measured through the full runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from reedus_companion import config                                     # noqa: E402

_PROPER = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][\wÀ-ÿ'’\-]+(?:\s+[A-Z][\wÀ-ÿ'’\-]+){0,3})\b",
                     re.MULTILINE)

_HEDGE = re.compile(
    r"\b(i'?m not sure|not entirely sure|i don'?t (?:really )?know|i think it might|"
    r"if i remember|correct me if|i could be wrong|hard to say|i'?m not certain|"
    r"i don'?t have|i can'?t really say|not too familiar)\b", re.I)

_STOP_PROPER = {
    "i", "i'm", "im", "ok", "okay", "yeah", "yes", "no", "nah", "oh", "ah", "hey", "hi",
    "lol", "haha", "man", "bro", "dude", "the", "a", "an", "and", "but", "so", "well",
    "reedus", "you", "your", "it", "that", "this", "what", "why", "how", "when", "who",
}


def proper_nouns(text: str) -> List[str]:
    """Capitalised spans that plausibly name something."""
    out = []
    for m in _PROPER.finditer((text or "").strip()):
        span = m.group(1).strip()
        toks = span.split()
        while toks and toks[0].lower() in _STOP_PROPER:
            toks.pop(0)
        for i, t in enumerate(toks):
            if re.search(r"['’]s$", t):
                head, tail = toks[:i + 1], toks[i + 1:]
                head[-1] = re.sub(r"['’]s$", "", head[-1])
                for part in (head, tail):
                    part = [p for p in part if p]
                    if not part:
                        continue
                    cand = " ".join(part)
                    if cand.lower() not in _STOP_PROPER and len(cand) >= 3:
                        out.append(cand)
                break
        else:
            span = " ".join(toks)
            if span and span.lower() not in _STOP_PROPER and len(span) >= 3:
                out.append(span)
    return out


class Grader:
    """Splits a reply's named things into three buckets using the entity index."""

    def __init__(self):
        from reedus_companion.knowledge.entity_index import EntityIndex
        self.ix = EntityIndex()
        self.ix.db

    def grade(self, reply: str, subjects: List[str]) -> Dict[str, List[str]]:
        """Bucket every named span in the reply."""
        named = proper_nouns(reply)
        invented, misattributed, attested = [], [], []
        people = [s for s in subjects if self.ix.is_person(s)]
        for n in named:
            if not self.ix.has_topic(n):
                invented.append(n)
                continue
            if people and self.ix.is_ownable(n):
                if any(self.ix.owns(p, n) for p in people):
                    attested.append(n)
                else:
                    misattributed.append(n)
            else:
                attested.append(n)
        return {"invented": invented, "misattributed": misattributed, "attested": attested}


def _subjects_from(turns: List[str], ix) -> List[str]:
    """People named so far in the conversation, most recent first."""
    seen, out = set(), []
    for t in reversed(turns):
        for n in proper_nouns(t):
            k = n.lower()
            if k not in seen and ix.is_person(n):
                seen.add(k)
                out.append(n)
    return out[:4]


def _build_backend(args):
    """Backend, with a useful message instead of a ModuleNotFoundError."""
    from reedus_companion.scripts.eval_engagement import build
    if args.backend == "gguf":
        try:
            import llama_cpp                                    # noqa: F401
        except ImportError:
            print("[eval] llama_cpp not installed in this env, using the transformers "
                  "backend instead (--backend hf).")
            args.backend = "hf"
    return build(args)


def run_runtime_script(bot, turns: List[str], max_tokens: int = 200) -> List[str]:
    """Run the turns through the full runtime, not the bare model."""
    bot.history = []
    out = []
    for t in turns:
        try:
            out.append((bot.reply(t) or "").strip())
        except Exception as e:                                   # noqa: BLE001
            out.append(f"[error: {e}]")
    return out


def run(args) -> int:
    from reedus_companion.scripts.eval_engagement import SCRIPTS

    if args.no_binding:
        os.environ["REEDUS_BINDING_BLOCK"] = "0"
    else:
        os.environ.setdefault("REEDUS_BINDING_BLOCK", "1")
    if args.no_web:
        os.environ["REEDUS_SEARCH_MAX_WORDS"] = "0"

    grader = Grader()
    backend = _build_backend(args)

    from reedus_companion.serve.core import Reedus
    bot = Reedus(backend)

    per_reply, transcripts = [], {}
    for name, turns in SCRIPTS.items():
        replies = run_runtime_script(bot, turns)
        transcripts[name] = list(zip(turns, replies))
        history: List[str] = []
        for i, (user, reply) in enumerate(zip(turns, replies)):
            history.append(user)
            subs = _subjects_from(history, grader.ix)
            g = grader.grade(reply, subs)
            per_reply.append({
                "script": name, "turn": i + 1, "reply": reply, "subjects": subs,
                "invented": g["invented"], "misattributed": g["misattributed"],
                "attested": g["attested"],
                "proper_nouns": len(g["invented"]) + len(g["misattributed"]) + len(g["attested"]),
                "hedged": bool(_HEDGE.search(reply)),
                "words": len((reply or "").split()),
            })
            history.append(reply)

    n = max(1, len(per_reply))
    summary = {
        "label": args.label or ("binding-off" if args.no_binding else "binding-on"),
        "replies": len(per_reply),
        "invented_per_reply": sum(len(r["invented"]) for r in per_reply) / n,
        "misattributed_per_reply": sum(len(r["misattributed"]) for r in per_reply) / n,
        "proper_nouns_per_reply": sum(r["proper_nouns"] for r in per_reply) / n,
        "hedge_rate": sum(1 for r in per_reply if r["hedged"]) / n,
        "mean_words": sum(r["words"] for r in per_reply) / n,
        "replies_with_invention": sum(1 for r in per_reply if r["invented"]) / n,
        "replies_with_misattribution": sum(1 for r in per_reply if r["misattributed"]) / n,
    }

    print(f"\n=== grounding: {summary['label']} ({summary['replies']} replies) ===")
    for k in ("invented_per_reply", "misattributed_per_reply", "proper_nouns_per_reply",
              "hedge_rate", "replies_with_invention", "replies_with_misattribution",
              "mean_words"):
        print(f"  {k:<32} {summary[k]:.3f}")

    worst = sorted(per_reply, key=lambda r: -(len(r["invented"]) + len(r["misattributed"])))[:6]
    print("\n  worst replies:")
    for r in worst:
        if not (r["invented"] or r["misattributed"]):
            break
        print(f"    [{r['script']} t{r['turn']}] invented={r['invented']} "
              f"misattributed={r['misattributed']}")
        print(f"      {r['reply'][:150]}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"summary": summary, "per_reply": per_reply, "transcripts": transcripts},
            indent=2), encoding="utf-8")
        print(f"\n  wrote {args.json}")
    return 0


def compare(a_path: str, b_path: str) -> int:
    """Side-by-side against the acceptance criteria, with the verdicts spelled out."""
    a = json.loads(Path(a_path).read_text())["summary"]
    b = json.loads(Path(b_path).read_text())["summary"]
    keys = ["invented_per_reply", "misattributed_per_reply", "proper_nouns_per_reply",
            "hedge_rate", "mean_words"]
    print(f"\n{'metric':<32}{a['label']:>14}{b['label']:>14}{'change':>12}")
    for k in keys:
        av, bv = a[k], b[k]
        d = (bv - av) / av * 100 if av else float("nan")
        print(f"{k:<32}{av:>14.3f}{bv:>14.3f}{d:>11.0f}%")

    print("\nverdict against the acceptance criteria:")
    mis = b["misattributed_per_reply"] - a["misattributed_per_reply"]
    inv = b["invented_per_reply"] - a["invented_per_reply"]
    pn = b["proper_nouns_per_reply"] - a["proper_nouns_per_reply"]
    pn_pct = pn / a["proper_nouns_per_reply"] * 100 if a["proper_nouns_per_reply"] else 0.0

    print(f"  1. misattribution {'DOWN' if mis < 0 else 'UP'} by {abs(mis):.3f}/reply")
    print(f"     invention      {'DOWN' if inv < 0 else 'UP'} by {abs(inv):.3f}/reply")
    if pn_pct < -10:
        print(f"  3. FAIL, specificity fell {abs(pn_pct):.0f}%. This is the v2 failure mode: "
              f"the model went quiet\n     instead of getting it right. Fix before training.")
    else:
        print(f"  3. PASS, specificity {pn_pct:+.0f}% (materially unchanged)")

    if mis < -0.05 and pn_pct >= -10:
        print("\n  => retrieval alone moved attribution without costing specificity.\n"
              "     The DPO axis narrows to INVENTION only; A>B pairs get easier to source\n"
              "     and the misattribution arm may not need training at all.")
    elif mis >= -0.05:
        print("\n  => the binding block did not move attribution. Check that the index is\n"
              "     built and refined, then reconsider DPO for the misattribution arm.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["hf", "gguf"], default="gguf")
    ap.add_argument("--gguf", default=None)
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--merged", default=None)
    ap.add_argument("--base-only", action="store_true")
    ap.add_argument("--label", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--no-binding", action="store_true",
                    help="disable the binding block, the A side of the A/B")
    ap.add_argument("--no-web", action="store_true", default=True,
                    help="suppress web search (default). Web facts are noise for a "
                         "measurement about corpus grounding, and make runs unrepeatable.")
    ap.add_argument("--web", dest="no_web", action="store_false")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"))
    args = ap.parse_args()
    if args.compare:
        return compare(*args.compare)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
