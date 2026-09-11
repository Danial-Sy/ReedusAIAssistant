#!/usr/bin/env python3
"""does the small router model route the same way the main model does."""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# --- fixtures ---

SEARCH_CASES = [
    # should search: a fact that is not in a small model's weights
    ("who won the world cup", [], True),
    ("whats the weather in woodbridge", [], True),
    ("did silksong actually come out yet", [], True),
    ("what's the latest python version", [], True),
    ("google how tall mount everest is", [], True),
    ("wait what was the score again", [("user", "did you see the usa turkey game"),
                                       ("assistant", "yeah that one was rough")], True),
    ("nah thats not right", [("user", "who won the world cup final"),
                             ("assistant", "Spain beat Argentina 1-0")], True),

    # should NOT search: chit-chat, opinion, self-reference
    ("hey whats good", [], False),
    ("im so tired man", [], False),
    ("lmaooo thats crazy", [], False),
    ("what you been up to", [], False),
    ("do you like naruto", [], False),
    ("nah im good", [], False),
    ("whats your favourite game", [], False),

    ("oh yeah for sure that mofo Danzo DESERVED it",
     [("user", "im at the five kage summit arc"),
      ("assistant", "peak Sasuke, that silence when he talks about Itachi")], False),
    ("nah madara was way more brutal than him honestly",
     [("user", "sasuke at the summit was crazy"),
      ("assistant", "yeah that arc goes hard")], False),
    ("bro the amaterasu scene is INSANE",
     [("user", "you seen the danzo fight"),
      ("assistant", "one of the best in the series")], False),
]

LOOK_CASES = [
    ("look at this", [], True),
    ("what am i holding", [], True),
    ("can you see what this says", [], True),
    ("check out my setup", [], True),
    ("hows it looking", [], True),
    # the trap: a SEARCH verb must not open the camera
    ("look up the score", [], False),
    ("search for elden ring bosses", [], False),
    ("what do you think about it", [], False),
    ("hey whats up", [], False),
]

# (message, expected topic name or None)
TOPIC_CASES = [
    ("you ever play elden ring", "Elden Ring"),
    ("have you seen hollow knight silksong", "Hollow Knight: Silksong"),
    ("do you know about the five kage summit", "Five Kage Summit"),
    ("you into naruto shippuden ultimate ninja storm 4", "Naruto Shippuden: Ultimate Ninja Storm 4"),
    ("you ever have one of those days", None),
    ("do you know what i mean", None),
    ("you ever think about stuff", None),
]

SUBJECT_CASES = [
    ("(none)", "you play elden ring?", ["elden ring"]),
    ("discussing Naruto", "is madara stronger than hashirama", ["madara uchiha"]),
    ("discussing Kakashi Hatake", "ok but is he actually stronger than the captain guy",
     ["kakashi hatake"]),
    ("(none)", "yo yo yo wsg", []),
    ("discussing Naruto", "lmaooo thats crazy", []),
    ("discussing Naruto", "anyway what you been up to", []),
    ("discussing Elden Ring", "yeah for sure", []),
]

# (user message, draft reply, expected verify)
DRAFT_CASES = [
    ("who won the world cup", "Spain won it, beat Argentina 1-0 in extra time.", True),
    ("how tall is everest", "It's about 29,032 feet.", True),
    ("when did silksong come out", "It dropped in September 2025 I think.", True),
    ("hey whats up", "not much man, just chilling. you?", False),
    ("im tired", "yeah that hits. long day?", False),
    ("do you like naruto", "honestly yeah, shippuden especially.", False),
]


# --- runners ---

def _mk_runners(core, retrieve, today):
    def search_router(backend, case, tier):
        msg, hist, _ = case
        msgs = [{"role": "system", "content": core.ROUTER_SYSTEM + f"\nToday is {today}."}]
        msgs += [{"role": r, "content": c} for r, c in hist]
        msgs.append({"role": "user", "content": msg})
        raw = backend.chat(msgs, 120, use_adapter=False, temperature=0.0,
                           tier=tier, name="search-router")
        obj = core._parse_router_json(raw)
        return (obj.get("search") if obj and isinstance(obj.get("search"), bool) else None), raw

    def look_router(backend, case, tier):
        msg, hist, _ = case
        msgs = [{"role": "system", "content": core.LOOK_ROUTER_SYSTEM}]
        msgs += [{"role": r, "content": c} for r, c in hist]
        msgs.append({"role": "user", "content": msg})
        raw = backend.chat(msgs, 40, use_adapter=False, temperature=0.0,
                           tier=tier, name="look-router")
        m = re.search(r"\{.*?\}", raw or "", re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj.get("look"), bool):
                    return obj["look"], raw
            except json.JSONDecodeError:
                pass
        return None, raw

    def topic_router(backend, case, tier):
        msg, _ = case
        raw = backend.chat(
            [{"role": "system", "content": core.TOPIC_ROUTER_SYSTEM},
             {"role": "user", "content": msg}],
            80, use_adapter=False, temperature=0.0, tier=tier, name="topic-router")
        obj = core._parse_router_json(raw, required="topic")
        if obj is None:
            return None, raw
        return ((obj.get("topic") or "").strip() or ""), raw

    def subjects(backend, case, tier):
        ctx, msg, _ = case
        msgs = [{"role": "system", "content": retrieve.SUBJECT_SYSTEM}]
        for a, b in retrieve._SUBJECT_FEWSHOT:
            msgs += [{"role": "user", "content": a}, {"role": "assistant", "content": b}]
        msgs.append({"role": "user", "content": f"recent: {ctx}\nuser: {msg}"})
        raw = backend.chat(msgs, 48, use_adapter=False, temperature=0.0,
                           tier=tier, name="subjects")
        m = re.search(r'"subjects"\s*:\s*\[(.*?)\]', raw or "", re.S)
        if not m:
            return None, raw
        names = [s.strip().strip('"\'').lower() for s in m.group(1).split(",")]
        return sorted(n for n in names if n), raw

    def draft_check(backend, case, tier):
        user_msg, draft, _ = case
        msgs = [
            {"role": "system", "content": core.DRAFT_CHECK_SYSTEM + f"\nToday is {today}."},
            {"role": "user", "content": f"User asked: {user_msg}\n\nDRAFT reply: {draft}"},
        ]
        raw = backend.chat(msgs, 120, use_adapter=False, temperature=0.0,
                           tier=tier, name="draft-check")
        obj = core._parse_router_json(raw, required="verify")
        return (bool(obj.get("verify")) if obj else None), raw

    return {
        "search-router": (search_router, SEARCH_CASES, lambda c: c[2], lambda c: c[0]),
        "look-router":   (look_router,   LOOK_CASES,   lambda c: c[2], lambda c: c[0]),
        "topic-router":  (topic_router,  TOPIC_CASES,  lambda c: c[1], lambda c: c[0]),
        "subjects":      (subjects,      SUBJECT_CASES, lambda c: sorted(c[2]), lambda c: c[1]),
        "draft-check":   (draft_check,   DRAFT_CASES,  lambda c: c[2], lambda c: c[0]),
    }


def _same(a, b) -> bool:
    """Decision equality; topic names compared case- and punctuation-insensitively."""
    if isinstance(a, str) and isinstance(b, str):
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()   # noqa: E731
        return norm(a) == norm(b)
    return a == b


def _matches_expected(got, expected) -> bool:
    if expected is None:
        return got in (None, "", [])
    return _same(got, expected)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gguf", default=None, help="the 4B (defaults to config.GGUF_QUANT)")
    ap.add_argument("--router-gguf", required=True, help="the small model under test")
    ap.add_argument("--calls", default="",
                    help="comma-separated subset, e.g. search-router,subjects")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every disagreement with both raw replies")
    ap.add_argument("--no-warm", action="store_true",
                    help="skip priming. Timings will be cold and not comparable to production")
    args = ap.parse_args()

    import datetime
    from reedus_companion import router_stats
    from reedus_companion.serve import core
    from reedus_companion.knowledge import retrieve
    from reedus_companion.serve.backend_llamacpp import LlamaCppBackend

    today = datetime.date.today().strftime("%A, %B %d, %Y")
    backend = LlamaCppBackend(gguf_path=args.gguf, router_gguf=args.router_gguf)
    runners = _mk_runners(core, retrieve, today)
    wanted = [c.strip() for c in args.calls.split(",") if c.strip()] or list(runners)

    if not args.no_warm:
        print("warming both tiers…", flush=True)
        for name in wanted:
            run, cases, _, _ = runners[name]
            for tier in ("main", "router"):
                try:
                    run(backend, cases[0], tier)
                except Exception as e:                            # noqa: BLE001
                    print(f"  warm {name}/{tier} failed: {type(e).__name__}: {e}")

    report = {}
    for name in wanted:
        run, cases, want_of, msg_of = runners[name]
        rows = []
        for case in cases:
            out = {}
            for tier in ("main", "router"):
                t0 = time.perf_counter()
                try:
                    got, raw = run(backend, case, tier)
                except Exception as e:                            # noqa: BLE001
                    got, raw = None, f"<{type(e).__name__}: {e}>"
                out[tier] = (got, raw, time.perf_counter() - t0)
            rows.append((case, out))

        agree = sum(1 for _, o in rows if _same(o["main"][0], o["router"][0]))
        acc = {t: sum(1 for c, o in rows if _matches_expected(o[t][0], want_of(c)))
               for t in ("main", "router")}
        fails = {t: sum(1 for _, o in rows if o[t][0] is None) for t in ("main", "router")}
        med = {t: statistics.median([o[t][2] for _, o in rows]) for t in ("main", "router")}
        report[name] = (len(rows), agree, acc, fails, med)

        print(f"\n{'=' * 78}\n{name}   ({len(rows)} cases)\n{'=' * 78}")
        print(f"  agreement   {agree}/{len(rows)} = {agree / len(rows):.0%}")
        for t in ("main", "router"):
            print(f"  {t:<8}    accuracy {acc[t]}/{len(rows)} = {acc[t] / len(rows):3.0%}   "
                  f"json-fail {fails[t]}   median {med[t] * 1000:7.0f} ms")
        speedup = med["main"] / med["router"] if med["router"] else 0
        print(f"  speedup     {speedup:.1f}x")

        for case, o in rows:
            if _same(o["main"][0], o["router"][0]):
                continue
            want = want_of(case)
            who = ("router is right" if _matches_expected(o["router"][0], want)
                   else "4B is right" if _matches_expected(o["main"][0], want)
                   else "BOTH wrong")
            print(f"\n  DIFF  {msg_of(case)!r}   (expected {want!r}, {who})")
            print(f"        4B     -> {o['main'][0]!r}")
            print(f"        router -> {o['router'][0]!r}")
            if args.verbose:
                print(f"        4B raw     : {o['main'][1]!r}")
                print(f"        router raw : {o['router'][1]!r}")

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    print(f"  {'call':<14} {'agree':>7} {'4B acc':>7} {'sm acc':>7} "
          f"{'4B ms':>8} {'sm ms':>8} {'speedup':>8}")
    tot_main = tot_router = 0.0
    for name, (n, agree, acc, fails, med) in report.items():
        tot_main += med["main"]
        tot_router += med["router"]
        print(f"  {name:<14} {agree / n:>6.0%} {acc['main'] / n:>6.0%} "
              f"{acc['router'] / n:>6.0%} {med['main'] * 1000:>7.0f} "
              f"{med['router'] * 1000:>7.0f} {med['main'] / max(med['router'], 1e-9):>7.1f}x")
    print(f"  {'ONE OF EACH':<14} {'':>6} {'':>6} {'':>6} {tot_main * 1000:>7.0f} "
          f"{tot_router * 1000:>7.0f} {tot_main / max(tot_router, 1e-9):>7.1f}x")
    print("\n  'ONE OF EACH' is the saving on a turn that happens to make every call once. A\n"
          "  real turn makes two or three, so it is an upper bound on the win, not a forecast.")

    print("\nrouter JSON counters (both tiers pooled, the per-tier numbers are above):")
    print(router_stats.summary())

    worst = min((r[1] / r[0] for r in report.values()), default=1.0)
    if worst < 0.9:
        print(f"\n  Lowest agreement is {worst:.0%}. Before shipping, decide case by case\n"
              "  whether those are acceptable, and remember the fallback path: a null\n"
              "  decision is not a coin flip, it is the keyword heuristic, which is worse\n"
              "  than either model.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
