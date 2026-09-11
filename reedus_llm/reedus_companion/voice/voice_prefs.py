"""one place that decides what he sounds like, per locale and per backend."""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

_KOKORO_LANG: Dict[str, str] = {"en-US": "a", "en-GB": "b"}

# locale -> kokoro voice blend [(voice, weight), ...]
_KOKORO_BLEND: Dict[str, List[Tuple[str, float]]] = {
    "en-US": [("am_michael", 0.6), ("am_fenrir", 0.4)],
    "en-GB": [("bm_daniel", 0.6), ("bm_fable", 0.4)],
}

_INWORLD_VOICE: Dict[str, str] = {
    "en-US": os.environ.get("REEDUS_INWORLD_VOICE_US", "Edward"),
    "en-GB": os.environ.get("REEDUS_INWORLD_VOICE_GB", "Theodore"),
}

_DEFAULT = "en-GB"
_OVERRIDE: Optional[str] = None


def set_locale(locale: str) -> None:
    """For the in-app dropdown. Takes effect on next TTS build."""
    global _OVERRIDE
    _OVERRIDE = _normalise(locale)


def _normalise(raw: str) -> str:
    if not raw:
        return _DEFAULT
    tag = raw.replace("_", "-").split(".")[0].strip()
    if not tag or tag.lower() in ("c", "posix"):
        return _DEFAULT
    parts = tag.split("-")
    lang = parts[0].lower()
    region = parts[1].upper() if len(parts) > 1 else ""
    if lang != "en":
        return _DEFAULT
    tag = f"en-{region}" if region else _DEFAULT
    return tag if tag in _KOKORO_LANG else _DEFAULT


def locale() -> str:
    if _OVERRIDE:
        return _OVERRIDE
    env = os.environ.get("REEDUS_LOCALE")
    if env:
        return _normalise(env)
    if os.environ.get("REEDUS_LOCALE_AUTO") == "1":
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            val = os.environ.get(var)
            if val:
                got = _normalise(val)
                if got:
                    return got
    return _DEFAULT


def kokoro_lang() -> str:
    return _KOKORO_LANG.get(locale(), "a")


def kokoro_blend() -> List[Tuple[str, float]]:
    """Explicit REEDUS_VOICE wins; a bare voice name or a blend spec like bm_daniel:0.6,bm_fable:0.4."""
    override = (os.environ.get("REEDUS_VOICE") or "").strip()
    if override:
        if ":" in override or "," in override:
            out: List[Tuple[str, float]] = []
            for part in override.split(","):
                name, _, weight = part.partition(":")
                try:
                    out.append((name.strip(), float(weight or 1.0)))
                except ValueError:
                    continue
            if out:
                return out
        return [(override, 1.0)]
    return list(_KOKORO_BLEND.get(locale(), _KOKORO_BLEND[_DEFAULT]))


def kokoro_fallback_voice() -> str:
    """Single voice used if blending fails."""
    blend = kokoro_blend()
    return blend[0][0] if blend else "am_michael"


def inworld_voice() -> str:
    override = (os.environ.get("REEDUS_INWORLD_VOICE") or "").strip()
    if override:
        return override
    return _INWORLD_VOICE.get(locale(), _INWORLD_VOICE[_DEFAULT])


def describe() -> str:
    return (f"locale={locale()} kokoro={kokoro_lang()}/"
            f"{'+'.join(v for v, _ in kokoro_blend())} inworld={inworld_voice()}")
