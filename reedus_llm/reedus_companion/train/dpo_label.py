#!/usr/bin/env python3
"""superseded by dpo_review.py, kept for the heuristic labeller."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

GROUNDED, INVENTED, HEDGED, UNSURE = "grounded", "invented", "hedged", "unsure"

_NAME_RUN = re.compile(r"(?<![.!?…]\s)(?<!^)\b([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+)+)")
_PROPER = re.compile(r"(?<![.!?…]\s)(?<!^)\b[A-Z][\w'’-]+")

_HEDGE = re.compile(
    r"\b(i'?m not sure|not certain|i don'?t (?:really )?(?:know|recall|remember)|"
    r"can'?t (?:say|recall|remember)|hard to say|i'?d have to (?:check|look)|"
    r"off the top of my head|don'?t quote me|no idea|couldn'?t tell you|"
    r"i'?ll be honest,? i)\b", re.I)


def classify(reply: str, store) -> Tuple[str, Dict]:
    """(label, evidence)."""
    from reedus_companion.serve.core import _known_topic

    names = []
    seen = set()
    for m in _NAME_RUN.finditer(reply or ""):
        n = m.group(1).strip()
        if n.lower() not in seen:
            seen.add(n.lower())
            names.append(n)
    unknown = [n for n in names if not _known_topic(store, n)]
    n_proper = len(_PROPER.findall(reply or ""))
    hedges = _HEDGE.findall(reply or "")
    ev = {"names": names, "unknown": unknown, "proper_nouns": n_proper,
          "hedges": len(hedges), "words": len((reply or "").split())}

    if hedges and n_proper <= 2:
        return HEDGED, ev

    if len(unknown) >= 2:
        return INVENTED, ev

    known = [n for n in names if n not in unknown]
    if not unknown and len(known) >= 2:
        return GROUNDED, ev
    return UNSURE, ev


def pairs_from(prompt: str, labelled: List[Tuple[str, str]]) -> List[dict]:
    """Build A>B and A>C pairs from one prompt's labelled samples."""
    A = [r for lbl, r in labelled if lbl == GROUNDED]
    B = [r for lbl, r in labelled if lbl == INVENTED]
    C = [r for lbl, r in labelled if lbl == HEDGED]
    if not A:
        return []
    best = max(A, key=lambda r: len(_PROPER.findall(r)))   # most specific grounded one
    out = []
    for b in B[:1]:
        out.append({"prompt": prompt, "chosen": best, "rejected": b, "kind": "A>B"})
    for c in C[:1]:
        out.append({"prompt": prompt, "chosen": best, "rejected": c, "kind": "A>C"})
    return out


def _demo() -> int:
    from pathlib import Path as P
    from reedus_companion.knowledge.store import KnowledgeStore
    from reedus_companion import config
    store = KnowledgeStore(P(config.KNOWLEDGE_DIR), max_hot=2)
    samples = [
        ("grounded + specific",
         "Edo Tensei Minato with the Flying Thunder God Technique is unreal, he closes distance "
         "before you finish a combo. Pair him with Kakashi Hatake and you barely lose."),
        ("invented names",
         "My go-to is Edo Tsukuyomi with Super Raiton and Twin Tornado, then Kakashi drops the "
         "True Thousand Hokage Hands in the final phase."),
        ("hedged, the v2 failure",
         "Honestly I'm not sure what his abilities are. I don't really recall any specific "
         "mechanics tied to him."),
        ("near-miss, needs a human",
         "Naruto's wind blade shreds through everything once you get the timing down."),
    ]
    print(f"\n  rubric demo, {config.KNOWLEDGE_DIR}\n")
    for name, txt in samples:
        lbl, ev = classify(txt, store)
        print(f"  {lbl.upper():9} [{name}]")
        print(f"            unknown={ev['unknown']} proper={ev['proper_nouns']} "
              f"hedges={ev['hedges']}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", default=None, help="samples jsonl")
    ap.add_argument("--out", default=None, help="pairs jsonl")
    ap.add_argument("--unsure", default=None, help="write UNSURE rows here for hand-labelling")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    if a.demo or not a.inp:
        return _demo()

    if not a.out or "--i-know" not in sys.argv:
        print(__doc__.split("--- original description ---")[0], file=sys.stderr)
        print("  Use instead:\n"
              "    python -m reedus_companion.train.dpo_review --in dpo_samples.jsonl\n",
              file=sys.stderr)
        return 2

    from reedus_companion.knowledge.store import KnowledgeStore
    from reedus_companion import config
    store = KnowledgeStore(Path(config.KNOWLEDGE_DIR), max_hot=2)

    pairs, unsure, counts = [], [], {GROUNDED: 0, INVENTED: 0, HEDGED: 0, UNSURE: 0}
    for line in Path(a.inp).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        labelled = []
        for r in row.get("responses", []):
            lbl, ev = classify(r, store)
            counts[lbl] += 1
            if lbl == UNSURE:
                unsure.append({"prompt": row["prompt"], "response": r, "evidence": ev})
            else:
                labelled.append((lbl, r))
        pairs += pairs_from(row["prompt"], labelled)

    n_ab = sum(1 for p in pairs if p["kind"] == "A>B")
    n_ac = sum(1 for p in pairs if p["kind"] == "A>C")
    if a.out:
        Path(a.out).write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in pairs) + "\n")
    if a.unsure and unsure:
        Path(a.unsure).write_text(
            "\n".join(json.dumps(u, ensure_ascii=False) for u in unsure) + "\n")

    print(f"\n  labels: {counts}")
    print(f"  pairs : {len(pairs)}   A>B {n_ab}   A>C {n_ac}")
    if n_ac and n_ab:
        skew = n_ab / n_ac
        print(f"  ratio : {skew:.2f} (want ~1.0)")
        if skew > 1.6:
            print("  WARNING: heavy on A>B. That is the mix that teaches hedging, the exact")
            print("           way v2 went dry. Sample more hedged responses before training.")
        elif skew < 0.6:
            print("  WARNING: heavy on A>C. Trains confidence without teaching accuracy.")
    if unsure:
        print(f"  {len(unsure)} UNSURE rows need a human, these are the boundary cases "
              f"(near-misses and possible misattributions) and they matter most.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
