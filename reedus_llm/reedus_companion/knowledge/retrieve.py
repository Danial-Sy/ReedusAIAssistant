"""corpus routing + retrieval, the one entry point core.py talks to."""

from __future__ import annotations

import itertools
import os
import re
import time
from pathlib import Path
from typing import Callable, List, Optional

from .. import router_stats
from . import domains
from .ingest import ingest_topic
from .rerank import ground
from .store import KnowledgeStore, Passage, rank_domains


def _confident_entities_unused(ents: List[str], ix) -> bool:
    """Is the rarity path's answer good enough to skip the model call?"""
    if not ents or len(ents) > 2 or ix is None:
        return False
    try:
        for e in ents:
            if len(e.split()) >= 2 and ix.has_topic(e):
                continue
            if ix.is_person(e):
                continue
            return False
    except Exception:                                            # noqa: BLE001
        return False
    return True


def _unit(v):
    import numpy as np
    a = np.asarray(v, dtype=np.float32)
    return a / (float(np.linalg.norm(a)) or 1.0)

# retrieved text in the prompt. sized against prefill: 1800 chars is ~450 tokens is ~6s of
# re-prefill every turn the block churns. REEDUS_KNOWLEDGE_CHARS.
MAX_INJECT_CHARS = int(os.environ.get("REEDUS_KNOWLEDGE_CHARS", "900"))
DEFAULT_K = int(os.environ.get("REEDUS_KNOWLEDGE_K", "6"))

CANDIDATES = int(os.environ.get("REEDUS_KNOWLEDGE_CANDIDATES", "60"))

SUBJECT_CARRY_ANCHOR = float(os.environ.get("REEDUS_SUBJECT_CARRY", "0.75"))
SUBJECT_MAX_MISSES = int(os.environ.get("REEDUS_SUBJECT_MAX_MISSES", "2"))

# two-name queries per turn. default 1 for memory not cpu: each pass is shard reads, and going
# 4 -> 7 searches a turn swapped the box to death. watch RSS in diagnose_grounding.py if raising.
MAX_PAIR_PASSES = int(os.environ.get("REEDUS_PAIR_PASSES", "1"))

MAX_ENTITY_PASSES = int(os.environ.get("REEDUS_ENTITY_PASSES", "3"))
ENTITY_MIN_IDF = float(os.environ.get("REEDUS_ENTITY_MIN_IDF", "6.0"))

_CHAT_WORDS = frozenset("""
dude bud buddy bro bruh fam mate man pal homie yo hey hiya sup wassup guy guys
lol lmao lmfao haha hehe ngl tbh fr rn imo imho af idk idc omg wtf ffs
peak goated fire lit dope sick sheesh bet based cringe mid ez gg glhf
yeah yea yep nah nope damn dang bruv innit aight
fav fave favs favorite favourite favorites favourites comp comps vibe vibes stuff thing things
hello hullo howdy greetings whos whose whats hows thats theres watching watched watch
talk talking talks chat chatting speak speaking particular because think thinking
reading read playing played play fun cool nice great awesome character characters
pivot pivoting switch switching topic topics conclusion ending intro anyway
wow woah whoa oof huh oh ohh ah ahh hm hmm ok okay alright aight sheesh
""".split())


# LLM subject detection, off: 3.6-3.9s on the xeon every retrieval turn and it returned (none)
# or reprinted a hint the caller already had. the regex entity pass still runs. REEDUS_LLM_SUBJECT=1.
LLM_SUBJECT = os.environ.get("REEDUS_LLM_SUBJECT", "0") == "1"

SUBJECT_SYSTEM = """Name the specific thing the user's message is ABOUT, so it can be looked up
in an encyclopedia.

Reply with ONLY JSON: {"subjects": ["...", "..."]}, at most two, most important first.

Rules:
- Use the full proper name as an encyclopedia would title it: "Elden Ring", not "elden";
  "Sasuke Uchiha", not "sasuke"; "Five Kage Summit", not "the summit arc".
- Carry the subject over from earlier turns ONLY when the message ASKS something that needs it
  resolved: "what about him", "is the other one better", "how does that compare".
- Return [] when the message needs nothing looked up, even mid-conversation. Reactions
  ("lmaooo thats crazy"), agreement ("yeah for sure", "nah im good"), and asides add no new
  subject, and repeating the running topic just re-fetches what is already in context.
- Return [] when the user CHANGES the subject or turns to you: "anyway what you been up to",
  "different question", "how's your day". The old topic is over; do not carry it.
- Return [] for greetings and meta-chat about the conversation itself.
- Never return ordinary words as subjects. "pivot", "conclusion", "vibes", "wsg" name nothing.

The test is simple: would looking this up tell you anything you need in order to reply? If the
message is just a reaction to what you already said, the answer is no.
"""

_SUBJECT_FEWSHOT = [
    # five examples not nine: each is ~40 tokens of prefill and nine cost ~4 of the 6 seconds measured
    ("recent: talking about Naruto\nuser: it was great man excellent conclusion. Lets pivot "
     "either way, you play elden ring?", '{"subjects": ["Elden Ring"]}'),
    ("recent: (none)\nuser: yo yo yo wsg", '{"subjects": []}'),
    ("recent: discussing Naruto\nuser: lmaooo thats crazy", '{"subjects": []}'),
    ("recent: discussing Naruto\nuser: anyway what you been up to", '{"subjects": []}'),
    ("recent: discussing Kakashi Hatake\nuser: ok but is he actually stronger than the captain "
     "guy or nah", '{"subjects": ["Kakashi Hatake"]}'),
]

DOMAIN_ROUTER_SYSTEM = (
    "Pick the knowledge domain the user's message is about.\n"
    "Choose from: " + ", ".join(d for d in domains.names() if d != domains.FALLBACK) + ".\n"
    "Reply with ONLY a JSON object: {\"domain\": \"one of the above, or empty string\"}"
)


class Knowledge:
    def __init__(self, embed: Callable[[List[str]], List[List[float]]],
                 root: Optional[Path] = None, max_hot: int = 2,
                 llm_route: Optional[Callable[[str], str]] = None,
                 llm_chat: Optional[Callable[[List[dict], int], str]] = None,
                 min_score: float = 0.18, debug: bool = False):
        from reedus_companion import config
        self.embed = embed
        self.root = Path(root or getattr(config, "KNOWLEDGE_DIR",
                                         config.MEMORY_DIR / "knowledge"))
        try:
            from .reindex import ensure_indexes
            ensure_indexes(self.root)
        except Exception:
            pass
        self.store = KnowledgeStore(self.root, max_hot=max_hot)
        self.llm_route = llm_route
        self.llm_chat = llm_chat
        # cosine floor, only discards the obviously-nothing tail. it is NOT what decides
        # relevance: an answerable and an unanswerable question land 0.045 apart on this corpus.
        # see rerank.py for what actually decides.
        self.min_score = float(os.environ.get("REEDUS_KNOWLEDGE_MIN_SCORE", min_score))
        self.debug = debug
        self.last_topics: List[str] = []
        self._subject_misses = 0
        self.last_miss = False
        self.last_weak = False
        self._probe_cache: Optional[tuple] = None

    # --- routing ---
    def route(self, text: str) -> List[str]:
        low = (text or "").lower()
        scores = {}
        for d in domains.names():
            if d == domains.FALLBACK:
                continue
            hits = sum(1 for t in domains.triggers(d)
                       if re.search(rf"\b{re.escape(t)}\b", low))
            if hits:
                scores[d] = hits
        if scores:
            ranked = sorted(scores, key=lambda d: -scores[d])
            return ranked[:domains.MAX_DOMAINS_PER_QUERY]

        if self.llm_route:                       # keywords missed, ask the model once
            try:
                raw = self.llm_route(text) or ""
                m = re.search(r'"domain"\s*:\s*"([^"]*)"', raw)
                if m and m.group(1) in domains.DOMAINS:
                    router_stats.note("domain-router", True)
                    return [m.group(1)]
                router_stats.note("domain-router", False)
            except Exception:
                pass
        return [domains.FALLBACK]

    # --- existence ---
    def has_topic(self, topic: str) -> bool:
        """Do we already hold this, under any spelling of its name?"""
        topic = (topic or "").strip()
        if not topic:
            return False
        try:
            if self.store.has_topic(topic):
                return True
        except Exception:                                        # noqa: BLE001
            pass
        ix = self._entity_index()
        if ix is None:
            return False
        try:
            return bool(ix.has_topic(topic) or ix.has_topic_prefix(topic))
        except Exception:                                        # noqa: BLE001
            return False

    # --- writing ---
    def learn(self, topic: str, domain: Optional[str] = None,
              extra_wikis: Optional[List[str]] = None) -> int:
        """Fetch and store a topic. Idempotent, a topic already held costs nothing."""
        if os.environ.get("REEDUS_KNOWLEDGE", "1") != "1":
            return 0
        topic = (topic or "").strip()
        if len(topic) < 3 or self.has_topic(topic):
            return 0
        d = domain or self.route(topic)[0]
        return ingest_topic(topic, self.store, self.embed, d,
                            extra_wikis=extra_wikis, debug=self.debug)

    def learn_expanded(self, topic: str, breadth: int = 12, depth: int = 1,
                       domain: Optional[str] = None,
                       extra_wikis: Optional[List[str]] = None,
                       on_progress: Optional[Callable[[str, int, int], None]] = None) -> int:
        """Learn a topic and what it links to, breadth-first."""
        from .ingest import related_topics
        seen, queue, total = set(), [(topic, 0)], 0
        while queue:
            t, d_ = queue.pop(0)
            key = t.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            n = self.learn(t, domain=domain, extra_wikis=extra_wikis)
            total += n
            if on_progress:
                on_progress(t, n, len(seen))
            if d_ < depth:
                for link in related_topics(t, limit=breadth, extra_wikis=extra_wikis):
                    if link.strip().lower() not in seen:
                        queue.append((link, d_ + 1))
        return total

    # --- reading ---
    def _entities(self, query: str, limit: int = MAX_ENTITY_PASSES) -> List[str]:
        """The most distinctive names in the message, as standalone search strings."""
        from .rerank import idf, terms as _terms
        keep = _terms(query)
        runs: List[List[str]] = []
        for seg in re.split(r"[^\w'\s]+", (query or "").lower()):
            cur: List[str] = []
            for w in re.findall(r"[a-z0-9']+", seg):
                if w in keep and w not in _CHAT_WORDS and idf(w) >= ENTITY_MIN_IDF:
                    cur.append(w)
                    continue
                if cur and self._extends_a_topic(cur, w):
                    cur.append(w)
                    continue
                if cur:
                    runs.append(cur)
                cur = []
            if cur:
                runs.append(cur)
        scored = sorted(runs, key=lambda r: -sum(idf(w) for w in r))
        out, seen = [], set()
        for r in scored:
            if not self._names_something(r[:4]):
                continue
            phrase = " ".join(r[:4])
            if phrase in seen:
                continue
            seen.add(phrase)
            out.append(phrase)
            if len(out) >= limit:
                break
        return out

    def llm_subjects(self, query: str, context: str = "") -> Optional[List[str]]:
        """The thing this turn is about, per the model. None when unavailable."""
        if not LLM_SUBJECT or self.llm_chat is None or not (query or "").strip():
            return None
        ix = self._entity_index()
        if ix is None:
            return None
        msgs = [{"role": "system", "content": SUBJECT_SYSTEM}]
        for a, b in _SUBJECT_FEWSHOT:
            msgs += [{"role": "user", "content": a}, {"role": "assistant", "content": b}]
        msgs.append({"role": "user", "content":
                     f"recent: {(context or '(none)')[:200]}\nuser: {query[:300]}"})
        t0 = time.time()
        try:
            raw = self.llm_chat(msgs, 48) or ""
        except Exception:                                        # noqa: BLE001
            return None
        dt = time.time() - t0
        m = re.search(r'"subjects"\s*:\s*\[(.*?)\]', raw, re.S)
        if not m:
            router_stats.note("subjects", False)
            return None
        router_stats.note("subjects", True)
        names = [s.strip().strip('"\'' ) for s in m.group(1).split(",")]
        out = self._validate_subjects(names) or []
        if self.debug:
            dropped = [n for n in names if n and n.lower() not in out]
            print(f"[knowledge] llm subjects: {out or '(none)'}  [{dt*1000:.0f} ms]"
                  + (f"  dropped (not in corpus): {dropped}" if dropped else ""))
        # empty list is a real answer ("nothing to look up"), not a failure. returning None
        # collapsed into `or ents` and restored the rarity pass ("talking" -> Ko-Guy).
        return out

    def _validate_subjects(self, names) -> Optional[List[str]]:
        """Keep only names the corpus actually has a topic for."""
        ix = self._entity_index()
        if ix is None:
            return None
        out = []
        for n in names or []:
            n = (n or "").strip().strip('"\'')
            if len(n) < 3:
                continue
            try:
                if ix.has_topic(n) or ix.has_topic_prefix(n):
                    out.append(n.lower())
            except Exception:                                    # noqa: BLE001
                return None
        return out or None

    def known_topic(self, name: str) -> bool:
        """Does the corpus hold this, per either the index or the live store?"""
        try:
            ix = self._entity_index()
            if ix is not None and (ix.has_topic(name) or ix.has_topic_prefix(name)):
                return True
        except Exception:                                        # noqa: BLE001
            pass
        try:
            return bool(self.store.has_topic(name))
        except Exception:                                        # noqa: BLE001
            return False

    def _confident_entities(self, ents: List[str]) -> bool:
        """Is the rarity path's answer good enough to skip the ~6s subject call?"""
        if not ents or len(ents) > 2:
            return False
        ix = self._entity_index()
        for e in ents:
            if len(e.split()) >= 2 and self.known_topic(e):
                continue
            try:
                if ix is not None and ix.is_person(e):
                    continue
            except Exception:                                    # noqa: BLE001
                return False
            return False
        return True

    def _extends_a_topic(self, cur: List[str], word: str) -> bool:
        """Would appending this common word complete a real title? "elden" + "ring"."""
        if len(cur) > 3 or len(word) < 2:
            return False
        ix = self._entity_index()
        if ix is None:
            return False
        try:
            return self.known_topic(" ".join(cur + [word]))
        except Exception:                                        # noqa: BLE001
            return False

    def _names_something(self, tokens: List[str]) -> bool:
        """Does this rare-word run refer to any topic in the corpus?"""
        if not tokens:
            return False
        ix = self._entity_index()
        if ix is None:
            return True
        n = len(tokens)
        try:
            for width in range(n, 0, -1):
                for start in range(0, n - width + 1):
                    span = " ".join(tokens[start:start + width])
                    if ix.has_topic(span) or ix.has_topic_prefix(span):
                        return True
        except Exception:                                        # noqa: BLE001
            return True
        return False

    def probe(self, query: str, context: str = "") -> float:
        """Best achievable anchor for this query from stage one only. In [0, 1]."""
        if os.environ.get("REEDUS_KNOWLEDGE", "1") != "1" or not (query or "").strip():
            return 0.0
        try:
            from .rerank import anchor_score, terms as _rk_terms
            q = self.embed([query])[0]
            routed = self.route(query)
            cand = self.store.search(q, routed, k=CANDIDATES, min_score=self.min_score)
        except Exception:                                        # noqa: BLE001
            return 0.0
        self._probe_cache = (query, q, cand, routed)
        if not cand:
            if self.debug:
                print("[knowledge] probe: no candidates -> 0.00")
            return 0.0
        try:
            qt = _rk_terms(query)
            ctx = frozenset(_rk_terms(context))
            best = max(anchor_score(qt, getattr(h, "topic", ""), getattr(h, "title", ""),
                                    getattr(h, "text", ""), context=ctx, use_body=False)
                       for h in cand)
        except Exception:                                        # noqa: BLE001
            return 0.0
        if self.debug:
            top = max(cand, key=lambda h: float(h.score))
            print(f"[knowledge] probe: {len(cand)} candidates, best anchor={best:.2f} "
                  f"(cosine top={float(top.score):.2f} {top.title[:40]})")
        return float(best)

    def recall(self, query: str, k: int = DEFAULT_K, context: str = "",
               hint: str = "") -> List[Passage]:
        if os.environ.get("REEDUS_KNOWLEDGE", "1") != "1" or not (query or "").strip():
            return []
        cached, self._probe_cache = getattr(self, "_probe_cache", None), None
        if cached is not None and cached[0] == query:
            _, q, cand, routed = cached
            if self.debug:
                print("[knowledge] reusing stage-1 candidates from probe")
        else:
            try:
                q = self.embed([query])[0]
            except Exception:
                return []

            routed = self.route(query)
            cand = self.store.search(q, routed, k=CANDIDATES, min_score=self.min_score)

        # per-entity passes: one embedding of a long message can't retrieve several subjects
        # (three character names inside a 60-word message are noise, the corpus held hundreds of
        # passages for them and returned none). pull the names out and search each alone.
        ents = self._entities(query)
        # a subject the caller already knows (the topic router often names it the same turn),
        # validated against the entity index so a hint naming nothing in the corpus is dropped
        hinted_ok = False
        if hint and not self._confident_entities(ents):
            hinted = self._validate_subjects([hint])
            if hinted:
                ents = hinted
                hinted_ok = True
                if self.debug:
                    print(f"[knowledge] subject from caller hint: {hinted[0]!r} "
                          f"- skipped the subject model call")
        # a validated hint IS the skip: _confident_entities is False for a one-word franchise,
        # so without this the model paid ~6s to reprint "naruto"
        if not hinted_ok and not self._confident_entities(ents):
            subj = self.llm_subjects(query, context)
            if subj is not None:
                ents = subj
        if ents:
            try:
                for vec, name in zip(self.embed(ents), ents):
                    cand += self.store.search(vec, routed, k=max(8, CANDIDATES // 4),
                                              min_score=self.min_score)
                if self.debug:
                    print(f"[knowledge] entity passes: {', '.join(ents)}")
            except Exception:
                pass

            if len(ents) >= 2 and MAX_PAIR_PASSES > 0:
                pairs = [f"{a} {b}" for a, b in
                         itertools.combinations(ents[:3], 2)][:MAX_PAIR_PASSES]
                try:
                    for vec in self.embed(pairs):
                        cand += self.store.search(vec, routed, k=max(6, CANDIDATES // 6),
                                                  min_score=self.min_score)
                    if self.debug:
                        print(f"[knowledge] pair passes: {'; '.join(pairs)}")
                except Exception:
                    pass

        hits = ground(cand, query, k, context=context)

        if not hits:
            rest = [d for d in domains.names() if d not in routed]
            if rest:
                rest = rank_domains(self.root, rest, _unit(q))
                cand = self.store.search(q, rest, k=CANDIDATES, min_score=self.min_score)
                hits = ground(cand, query, k, context=context)
                if self.debug and hits:
                    print(f"[knowledge] routed to {routed} and missed; found it in a "
                          f"wider scan ({hits[0].title[:40]})")
        strong = [h for h in hits if getattr(h, "anchor", 0.0) >= SUBJECT_CARRY_ANCHOR]
        if not strong and self.last_topics:
            self._subject_misses += 1
            if self._subject_misses >= SUBJECT_MAX_MISSES:
                if self.debug:
                    print(f"[knowledge] dropping stale subject {self.last_topics[0]!r} "
                          f"after {self._subject_misses} turns without a strong hit")
                self.last_topics = []
                self._subject_misses = 0
        elif strong:
            self._subject_misses = 0
        if strong:
            seen_t, subs = set(), []
            for h in strong:
                t = (getattr(h, "topic", "") or "").strip()
                if t and t.lower() not in seen_t:
                    seen_t.add(t.lower())
                    subs.append(t)
            self.last_topics = subs[:2]

        self.last_weak = not strong
        self.last_miss = bool(cand and not hits)
        if self.debug:
            if hits:
                print(f"[knowledge] {len(hits)} passages, top={hits[0].score:.2f} "
                      f"anchor={getattr(hits[0], 'anchor', 0):.2f} ({hits[0].title[:48]})")
            elif cand:
                print(f"[knowledge] {len(cand)} candidates, none anchored to the conversation "
                      f"- injecting nothing (best was {cand[0].title[:40]!r} at "
                      f"{cand[0].score:.2f})")
        return hits

    def context_for(self, query: str, k: int = DEFAULT_K, context: str = "",
                    hint: str = "") -> str:
        """Retrieved passages as a prompt block, or "" if nothing was relevant."""
        hits = self.recall(query, k=k, context=context, hint=hint)
        if not hits:
            return ""

        ents = [e for e in self._entities(query) if len(e) >= 3]
        bridging: List = []
        if len(ents) >= 2:
            for h in hits:
                blob = f"{h.title} {h.text}".lower()
                if sum(1 for e in ents if e in blob) >= 2:
                    bridging.append(h)

        # group by subject, keep rank order within each
        by_topic: dict = {}
        for h in hits:
            if h in bridging:
                continue
            by_topic.setdefault((getattr(h, "topic", "") or h.title or "").lower(), []).append(h)
        order = sorted(by_topic.values(), key=lambda g: -float(getattr(g[0], "rank_score",
                                                                      g[0].score)))

        out, used, round_i = [], 0, 0
        for h in bridging:
            block = f"- {h.title}: {h.text.strip()}"
            if used + len(block) > MAX_INJECT_CHARS // 2:
                break
            out.append(block)
            used += len(block)
        while any(len(g) > round_i for g in order):
            for g in order:
                if len(g) <= round_i:
                    continue
                h = g[round_i]
                block = f"- {h.title}: {h.text.strip()}"
                if used + len(block) > MAX_INJECT_CHARS:
                    continue
                out.append(block)
                used += len(block)
            round_i += 1

        if not out:
            return ""
        # short on purpose, every token here is persona prefill
        block = ("Notes (labels are binding, only attribute something to a name if THAT "
                 "item says so; otherwise leave the owner unnamed):\n"
                 + "\n".join(out))

        binding = self._binding_for(hits, ents)
        if binding:
            room = MAX_INJECT_CHARS - len(block) - 2
            if room >= 40:
                block += "\n\n" + binding[:room]
        return block

    def _binding_for(self, hits: List[Passage], ents: List[str]) -> str:
        """Ownership facts for the people this turn is actually about."""
        if os.environ.get("REEDUS_BINDING_BLOCK", "1") != "1":
            return ""
        ix = self._entity_index()
        if ix is None:
            return ""
        subjects, seen = [], set()
        for h in hits:
            t = (getattr(h, "topic", "") or h.title or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                subjects.append(t)
        for e in ents:
            if e.lower() not in seen:
                seen.add(e.lower())
                subjects.append(e)
        try:
            people = [s for s in subjects if ix.is_person(s)][:4]
            block = ix.binding_block(people) if people else ""
            if self.debug:
                if block:
                    print(f"[knowledge] binding block: {', '.join(people)}")
                elif subjects:
                    print(f"[knowledge] binding block: EMPTY, none of "
                          f"{subjects[:3]} classified as a person")
            return block
        except Exception:
            return ""

    def _entity_index(self):
        """Lazily opened, cached, and never fatal."""
        if getattr(self, "_ei", None) is None:
            try:
                from .entity_index import EntityIndex
                ei = EntityIndex()
                ei.db                      # force open, raises if not built
                self._ei = ei
            except Exception as e:         # noqa: BLE001
                if self.debug:
                    print(f"[knowledge] entity index unavailable ({e}), "
                          f"binding block disabled")
                self._ei = False
        return self._ei or None

    def stats(self):
        return self.store.stats()
