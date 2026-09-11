#!/usr/bin/env python3
"""is searxng actually a better snippet source than DDG."""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

CASES = [
    ("weather woodbridge va",
     r"\b\d{1,3}\s*(?:°|degrees?|f\b|fahrenheit)", "a temperature"),

    ("current ceo of openai",
     r"\baltman\b", "the name"),

    # version string: short, exact, easy for a snippet to omit
    ("latest python version release",
     r"\b3\.1[0-9](?:\.\d+)?\b", "a 3.x version number"),

    ("hollow knight silksong release date",
     r"\b(?:20\d\d|september|august)\b", "a date"),

    ("elden ring shadow of the erdtree dlc bosses",
     r"\b(?:messmer|radahn|bayle|rellana|midra)\b", "a named boss"),

    # numeric sports/stat shape, no proper noun to anchor on
    ("how tall is mount everest in feet",
     r"\b29[,.]?0?3\d\b", "the height"),
]


def _match(results, rx) -> bool:
    return any(rx.search((r.get("snippet") or "") + " " + (r.get("title") or ""))
               for r in results)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-r", "--runs", type=int, default=3,
                    help="times to repeat each query per provider (default 3)")
    ap.add_argument("-n", "--results", type=int, default=5,
                    help="results requested per query (default 5)")
    ap.add_argument("-p", "--providers", default="ddg,searxng")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every snippet")
    ap.add_argument("--sleep", type=float, default=1.0,
                    help="pause between calls; DDG rate-limits and will CAPTCHA a tight loop")
    args = ap.parse_args()

    from reedus_companion.serve import websearch as ws

    fns = {"ddg": ws._search_ddg, "searxng": ws._search_searxng}
    providers = [p.strip() for p in args.providers.split(",") if p.strip() in fns]

    engine_ok: dict = defaultdict(int)
    engine_bad: dict = defaultdict(int)
    if "searxng" in providers:
        _orig = ws._search_searxng

        def _instrumented(query, n):
            import requests
            r = requests.get(f"{ws.SEARXNG_URL}/search",
                             params={"q": query, "format": "json", "language": "en",
                                     "safesearch": 0, "categories": "general"},
                             headers={"User-Agent": "Reedus/1.0"}, timeout=20)
            r.raise_for_status()
            data = r.json()
            for item in (data.get("results") or []):
                for e in (item.get("engines") or []):
                    engine_ok[e] += 1
            for pair in (data.get("unresponsive_engines") or []):
                engine_bad[f"{pair[0]}: {pair[1] if len(pair) > 1 else '?'}"] += 1
            return [{"title": i.get("title", ""), "url": i.get("url", ""),
                     "snippet": i.get("content", "") or ""}
                    for i in (data.get("results") or [])[:n]]

        fns["searxng"] = _instrumented

    summary = {}
    for prov in providers:
        print(f"\n{'=' * 74}\n{prov.upper()}  ({args.runs} runs x {len(CASES)} cases, "
              f"{args.results} results each)\n{'=' * 74}")
        hits = 0
        total = 0
        flaky, dead, lengths, errors = [], [], [], 0
        for query, pattern, want in CASES:
            rx = re.compile(pattern, re.I)
            got = []
            for _ in range(args.runs):
                try:
                    res = fns[prov](query, args.results)
                except Exception as e:                            # noqa: BLE001
                    print(f"  !! {type(e).__name__}: {e}")
                    errors += 1
                    got.append(False)
                    continue
                lengths.extend(len((r.get("snippet") or "")) for r in res)
                ok = _match(res, rx)
                got.append(ok)
                if args.verbose:
                    print(f"  [{'HIT ' if ok else 'miss'}] {query}")
                    for r in res[:3]:
                        print(f"        {(r.get('snippet') or '')[:110]}")
                time.sleep(args.sleep)
            n_hit = sum(got)
            hits += n_hit
            total += len(got)
            mark = "OK  " if n_hit == len(got) else ("FLAKY" if n_hit else "DEAD")
            print(f"  {mark:5s} {n_hit}/{len(got)}  {query!r}  (wanted {want})")
            if 0 < n_hit < len(got):
                flaky.append(query)
            elif n_hit == 0:
                dead.append(query)

        rate = hits / total if total else 0.0
        med = statistics.median(lengths) if lengths else 0
        summary[prov] = (rate, len(flaky), len(dead), med)
        print(f"\n  hit rate  {hits}/{total} = {rate:.0%}")
        print(f"  flaky     {len(flaky)}  {flaky}")
        print(f"  dead      {len(dead)}  {dead}")
        print(f"  snippet   median {med:.0f} chars "
              f"(cut at {ws.SNIPPET_CHARS} before the extractor sees it)")
        if errors:
            print(f"  errors    {errors}")

    if "searxng" in summary and engine_ok:
        print(f"\n{'=' * 74}\nSEARXNG BACKENDS\n{'=' * 74}")
        for e, c in sorted(engine_ok.items(), key=lambda kv: -kv[1]):
            print(f"  answered  {c:4d} results   {e}")
        for e, c in sorted(engine_bad.items(), key=lambda kv: -kv[1]):
            print(f"  BLOCKED   {c:4d} queries   {e}")
        if len(engine_ok) <= 2:
            print("\n  Only a couple of backends are answering, so the multi-engine merge that\n"
                  "  justifies running this at all is mostly not happening. Enable engines that\n"
                  "  don't CAPTCHA datacenter traffic (mojeek, wikipedia, wikidata, marginalia)\n"
                  "  in searxng/settings.yml before concluding SearXNG can't beat DDG.")

    if len(summary) == 2:
        d, s = summary.get("ddg"), summary.get("searxng")
        print(f"\n{'=' * 74}\nVERDICT\n{'=' * 74}")
        print(f"  ddg      {d[0]:.0%} hit, {d[1]} flaky, {d[2]} dead, {d[3]:.0f}-char snippets")
        print(f"  searxng  {s[0]:.0%} hit, {s[1]} flaky, {s[2]} dead, {s[3]:.0f}-char snippets")
        if s[0] > d[0] + 0.05:
            print("\n  SearXNG wins on answerability. Leave the auto-detect on.")
        elif d[0] > s[0] + 0.05:
            print("\n  DDG still wins. Do not switch on a story about how it ought to work,\n"
                  "  either fix the blocked engines above or leave REEDUS_SEARCH_PROVIDER=ddg.")
        else:
            print("\n  Too close to call at this sample size. Re-run with -r 5 before deciding;\n"
                  "  the flaky count matters more than the rate if they tie.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
