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


def public(account: Optional[dict]) -> dict:
    return {"patience": patience_id(account), "patience_choices": PATIENCE,
            "wake": wake_cutoff(account) or DEFAULT_CUTOFF, "wake_default": DEFAULT_CUTOFF,
            "wake_min": WAKE_MIN, "wake_max": WAKE_MAX}
