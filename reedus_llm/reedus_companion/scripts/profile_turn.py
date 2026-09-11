"""per-turn latency profiler. monkeypatches from outside, nothing in serve/ imports it."""
from __future__ import annotations
import os
import sys
import time
import argparse
from collections import defaultdict

os.environ.setdefault("REEDUS_PROFILE", "1")

SPANS: list[tuple[str, float]] = []
COUNTS: defaultdict[str, int] = defaultdict(int)


class span:
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t = time.perf_counter()
        return self

    def __exit__(self, *a):
        SPANS.append((self.name, time.perf_counter() - self.t))
        COUNTS[self.name] += 1
        return False


def _wrap(obj, attr, label, unwrap_self=True):
    orig = getattr(obj, attr)

    def wrapped(*a, **k):
        with span(label):
            return orig(*a, **k)
    setattr(obj, attr, wrapped)
    return orig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("messages", nargs="*", default=["hey whats up"])
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--gguf", default=None)
    ap.add_argument("--router-gguf", default=None,
                    help="small GGUF for the router/classifier calls. Omit for single-model.")
    ap.add_argument("--warm", action="store_true",
                    help="prime the persona prompt before timing, as the real entry points do. "
                         "Without this, turn 1 pays the ~13s cold prefill and looks anomalous.")
    args = ap.parse_args()
    if args.no_cache:
        os.environ["REEDUS_PROMPT_CACHE"] = "0"

    from reedus_companion.serve import core as coremod
    from reedus_companion.serve import memory as memmod
    from reedus_companion.serve import websearch as wsmod
    from reedus_companion.serve.backend_llamacpp import LlamaCppBackend

    # instrument the pieces we care about
    _wrap(memmod._Embedder, "embed", "  · embedding (fastembed)")
    _wrap(memmod.Memory, "recall", "  · episodic recall (sqlite+cosine)")
    _wrap(memmod.Memory, "add_turn", "episodic write (embed+insert)")
    _wrap(coremod.Reedus, "_system", "prompt assembly (_system)")
    _wrap(coremod.Reedus, "_bulletin_for", "  · bulletin relevance gate")
    _wrap(coremod.Reedus, "_route_search", "search router (model call)")
    _wrap(coremod.Reedus, "_verify_draft", "draft verify (model call)")
    _wrap(coremod.Reedus, "_extract_fact", "fact extract (model call)")
    _wrap(coremod, "web_search", "web_search (network)")

    t0 = time.perf_counter()
    backend = LlamaCppBackend(gguf_path=args.gguf, router_gguf=args.router_gguf)
    load_s = time.perf_counter() - t0
    r = coremod.Reedus(backend)
    warm_s = 0.0
    if args.warm:
        warm_s = r.warm()
        LlamaCppBackend.PROFILE.clear()      # don't bill warm-up to turn 1
    print(f"\nmodel load: {load_s:.1f}s   embedder: {r.mem.embedder.mode}"
          f"{f'   warm: {warm_s:.1f}s' if args.warm else ''}\n")

    grand = 0.0
    for i, msg in enumerate(args.messages, 1):
        SPANS.clear()
        COUNTS.clear()
        LlamaCppBackend.PROFILE.clear()
        t = time.perf_counter()
        answer = r.reply(msg)
        turn = time.perf_counter() - t
        grand += turn

        print(f"=== turn {i}: {msg!r}  ->  {turn:.2f}s")
        calls = LlamaCppBackend.PROFILE
        model_s = sum(c["wall_s"] for c in calls)
        ptot = sum(c["prompt_tokens"] for c in calls)
        gtot = sum(c["completion_tokens"] for c in calls)
        print(f"    model calls: {len(calls)}   prompt tokens {ptot}   generated {gtot}   "
              f"{model_s:.2f}s ({model_s / turn * 100:.0f}% of turn)")
        for j, c in enumerate(calls, 1):
            pt, gt, w = c["prompt_tokens"], c["completion_tokens"], c["wall_s"]
            label = c.get("name") or f"{c['system_head']!r}..."
            ttft = c.get("ttft_s") or 0.0
            extra = f"  {ttft:5.2f}s pre" if ttft else ""
            print(f"      call {j}: {c.get('tier', 'main'):<6} {label:<14} "
                  f"prompt={pt:5d} gen={gt:4d}  {w:6.2f}s{extra}")
        rt = sum(c["wall_s"] for c in calls if c.get("tier") == "router")
        if rt:
            n_rt = sum(1 for c in calls if c.get("tier") == "router")
            print(f"    router tier: {n_rt} call(s)  {rt:.2f}s "
                  f"({rt / turn * 100:.0f}% of turn, {rt / max(model_s, 1e-6) * 100:.0f}% "
                  f"of model time)")
        agg: defaultdict[str, float] = defaultdict(float)
        for name, dt in SPANS:
            agg[name] += dt
        print("    non-inference breakdown:")
        for name, dt in sorted(agg.items(), key=lambda x: (name.startswith("  ·"), -x[1])):
            if name.endswith("(model call)"):
                continue
            print(f"      {name:38s} {dt * 1000:8.1f} ms  x{COUNTS[name]}")
        top_level = sum(dt for n, dt in agg.items()
                        if not n.endswith("(model call)") and not n.startswith("  ·"))
        other = turn - model_s - top_level
        print(f"      {'everything else (python/regex/io)':38s} {other * 1000:8.1f} ms")
        # whole reply, truncating at 100 chars looked like the model got cut off
        print(f"    reply: {answer!r}\n")

    print(f"TOTAL {len(args.messages)} turns: {grand:.1f}s  "
          f"({grand / max(len(args.messages), 1):.1f}s/turn)")
    from reedus_companion import router_stats
    print("\nrouter JSON:")
    print(router_stats.summary())
    r.close()


if __name__ == "__main__":
    main()
