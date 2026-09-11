"""few-shot voice anchoring for the cloud model, pulled from the authored convo data."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

_CACHE: Optional[str] = None

_HEADER = (
    "Here is how you actually sound. These are real exchanges in your voice, "
    "match this cadence, length and warmth. Do not quote them; they are a "
    "reference for delivery, not content:\n")


def _data_path() -> Optional[Path]:
    env = os.environ.get("REEDUS_VOICE_DATA")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    here = Path(__file__).resolve().parent.parent
    p = here / "data" / "reedus_voice.jsonl"
    return p if p.exists() else None


def _pairs(path: Path) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msgs = json.loads(line).get("messages") or []
        except ValueError:
            continue
        user = next((m.get("content", "") for m in msgs
                     if (m.get("role") or "") == "user"), "")
        asst = next((m.get("content", "") for m in msgs
                     if (m.get("role") or "") == "assistant"), "")
        if user and asst:
            out.append((user.strip(), asst.strip()))
    return out


def block(n: Optional[int] = None) -> str:
    """The exemplar block, or "" if disabled/unavailable."""
    global _CACHE
    if os.environ.get("REEDUS_PERSONA_SHOTS", "1") == "0":
        return ""
    if _CACHE is not None:
        return _CACHE

    path = _data_path()
    if path is None:
        _CACHE = ""
        return _CACHE

    try:
        limit = int(n or os.environ.get("REEDUS_PERSONA_SHOTS_N", "14"))
    except ValueError:
        limit = 14

    usable = [(u, a) for u, a in _pairs(path) if 16 <= len(a) <= 200]
    if not usable:
        _CACHE = ""
        return _CACHE

    step = max(1, len(usable) // limit)
    seen_openers, picked = set(), []
    for i in range(0, len(usable), step):
        user, asst = usable[i]
        opener = user.split()[0].lower().strip(",.!?") if user.split() else ""
        if opener in seen_openers:
            continue
        seen_openers.add(opener)
        picked.append((user, asst))
        if len(picked) >= limit:
            break
    # top up from anywhere if striding + dedupe came up short
    if len(picked) < limit:
        for user, asst in usable:
            if (user, asst) in picked:
                continue
            picked.append((user, asst))
            if len(picked) >= limit:
                break

    if not picked:
        _CACHE = ""
        return _CACHE

    lines = [_HEADER]
    for user, asst in picked:
        lines.append(f"  Them: {user}\n  You:  {asst}")
    _CACHE = "\n".join(lines)
    return _CACHE


def stats() -> str:
    path = _data_path()
    b = block()
    return (f"source={path if path else 'MISSING'} "
            f"exemplars={b.count('  Them:')} chars={len(b)} "
            f"~tokens={len(b) // 4}")


if __name__ == "__main__":
    print(stats())
    print()
    print(block())
