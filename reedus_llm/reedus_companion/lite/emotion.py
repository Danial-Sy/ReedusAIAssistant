"""text -> a face. keyword + punctuation scoring, no model call.

a classifier call on gemini would cost a round trip per turn and the face has to change
the moment speech starts, so this runs on the reply text before the first sentence plays.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# (emotion, weight, pattern). patterns are matched on lowercased text with word boundaries
_RULES = [
    ("laugh", 3.0, r"\b(ha){2,}h?\b|\bhah\b|\bhe(he)+\b|\blol\b|\blmao\b|\bhilarious\b|"
                   r"\bcracks? me up\b|😂|🤣"),
    ("love", 3.0, r"\blove (you|ya|that|this|it)\b|\badore\b|\baww+\b|<3|❤|"
                  r"\bsweet(est)? of you\b|\bmeans a lot\b|\bmy favou?rite\b"),
    ("excited", 2.2, r"\bawesome\b|\bamazing\b|\bincredible\b|\blet'?s go\b|\bcan'?t wait\b|"
                     r"\bwoo+\b|\bhell yeah\b|\bno way!|\bfantastic\b|\bepic\b"),
    ("happy", 1.4, r"\bglad\b|\bgreat\b|\bnice\b|\bhappy\b|\bfun\b|\bcool\b|\byay\b|"
                   r"\bcongrat|\bperfect\b|\bthanks?\b|\bthank you\b|\bgood (job|one|call)\b|"
                   r"\bhey there\b|\bwelcome back\b|\bmissed you\b|\bproud of you\b|🙂|😊|😄"),
    ("sad", 2.2, r"\bsorry\b|\bsad\b|\bunfortunately\b|\bthat'?s rough\b|\btough\b|\bhurts?\b|"
                 r"\bcondolences\b|\boh no\b|\bheartbreak|\bmiss (him|her|them|it)\b|😢|😞"),
    ("surprised", 2.0, r"\bwhoa+\b|\bwow+\b|\bno way\b|\bseriously\?|\breally\?!|\bwhat\?!|"
                       r"\bwait,? what\b|\bholy\b|\bget out\b|😮|😲"),
    ("angry", 2.0, r"\bugh+\b|\brude\b|\bannoy(ed|ing)\b|\bhate (that|it|this)\b|"
                   r"\bridiculous\b|\bcut it out\b|😠|😤"),
    ("confused", 1.8, r"\bhmm+\b|\bnot sure\b|\bi don'?t know\b|\bno idea\b|\bconfus|"
                      r"\bwhat do you mean\b|\bunclear\b|\bbeats me\b|🤔"),
    ("sleepy", 2.0, r"\btired\b|\bsleepy\b|\byawn|\bgood ?night\b|\bnap\b|\bbedtime\b|"
                    r"\bsweet dreams\b|😴"),
    ("smug", 1.6, r"\bobviously\b|\bof course\b|\btold you\b|\bnaturally\b|\bi know,? right\b|"
                  r"\bnailed it\b|\bthank me later\b|😏"),
    ("shy", 2.2, r"\bblush|\bflattered\b|\boh stop\b|\byou'?re making me\b|\baw shucks\b|"
                 r"\bstop it,? you\b|☺"),
    ("curious", 1.0, r"\bi wonder\b|\binteresting\b|\btell me more\b|\bcurious\b|"
                     r"\bwhat about\b|\bhow come\b"),
]
_COMPILED = [(e, w, re.compile(p, re.IGNORECASE)) for e, w, p in _RULES]

# what the user says to him, the face reacts while he thinks
_USER_RULES = [
    ("love", r"\bi love you\b|\blove you\b|\byou'?re (the best|my best friend)\b|<3"),
    ("shy", r"\byou'?re (so )?(cute|adorable|smart|clever|sweet|awesome|amazing|great|funny|"
            r"the cutest)\b|\bgood (boy|bot|job reedus)\b|\bi like you\b|\bproud of you\b"),
    ("sad", r"\byou'?re (so )?(dumb|stupid|useless|annoying|boring|the worst)\b|\bshut up\b|"
            r"\bi hate you\b|\bbad (boy|bot)\b"),
    ("laugh", r"\b(ha){2,}h?\b|\blol\b|\blmao\b|\bthat'?s hilarious\b"),
    ("sleepy", r"\bgood ?night\b|\bgoing to (bed|sleep)\b|\bi'?m (so )?tired\b"),
    ("excited", r"\bguess what\b|\bi got (the|a) (job|offer)\b|\bwe won\b|\bi passed\b"),
    ("sad", r"\bi'?m (so )?(sad|down|upset|stressed|lonely)\b|\bbad day\b"),
    ("surprised", r"\bguess who\b|\bsurprise\b"),
]
_USER_COMPILED = [(e, re.compile(p, re.IGNORECASE)) for e, p in _USER_RULES]


def classify(text: str) -> Tuple[str, float]:
    """Reply text -> (emotion, intensity 0..1). 'neutral' when nothing stands out."""
    t = (text or "").strip()
    if not t:
        return "neutral", 0.0
    scores: dict = {}
    for emo, w, rx in _COMPILED:
        n = len(rx.findall(t))
        if n:
            scores[emo] = scores.get(emo, 0.0) + w * min(n, 3)
    bangs = t.count("!")
    if bangs:
        for emo in ("happy", "excited", "surprised", "laugh", "angry"):
            if emo in scores:
                scores[emo] += 0.5 * min(bangs, 4)
        if not scores:
            scores["happy"] = 0.6 * min(bangs, 3)
        if bangs >= 3 and "happy" in scores:
            scores["excited"] = scores.get("excited", 0.0) + scores["happy"]
    q = t.count("?")
    if q and not scores:
        scores["curious"] = 0.8 + 0.3 * min(q, 3)
    if "..." in t or "…" in t:
        scores["confused"] = scores.get("confused", 0.0) + 0.4
    if not scores:
        return "neutral", 0.0
    emo = max(scores, key=scores.get)
    # short replies with one hit read stronger than one hit buried in a paragraph
    words = max(1, len(t.split()))
    intensity = min(1.0, scores[emo] / (2.0 + words / 30.0))
    if intensity < 0.25:
        return "neutral", intensity
    return emo, round(intensity, 2)


def react(user_text: str) -> Optional[str]:
    """The face's instant reaction to what the user said, or None."""
    t = (user_text or "").strip()
    if not t:
        return None
    for emo, rx in _USER_COMPILED:
        if rx.search(t):
            return emo
    return None


def speech_seconds(text: str) -> float:
    """Rough speaking time, so an emotion holds for as long as the reply lasts."""
    return max(1.5, len((text or "").split()) / 2.6)
