"""how quick each AI model is for a voice: can it answer without thinking first, and how long
it takes to say its first word. shown in the app's AI model card (and in the model list).

three sources, best first:
    tested    the app's "Test speed" button ran it (probe())
    learned   the provider turned a lower thinking level down mid-conversation (Gemini's
              backend remembers that in _THINK_LEARNED: 3.7 Flash refused "minimal", 2026-11-22)
    known     what we know about the model ahead of time (KNOWN below, Claude's own table)

plus the first-word times from real replies (record()), which is what you actually feel.
all of this lives in memory: a redeploy forgets it, the next replies fill it back in.
"""
from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from typing import Dict, Optional, Tuple

# the least thinking each model allows: "off" (none), "minimal", "low" (always thinks a little),
# "always" (a reasoning model). only what we're fairly sure of; the rest says "not tested yet"
KNOWN: Dict[str, str] = {
    "gemini-3.7-flash": "low",
    "gemini-3.1-pro-preview": "low",
    "gemini-3.5-flash-lite": "minimal",
    "deepseek-chat": "off",
    "deepseek-reasoner": "always",
}
LABELS = {
    "off": "no thinking",
    "minimal": "minimal thinking",
    "low": "always thinks a little",
    "medium": "always thinks",
    "always": "always thinks",
    "unknown": "not tested yet",
}
_lock = threading.Lock()
_speed: Dict[Tuple[str, str], deque] = {}     # first-word seconds from real replies
_probes: Dict[Tuple[str, str], dict] = {}     # "Test speed" results


def _known(pid: str, model: str) -> Optional[str]:
    if model in KNOWN:
        return KNOWN[model]
    if pid == "anthropic":
        try:
            from reedus_companion.cloud.anthropic_backend import thinks
            return "low" if thinks(model) else "off"
        except Exception:                                        # noqa: BLE001
            return None
    return None


def lowest(pid: str, model: str) -> Tuple[str, str]:
    """(level, source): the least thinking this model does before it answers."""
    with _lock:
        p = _probes.get((pid, model))
    if p and p.get("thinking") and p["thinking"] != "unknown":
        return p["thinking"], "tested"
    if pid == "gemini":
        try:
            from reedus_companion.cloud.gemini_backend import _THINK_LEARNED
            if model in _THINK_LEARNED:
                return _THINK_LEARNED[model], "learned"
        except Exception:                                        # noqa: BLE001
            pass
    k = _known(pid, model)
    return (k, "known") if k else ("unknown", "")


def record(pid: str, model: str, first_word_s: float) -> None:
    """A real reply's time to first word (no web search, at the Fastest thinking setting)."""
    if not (0 < first_word_s < 60):
        return
    with _lock:
        _speed.setdefault((pid, model), deque(maxlen=20)).append(first_word_s)


def typical(pid: str, model: str) -> Tuple[Optional[float], int]:
    with _lock:
        d = list(_speed.get((pid, model)) or [])
    return (round(statistics.median(d), 2), len(d)) if d else (None, 0)


def info(pid: str, model: str) -> dict:
    level, source = lowest(pid, model)
    first, n = typical(pid, model)
    with _lock:
        probe_ = dict(_probes.get((pid, model)) or {})
    return {"thinking": level, "label": LABELS.get(level, level), "source": source,
            "first_word_s": first, "samples": n, "probe": probe_ or None}


def annotate(pid: str, models: list) -> list:
    """The model list with each one's thinking label added (what's known or tested)."""
    out = []
    for m in models or []:
        level, source = lowest(pid, m.get("id") or "")
        out.append({**m, "thinking": level, "thinking_label": LABELS.get(level, level),
                    "thinking_source": source})
    return out


def probe(pid: str, model: str) -> dict:
    """The app's "Test speed": one tiny request at the least thinking we can ask for. Returns
    how long until the first word and the whole answer, and the least thinking it accepted.
    (a tiny prompt answers faster than his real one; it's for comparing models.)"""
    from reedus_companion.cloud import llm
    from reedus_relay.brain import apply_think
    backend = llm.build(pid, model=model)
    try:
        apply_think(backend, "fast")
        if hasattr(backend, "_search"):
            backend._search = False
        t0 = time.monotonic()
        first: list = []

        def _tok(t: str) -> None:
            if not first and t.strip():
                first.append(time.monotonic() - t0)
        backend.chat([{"role": "user", "content": "Reply with just the word: hello"}], 16,
                     use_adapter=False, on_token=_tok, name="probe")
        total = time.monotonic() - t0
    finally:
        try:
            backend.close()
        except Exception:                                        # noqa: BLE001
            pass
    level = "unknown"
    if pid == "gemini":
        from reedus_companion.cloud.gemini_backend import _THINK_LEARNED, _clamp_think
        level = _THINK_LEARNED.get(model) or _clamp_think(model, "minimal")
    elif pid == "anthropic":
        level = _known(pid, model) or "unknown"
    elif getattr(backend, "wire", "") == "responses":
        level = "low"
    else:
        level = KNOWN.get(model, "unknown")
    out = {"first_word_s": round(first[0] if first else total, 2), "total_s": round(total, 2),
           "thinking": level, "at": time.time()}
    with _lock:
        _probes[(pid, model)] = out
    return out
