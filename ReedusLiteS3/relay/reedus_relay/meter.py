"""who pays for this call. the shared brain code (reedus_companion) bills every LLM call,
voice synthesis and web search through cloud/budget.py; the relay wraps those functions so
each cost also lands on the Meter of whichever turn is running on this thread, and that meter
is charged to the account's pool when the turn ends.

budget.py's own monthly cap is switched off here (REEDUS_CLOUD_BUDGET=0): the pool is the cap.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import List, Tuple

_tl = threading.local()
_installed = False


class Meter:
    def __init__(self):
        self.usd = 0.0
        self.parts: List[Tuple[str, float]] = []
        self._lock = threading.Lock()

    def add(self, usd: float, what: str) -> None:
        if usd <= 0:
            return
        with self._lock:
            self.usd += usd
            self.parts.append((what, usd))

    def summary(self) -> str:
        by: dict = {}
        for what, usd in self.parts:
            by[what] = by.get(what, 0.0) + usd
        return ", ".join(f"{k} ${v:.4f}" for k, v in sorted(by.items(), key=lambda kv: -kv[1]))


@contextmanager
def metering(meter: Meter):
    """Bill whatever this thread spends inside the block to `meter`."""
    prev = getattr(_tl, "meter", None)
    _tl.meter = meter
    try:
        yield meter
    finally:
        _tl.meter = prev


def current() -> "Meter | None":
    return getattr(_tl, "meter", None)


def install() -> None:
    """Wrap budget.py's recorders once per process."""
    global _installed
    if _installed:
        return
    os.environ.setdefault("REEDUS_CLOUD_BUDGET", "0")
    from reedus_companion.cloud import budget
    from reedus_companion.cloud import providers

    orig_tokens, orig_chars, orig_search = budget.record_tokens, budget.record_chars, budget.record_searches

    def record_tokens(model, prompt_tokens, completion_tokens, grounded=False):
        m = current()
        if m is not None:
            p = budget._price(model)
            m.add(prompt_tokens / 1e6 * p.get("input", 0.0)
                  + completion_tokens / 1e6 * p.get("output", 0.0), model)
        return orig_tokens(model, prompt_tokens, completion_tokens, grounded)

    def record_chars(model, chars):
        m = current()
        if m is not None:
            per = budget.PRICES.get(model, {}).get("chars", 30.00)
            m.add(chars / 1e6 * per, model)
        return orig_chars(model, chars)

    def record_searches(provider, n):
        m = current()
        if m is not None:
            m.add(max(0, int(n)) * providers.SEARCH_PER_1000.get(provider, 10.0) / 1000.0,
                  f"{provider} search")
        return orig_search(provider, n)

    budget.record_tokens = record_tokens
    budget.record_chars = record_chars
    budget.record_searches = record_searches
    _installed = True
