"""music commands that never need a language model: "pause", "skip", "play X by Y".

a voice assistant that thinks for two seconds before pausing a song feels broken, so these go
straight to Spotify. anything fuzzier ("play something chill") falls through to the persona,
which can still reach Spotify through the apps tool.
"""
from __future__ import annotations

import random
import re
from typing import Optional, Tuple

_LEAD = r"^(?:(?:hey|ok|okay|yo)\s+)?(?:reedus[,!.]?\s+)?(?:can you\s+|could you\s+|please\s+)?"
_TAIL = r"(?:\s+(?:on|in)\s+spotify)?(?:\s+please)?[.!?]*$"
_MUSIC = r"(?:the\s+)?(?:music|song|track|spotify|playback|tunes)"


def _rx(body: str) -> re.Pattern:
    return re.compile(_LEAD + body + _TAIL, re.I)


_RULES = [
    ("pause", _rx(rf"(?:pause(?:\s+{_MUSIC}|\s+it)?|stop\s+{_MUSIC}|stop\s+playing|kill\s+{_MUSIC})")),
    ("resume", _rx(rf"(?:resume(?:\s+{_MUSIC})?|unpause(?:\s+{_MUSIC})?|keep\s+playing|"
                   rf"(?:play|continue)\s+{_MUSIC}|play\s+it\s+again)")),
    ("next", _rx(rf"(?:skip(?:\s+(?:this|it|{_MUSIC}|this\s+(?:song|one|track)))?|"
                 rf"next(?:\s+(?:song|track|one))?|play\s+the\s+next\s+(?:song|track))")),
    ("previous", _rx(r"(?:previous(?:\s+(?:song|track))?|go\s+back(?:\s+a\s+(?:song|track))?|"
                     r"(?:play\s+)?(?:the\s+)?last\s+(?:song|track)(?:\s+again)?)")),
    ("louder", _rx(rf"(?:(?:turn|crank)\s+(?:it|{_MUSIC}|the\s+volume)\s+up|"
                   rf"(?:turn|crank)\s+up\s+(?:the\s+)?(?:music|volume|song|spotify)|louder|volume\s+up)")),
    ("quieter", _rx(rf"(?:turn\s+(?:it|{_MUSIC}|the\s+volume)\s+down|"
                    rf"turn\s+down\s+(?:the\s+)?(?:music|volume|song|spotify)|quieter|softer|volume\s+down)")),
    ("now", _rx(r"(?:what(?:'s|\s+is)\s+(?:this\s+)?(?:song|playing|track)(?:\s+called)?|"
                r"what\s+song\s+is\s+(?:this|playing)|who\s+(?:sings|is\s+singing)\s+this(?:\s+song)?|"
                r"what\s+am\s+i\s+listening\s+to)")),
    ("like", _rx(r"(?:like|save|heart)\s+(?:this|this\s+song|this\s+track|the\s+song)")),
    ("shuffle_on", _rx(r"(?:shuffle(?:\s+on)?|turn\s+(?:on\s+)?shuffle(?:\s+on)?)")),
    ("shuffle_off", _rx(r"(?:stop\s+shuffling|(?:turn\s+)?shuffle\s+off|turn\s+off\s+shuffle)")),
]
_VOLUME = re.compile(_LEAD + rf"(?:set\s+)?(?:the\s+)?(?:volume|{_MUSIC})\s+(?:to\s+|at\s+)?(\d{{1,3}})"
                     r"(?:\s*%|\s+percent)?" + _TAIL, re.I)
_QUEUE = re.compile(_LEAD + r"(?:queue(?:\s+up)?|add)\s+(.+?)(?:\s+to\s+(?:the\s+)?queue)?" + _TAIL, re.I)
_MOVE = re.compile(_LEAD + rf"(?:move|switch|send|transfer)\s+(?:it|{_MUSIC})\s+to\s+(?:the\s+)?(.+?)" + _TAIL, re.I)
_PLAY = re.compile(_LEAD + r"(?:play|put\s+on|throw\s+on|blast)\s+(?:me\s+)?(?:some\s+)?(.+?)" + _TAIL, re.I)

# "play X" is also how people start games. these stay with the persona
_NOT_MUSIC = re.compile(
    r"\b(game|games|with me|pretend|tag|catch|chess|checkers|trivia|20 questions|twenty questions|"
    r"would you rather|rock paper|i spy|hide and seek|a joke|dead|fair|along|nice|it cool|"
    r"the role|a role|devil'?s advocate|hard to get)\b", re.I)
_KIND = re.compile(r"\b(?:the\s+)?(album|playlist|artist)\b", re.I)


def parse(text: str) -> Optional[Tuple[str, dict]]:
    """(action, args) or None. Pure: no network, safe to call on every turn."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or len(t) > 120:
        return None
    for action, rx in _RULES:
        if rx.match(t):
            return action, {}
    m = _VOLUME.match(t)
    if m:
        return "volume", {"percent": int(m.group(1))}
    m = _MOVE.match(t)
    if m:
        return "transfer", {"device": m.group(1)}
    m = _QUEUE.match(t)
    if m and not _NOT_MUSIC.search(m.group(1)):
        return "queue", {"query": m.group(1)}
    m = _PLAY.match(t)
    if m:
        what = m.group(1).strip()
        explicit = re.search(r"\b(spotify|song|songs|music|album|playlist|track|by)\b", t, re.I)
        if _NOT_MUSIC.search(what) and not explicit:
            return None
        if len(what.split()) > 9:
            return None
        kind = None
        k = _KIND.search(what)
        if k:
            kind = k.group(1).lower()
            what = _KIND.sub(" ", what)
        what = re.sub(r"\b(songs?|music|tracks?)\b", " ", what, flags=re.I)
        what = re.sub(r"\s+", " ", what).strip(" ,.")
        if not what:
            return "resume", {}
        return "play", {"query": what, "kind": kind}
    return None


_ACK = {
    "pause": ("Paused.", "Pausing it.", "Paused it for you."),
    "resume": ("Back on.", "Resuming.", "And we're back."),
    "next": ("Skipped.", "Next one.", "Skipping it."),
    "previous": ("Going back.", "Back one.", "Playing the last one again."),
    "shuffle_on": ("Shuffle's on.", "Shuffling."),
    "shuffle_off": ("Shuffle's off.",),
    "like": ("Saved it to your likes.", "Liked."),
}


def run(spotify, action: str, args: dict) -> str:
    """Do it and say it. Errors come back as something speakable, never an exception."""
    from reedus_companion.connect.spotify import SpotifyError
    try:
        if action in _ACK:
            spotify.control(action)
            return random.choice(_ACK[action])
        if action in ("louder", "quieter"):
            v = spotify.volume(delta=15 if action == "louder" else -15)
            return f"Volume's at {v}."
        if action == "volume":
            return f"Volume's at {spotify.volume(args['percent'])}."
        if action == "now":
            n = spotify.now()
            if not n.get("track"):
                return "Nothing's playing right now."
            return f"That's {n['track']} by {n['artist']}." if n["playing"] else \
                f"It's paused on {n['track']} by {n['artist']}."
        if action == "play":
            return "Playing " + spotify.play(args["query"], args.get("kind")) + "."
        if action == "queue":
            return "Queued " + spotify.queue(args["query"]) + "."
        if action == "transfer":
            return "Moved it to " + spotify.transfer(args["device"]) + "."
    except SpotifyError as e:
        msg = str(e)
        if msg == "no active device":
            return "Spotify isn't playing anywhere right now. Open it on your phone or computer first."
        return msg[:1].upper() + msg[1:] + ("" if msg.endswith((".", "!", "?")) else ".")
    except Exception as e:                                       # noqa: BLE001
        return f"Spotify tripped over that ({type(e).__name__})."
    return "I'm not sure what to do with that."
