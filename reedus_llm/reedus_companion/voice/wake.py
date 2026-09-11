"""wake / sleep phrase detection on transcribed text."""
from __future__ import annotations

import re

_WAKE_TRAIL = r"[\s,!.?…:;]*"

# leading fillers, optional hey/hi, then Reedus
_WAKE_FILLER = (
    r"(?:oh|ah|well|yeah|yep|so|uh|um|hmm|hm|"
    r"hey|hi|hello|yo|okay|ok|alright|ay|sup)"
)
_WAKE_PREFIX = re.compile(
    rf"(?is)^\s*(?:{_WAKE_FILLER}[\s,!.]+)*reedus\b{_WAKE_TRAIL}"
)
_WAKE_ONLY = re.compile(
    rf"(?is)^\s*(?:{_WAKE_FILLER}[\s,!.]+)*reedus\b{_WAKE_TRAIL}$"
)
_WAKE_GARBLED = re.compile(
    rf"(?is)^\s*(?:i(?:'?ll|'?d)?\s+)?(?:hear|here|hey|hi)\s+reedus\b{_WAKE_TRAIL}$"
)
_EMPTY_CMD = re.compile(r"^[\s,!.?…:;\"'\-—–]*$")

_REEDUS_WORD = re.compile(r"(?is)\breedus\b")
_WAKE_MAX_WORDS = 10

_STOP_LEAD = (
    r"(?:(?:hey|hi|yo|okay|ok|alright|oh|ah|well|yeah|yep|so|uh|um|okay\s+then|"
    r"alright\s+then)[\s,]+)*"
)
_STOP_CORE = (
    r"(?:"
    r"good\s*bye|goodbye|bye(?:\s+bye)?|see\s+ya|see\s+you|"
    r"go\s+to\s+sleep|go\s+sleep|sleep\s+now|night\s+night|"
    r"stop\s+listening(?:\s+please)?|"
    r"that'?s\s+all|that\s+is\s+all|i'?m\s+done|"
    r"shut\s+(?:up|down)|power\s+down|"
    r"gotta\s+go|got\s+to\s+go|i'?m\s+out|later"
    r")"
)

_DISMISS_CORE = (
    r"(?:"
    r"never\s*mind|nvm|"
    r"forget\s+(?:it|that|about\s+it)|"
    r"cancel(?:\s+that)?|"
    r"drop\s+it|"
    r"don'?t\s+worry\s+about\s+it|"
    r"figured\s+it\s+out"
    r")"
)
_DISMISS = re.compile(
    rf"(?is)^\s*(?:(?:hey|hi|yo|okay|ok)[\s,]+)?(?:reedus[\s,]+)?"
    rf"{_STOP_LEAD}{_DISMISS_CORE}\s*[.!?]?\s*$"
)
_DISMISS_ANY = re.compile(rf"(?is)\b{_DISMISS_CORE}\b")
_DISMISS_MAX_WORDS = 8
_STOP = re.compile(
    rf"(?is)^\s*(?:(?:hey|hi|yo|okay|ok)[\s,]+)?(?:reedus[\s,]+)?"
    rf"{_STOP_LEAD}{_STOP_CORE}\s*[.!?]?\s*$"
)
_STOP_STRONG = re.compile(
    r"(?is)\b(?:"
    r"good\s*bye|goodbye|bye(?:\s+bye)?|see\s+ya|see\s+you(?:\s+later)?|"
    r"go\s+to\s+sleep|stop\s+listening|gotta\s+go|got\s+to\s+go|"
    r"i'?m\s+out|power\s+down|shut\s+(?:up|down)"
    r")\b"
)
_STOP_MAX_WORDS = 14


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text or ""))


def has_wake(text: str) -> bool:
    """The utterance addresses Reedus (leading, garbled, or short and contains)."""
    t = text or ""
    if _WAKE_PREFIX.match(t) or _WAKE_GARBLED.match(t):
        return True
    if _REEDUS_WORD.search(t) and _word_count(t) <= _WAKE_MAX_WORDS:
        return True
    return False


def is_wake_only(text: str) -> bool:
    """A bare wake with no command attached."""
    t = text or ""
    if _WAKE_ONLY.match(t) or _WAKE_GARBLED.match(t):
        return True
    if _REEDUS_WORD.search(t) and _word_count(t) <= 4:
        rest = _REEDUS_WORD.sub(" ", t)
        rest = re.sub(
            rf"(?is)\b(?:{_WAKE_FILLER})\b", " ", rest,
        )
        if _EMPTY_CMD.match(rest.strip()) or not re.search(r"[A-Za-z0-9]", rest):
            return True
    return False


def strip_wake(text: str) -> str:
    """Strip a leading 'hey Reedus' so the brain sees the command."""
    t = (text or "").strip()
    if not t:
        return ""
    if _WAKE_GARBLED.match(t):
        return ""
    if _WAKE_PREFIX.match(t):
        rest = _WAKE_PREFIX.sub("", t, count=1).strip(" ,")
        if _EMPTY_CMD.match(rest):
            return ""
        return rest
    # loose: drop the first Reedus token only
    if _REEDUS_WORD.search(t):
        rest = _REEDUS_WORD.sub("", t, count=1)
        rest = re.sub(r"\s{2,}", " ", rest).strip(" ,")
        if _EMPTY_CMD.match(rest):
            return ""
        return rest
    return t


def _wake_at_start(text: str) -> bool:
    """The address opens the utterance: name first, then command."""
    t = text or ""
    return bool(_WAKE_PREFIX.match(t) or _WAKE_GARBLED.match(t))


def _wake_at_end(text: str) -> bool:
    """'Hey Reedus?' tacked onto the end of other talk."""
    return bool(re.search(
        rf"(?is)(?:{_WAKE_FILLER}[\s,!.]+)*reedus{_WAKE_TRAIL}$",
        text or "",
    ))


def from_wake_onward(text: str) -> str:
    """While idle, keep only from the wake address to the end."""
    t = (text or "").strip()
    if not t or not has_wake(t):
        return ""
    if _wake_at_start(t):
        return t
    m = re.search(
        rf"(?is)(?:{_WAKE_FILLER}[\s,!.]+)*reedus\b{_WAKE_TRAIL}.*$",
        t,
    )
    if m:
        return m.group(0).strip()
    last = None
    for hit in _REEDUS_WORD.finditer(t):
        last = hit
    if last is not None:
        return t[last.start():].strip()
    return t


def is_stop(text: str) -> bool:
    """The user is ending the session."""
    t = (text or "").strip()
    if not t:
        return False
    if _STOP.match(t):
        return True
    t_trail = re.sub(r"(?is)[\s,]+reedus\s*[.!?]?\s*$", "", t).strip()
    if t_trail != t and _STOP.match(t_trail):
        return True
    rest = strip_wake(t) if has_wake(t) else t
    if rest and _STOP.match(rest):
        return True
    if _word_count(t) <= _STOP_MAX_WORDS:
        m = _STOP_STRONG.search(t)
        if m and _word_count(t[m.end():]) <= 3:
            return True
    return False


def is_dismiss(text: str) -> bool:
    """The user is calling off the current request, not the session."""
    t = (text or "").strip()
    if not t:
        return False
    if _DISMISS.match(t):
        return True
    rest = strip_wake(t) if has_wake(t) else t
    if rest and _DISMISS.match(rest):
        return True
    if _word_count(t) <= _DISMISS_MAX_WORDS:
        m = _DISMISS_ANY.search(t)
        if m and _word_count(t[m.end():]) <= 2:
            return True
    return False


def classify_utterance(text: str) -> tuple[str, str]:
    """(kind, payload) for the session state machine."""
    t = (text or "").strip()
    if not t:
        return "idle_noise", ""
    if is_stop(t):
        return "stop", t
    if is_dismiss(t):
        return "dismiss", t
    if is_wake_only(t):
        return "wake_only", ""
    if has_wake(t):
        if _wake_at_start(t):
            cmd = strip_wake(t)
            if not cmd or _EMPTY_CMD.match(cmd):
                return "wake_only", ""
            if is_stop(cmd):
                return "stop", cmd
            if is_dismiss(cmd):
                return "dismiss", cmd
            return "wake_cmd", cmd
        tail = from_wake_onward(t)
        cmd = strip_wake(tail) if tail else ""
        if cmd and not _EMPTY_CMD.match(cmd):
            if is_stop(cmd):
                return "stop", cmd
            if is_dismiss(cmd):
                return "dismiss", cmd
            return "wake_cmd", cmd
        return "wake_only", ""
    return "turn", t
