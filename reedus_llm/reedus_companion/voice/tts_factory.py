"""build the right voice for the running mode."""
from __future__ import annotations

import os
from typing import Optional

from reedus_companion.cloud import CloudUnavailable


def make_tts(mode: Optional[str] = None, *, strict: bool = False):
    """mode: "local" | "cloud" | None (read REEDUS_MODE, default local)"""
    from reedus_companion import mode as mode_mod
    resolved = (mode or mode_mod.current() or "local").lower()

    if resolved == "cloud":
        try:
            from reedus_companion.voice.tts_inworld import InworldTTS
            return InworldTTS()
        except CloudUnavailable as e:
            if strict:
                raise
            print(f"[reedus] cloud voice unavailable ({e}); using Kokoro.", flush=True)

    from reedus_companion.voice.tts import TTS
    return TTS()
