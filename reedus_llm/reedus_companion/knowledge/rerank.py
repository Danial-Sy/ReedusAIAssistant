"""grounding filter over the vector hits. this, not cosine, decides relevance."""

from __future__ import annotations

import os
import re
from typing import Iterable, List, Sequence

ANCHOR_WEIGHT = float(os.environ.get("REEDUS_ANCHOR_WEIGHT", "0.25"))

MAX_PER_TOPIC = int(os.environ.get("REEDUS_MAX_PER_TOPIC", "3"))

MIN_ANCHOR = float(os.environ.get("REEDUS_MIN_ANCHOR", "0.35"))

MIN_SUBJECT_MASS = float(os.environ.get("REEDUS_MIN_SUBJECT_MASS", "6.0"))
SUBJECT_FULL_MASS = float(os.environ.get("REEDUS_SUBJECT_FULL_MASS", "8.0"))

_WORD = re.compile(r"[a-z0-9']+")

_STOP = frozenset("""
a about actually after again all also am an and another any anyone anything are around as ask
at away back bad be because been before being best better big both but buy by call came can
cant come could couldnt did didnt do does doesnt doing done dont down each even ever every
everything feel few find first for from get gets getting give go going good got great had has
have havent having he her here hes him his how however i id if ill im in into is isnt it its
ive just keep kind know known lately least less let lets like little ll long look looking lot
made make makes many may maybe me mean means might mine more most much must my need never new
next no not nothing now of off oh ok okay on once one only or other our out over own people
pretty probably put question quite rather real really right said same say says see seen shes
should shouldnt show since so some someone something sort still stuff such sure take tell than
that thats the their them then there these they thing things think this those though thought
through time to together too took totally true try trying two under until up us use used using
very want wanted was wasnt way we well went were weve what whats when where which while who
whole why will with without wont would wouldnt yeah yes yet you youd your youre yours
dude bud buddy bro bruh fam mate pal homie yo hey hiya sup wassup goin goin' gonna wanna
lol lmao lmfao haha hehe ngl tbh fr rn imo imho idk idc omg wtf peak goated lit dope ez gg
yea yep nah nope damn dang aight
""".split())


_DF: dict | None = None
_DF_N: int = 0                   # passages in that sample
_DF_DEFAULT_IDF: float = 6.0

DF_FILE = "_termdf.json"


def load_df(root=None) -> None:
    """Load the corpus term-frequency table written by knowledge.reindex, once."""
    global _DF, _DF_N
    if _DF is not None:
        return
    import json
    from pathlib import Path
    if root is None:
        from reedus_companion import config
        root = getattr(config, "KNOWLEDGE_DIR", config.MEMORY_DIR / "knowledge")
    p = Path(root) / DF_FILE
    try:
        blob = json.loads(p.read_text())
        _DF, _DF_N = blob["df"], int(blob["n"])
    except Exception:
        _DF, _DF_N = {}, 0


def idf(term: str) -> float:
    """IDF clamped to [0.5, 8]. Rare terms decide, common ones nudge."""
    load_df()
    if not _DF_N:
        return 1.0
    import math
    df = _DF.get(term, 0)
    return max(0.5, min(8.0, math.log((_DF_N + 1) / (df + 1))))


def terms(text: str, min_len: int = 3) -> set:
    """Content words: lowercased, stopworded, short tokens dropped."""
    return {w for w in _WORD.findall((text or "").lower())
            if len(w) >= min_len and w not in _STOP}


CONTEXT_DISCOUNT = float(os.environ.get("REEDUS_CONTEXT_DISCOUNT", "0.2"))


def anchor_score(query_terms: set, topic: str, title: str, text: str,
                 context: frozenset = frozenset(), use_body: bool = True) -> float:
    """How strongly this passage is tied to what's being talked about, in [0, 1]."""
    if not query_terms:
        return 0.0
    q_w = {t: idf(t) for t in query_terms}
    q_total = sum(q_w.values()) or 1.0

    subject = terms(f"{topic} {title}")
    if subject:
        s_w = {t: idf(t) for t in subject}
        matched = sum(w * (CONTEXT_DISCOUNT if t in context else 1.0)
                      for t, w in s_w.items() if t in query_terms)
        if matched < MIN_SUBJECT_MASS:
            topic_hit = 0.0
        else:
            ratio = matched / (sum(s_w.values()) or 1.0)
            topic_hit = min(1.0, matched / SUBJECT_FULL_MASS) * (0.5 + 0.5 * ratio)
    else:
        topic_hit = 0.0

    if not use_body:
        return topic_hit
    body = terms(text)
    body_hit = sum(w for t, w in q_w.items() if t in body) / q_total if body else 0.0
    return max(topic_hit, min(body_hit, 1.0))


def ground(hits: Sequence, query: str, k: int,
           require_anchor: bool = True, context: str = "") -> List:
    """Filter and reorder vector hits by how well they're anchored to the conversation."""
    q = terms(query)
    ctx = frozenset(terms(context))
    scored = []
    for h in hits:
        a = anchor_score(q, getattr(h, "topic", ""), getattr(h, "title", ""),
                         getattr(h, "text", ""), context=ctx)
        if require_anchor and a < MIN_ANCHOR:
            continue
        h.anchor = a
        h.rank_score = float(h.score) + ANCHOR_WEIGHT * a
        scored.append(h)
    scored.sort(key=lambda p: -p.rank_score)

    out, per_topic = [], {}
    for h in scored:
        t = (getattr(h, "topic", "") or "").lower()
        if per_topic.get(t, 0) >= MAX_PER_TOPIC:
            continue
        per_topic[t] = per_topic.get(t, 0) + 1
        out.append(h)
        if len(out) >= k:
            break
    return out
