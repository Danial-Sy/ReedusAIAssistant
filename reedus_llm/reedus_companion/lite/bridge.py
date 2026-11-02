"""voice loop events -> Lite face events."""
from __future__ import annotations

import os
import time

from reedus_companion.lite import emotion
from reedus_companion.lite import protocol as P

DIM, RESET = "\033[2m", "\033[0m"


class FaceBridge:
    """voice.events listener. Everything here runs on voice-loop threads, so it only posts."""

    def __init__(self, hub, debug: bool = False):
        self.hub = hub
        self.debug = debug or os.environ.get("REEDUS_LITE_DEBUG", "0") != "0"
        self._last_wake = 0.0

    def __call__(self, kind: str, **data) -> None:
        hub = self.hub
        if kind == "state":
            hub.set_state(data.get("state") or "idle")
        elif kind == "wake":
            now = time.monotonic()
            if now - self._last_wake > 1.5:          # the listener and the loop both see it
                self._last_wake = now
                hub.send({"type": P.WAKE})
        elif kind == "heard":
            emo = emotion.react(data.get("text") or "")
            if emo:
                hub.send({"type": P.EXPR, "name": emo, "hold": 1.6})
                self._dbg(f"reacts to you: {emo}")
        elif kind == "reply":
            text = data.get("text") or ""
            emo, strength = emotion.classify(text)
            hold = emotion.speech_seconds(text) + 2.0
            hub.send({"type": P.EMOTION, "emotion": emo, "strength": strength, "hold": hold})
            self._dbg(f"face: {emo} ({strength:.2f}) for {hold:.0f}s")
        elif kind == "speech_abort":
            hub.flush()
        elif kind == "session_end":
            hub.send({"type": P.EXPR, "name": "bye", "hold": 1.8})

    def _dbg(self, msg: str) -> None:
        if self.debug:
            print(f"{DIM}  [lite] {msg}{RESET}", flush=True)
