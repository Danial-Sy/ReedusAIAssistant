#!/usr/bin/env python3
"""prove the fandom extractor works before spending hours on a build."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

os.environ.setdefault("REEDUS_FANDOM_NO_DEPS", "1")

from reedus_companion.knowledge.build_fandom import (          # noqa: E402
    all_titles, extraction_yield, fetch_page_html, html_to_text, load_wiki_list,
    sample_titles, site_stats, HARD_FLOOR_YIELD, PAUSE)

OUTLIER_FRACTION = 0.35

SAMPLES = 6
JUNK = ("v t e", "[edit", "edit source", "Categories:", "Add category", "jump to navigation")


def check_wiki(wiki: str, verbose: bool = False,
               samples: int = SAMPLES) -> Tuple[bool, str, float]:
    """(passed, one-line summary). Prints its own detail as it goes."""
    print(f"\n  {wiki}.fandom.com")
    base = f"https://{wiki}.fandom.com/api.php"

    # 1. does the wiki answer at all
    st = site_stats(wiki)
    if not st or st.get("error"):
        msg = (st or {}).get("error", "no response")
        print(f"    FAIL  siteinfo: {msg}")
        return False, f"siteinfo: {msg}", 0.0
    print(f"    site   {st['name']}, {st['articles']:,} articles")

    t0 = time.time()
    head = list(all_titles(base, limit=5))
    if not head:
        print("    FAIL  allpages returned no titles")
        return False, "allpages returned no titles", 0.0
    print(f"    enum   allpages ok in {time.time()-t0:.1f}s")

    picks = sample_titles(wiki, base, samples, head)
    if not picks:
        print("    FAIL  could not sample any titles")
        return False, "could not sample any titles", 0.0

    rows: List[Tuple[str, int, float, int]] = []
    junky = 0
    failures: List[str] = []
    for title in picks:
        _, raw, why = fetch_page_html(base, title)
        if not raw:
            failures.append(f"{title}: {why}")
            print(f"    ---   {title[:40]:42} FAILED, {why}")
            continue
        text = html_to_text(raw)
        n, y = len(text.split()), extraction_yield(raw, text)
        secs = text.count("\n== ")
        bad = [j for j in JUNK if j.lower() in text.lower()]
        if bad:
            junky += 1
        flag = f"  junk:{','.join(bad[:2])}" if bad else ""
        rows.append((title, n, y, secs))
        print(f"    ok    {title[:40]:42} {n:>6,}w  {len(raw)/1024:>6.1f}KB html  "
              f"{y:>5.1f} w/KB  {secs:>2} sec{flag}")
        if verbose:
            body = text[:600].replace("\n", "\n            ")
            print(f"            | {body}…\n")

    if not rows:
        print("    FAIL  every sampled page produced no text")
        return False, failures[0] if failures else "no text from any page", 0.0

    yields = [y for _, _, y, _ in rows]
    words = [n for _, n, _, _ in rows]
    best_y = max(yields)
    med_y = sorted(yields)[len(yields) // 2]

    ok, notes = True, []
    if len(rows) < len(picks):
        notes.append(f"{len(picks)-len(rows)}/{len(picks)} pages returned nothing")
        ok = False
    if best_y < HARD_FLOOR_YIELD:
        notes.append(f"best yield {best_y:.1f} w/KB, real HTML came back and almost no words")
        ok = False
    if junky > len(rows) // 2:
        notes.append(f"{junky}/{len(rows)} samples contain navbox/layout junk")
        ok = False

    verdict = "PASS" if ok else "FAIL"
    summary = (f"yield med {med_y:.0f} / best {best_y:.0f} w/KB, "
               f"words {min(words):,}-{max(words):,}"
               + (("; " + "; ".join(notes)) if notes else ""))
    print(f"    {verdict}  {summary}")
    return ok, summary, best_y


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wikis", nargs="*", help="subdomains to check (default: targeted list)")
    ap.add_argument("--all", action="store_true", help="check the full curated list")
    ap.add_argument("--file", default=None, help="newline-separated subdomains")
    ap.add_argument("-n", "--samples", type=int, default=SAMPLES, help="pages per wiki")
    ap.add_argument("-v", "--verbose", action="store_true", help="print extracted text")
    args = ap.parse_args()

    seeds = Path(__file__).parent / "seeds"
    wikis = list(args.wikis)
    if args.all:
        wikis += load_wiki_list(seeds / "fandom_wikis.txt")
    if args.file:
        wikis += load_wiki_list(Path(args.file).expanduser())
    if not wikis:
        wikis = load_wiki_list(seeds / "fandom_targeted.txt")
    seen: set = set()
    wikis = [w.split(":")[0] for w in wikis]
    wikis = [w for w in wikis if not (w in seen or seen.add(w))]

    print(f"\n  Fandom extractor doctor, {len(wikis)} wiki(s), {args.samples} pages each")
    print(f"  (no build, no embedder, nothing written to disk)")

    t0 = time.time()
    results: List[Tuple[str, bool, str, float]] = []
    for w in wikis:
        try:
            ok, why, best = check_wiki(w, verbose=args.verbose, samples=args.samples)
        except KeyboardInterrupt:
            print("\n  interrupted")
            break
        except Exception as e:                                   # noqa: BLE001
            ok, why, best = False, f"{type(e).__name__}: {e}", 0.0
            print(f"    FAIL  crashed: {why}")
        results.append((w, ok, why, best))
        time.sleep(PAUSE)

    good = [r for r in results if r[1]]
    bad = [r for r in results if not r[1]]
    print(f"\n{'='*74}")
    print(f"  {len(good)}/{len(results)} wikis PASSED in {time.time()-t0:.0f}s")

    # the check that actually carries signal
    if len(good) >= 4:
        ys = sorted(r[3] for r in good)
        med = ys[len(ys) // 2]
        low = [r for r in good if r[3] < med * OUTLIER_FRACTION]
        print(f"\n  extraction yield across {len(good)} wikis: "
              f"median {med:.0f} w/KB, range {ys[0]:.0f}-{ys[-1]:.0f}")
        if low:
            print(f"\n  WARNING, these passed the floor but sit far below the pack, which\n"
                  f"  usually means their skin has markup the stripper doesn't know:")
            for w, _, _, y in low:
                print(f"    {w:22} {y:.0f} w/KB  ({y/med*100:.0f}% of median)")
            print(f"  Not fatal. Inspect one before building:  "
                  f"fandom_doctor {low[0][0]} -v")
        else:
            print("  no outliers, every wiki extracts consistently")

    if bad:
        print(f"\n  FAILED, do NOT start the build until these are understood:")
        for w, _, why, _ in bad:
            print(f"    {w:22} {why}")
        print(f"\n  Same failure on every wiki  -> the extractor or the network.")
        print(f"  One or two only             -> probably a renamed subdomain; check the seed")
        print(f"                                 file, then re-run with --file <your list>.")
        print(f"  Read the actual text before believing any of this:  fandom_doctor {bad[0][0]} -v")
    else:
        print(f"\n  Extraction is working. Safe to build:")
        print(f"    ./run_fandom.sh smoke     # then:  ./run_fandom.sh build")
    print()
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
