"""wake-word session state machine, pure logic, no audio."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from reedus_companion.voice.wake import classify_utterance, from_wake_onward, has_wake


@dataclass
class SessionEvent:
    """One step the outer loop should take."""
    action: str          # ignore | arm | turn | stop | stay
    text: str = ""       # user text for the brain (wake stripped)
    jingle: str = ""     # listen | ready | sleep | ""
    note: str = ""


@dataclass
class WakeSession:
    active: bool = False
    empty_streak: int = 0
    max_empty: int = 3          # consecutive empty/noise -> sleep
    log: List[str] = field(default_factory=list)

    def on_transcript(self, text: str) -> SessionEvent:
        """Feed one STT result; get the next action."""
        t = (text or "").strip()
        if not t:
            self.empty_streak += 1
            if self.active and self.empty_streak >= self.max_empty:
                self.active = False
                self.empty_streak = 0
                self.log.append("timeout→idle")
                return SessionEvent("stop", jingle="sleep",
                                    note="session timed out (no speech)")
            return SessionEvent("ignore", note="empty")

        dropped_preamble = False
        if not self.active and has_wake(t):
            gated = from_wake_onward(t)
            if gated and gated != t:
                dropped_preamble = True
                t = gated
            elif not gated:
                return SessionEvent("ignore", note="no wake")

        kind, payload = classify_utterance(t)
        self.empty_streak = 0

        if not self.active:
            if kind == "wake_only":
                self.active = True
                self.log.append("wake_only")
                note = "listening"
                if dropped_preamble:
                    note = "listening (ignored talk before wake)"
                return SessionEvent("arm", jingle="listen", note=note)
            if kind == "wake_cmd":
                self.active = True
                self.log.append(f"wake_cmd:{payload!r}")
                return SessionEvent("turn", text=payload, jingle="listen",
                                    note="wake+command")
            if kind in ("stop", "dismiss"):
                return SessionEvent("ignore", note=f"{kind} while idle")
            return SessionEvent("ignore", note="no wake")

        # in session
        if kind == "stop":
            self.active = False
            self.log.append("stop")
            return SessionEvent("stop", jingle="sleep", note="goodbye")
        if kind == "dismiss":
            self.log.append("dismiss")
            return SessionEvent("arm", jingle="ready", note="okay, still listening")
        if kind == "wake_only":
            # re-arm: user said the name again mid-session
            return SessionEvent("arm", jingle="listen", note="re-arm")
        if kind == "wake_cmd":
            return SessionEvent("turn", text=payload, note="in-session wake+cmd")
        # bare turn (or wake already stripped upstream)
        return SessionEvent("turn", text=payload or t, note="turn")

    def after_reply(self) -> SessionEvent:
        """Call after TTS finishes, cues the user they can keep talking."""
        if not self.active:
            return SessionEvent("ignore")
        return SessionEvent("stay", jingle="ready", note="ready for next")
