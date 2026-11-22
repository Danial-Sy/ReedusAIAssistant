"""how Lite listens, set per account in the app (Settings → Listening):

    patience    how long a pause has to last before he takes it as the end of what you said.
                the early transcript starts well before that (speech.Endpointer 'pause'), so a
                longer wait costs little: the transcript is usually ready by the time it ends
    wake        how sure (60-95%) the S3's wake word has to be that it heard "Reedus". lower = he hears
                you from further away, and wakes by mistake more often (a TV, a similar word).
                the S3 reports near misses (wake_near) so the app can show how close a call
                from across the room came, and the setting can be picked from that
"""
from __future__ import annotations

from typing import Optional

PATIENCE = [
    {"id": "quick", "label": "Quick", "s": 0.8,
     "note": "Answers soon after you stop. Can cut in if you pause mid-sentence."},
    {"id": "normal", "label": "Normal", "s": 1.2,
     "note": "Waits about a second after you stop talking."},
    {"id": "patient", "label": "Patient", "s": 1.8,
     "note": "Waits almost two seconds, so thinking pauses don't get cut off."},
    {"id": "very", "label": "Longest", "s": 2.6,
     "note": "Lets you pause and think a lot. Slower to answer."},
]
# conversation: after he answers he keeps listening, so you can just reply. single: he answers
# and goes back to idle; say his name to ask something else
MODES = [
    {"id": "conversation", "label": "Conversation",
     "note": "After he answers he keeps listening for a few seconds, so you can just reply."},
    {"id": "single", "label": "One question",
     "note": "He answers and goes back to sleep. Say his name again to ask something else "
             "(he still remembers what you just asked)."},
]

# who the head turns to: a voice must be within this many dB of the loudest one since he woke
# (whoever said his name). someone in the next room is far quieter than you at the desk
HEAD = [
    {"id": "close", "label": "Close", "db": 6.0,
     "note": "Only whoever's right there talking to him. Ignores the rest of the house."},
    {"id": "normal", "label": "Normal", "db": 10.0,
     "note": "People in the room. Voices from further away don't turn him."},
    {"id": "anyone", "label": "Anyone", "db": 40.0,
     "note": "Turns to any voice he picks up, even in the next room."},
]

# how sure the wake word must be, set exactly in the app (a slider). below 60% it wakes on
# almost anything; above 95% it barely wakes at all
WAKE_MIN, WAKE_MAX = 0.60, 0.95
DEFAULT_CUTOFF = 0.90           # the model's own (wakeword/models/reedus.json)


def _settings(account: Optional[dict]) -> dict:
    return ((account or {}).get("settings") or {}).get("listen") or {}


def patience_id(account: Optional[dict]) -> str:
    v = _settings(account).get("patience")
    return v if any(p["id"] == v for p in PATIENCE) else "normal"


def end_silence(account: Optional[dict]) -> float:
    return next(p["s"] for p in PATIENCE if p["id"] == patience_id(account))


def spec_after(end_s: float) -> float:
    """When to start the early transcript: about a second before the end, so it's ready just
    as the pause turns into an answer, and not so early that every breath costs a call."""
    return max(0.35, end_s - 1.0)


def valid_cutoff(v) -> Optional[float]:
    try:
        c = round(float(v), 2)
    except (TypeError, ValueError):
        return None
    return c if WAKE_MIN - 1e-9 <= c <= WAKE_MAX + 1e-9 else None


def wake_cutoff(account: Optional[dict]) -> float:
    """What the S3 is told: the app's setting, or 0 = the firmware's own default (90%)."""
    return valid_cutoff(_settings(account).get("wake")) or 0.0


def mode_id(account: Optional[dict]) -> str:
    v = _settings(account).get("mode")
    return v if any(m["id"] == v for m in MODES) else "conversation"


def head_id(account: Optional[dict]) -> str:
    v = _settings(account).get("head")
    return v if any(h["id"] == v for h in HEAD) else "normal"


def head_near_db(account: Optional[dict]) -> float:
    return next(h["db"] for h in HEAD if h["id"] == head_id(account))


def device_cfg(account: Optional[dict]) -> dict:
    """What the S3 is told (welcome / wake_cfg)."""
    return {"cutoff": wake_cutoff(account), "head_near_db": head_near_db(account)}


def public(account: Optional[dict]) -> dict:
    return {"mode": mode_id(account), "mode_choices": MODES,
            "patience": patience_id(account), "patience_choices": PATIENCE,
            "head": head_id(account), "head_choices": HEAD,
            "wake": wake_cutoff(account) or DEFAULT_CUTOFF, "wake_default": DEFAULT_CUTOFF,
            "wake_min": WAKE_MIN, "wake_max": WAKE_MAX}
