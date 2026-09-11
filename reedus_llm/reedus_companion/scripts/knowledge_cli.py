#!/usr/bin/env python3
"""inspect and seed the corpus."""

from __future__ import annotations

import argparse
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from reedus_companion import config                          # noqa: E402
from reedus_companion.knowledge import Knowledge, domains    # noqa: E402


def build(debug=True) -> Knowledge:
    from reedus_companion.serve.memory import _Embedder
    emb = _Embedder()
    kb = Knowledge(embed=emb.embed, max_hot=config.KNOWLEDGE_MAX_HOT, debug=debug)
    if emb.mode != "fastembed":
        print(f"  WARNING: embedder mode is {emb.mode!r}, not 'fastembed'. Retrieval quality\n"
              f"  will be poor, install fastembed before seeding a corpus you care about.\n")
    return kb


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats")
    p_learn = sub.add_parser("learn")
    p_learn.add_argument("topic", nargs="*")
    p_learn.add_argument("--file", help="newline-separated topics")
    p_learn.add_argument("--domain", default=None, help="force a domain")
    p_learn.add_argument("--wiki", action="append", default=None,
                         help="extra MediaWiki api.php base, tried before Wikipedia")
    p_learn.add_argument("--expand", type=int, default=0, metavar="N",
                         help="also learn the N most central pages each topic links to. "
                              "This is how the corpus grows at a useful rate, 'Naruto' "
                              "--expand 15 pulls the series, characters and arcs too.")
    p_learn.add_argument("--depth", type=int, default=1,
                         help="expansion depth (default 1). 2 multiplies by --expand again "
                              "and gets slow; ~3s per topic.")
    p_ask = sub.add_parser("ask")
    p_ask.add_argument("query", nargs="+")
    p_ask.add_argument("-k", type=int, default=3)
    p_route = sub.add_parser("route")
    p_route.add_argument("query", nargs="+")
    args = ap.parse_args()

    if args.cmd == "route":
        kb = Knowledge(embed=lambda t: [[0.0] * 384 for _ in t])
        print("  ->", kb.route(" ".join(args.query)))
        return 0

    if args.cmd == "stats":
        kb = build(debug=False)
        rows = kb.stats()
        if not rows:
            print(f"\n  corpus empty ({kb.root})")
            print("  seed it:  knowledge_cli learn \"Elden Ring Nightreign\"")
            return 0
        print(f"\n  {kb.root}")
        print(f"  {'domain':18}{'passages':>10}{'size':>10}")
        tn = tm = 0
        for d, n, mb in rows:
            tn += n
            tm += mb
            print(f"  {d:18}{n:>10}{mb:>9.1f}M")
        print(f"  {'TOTAL':18}{tn:>10}{tm:>9.1f}M")
        print(f"\n  ~{tm/max(1,tn)*1000:.1f} KB per passage")
        return 0

    kb = build()

    if args.cmd == "learn":
        topics = list(args.topic)
        if args.file:
            topics += [l.strip() for l in Path(args.file).read_text().splitlines()
                       if l.strip() and not l.startswith("#")]
        if not topics:
            print("nothing to learn")
            return 1
        total, t0 = 0, time.time()
        if args.expand:
            def prog(name, n, i):
                mark = f"+{n:>3}" if n else "  ."
                print(f"  [{i:>4}] {mark}  {name[:64]}")
            for t in topics:
                total += kb.learn_expanded(t, breadth=args.expand, depth=args.depth,
                                           domain=args.domain, extra_wikis=args.wiki,
                                           on_progress=prog)
        else:
            for t in topics:
                n = kb.learn(t, domain=args.domain, extra_wikis=args.wiki)
                total += n
                if n == 0:
                    print(f"  (skip) {t}, already known, or no article found")
        mins = (time.time() - t0) / 60
        print(f"\n  {total} passages added in {mins:.1f} min")
        return 0

    if args.cmd == "ask":
        q = " ".join(args.query)
        hits = kb.recall(q, k=args.k)
        if not hits:
            print(f"\n  nothing retrieved for {q!r}")
            print("  either the corpus doesn't cover it, or min_score is too high "
                  "(REEDUS_KNOWLEDGE_MIN_SCORE)")
            return 0
        print(f"\n  {len(hits)} passages for {q!r}:\n")
        for h in hits:
            print(f"  [{h.score:.2f}] {h.title}")
            print(f"        {h.text[:220]}…\n")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
