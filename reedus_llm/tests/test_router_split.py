#!/usr/bin/env python3
"""Regression suite for the dual-model router split."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reedus_companion import config, router_stats             # noqa: E402
from reedus_companion.serve.backend_llamacpp import LlamaCppBackend   # noqa: E402

_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


def _backend(router_path):
    """A LlamaCppBackend with the routing state set by hand and no model behind it."""
    b = object.__new__(LlamaCppBackend)
    b.llm = "BIG"
    b._router_path = router_path
    b._router = "SMALL" if router_path else None
    return b


# --- single-model fallback ---
print("[1/5] no router model configured -> nothing changes, at all")
single = _backend(None)
for name in ("persona", "search-router", "extract", "subjects", ""):
    llm, tier = single._pick(use_adapter=(name == "persona"), tier=None, name=name)
    check(llm == "BIG" and tier == "main",
          f"{name or '(unnamed)'} stays on the 4B when REEDUS_ROUTER_MODEL is unset")

# --- the split itself ---
print("\n[2/5] router model configured -> aux calls move, persona calls do not")
split = _backend("/models/qwen3-1.7b-Q4_K_M.gguf")

PERSONA_CALLS = ["persona", "persona-retry", "persona-redo"]
for name in PERSONA_CALLS:
    llm, tier = split._pick(use_adapter=True, tier=None, name=name)
    check(llm == "BIG" and tier == "main", f"{name} stays on the 4B, this is the VOICE")

ROUTED_CALLS = ["search-router", "topic-router",
                "draft-check", "forced-query", "domain-router"]
for name in ROUTED_CALLS:
    llm, tier = split._pick(use_adapter=False, tier=None, name=name)
    check(llm == "SMALL" and tier == "router", f"{name} moves to the small model")
check(not (set(ROUTED_CALLS) & set(config.ROUTER_KEEP_ON_MAIN)),
      "no call is claimed by both the routed list and the pin list")

# --- the two that must not move ---
print("\n[3/5] extract and topic-brief are pinned to the 4B")
for name in config.ROUTER_KEEP_ON_MAIN:
    llm, tier = split._pick(use_adapter=False, tier=None, name=name)
    check(llm == "BIG" and tier == "main", f"{name} is held on the 4B by ROUTER_KEEP_ON_MAIN")
# the pinned set is measured (see config.ROUTER_KEEP_ON_MAIN). asserted so a "tidy up the
# defaults" can't quietly undo it. extract is deliberately unpinned.
check(set(config.ROUTER_KEEP_ON_MAIN) == {"topic-brief", "subjects", "correction",
                                          "look-router"},
      "the default pin list is topic-brief + subjects + correction + look-router "
      "(extract rides the 1.7B)")

print("\n[4/5] explicit overrides win over both")
check(split._pick(use_adapter=False, tier="main", name="search-router") == ("BIG", "main"),
      "tier='main' pins an aux call to the 4B")
check(split._pick(use_adapter=False, tier="router", name="extract") == ("SMALL", "router"),
      "tier='router' forces a pinned call onto the small model (for eval_router_split)")
broken = _backend("/models/does-not-exist.gguf")
broken._router = False
check(broken._pick(use_adapter=False, tier=None, name="search-router") == ("BIG", "main"),
      "a router model that failed to load degrades to the 4B, it does not raise")

# --- call sites really pass names ---
print("\n[5/5] every backend.chat call site in core.py names itself")
src = (Path(__file__).resolve().parent.parent
       / "reedus_companion" / "serve" / "core.py").read_text()
calls = re.findall(r"self\.backend\.chat\((?:[^()]|\([^()]*\))*\)", src, re.S)
unnamed = [c for c in calls if "name=" not in c]
check(len(calls) >= 12, f"found {len(calls)} backend.chat call sites (expected 12+)")
for c in unnamed:
    print(f"        unnamed: {' '.join(c.split())[:100]}")
check(not unnamed, f"all {len(calls)} call sites pass name= ({len(unnamed)} missing)")

router_stats.reset()
router_stats.note("search-router", True)
router_stats.note("search-router", False)
check(abs(router_stats.failure_rate("search-router") - 0.5) < 1e-9,
      "router_stats counts failures (50% on one ok + one fail)")
router_stats.note("unnamed-is-ignored", True)
router_stats.reset()
check(router_stats.failure_rate() == 0.0 and "no router JSON" in router_stats.summary(),
      "router_stats.reset() clears the counters between measured runs")

print("\n[6/6] /no_think is appended to router calls only")
from reedus_companion.serve.backend_llamacpp import _no_think        # noqa: E402
sysmsg = [{"role": "system", "content": "ROUTE THIS"}, {"role": "user", "content": "hi"}]
out = _no_think(sysmsg)
check(out[0]["content"].endswith("/no_think"), "the switch lands on the system message")
check(sysmsg[0]["content"] == "ROUTE THIS",
      "the caller's list is NOT mutated (history slices get reused across turns)")
check(_no_think(out)[0]["content"].count("/no_think") == 1,
      "applying it twice does not stack (retry loops re-send the same messages)")
check(_no_think([{"role": "user", "content": "hi"}])[0]["content"] == "hi",
      "a message list with no system prompt is left alone")

print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("ALL PASS")
