"""cloud spend meter and cap. fails open on any error, a broken ledger must never mute him."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

PRICES: Dict[str, Dict[str, float]] = {
    "gemini-3.7-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.6-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "inworld-tts-2": {"chars": 30.00},
    "inworld-tts-2-flash": {"chars": 15.00},
}

GROUNDING_FREE_PER_MONTH = 5000
GROUNDING_PER_1000 = 14.00

_LOCK = threading.RLock()


def _ledger_path() -> Path:
    explicit = os.environ.get("REEDUS_CLOUD_LEDGER")
    if explicit:
        return Path(explicit).expanduser()
    root = os.environ.get("REEDUS_ROOT") or str(Path.home() / "Reedus")
    return Path(root).expanduser() / "cloud_ledger.json"


def _period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def cap() -> float:
    try:
        return float(os.environ.get("REEDUS_CLOUD_BUDGET", "35.00"))
    except ValueError:
        return 35.00


def free_tts_minutes() -> float:
    try:
        return float(os.environ.get("REEDUS_INWORLD_FREE_MINUTES", "40"))
    except ValueError:
        return 40.0


def _blank(period: str, carry: Optional[Dict] = None) -> Dict:
    return {"period": period, "usd": 0.0, "grounded_calls": 0, "turns": 0,
            "by_model": {}, "started": time.time(),
            # survives the monthly roll, see free_tts_minutes()
            "lifetime_tts_s": float((carry or {}).get("lifetime_tts_s", 0.0))}


def _load() -> Dict:
    p, period = _ledger_path(), _period()
    try:
        data = json.loads(p.read_text())
    except Exception:
        return _blank(period)
    if not isinstance(data, dict) or data.get("period") != period:
        return _blank(period, carry=data if isinstance(data, dict) else None)
    return data


def _save(data: Dict) -> None:
    p = _ledger_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(p)                      # atomic, a torn ledger reads as blank
    except Exception:
        pass                                # fail open


def record_tokens(model: str, prompt_tokens: int, completion_tokens: int,
                  grounded: bool = False) -> float:
    """Bill one LLM call. Returns the month-to-date total in USD."""
    price = PRICES.get(model)
    if price is None:
        price = max(PRICES.values(), key=lambda d: d.get("output", 0.0))
    cost = (prompt_tokens / 1e6) * price.get("input", 0.0) \
         + (completion_tokens / 1e6) * price.get("output", 0.0)
    with _LOCK:
        data = _load()
        if grounded:
            data["grounded_calls"] = int(data.get("grounded_calls", 0)) + 1
            over = data["grounded_calls"] - GROUNDING_FREE_PER_MONTH
            if over > 0:
                cost += GROUNDING_PER_1000 / 1000.0
        data["usd"] = float(data.get("usd", 0.0)) + cost
        data["turns"] = int(data.get("turns", 0)) + 1
        by = data.setdefault("by_model", {})
        row = by.setdefault(model, {"usd": 0.0, "in": 0, "out": 0})
        row["usd"] += cost
        row["in"] += int(prompt_tokens)
        row["out"] += int(completion_tokens)
        _save(data)
        return float(data["usd"])


def record_chars(model: str, chars: int) -> float:
    """Bill one TTS synthesis. Returns the month-to-date total in USD."""
    price = PRICES.get(model, {}).get("chars", 30.00)
    cost = (chars / 1e6) * price
    with _LOCK:
        data = _load()
        data["usd"] = float(data.get("usd", 0.0)) + cost
        by = data.setdefault("by_model", {})
        row = by.setdefault(model, {"usd": 0.0, "chars": 0})
        row["usd"] += cost
        row["chars"] = int(row.get("chars", 0)) + int(chars)
        _save(data)
        return float(data["usd"])


_TTS_WARN_AT = (0.50, 0.80, 0.95, 1.0)
_TTS_WARNED: set = set()


def record_audio(model: str, chars: int, seconds: float) -> None:
    """Bill a synthesis and track it against the inworld free allotment."""
    record_chars(model, chars)
    limit_s = free_tts_minutes() * 60.0
    with _LOCK:
        data = _load()
        used = float(data.get("lifetime_tts_s", 0.0)) + max(0.0, float(seconds))
        data["lifetime_tts_s"] = used
        _save(data)
    if limit_s <= 0:
        return                                  # paid plan
    frac = used / limit_s
    for mark in _TTS_WARN_AT:
        if frac >= mark and mark not in _TTS_WARNED:
            _TTS_WARNED.add(mark)
            left = max(0.0, (limit_s - used) / 60.0)
            if frac >= 1.0:
                print(f"\n[reedus] *** INWORLD FREE TIER EXHAUSTED *** "
                      f"{used / 60:.1f} of {limit_s / 60:.0f} free minutes used. "
                      f"The next synthesis will likely fail and Reedus will fall "
                      f"back to Kokoro. Upgrade the Inworld plan, or set "
                      f"REEDUS_INWORLD_FREE_MINUTES=0 once you have.\n", flush=True)
            else:
                print(f"[reedus] inworld free tier {frac * 100:.0f}% used "
                      f"({used / 60:.1f}/{limit_s / 60:.0f} min, ~{left:.1f} min left)",
                      flush=True)
            break


def tts_minutes_used() -> float:
    return float(_load().get("lifetime_tts_s", 0.0)) / 60.0


def spent() -> float:
    with _LOCK:
        return float(_load().get("usd", 0.0))


def exhausted() -> bool:
    """The month's spend has passed the cap. Fails open on any error."""
    try:
        c = cap()
        if c <= 0:
            return False
        return spent() >= c
    except Exception:
        return False


def summary() -> str:
    d = _load()
    c = cap()
    line = f"{d.get('period', '?')}: ${d.get('usd', 0.0):.2f}"
    if c > 0:
        line += f" of ${c:.2f}"
    line += f"  ({d.get('turns', 0)} turns, {d.get('grounded_calls', 0)} grounded)"
    free_m = free_tts_minutes()
    used_m = float(d.get("lifetime_tts_s", 0.0)) / 60.0
    if free_m > 0:
        state = "EXHAUSTED" if used_m >= free_m else f"{free_m - used_m:.1f} min left"
        line += f"\n    inworld free tier      {used_m:6.1f} / {free_m:.0f} min   {state}"
    else:
        line += f"\n    inworld (paid)         {used_m:6.1f} min synthesised"
    for model, row in sorted(d.get("by_model", {}).items()):
        detail = f"in={row['in']} out={row['out']}" if "in" in row else f"chars={row.get('chars', 0)}"
        line += f"\n    {model:24s} ${row['usd']:6.3f}  {detail}"
    return line


if __name__ == "__main__":
    print(summary(), file=sys.stdout)
