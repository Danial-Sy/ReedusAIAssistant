#!/usr/bin/env python3
"""hand-label sampled replies fast. one keypress each, resumable."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

LABELS = {"g": "grounded", "i": "invented", "p": "pseudo", "h": "hedged",
          "o": "offtarget", "s": "skip"}
WRAP = 96


def _getch() -> str:
    try:
        import termios, tty                                    # noqa: E401
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return (sys.stdin.readline() or "s").strip()[:1] or "s"


def _wrap(text: str, width: int = WRAP, indent: str = "    ") -> str:
    import textwrap
    out = []
    for para in (text or "").split("\n"):
        out += textwrap.wrap(para, width, initial_indent=indent,
                             subsequent_indent=indent) or [""]
    return "\n".join(out)


def _similarity(a: str, b: str) -> float:
    """Jaccard token overlap, enough to pick the nearest sibling of a few."""
    sa, sb = set((a or "").lower().split()), set((b or "").lower().split())
    return len(sa & sb) / (len(sa | sb) or 1)


def build_pairs(labelled: Dict[str, Dict[str, str]], samples: List[dict]) -> List[dict]:
    """A>B, A>P, A>C and A>O per prompt."""
    pairs = []
    for row in samples:
        p = row["prompt"]
        marks = labelled.get(p, {})
        A = [r for r in row["responses"] if marks.get(r) == "grounded"]
        B = [r for r in row["responses"] if marks.get(r) == "invented"]
        P = [r for r in row["responses"] if marks.get(r) == "pseudo"]
        C = [r for r in row["responses"] if marks.get(r) == "hedged"]
        O = [r for r in row["responses"] if marks.get(r) == "offtarget"]
        if not A:
            continue
        for bucket, kind in ((B, "A>B"), (P, "A>P"), (C, "A>C"), (O, "A>O")):
            if not bucket:
                continue
            rej = bucket[0]
            chosen = max(A, key=lambda c: _similarity(c, rej))
            pairs.append({"prompt": p, "chosen": chosen, "rejected": rej, "kind": kind})
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", default="dpo_samples.jsonl")
    ap.add_argument("--labels", default="dpo_labels.json", help="resumable progress file")
    ap.add_argument("--pairs", default=None, help="write pairs and exit (no labelling)")
    ap.add_argument("--reset", action="store_true",
                    help="discard all labels and start over (keeps a .bak)")
    ap.add_argument("--prune", action="store_true",
                    help="triage PROMPTS first: drop ones that can't produce a domain claim")
    a = ap.parse_args()

    pruned = Path(a.inp).with_suffix(".pruned.jsonl")
    if not a.prune and "--in" not in sys.argv and pruned.exists():
        print(f"\n  using {pruned.name} ({sum(1 for l in pruned.read_text().splitlines() if l.strip())}"
              f" prompts). Pass --in {a.inp} to use the unpruned set.")
        a.inp = str(pruned)

    if a.prune:
        rows = [json.loads(l) for l in Path(a.inp).read_text().splitlines() if l.strip()]
        print(f"\n  {len(rows)} prompts. k = keep, d = drop, q = stop here.")
        print("  drop the ones that can't produce a domain claim (greetings, pure confusion).\n")
        kept = []
        for i, row in enumerate(rows, 1):
            hist = [m for m in row.get("messages", []) if m.get("role") != "system"][:-1]
            prev = next((" ".join(m["content"].split())[:70]
                         for m in reversed(hist) if m["role"] == "user"), "")
            print(f"  [{i}/{len(rows)}] {'(prev: ' + prev + ')' if prev else ''}")
            print(f"      > {row['prompt'][:WRAP-8]}")
            sys.stdout.write("      k/d/q > ")
            sys.stdout.flush()
            k = (_getch() or "k").lower()
            print(k)
            if k == "q":
                kept += rows[i - 1:]
                break
            if k != "d":
                kept.append(row)
        out = Path(a.inp).with_suffix(".pruned.jsonl")
        out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n")
        print(f"\n  kept {len(kept)}/{len(rows)} -> {out}")
        print(f"  label them with:  python -m reedus_companion.train.dpo_review --in {out}\n")
        return 0

    if a.reset:
        p = Path(a.labels)
        if p.exists():
            n = sum(len(v) for v in json.loads(p.read_text()).values())
            bak = p.with_suffix(".json.bak")
            p.replace(bak)
            print(f"\n  cleared {n} label(s) -> {bak}")
        else:
            print("\n  nothing to clear")
        print("  start fresh with:  python -m reedus_companion.train.dpo_review "
              f"--in {a.inp}\n")
        return 0

    samples = [json.loads(l) for l in Path(a.inp).read_text().splitlines() if l.strip()]
    lab_path = Path(a.labels)
    labelled: Dict[str, Dict[str, str]] = (
        json.loads(lab_path.read_text()) if lab_path.exists() else {})

    if a.pairs:
        pairs = build_pairs(labelled, samples)
        ab = sum(1 for p in pairs if p["kind"] in ("A>B", "A>P"))
        ac = sum(1 for p in pairs if p["kind"] == "A>C")
        Path(a.pairs).write_text(
            "\n".join(json.dumps(p, ensure_ascii=False) for p in pairs) + "\n")
        from collections import Counter
        kinds = dict(Counter(x["kind"] for x in pairs))
        print(f"\n  {len(pairs)} pairs -> {a.pairs}   {kinds}")
        if ac == 0:
            print("  no A>C pairs, train_dpo will refuse. Label some replies 'h', or")
            print("  generate the hedged arm from the v31 adapter (see --help notes).")
        return 0

    todo = [(row, r) for row in samples for r in row["responses"]
            if labelled.get(row["prompt"], {}).get(r) is None]
    done = sum(len(v) for v in labelled.values())
    print(f"\n  {len(todo)} to label, {done} already done.")
    print("  keys:  o offtarget (wrong question)  <- check this FIRST")
    print("         g grounded (right)   i invented (wrong)   p pseudo (nothing to check)")
    print("         h hedged (admits it)   s skip   q save+quit")
    print("  ask: (1) does it answer what I asked?  (2) could someone check it, is it right?\n")

    counts: Dict[str, int] = {}
    for n, (row, resp) in enumerate(todo, 1):
        print("=" * WRAP)
        print(f"  [{n}/{len(todo)}]")
        hist = [m for m in row.get("messages", []) if m.get("role") != "system"][:-1]
        for m in hist[-4:]:
            who = "you" if m["role"] == "user" else "reedus"
            line = " ".join((m.get("content") or "").split())
            print(f"  {who:>7} | {line[:WRAP-12]}")
        print(f"  {'YOU':>7} > {row['prompt'][:WRAP-12]}")
        print("-" * WRAP)
        print(_wrap(resp))
        print("-" * WRAP)
        sys.stdout.write("  o/g/i/p/h/s (q=quit) > ")
        sys.stdout.flush()
        k = (_getch() or "s").lower()
        print(k)
        if k == "q":
            break
        lbl = LABELS.get(k, "skip")
        labelled.setdefault(row["prompt"], {})[resp] = lbl
        counts[lbl] = counts.get(lbl, 0) + 1
        if n % 10 == 0:
            lab_path.write_text(json.dumps(labelled, ensure_ascii=False))

    lab_path.write_text(json.dumps(labelled, ensure_ascii=False))
    print(f"\n  this session: {counts}")
    print(f"  saved -> {lab_path}")
    pairs = build_pairs(labelled, samples)
    from collections import Counter
    kinds = Counter(p["kind"] for p in pairs)
    print(f"  pairs available now: {len(pairs)}  {dict(kinds)}")
    print(f"\n  write them with:  python -m reedus_companion.train.dpo_review \\")
    print(f"      --in {a.inp} --pairs dpo_pairs.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
