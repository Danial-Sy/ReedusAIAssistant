"""what the voice loop is doing, for anything that wants to show it (the Reedus Lite face).

no listeners = no cost, so the robot's own loop is unchanged. listeners run on the
emitting thread and must not block.
"""
from __future__ import annotations

import threading
from typing import Callable, List

_LISTENERS: List[Callable[..., None]] = []
_LOCK = threading.Lock()

# kinds:
#   state    state=idle|listening|thinking|speaking|sleep
#   wake     the name was heard
#   heard    text=what the user said
#   reply    text=what reedus is about to say
#   speech_abort   speech was cut mid-sentence (barge-in), drop anything queued


def subscribe(fn: Callable[..., None]) -> None:
    with _LOCK:
        if fn not in _LISTENERS:
            _LISTENERS.append(fn)


def unsubscribe(fn: Callable[..., None]) -> None:
    with _LOCK:
        if fn in _LISTENERS:
            _LISTENERS.remove(fn)


def emit(kind: str, **data) -> None:
    if not _LISTENERS:
        return
    with _LOCK:
        fns = list(_LISTENERS)
    for fn in fns:
        try:
            fn(kind, **data)
        except Exception:                                        # noqa: BLE001
            pass
