#!/usr/bin/env python3
"""staged diagnostic for the grounding stack, finds the layer that eats the machine."""

from __future__ import annotations

import argparse
import os
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

_T0 = time.time()


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def mark(label: str) -> None:
    print(f"  [{time.time() - _T0:6.1f}s] peak RSS {_rss_mb():8.0f} MB   {label}", flush=True)


def available_mb() -> float:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    except Exception:                                            # noqa: BLE001
        pass
    return float("nan")


QUERIES = [
    "man i love rinnegan sasuke",
    "kakashi vs itachi who takes it",
    "whats your favorite naruto team comp",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", type=int, default=3, choices=[1, 2, 3, 4])
    ap.add_argument("--pairs", type=int, default=None,
                    help="override REEDUS_PAIR_PASSES for this run")
    args = ap.parse_args()
    if args.pairs is not None:
        os.environ["REEDUS_PAIR_PASSES"] = str(args.pairs)

    print(f"physical memory available at start: {available_mb():,.0f} MB")
    print(f"REEDUS_PAIR_PASSES={os.environ.get('REEDUS_PAIR_PASSES', '1')}  "
          f"REEDUS_BINDING_BLOCK={os.environ.get('REEDUS_BINDING_BLOCK', '1')}\n")

    # stage 1: entity index
    print("stage 1, entity index")
    from reedus_companion.knowledge.entity_index import EntityIndex
    ix = EntityIndex()
    ix.db
    mark("opened")
    t = time.time()
    b = ix.binding_block(["Sasuke Uchiha", "Naruto Uzumaki", "Kakashi Hatake"])
    mark(f"binding_block in {(time.time() - t) * 1000:.0f} ms, {len(b)} chars")
    if args.stage < 2:
        return 0

    # stage 2: embedder
    print("\nstage 2, embedder")
    from reedus_companion.serve.memory import Memory
    mem = Memory()
    mark("Memory() constructed")
    t = time.time()
    mem.embedder.embed(["warmup"])
    mark(f"first embed in {(time.time() - t) * 1000:.0f} ms")
    if args.stage < 3:
        return 0

    # stage 3: corpus retrieval
    # the suspect layer: every query is a real multi-shard search over the corpus
    print("\nstage 3, corpus retrieval (the layer that ate the box)")
    from reedus_companion.knowledge import Knowledge
    from reedus_companion import config
    kb = Knowledge(embed=mem.embedder.embed, max_hot=config.KNOWLEDGE_MAX_HOT, debug=True)
    mark("Knowledge() constructed")
    for q in QUERIES:
        t = time.time()
        ctx = kb.context_for(q)
        mark(f"context_for({q[:34]!r}) {(time.time() - t):.2f}s, {len(ctx)} chars")
        if available_mb() < 1500:
            print("\n  !! under 1.5 GB available, stopping before the box starts swapping.")
            print("     Re-run with --pairs 0, and if that fixes it the shard cache is the "
                  "problem,\n     not the entity index.")
            return 1
    if args.stage < 4:
        print("\n  stage 3 clean. If the box only dies at stage 4, it is the MODEL plus the "
              "corpus\n  competing for RAM, not anything in the grounding path.")
        return 0

    # stage 4: model
    print("\nstage 4, model (this is the one that needs headroom)")
    from reedus_companion.serve.backend_hf import HFBackend
    backend = HFBackend(load_4bit=True, base_only=True)
    mark("HFBackend loaded")
    from reedus_companion.serve.core import Reedus
    bot = Reedus(backend)
    mark("Reedus constructed")
    t = time.time()
    out = bot.reply(QUERIES[0])
    mark(f"first full reply in {time.time() - t:.1f}s")
    print(f"\n  reply: {out[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
