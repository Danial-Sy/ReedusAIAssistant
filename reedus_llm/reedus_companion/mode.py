"""local vs cloud mode: which brain and voice this process uses."""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

LOCAL, CLOUD = "local", "cloud"

_PROMPT = "Enable local mode or cloud mode?"
_REASK = "Sorry, local, or cloud?"

_CLOUD_RE = re.compile(r"\b(cloud[a-z]*|crowd[a-z]*|clout|klaud|glaud|"
                       r"loud|allowed|aloud|online|remote|gemini)\b", re.I)
_LOCAL_RE = re.compile(r"\b(local[a-z]*|locale|lokal|vocal|logical|"
                       r"offline|home|here|normal|usual)\b", re.I)

_STRONG_VERB = r"(?:switch|change|flip|swap|go|jump|move|turn|revert|drop|kick)"
_WEAK_VERB = (r"(?:enable|activate|engage|start|launch|boot|load|use|run|put|set"
              r"|give\s+me|i\s+want|lets|let's|do)")
_SWITCH_RE = re.compile(
    rf"\b{_STRONG_VERB}\s+(?:me\s+|us\s+|yourself\s+|back\s+|over\s+|on\s+|in\s+)*"
    rf"(?:to\s+|into\s+|onto\s+)?(?P<mode>cloud|crowd|loud|local|locale|vocal|offline|online)\b",
    re.I)
_ENABLE_RE = re.compile(
    rf"\b(?:{_STRONG_VERB}|{_WEAK_VERB})\b[^.?!]{{0,24}}?"
    rf"\b(?P<mode>cloud|crowd|loud|local|locale|vocal|offline|online)\s*mode\b", re.I)
# "cloud mode." said alone, as an instruction
_BARE_RE = re.compile(
    r"^\W*(?P<mode>cloud|crowd|local|offline|online)\s*mode\W*$", re.I)

_CURRENT: Optional[str] = None


def current() -> str:
    """The mode this process is running in."""
    if _CURRENT:
        return _CURRENT
    env = (os.environ.get("REEDUS_MODE") or "").strip().lower()
    return env if env in (LOCAL, CLOUD) else LOCAL


def set_current(mode: str) -> str:
    """Pin the mode for this process (and any child that reads the environment)."""
    global _CURRENT
    _CURRENT = CLOUD if str(mode).lower() == CLOUD else LOCAL
    os.environ["REEDUS_MODE"] = _CURRENT
    return _CURRENT


def classify(text: str) -> Optional[str]:
    """"cloud" / "local" / None if the answer was neither."""
    said = (text or "").strip()
    if not said:
        return None
    cloud, local = bool(_CLOUD_RE.search(said)), bool(_LOCAL_RE.search(said))
    if cloud and not local:
        return CLOUD
    if local and not cloud:
        return LOCAL
    if cloud and local:
        # "not local, cloud": take the last one mentioned
        return CLOUD if said.lower().rfind("cloud") > said.lower().rfind("local") else LOCAL
    return None


def switch_request(text: str) -> Optional[str]:
    """"switch to cloud mode" -> "cloud". None when this is an ordinary sentence."""
    said = (text or "").strip()
    if not said:
        return None
    for rx in (_BARE_RE, _ENABLE_RE, _SWITCH_RE):
        m = rx.search(said)
        if not m:
            continue
        word = m.group("mode").lower()
        if word in ("cloud", "crowd", "loud", "online"):
            return CLOUD
        if word in ("local", "locale", "vocal", "offline"):
            return LOCAL
    return None


def knowledge_enabled() -> bool:
    """Should the local knowledge corpus be consulted this turn?"""
    explicit = (os.environ.get("REEDUS_KNOWLEDGE") or "").strip()
    if explicit:
        return explicit != "0"
    return current() != CLOUD


def local_search_enabled() -> bool:
    """Should core.py do its own search routing and topic learning this turn?"""
    explicit = (os.environ.get("REEDUS_LOCAL_SEARCH") or "").strip()
    if explicit:
        return explicit != "0"
    return current() != CLOUD


def preflight(mode: str) -> tuple[bool, str]:
    """Can cloud mode actually run? Returns (ok, reason)."""
    if mode != CLOUD:
        return True, ""
    if not (os.environ.get("GEMINI_API_KEY") or "").strip():
        return False, "I don't have a Gemini key."
    if not (os.environ.get("INWORLD_API_KEY") or "").strip():
        return False, "I don't have an Inworld key."
    try:
        from reedus_companion.cloud import budget
        if budget.exhausted():
            return False, (f"this month's cloud budget is spent, "
                           f"${budget.spent():.2f} of ${budget.cap():.2f}.")
    except Exception:
        pass
    try:
        import google.genai                                     # noqa: F401
    except ImportError:
        return False, ("the cloud packages aren't installed, "
                       "pip install -r reedus_companion/requirements/cloud.txt")
    return True, ""


def ask(speaker, stt, hear: Callable[[], str], *, attempts: int = 2) -> str:
    """Speak the prompt, listen, return "local" or "cloud"."""
    env = (os.environ.get("REEDUS_MODE") or "").strip().lower()
    if env in (LOCAL, CLOUD):
        print(f"[reedus] mode pinned by REEDUS_MODE={env}")
        return set_current(env)

    question = _PROMPT
    for attempt in range(max(1, attempts)):
        try:
            if speaker is not None:
                speaker.say(question)
                speaker.finish(timeout=20.0)
            else:
                print(f"Reedus: {question}")
        except Exception:
            print(f"Reedus: {question}")

        said = ""
        try:
            audio = hear()
            said = audio if isinstance(audio, str) else (stt.transcribe(audio) or "")
        except (KeyboardInterrupt, EOFError):
            raise
        except Exception as e:                                   # noqa: BLE001
            print(f"[mode] could not hear an answer ({type(e).__name__}: {e})")

        print(f"[mode] heard: {said!r}")
        choice = classify(said)
        if choice:
            return set_current(choice)
        question = _REASK

    print("[mode] no clear answer, defaulting to local.")
    try:
        if speaker is not None:
            speaker.say("No answer, so I'm starting local. "
                        "Say switch to cloud mode whenever you want it.")
            speaker.finish(timeout=25.0)
    except Exception:
        pass
    return set_current(LOCAL)
