"""router json parse accounting."""
from __future__ import annotations
from typing import Dict

#: call name -> {"ok": n, "fail": n}
ROUTER_JSON: Dict[str, Dict[str, int]] = {}


def note(call: str, ok: bool) -> None:
    """Record one parse attempt. Unnamed calls are ignored rather than bucketed together."""
    if not call:
        return
    row = ROUTER_JSON.setdefault(call, {"ok": 0, "fail": 0})
    row["ok" if ok else "fail"] += 1


def reset() -> None:
    ROUTER_JSON.clear()


def failure_rate(call: str = "") -> float:
    """Failure rate for one call, or across all of them when `call` is empty."""
    rows = [ROUTER_JSON[call]] if call else list(ROUTER_JSON.values())
    fail = sum(r["fail"] for r in rows) if rows else 0
    total = sum(r["ok"] + r["fail"] for r in rows) if rows else 0
    return (fail / total) if total else 0.0


def summary() -> str:
    """One line per call: successes, failures, failure rate."""
    if not ROUTER_JSON:
        return "  no router JSON parsed this session"
    out = []
    for call in sorted(ROUTER_JSON):
        r = ROUTER_JSON[call]
        n = r["ok"] + r["fail"]
        out.append(f"  {call:<14} {r['ok']:>4} ok  {r['fail']:>3} fail  "
                   f"{100.0 * r['fail'] / max(n, 1):5.1f}% fail")
    return "\n".join(out)
