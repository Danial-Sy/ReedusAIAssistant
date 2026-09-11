#!/usr/bin/env python3
"""cancel-during-think, extract on the router tier, PAGE_FETCH default, speakable facts."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


print("[1/5] PAGE_FETCH defaults OFF; extract not kept on main")
from reedus_companion.serve import core as C
from reedus_companion import config

check(C.PAGE_FETCH is False, f"PAGE_FETCH default False (got {C.PAGE_FETCH})")
check("extract" not in config.ROUTER_KEEP_ON_MAIN,
      f"extract not in ROUTER_KEEP_ON_MAIN ({config.ROUTER_KEEP_ON_MAIN})")
check("subjects" in config.ROUTER_KEEP_ON_MAIN, "subjects still on main")

print("\n[2/5] speakable filter still blocks SEO titles")
check(not C._fact_is_speakable(
    "Weather Today: Local Forecast & Live Weather Radar | WeatherBug"),
      "title rejected")
check(C._fact_is_speakable("Right now in Woodbridge it is clear, 72 degrees Fahrenheit."),
      "clean fact ok")

print("\n[3/5] backend cancel stops stream mid-generation")


class _ChunkyBackend:
    """Emits tokens one at a time; respects cancel Event."""

    def __init__(self):
        self.names = []
        self.cancelled_seen = False

    def chat(self, messages, max_tokens, use_adapter=True, temperature=None,
             on_token=None, tier=None, name="", cancel=None):
        self.names.append(name)
        if name != "persona":
            # routers / extract: tiny canned JSON or fact
            if name == "search-router":
                return '{"search": false, "query": ""}'
            if name == "extract":
                return "Spain won 1-0."
            return "ok"
        parts = ["Yo", ",", " still", " thinking", " about", " that", " long", " answer."]
        out = []
        for i, p in enumerate(parts):
            if cancel is not None and cancel.is_set():
                self.cancelled_seen = True
                break
            out.append(p)
            if on_token:
                on_token(p)
            # Auto-cancel after a few tokens to prove the path
            if i == 2 and cancel is not None:
                cancel.set()
        return "".join(out).strip()


be = _ChunkyBackend()
r = C.Reedus(be)
r.mem = type("M", (), {
    "embedder": None,
    "read_bulletin": lambda *a, **k: "",
    "recall": lambda *a, **k: "",
    "add_turn": lambda *a, **k: None,
    "close": lambda *a, **k: None,
})()
r._vision = False
r._cancel = threading.Event()
# the backend sets cancel mid-stream
# Pre-clear; backend sets after 3 tokens.
ans = r.reply("hey whats up")
check(r.cancelled or be.cancelled_seen, "cancel observed during persona")
check(ans == "", f"cancelled reply is empty (got {ans!r})")
check(not r.history, "cancelled turn not committed to history")

print("\n[4/5] extract call is named extract (router can pick it up)")
be2 = _ChunkyBackend()
r2 = C.Reedus(be2)
r2.mem = r.mem
r2._vision = False
# Direct extract
fact = r2._extract_fact("who won", "1. Spain beat France 1-0, final.")
check("extract" in be2.names, f"extract call recorded (names={be2.names})")
check(fact == "Spain won 1-0.", f"extract returned fact ({fact!r})")

print("\n[5/5] ThinkCancelWatcher + pick routing helpers")
from reedus_companion.voice.barge import ThinkCancelWatcher, think_barge_enabled
check(think_barge_enabled(), "think barge enabled by default")
ev = threading.Event()
# Don't start real mic, just construct
w = ThinkCancelWatcher(ev)
check(w.cancel is ev, "watcher holds cancel event")
check(w.hit is False, "not hit before start")

class _FakeLLM:
    pass


class _PickBackend:
    def __init__(self):
        self.llm = _FakeLLM()
        self._router_path = "/fake/router.gguf"
        self._router = _FakeLLM()

    def _router_llm(self):
        return self._router

    def _pick(self, use_adapter, tier, name):
        from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
        # Bind unbound method
        return LlamaCppBackend._pick(self, use_adapter, tier, name)


pb = _PickBackend()
inst, label = pb._pick(False, None, "extract")
check(label == "router" and inst is pb._router,
      f"extract routes to router (got {label})")
inst2, label2 = pb._pick(False, None, "subjects")
check(label2 == "main" and inst2 is pb.llm,
      f"subjects stays on main (got {label2})")
inst3, label3 = pb._pick(True, None, "persona")
check(label3 == "main", f"persona on main (got {label3})")

print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("ALL PASS")
