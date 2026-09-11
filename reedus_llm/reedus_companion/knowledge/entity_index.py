"""Entity-attribute index: who actually owns what, according to the corpus."""

from __future__ import annotations

import argparse
import gc
import json
import multiprocessing
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


_HERE = Path(__file__).resolve().parent
try:
    from reedus_companion import config as _cfg
    KNOWLEDGE_DIR = Path(getattr(_cfg, "KNOWLEDGE_DIR", _HERE.parent / "memory" / "knowledge"))
except Exception:
    KNOWLEDGE_DIR = _HERE.parent / "memory" / "knowledge"
INDEX_PATH = KNOWLEDGE_DIR / "entity_index.sqlite"
PARTIAL_DIR = KNOWLEDGE_DIR / "_entity_partials"


# person-likeness thresholds, only used when the defining opening sentence is missing.
# pronoun density alone called 81% of topics people (technique pages say "he/his" constantly);
# generic voice ("the user", "this jutsu") is what actually separates them.
GENERIC_VOICE_RATE = 0.30      # at or above -> not a person
BIOGRAPHY_RATE = 0.20          # at or above -> person
PRONOUN_RATE_FALLBACK = 7.0    # last resort, only with corroboration

MIN_ATTR_CHARS = 4

# longest capitalised n-gram considered as an attribute name ("Six Paths of Pain" is 4)
MAX_NGRAM = 5

MIN_TOPIC_PASSAGES = 1



_STOPCAPS = {
    "the", "a", "an", "this", "that", "these", "those", "it", "its", "he", "she", "they",
    "his", "her", "their", "him", "them", "i", "we", "you", "and", "but", "or", "so", "if",
    "when", "while", "after", "before", "during", "because", "although", "though", "however",
    "meanwhile", "then", "there", "here", "what", "which", "who", "whom", "whose", "why",
    "how", "as", "at", "by", "for", "from", "in", "into", "of", "on", "to", "with", "was",
    "were", "is", "are", "be", "been", "being", "had", "has", "have", "do", "does", "did",
    "not", "no", "yes", "one", "two", "three", "first", "second", "third", "later", "once",
    "upon", "despite", "unlike", "since", "until", "unless", "both", "each", "every", "all",
    "some", "any", "many", "most", "more", "less", "other", "another", "such", "only", "also",
    "even", "just", "still", "yet", "now", "soon", "however", "thus", "hence", "therefore",
}

_CONNECTORS = {"of", "the", "no", "de", "and", "in", "a"}

_TOKEN = re.compile(r"[^\W\d_][\wÀ-ɏḀ-ỿ'’\-]*", re.UNICODE)

_SENT = re.compile(r"(?<=[.!?])\s+|\s*[•\n]+\s*")

_THIRD_PERSON = re.compile(r"\b(he|him|his|she|her|hers|himself|herself)\b", re.I)

_GENERIC_VOICE = re.compile(
    r"\b(the user|the users|users |the wielder|the caster|the target|this technique|"
    r"this jutsu|this ability|hand seals|chakra is|the opponent|allows the|enables the)\b",
    re.I)

_BIOGRAPHY = re.compile(
    r"\b(was born|is born|his father|her father|his mother|her mother|his brother|her brother|"
    r"his sister|her sister|his son|her son|graduated|childhood|his sensei|her sensei|"
    r"his teammates|her teammates|birthday|he was assigned|she was assigned|his parents|"
    r"her parents|grew up|his death|her death|he died|she died|his wife|her husband)\b", re.I)

_NOT_OWNER_PAGE = re.compile(
    r"^(chapter|episode|volume|season|arc|list of|gallery|omake|opening|ending|sbs|databook)\b"
    r"|\((episode|chapter|arc|film|movie|volume|song|album|game|novel|manga|anime|ova|special|"
    r"location|island)\)$|^[\"“]|/"
    r"|\b(clan|family|council|crew|team|group|squad|army|corps|guild|village|organization|"
    r"organisation|company|band|series)$", re.I)

_COPULA = re.compile(
    r"\b(is|was)\s+(?:a|an|the|one of|among)\b"
    r"|\b(is|was)\s+(?:reputed|known|referred to|considered|regarded)\s+(?:as|to be)\b", re.I)

_DEF = re.compile(
    r"\b(?:is|was)\s+"
    r"(?:(?:reputed|known|referred to|considered|regarded)\s+(?:as|to be)\s+)?"
    r"(?:a|an|the|one of the|among the)?\s*([a-zß-ɏ'’\- ]{2,60})", re.I)

_STOP_AT = re.compile(r"\b(of|in|from|amongst|among|for|to|with|that|which|who|and|by|on|at)\b",
                      re.I)

_PERSON_NOUN = re.compile(
    r"\b(characters?|shinobis?|ninjas?|kunoichis?|samurais?|jōnin|jonin|chūnin|chunin|genin|"
    r"anbu|protagonists?|antagonists?|persons?|people|men|women|boys?|girls?|students?|"
    r"pirates?|captains?|leaders?|members?|kings?|queens?|princes?|princess(?:es)?|lords?|"
    r"gods?|goddess(?:es)?|deities|heroe?s?|heroines?|villains?|swordsm[ae]n|warriors?|"
    r"soldiers?|singers?|musicians?|rappers?|actors?|actress(?:es)?|players?|footballers?|"
    r"athletes?|authors?|writers?|artists?|producers?|directors?|politicians?|founders?|"
    r"scientists?|jinchūrikis?|descendants?|reincarnations?|heirs?|orphans?|nukenin|"
    r"missing-nin|sannin|hokage|kage|shinigami|mercenar(?:y|ies)|assassins?|survivors?)\b",
    re.I)

_THING_NOUN = re.compile(
    r"\b(techniques?|jutsus?|dōjutsu|dojutsu|ninjutsu|genjutsu|taijutsu|kekkei genkai|"
    r"abilit(?:y|ies)|powers?|weapons?|swords?|blades?|items?|tools?|villages?|clans?|"
    r"organi[sz]ations?|teams?|groups?|squads?|countr(?:y|ies)|cit(?:y|ies)|islands?|"
    r"episodes?|chapters?|arcs?|volumes?|films?|movies?|albums?|songs?|singles?|games?|series|"
    r"mangas?|animes?|novels?|eyes?|modes?|forms?|transformations?|styles?|releases?|natures?|"
    r"ranks?|titles?|terms?|concepts?|events?|battles?|wars?|tournaments?|locations?|places?|"
    r"buildings?|ships?|fruits?|beasts?|creatures?|animals?|substances?|materials?)\b", re.I)


_NOT_ATTRIBUTE_PAGE = re.compile(
    r"^(chapter|episode|volume|season|list of|gallery|omake|opening|ending|sbs|databook)\b"
    r"|\((series|episode|chapter|arc|film|movie|volume|song|album|game|novel|manga|anime|ova|"
    r"special|character|disambiguation)\)$"
    r"|^[\"“]|/"
    r"|[…‼]|[!?]{2}|\.\.\.", re.I)


def is_attribute_candidate(topic: str) -> bool:
    """Can this name plausibly be something a character owns?"""
    return not _NOT_ATTRIBUTE_PAGE.search(topic)


_CAP_SPAN = re.compile(r"\b([A-Z][\wÀ-ÿ'’\-]+(?:\s+[A-Z][\wÀ-ÿ'’\-]+){0,3})\b")


def capitalised_spans(text: str) -> List[str]:
    """Capitalised runs, longest first, for finding who a turn is about."""
    out, seen = [], set()
    for m in _CAP_SPAN.finditer(text or ""):
        s = m.group(1).strip()
        if len(s) >= 3 and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return sorted(out, key=lambda s: -len(s))


def subjects_in(text: str, ix, limit: int = 4) -> List[str]:
    """The people a piece of text is about, longest name first."""
    return [s for s in capitalised_spans(text) if ix.is_person(s)][:limit]


def drop_subspans(names: Sequence[str], known) -> List[str]:
    """Remove names that sit inside a longer candidate which is not itself real."""
    out = []
    for n in names:
        inside = False
        for other in names:
            if other is n or len(other) <= len(n):
                continue
            if n.lower() in other.lower():
                inside = True
                break
        if not inside:
            out.append(n)
    return out


def _lead_definition(topic: str, text: str) -> Optional[str]:
    """The phrase that defines this topic, or None."""
    bare = topic.split("(")[0].strip()
    if not text or not bare:
        return None
    m = re.match(r"^\s*(?:The\s+)?" + re.escape(bare) + r"\b", text, re.I)
    if not m:
        return None
    d = _DEF.search(text[m.end():m.end() + 200])
    return d.group(1).strip() if d else None


def classify_person(topic: str, passages: int, pronouns: int, generic: int, biography: int,
                    head: str, first: str) -> bool:
    """Is this topic a person, i.e. something that can own an ability?"""
    head = head or first
    if _NOT_OWNER_PAGE.search(topic):
        return False
    bare = topic.split("(")[0].strip()
    if bare and re.match(r"^\s*The\s+" + re.escape(bare), first or "", re.I):
        return False

    phrase = _lead_definition(topic, first) or _lead_definition(topic, head)
    if phrase:
        cut = _STOP_AT.search(phrase)
        head_noun = phrase[:cut.start()] if cut else phrase
        if _PERSON_NOUN.search(head_noun):
            return True
        if _THING_NOUN.search(head_noun):
            return False
        if _PERSON_NOUN.search(phrase):
            return True
        if _THING_NOUN.search(phrase):
            return False

    if not passages:
        return False
    gen_rate = generic / passages
    bio_rate = biography / passages
    pro_rate = pronouns / passages
    if gen_rate >= GENERIC_VOICE_RATE:
        return False
    if bio_rate >= BIOGRAPHY_RATE:
        return True
    return pro_rate >= PRONOUN_RATE_FALLBACK and gen_rate < 0.10 and bio_rate >= 0.05

_SUBJ_PRONOUN = re.compile(r"\b(he|his|him|she|her|hers|they|their|them)\b", re.I)

_OWN_VERB = re.compile(
    r"\b(awakened?|awakens|unlocked?|obtains?|obtained|acquires?|acquired|learns?|learned|"
    r"learnt|masters?|mastered|develops?|developed|creates?|created|invented?|invents|"
    r"possess(?:es|ed)?|wields?|wielded|uses?|used|using|casts?|performs?|performed|"
    r"summons?|summoned|activates?|activated|manifests?|manifested|inherits?|inherited|"
    r"receives?|received|gains?|gained|employs?|employed|utilis(?:es|ed)|utiliz(?:es|ed)|"
    r"can use|is capable of|is known for|is famous for|specialis(?:es|ed) in|"
    r"specializ(?:es|ed) in|trademark|signature)\b",
    re.I,
)

_POSSESSIVE_GAP = re.compile(r"\s*['’]s\s*")

_NORM_WS = re.compile(r"\s+")
_NORM_STRIP = re.compile(r"^[^\wÀ-ɏ]+|[^\wÀ-ɏ]+$")


def normalise(name: str) -> str:
    """Vocabulary key. Case-folded, whitespace-collapsed, edge punctuation stripped."""
    name = _NORM_STRIP.sub("", name)
    return _NORM_WS.sub(" ", name).strip().lower()


def fold(s: str) -> str:
    """Diacritic-folded key. "mangekyō" -> "mangekyo", "hyūga" -> "hyuga"."""
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


_FUZZ_STRIP = re.compile(r"[^a-z0-9 ]+")
_FUZZ_DOUBLE = re.compile(r"(.)\1+")
_FUZZ_LONG = re.compile(r"(?:ou|oh|uu|uh)(?=[^aeiou]|$)")


def fuzzy_key(s: str) -> str:
    """Spelling-insensitive existence key. "mangekyō" == "mangekyo" == "mangekyou"."""
    x = fold(s or "").lower()
    x = _FUZZ_STRIP.sub(" ", x)
    x = _FUZZ_LONG.sub(lambda m: m.group(0)[0], x)
    x = _FUZZ_DOUBLE.sub(r"\1", x)
    return _NORM_WS.sub(" ", x).strip()


def name_variants(topic: str) -> Set[str]:
    """Surface forms that refer to the article subject."""
    base = re.sub(r"\s*\([^)]*\)\s*$", "", topic).strip()
    out = {normalise(base)}
    parts = [p for p in base.split() if len(p) >= 3]
    for p in parts:
        n = normalise(p)
        if n and n not in _STOPCAPS:
            out.add(n)
    out.discard("")
    return out



_SHARD_RE = re.compile(r"^(?P<domain>[a-z_]+)\.(?P<n>\d\d)\.jsonl$")


def find_shards(domains: Optional[Sequence[str]] = None) -> List[Path]:
    """Every shard file, quarantine directories excluded."""
    if not KNOWLEDGE_DIR.is_dir():
        raise SystemExit(f"knowledge dir not found: {KNOWLEDGE_DIR}")
    out = []
    for p in sorted(KNOWLEDGE_DIR.glob("*.jsonl")):
        m = _SHARD_RE.match(p.name)
        if not m:
            continue
        if domains and m.group("domain") not in domains:
            continue
        out.append(p)
    return out


def _iter_rows(path: Path) -> Iterable[dict]:
    """Rows of a shard, skipping anything malformed."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue



def _pass1_shard(path_str: str) -> str:
    """Collect per-topic passage count, pronoun hits and a sample URL for one shard."""
    path = Path(path_str)
    domain = _SHARD_RE.match(path.name).group("domain")
    out = PARTIAL_DIR / f"p1.{path.stem}.json"
    if out.exists():
        return f"skip {path.name}"

    passages: Counter = Counter()
    pronouns: Counter = Counter()
    generic: Counter = Counter()
    biography: Counter = Counter()
    urls: Dict[str, str] = {}
    titles: Dict[str, str] = {}
    heads: Dict[str, str] = {}
    firsts: Dict[str, str] = {}

    for row in _iter_rows(path):
        topic = (row.get("topic") or row.get("title") or "").strip()
        if not topic:
            continue
        key = normalise(topic)
        if not key:
            continue
        passages[key] += 1
        text = row.get("text") or ""
        pronouns[key] += len(_THIRD_PERSON.findall(text))
        generic[key] += len(_GENERIC_VOICE.findall(text))
        biography[key] += len(_BIOGRAPHY.findall(text))
        if key not in urls:
            urls[key] = row.get("url") or ""
            titles[key] = topic
            firsts[key] = text[:400]
        if key not in heads and _COPULA.search(text[:400]):
            heads[key] = text[:400]

    payload = {
        "domain": domain,
        "shard": path.name,
        "topics": {
            k: [passages[k], pronouns[k], generic[k], biography[k],
                urls.get(k, ""), titles.get(k, k), heads.get(k, ""), firsts.get(k, "")]
            for k in passages
        },
    }
    PARTIAL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(out)
    return f"pass1 {path.name}: {len(passages)} topics"


def _merge_pass1() -> Dict[str, dict]:
    """Fold every shard's pass-1 partial into one vocabulary."""
    vocab: Dict[str, dict] = {}
    per_domain: Dict[str, Counter] = defaultdict(Counter)
    for p in sorted(PARTIAL_DIR.glob("p1.*.json")):
        blob = json.loads(p.read_text(encoding="utf-8"))
        dom = blob["domain"]
        for key, rec in blob["topics"].items():
            npass, npron, ngen, nbio, url, title, head, first = rec
            e = vocab.get(key)
            if e is None:
                e = vocab[key] = {"passages": 0, "pronouns": 0, "generic": 0, "biography": 0,
                                  "url": url, "title": title, "domain": dom,
                                  "head": head, "first": first}
            e["passages"] += npass
            e["pronouns"] += npron
            e["generic"] += ngen
            e["biography"] += nbio
            if not e["url"] and url:
                e["url"] = url
            if not e["head"] and head:
                e["head"] = head
            if not e["first"] and first:
                e["first"] = first
            per_domain[key][dom] += npass
    for key, e in vocab.items():
        e["domain"] = per_domain[key].most_common(1)[0][0]
        e["person"] = 1 if classify_person(e["title"], e["passages"], e["pronouns"],
                                           e["generic"], e["biography"],
                                           e["head"], e["first"]) else 0
        # released the moment the classification is made, two 400-char strings per topic is ~5 GB
        del e["head"]
        del e["first"]
    per_domain.clear()
    gc.collect()
    return vocab



class _Extractor:
    """Sentence-level ownership extraction against a fixed vocabulary."""

    def __init__(self, vocab_keys: Set[str], person_keys: Set[str],
                 attr_keys: Optional[Set[str]] = None,
                 person_aliases: Optional[Set[str]] = None):
        self.vocab = vocab_keys
        self.persons = person_keys
        self.aliases = person_aliases or set()
        self.attrs = attr_keys if attr_keys is not None else (vocab_keys - person_keys)

    def _spans(self, sent: str) -> List[Tuple[int, int, int, str]]:
        """Vocabulary hits in a sentence as (token_position, normalised_name)."""
        toks = [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(sent)]
        hits: List[Tuple[int, int, int, str]] = []
        i = 0
        n = len(toks)
        while i < n:
            word = toks[i][0]
            if not word[:1].isupper() or word.lower() in _STOPCAPS:
                i += 1
                continue
            best_len = 0
            best_key = ""
            for span in range(min(MAX_NGRAM, n - i), 0, -1):
                chunk_toks = toks[i:i + span]
                ok = True
                for w, _s, _e in chunk_toks[1:]:
                    if not (w[:1].isupper() or w.lower() in _CONNECTORS):
                        ok = False
                        break
                if not ok:
                    continue
                if chunk_toks[-1][0].lower() in _CONNECTORS:
                    continue  # names don't end on "of"/"the"
                key = normalise(" ".join(w for w, _s, _e in chunk_toks))
                if len(key) >= MIN_ATTR_CHARS and (key in self.vocab or key in self.aliases):
                    best_len, best_key = span, key
                    break
            if best_key:
                hits.append((i, toks[i][1], toks[i + best_len - 1][2], best_key))
                i += best_len
            else:
                i += 1
        return hits

    def edges(self, subject_key: str, subject_variants: Set[str], text: str):
        """Yield (attribute_key, frame) for ownership assertions about the subject."""
        for sent in _SENT.split(text):
            if len(sent) < 12:
                continue
            hits = self._spans(sent)
            if not hits:
                continue
            has_frame = bool(_OWN_VERB.search(sent))
            pronoun_pos = {m.start() for m in _SUBJ_PRONOUN.finditer(sent)}
            toks = [m.start() for m in _TOKEN.finditer(sent)]
            pronoun_tok = {i for i, off in enumerate(toks) if off in pronoun_pos}

            for idx, (pos, cstart, cend, key) in enumerate(hits):
                if key == subject_key or key in subject_variants:
                    continue
                if key in self.persons or key in self.aliases:
                    continue  # a person is not an attribute of another person
                if key not in self.attrs:
                    continue

                if idx > 0:
                    p_pos, p_start, p_end, p_key = hits[idx - 1]
                    gap = sent[p_end:cstart]
                    if _POSSESSIVE_GAP.fullmatch(gap):
                        if p_key in subject_variants or p_key == subject_key:
                            yield key, "possessive"      # "Sasuke's Chidori", his
                        continue                          # somebody else's

                # walk back for the nearest owner reference
                owner = None
                for prev in reversed([h for h in hits if h[0] < pos]):
                    if (prev[3] in self.persons or prev[3] in self.aliases
                            or prev[3] in subject_variants):
                        owner = prev[3]
                        break
                nearest_pron = max((i for i in pronoun_tok if i < pos), default=-1)
                owner_pos = -1
                if owner is not None:
                    owner_pos = max(p for p, _cs, _ce, k in hits if k == owner and p < pos)

                if owner is not None and owner_pos > nearest_pron:
                    # an explicit name is closest, it must be the subject
                    if owner not in subject_variants and owner != subject_key:
                        continue
                    frame = "possessive"
                elif nearest_pron >= 0 and has_frame:
                    frame = "pronoun"
                elif has_frame:
                    frame = "subject_page"
                else:
                    continue
                yield key, frame


# module globals so forked workers inherit them copy-on-write. passed as task arguments the
# pool pickled ~1 GB per worker x 23 and died on shard two. gc.freeze() keeps the pages clean.
_G_VOCAB: Set[str] = set()
_G_PERSONS: Set[str] = set()
_G_ATTRS: Set[str] = set()
_G_ALIASES: Set[str] = set()


def _pass2_shard(path_str: str) -> str:
    path = Path(path_str)
    out = PARTIAL_DIR / f"p2.{path.stem}.json"
    if out.exists():
        return f"skip {path.name}"

    person_keys = _G_PERSONS
    ex = _Extractor(_G_VOCAB, _G_PERSONS, _G_ATTRS, _G_ALIASES)
    domain = _SHARD_RE.match(path.name).group("domain")
    counts: Counter = Counter()

    for row in _iter_rows(path):
        topic = (row.get("topic") or row.get("title") or "").strip()
        if not topic:
            continue
        skey = normalise(topic)
        if skey not in person_keys:
            continue
        variants = name_variants(topic)
        for attr, frame in ex.edges(skey, variants, row.get("text") or ""):
            counts[(skey, attr, frame)] += 1

    payload = {"domain": domain,
               "edges": [[s, a, f, n] for (s, a, f), n in counts.items()]}
    PARTIAL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(out)
    return f"pass2 {path.name}: {len(counts)} edges"



_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS topics (
    key       TEXT PRIMARY KEY,
    title     TEXT NOT NULL,
    domain    TEXT NOT NULL,
    passages  INTEGER NOT NULL,
    person    INTEGER NOT NULL,
    url       TEXT
);
CREATE TABLE IF NOT EXISTS edges (
    subject   TEXT NOT NULL,
    attribute TEXT NOT NULL,
    frame     TEXT NOT NULL,
    n         INTEGER NOT NULL,
    source    TEXT NOT NULL DEFAULT 'text',
    PRIMARY KEY (subject, attribute, frame, source)
);
CREATE INDEX IF NOT EXISTS edges_subject ON edges(subject);
CREATE INDEX IF NOT EXISTS edges_attribute ON edges(attribute);
CREATE INDEX IF NOT EXISTS topics_person ON topics(person);
"""

_FRAME_WEIGHT = {"infobox": 1.0, "possessive": 0.8, "pronoun": 0.6, "subject_page": 0.35}

MAX_ATTRIBUTE_OWNERS = int(os.environ.get("REEDUS_MAX_ATTR_OWNERS", "120"))

ALIAS_STUB_MAX_PASSAGES = int(os.environ.get("REEDUS_ALIAS_STUB_MAX", "3"))


class EntityIndex:
    """Read-side API. Cheap to construct, safe to hold open for the process lifetime."""

    def __init__(self, path: Path = INDEX_PATH):
        self.path = Path(path)
        self._db: Optional[sqlite3.Connection] = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            if not self.path.exists():
                raise FileNotFoundError(
                    f"entity index not built: {self.path}\n"
                    f"run: python -m reedus_companion.knowledge.entity_index build")
            self._db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                       check_same_thread=False)
        return self._db

    def available(self) -> bool:
        return self.path.exists()

    def has_topic(self, name: str) -> bool:
        """Does this name refer to anything at all in the corpus?"""
        key = normalise(name)
        r = self.db.execute("SELECT 1 FROM topics WHERE key=? LIMIT 1", (key,)).fetchone()
        if r is not None:
            return True
        if self._refined():
            r = self.db.execute("SELECT 1 FROM topics WHERE key_folded=? LIMIT 1",
                                (fold(key),)).fetchone()
            if r is None:
                r = self.db.execute("SELECT 1 FROM topics WHERE key_fuzzy=? LIMIT 1",
                                    (fuzzy_key(key),)).fetchone()
        return r is not None

    def has_topic_prefix(self, name: str) -> bool:
        """Is this the first word (or words) of some topic? "itachi" -> "Itachi Uchiha"."""
        key = normalise(name)
        if not key:
            return False
        lo = key + " "
        hi = key + "!"
        r = self.db.execute(
            "SELECT 1 FROM topics WHERE key >= ? AND key < ? LIMIT 1", (lo, hi)).fetchone()
        if r is None and self._refined():
            f = fold(key)
            r = self.db.execute(
                "SELECT 1 FROM topics WHERE key_folded >= ? AND key_folded < ? LIMIT 1",
                (f + " ", f + "!")).fetchone()
            if r is None:
                z = fuzzy_key(key)
                r = self.db.execute(
                    "SELECT 1 FROM topics WHERE key_fuzzy >= ? AND key_fuzzy < ? LIMIT 1",
                    (z + " ", z + "!")).fetchone()
        return r is not None

    def is_person(self, name: str) -> bool:
        r = self.db.execute("SELECT person FROM topics WHERE key=?",
                            (normalise(name),)).fetchone()
        return bool(r and r[0])

    def confidence(self, subject: str, attribute: str) -> float:
        """0.0 if unattested, else the strongest frame's weight scaled by repetition."""
        rows = self.db.execute(
            "SELECT frame, n, source FROM edges WHERE subject=? AND attribute=?",
            (normalise(subject), normalise(attribute))).fetchall()
        best = 0.0
        for frame, n, source in rows:
            w = _FRAME_WEIGHT.get("infobox" if source == "infobox" else frame, 0.3)
            best = max(best, w * min(1.0, 0.55 + 0.15 * min(n, 3)))
        return best

    def owns(self, subject: str, attribute: str, threshold: float = 0.3) -> bool:
        return self.confidence(subject, attribute) >= threshold

    def _refined(self) -> bool:
        if getattr(self, "_has_refine", None) is None:
            try:
                self.db.execute("SELECT owners FROM attr_stats LIMIT 1").fetchone()
                cols = {r[1] for r in self.db.execute("PRAGMA table_info(topics)")}
                self._has_refine = ("person_alias" in cols and "key_folded" in cols
                                    and "key_fuzzy" in cols)
            except Exception:                                    # noqa: BLE001
                self._has_refine = False
        return self._has_refine

    def attributes_of(self, subject: str, limit: int = 40,
                      clean: bool = True) -> List[Tuple[str, float]]:
        """Attributes of a subject, strongest first."""
        if not (clean and self._refined()):
            rows = self.db.execute(
                "SELECT attribute, MAX(n) FROM edges WHERE subject=? GROUP BY attribute "
                "ORDER BY 2 DESC LIMIT ?", (normalise(subject), limit)).fetchall()
            return [(self.title(a), self.confidence(subject, a)) for a, _ in rows]

        rows = self.db.execute(
            "SELECT e.attribute, MAX(e.n) FROM edges e "
            "LEFT JOIN attr_stats s ON s.attribute = e.attribute "
            "LEFT JOIN topics t ON t.key = e.attribute "
            "WHERE e.subject = ? "
            "  AND COALESCE(s.owners, 1) <= ? "
            "  AND COALESCE(t.person_alias, 0) = 0 "
            "GROUP BY e.attribute ORDER BY 2 DESC LIMIT ?",
            (normalise(subject), MAX_ATTRIBUTE_OWNERS, limit)).fetchall()
        return [(self.title(a), self.confidence(subject, a)) for a, _ in rows]

    def owner_count(self, attribute: str) -> int:
        key = normalise(attribute)
        if self._refined():
            r = self.db.execute("SELECT owners FROM attr_stats WHERE attribute=?",
                                (key,)).fetchone()
            return int(r[0]) if r else 0
        r = self.db.execute("SELECT COUNT(DISTINCT subject) FROM edges WHERE attribute=?",
                            (key,)).fetchone()
        return int(r[0]) if r else 0

    def is_ownable(self, name: str) -> bool:
        """Is this the kind of thing a character can possess?"""
        key = normalise(name)
        row = self.db.execute(
            "SELECT person, COALESCE(person_alias, 0) FROM topics WHERE key=?"
            if self._refined() else
            "SELECT person, 0 FROM topics WHERE key=?", (key,)).fetchone()
        if not row or row[0] or row[1]:
            return False
        n = self.owner_count(key)
        return 0 < n <= MAX_ATTRIBUTE_OWNERS

    def owners_of(self, attribute: str, limit: int = 20) -> List[str]:
        rows = self.db.execute(
            "SELECT subject, MAX(n) FROM edges WHERE attribute=? GROUP BY subject "
            "ORDER BY 2 DESC LIMIT ?", (normalise(attribute), limit)).fetchall()
        return [self.title(s) for s, _ in rows]

    def title(self, key: str) -> str:
        r = self.db.execute("SELECT title FROM topics WHERE key=?",
                            (normalise(key),)).fetchone()
        return r[0] if r else key

    def misattributions(self, subject: str, candidates: Iterable[str]) -> List[str]:
        """Of these attribute names, which are real but not this subject's?"""
        out = []
        for c in candidates:
            if self.is_ownable(c) and not self.owns(subject, c):
                out.append(self.title(c))
        return out

    def binding_block(self, subjects: Sequence[str], max_attrs: int = 8) -> str:
        """Structured ownership facts for the prompt."""
        lines = []
        for s in subjects:
            attrs = [t for t, c in self.attributes_of(s, limit=max_attrs * 3) if c >= 0.5]
            if attrs:
                lines.append(f"- {self.title(s)}: {', '.join(attrs[:max_attrs])}")
        if not lines:
            return ""
        return ("Attested abilities, techniques and traits, by owner. This list is binding: "
                "do not give anyone something that is not on their own line, and if something "
                "you want to mention is not listed at all, describe it without naming an "
                "owner.\n" + "\n".join(lines))



def build(domains: Optional[Sequence[str]], workers: int, keep_partials: bool) -> None:
    shards = find_shards(domains)
    if not shards:
        raise SystemExit("no shards matched")
    PARTIAL_DIR.mkdir(parents=True, exist_ok=True)
    total_mb = sum(p.stat().st_size for p in shards) / 1e6
    print(f"[index] {len(shards)} shards, {total_mb:,.0f} MB, {workers} workers")

    # --- pass 1 ---
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_pass1_shard, str(p)): p for p in shards}
        for i, f in enumerate(as_completed(futs), 1):
            print(f"  [{i}/{len(shards)}] {f.result()}", flush=True)
    print(f"[index] pass 1 done in {time.time() - t0:.0f}s")

    vocab = _merge_pass1()
    vocab = {k: v for k, v in vocab.items() if v["passages"] >= MIN_TOPIC_PASSAGES}
    persons = {k for k, v in vocab.items() if v["person"]}
    print(f"[index] vocabulary: {len(vocab):,} topics, {len(persons):,} person-like")
    if not persons:
        raise SystemExit("no person-like topics found, check the classify_person thresholds")

    # --- topics table, written now so the vocab dict can be released ---
    import shutil
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="entity_index_")
    tmp_db = Path(tmpdir) / "entity_index.sqlite"
    db = sqlite3.connect(tmp_db)
    db.executescript(_SCHEMA)
    db.executemany(
        "INSERT OR REPLACE INTO topics(key,title,domain,passages,person,url) "
        "VALUES(?,?,?,?,?,?)",
        [(k, v["title"], v["domain"], v["passages"], v["person"], v["url"])
         for k, v in vocab.items()])
    db.commit()

    # --- pass 2 ---
    attr_keys = {k for k, v in vocab.items()
                 if not v["person"] and is_attribute_candidate(v["title"])}
    alias_keys: Set[str] = set()
    for k in persons:
        alias_keys |= name_variants(vocab[k]["title"])
    alias_keys -= attr_keys
    alias_keys -= {a for a in alias_keys if len(a) < 3}
    print(f"[index] {len(attr_keys):,} usable attribute names, "
          f"{len(alias_keys):,} person aliases")

    # publish to globals for the forked workers, then DROP the vocab dict (~5 GB of strings
    # nothing needs once person-likeness is decided). keeping it across the fork OOMed pass 2.
    global _G_VOCAB, _G_PERSONS, _G_ATTRS, _G_ALIASES
    _G_VOCAB = set(vocab)
    _G_PERSONS = persons
    _G_ATTRS = attr_keys
    _G_ALIASES = alias_keys
    vocab.clear()
    del vocab
    gc.collect()
    gc.freeze()

    t0 = time.time()
    try:
        mp_ctx = multiprocessing.get_context("fork")
    except ValueError:
        mp_ctx = None
        workers = min(workers, 4)
        print("[index] fork unavailable, falling back to 4 workers to bound memory")
    with ProcessPoolExecutor(max_workers=workers, mp_context=mp_ctx) as pool:
        futs = {pool.submit(_pass2_shard, str(p)): p for p in shards}
        for i, f in enumerate(as_completed(futs), 1):
            print(f"  [{i}/{len(shards)}] {f.result()}", flush=True)
    gc.unfreeze()
    print(f"[index] pass 2 done in {time.time() - t0:.0f}s")

    # --- merge edges, one shard at a time (a merged dict would be ~200 bytes an entry and OOM again) ---
    total_edges = 0
    for p in sorted(PARTIAL_DIR.glob("p2.*.json")):
        blob = json.loads(p.read_text(encoding="utf-8"))
        rows = blob["edges"]
        db.executemany(
            "INSERT INTO edges(subject,attribute,frame,n,source) VALUES(?,?,?,?,'text') "
            "ON CONFLICT(subject,attribute,frame,source) DO UPDATE SET n=n+excluded.n",
            [(s, a, f, n) for s, a, f, n in rows])
        db.commit()
        total_edges += len(rows)
    merged = range(total_edges)     # only its length is used below
    db.execute("ANALYZE")
    db.commit()
    db.execute("PRAGMA journal_mode=DELETE")   # no stray -wal/-shm next to the shipped file
    db.commit()
    db.close()

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    if INDEX_PATH.exists():
        INDEX_PATH.unlink()
    shutil.move(str(tmp_db), str(INDEX_PATH))
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"[index] wrote {INDEX_PATH}  ({len(merged):,} distinct edges)")

    if not keep_partials:
        for p in PARTIAL_DIR.glob("p*.json"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            PARTIAL_DIR.rmdir()
        except OSError:
            pass


def refine() -> None:
    """Add the display-quality filters to an already-built index, in place."""
    if not INDEX_PATH.exists():
        raise SystemExit(f"no index at {INDEX_PATH}, run build first")
    db = sqlite3.connect(INDEX_PATH)

    t0 = time.time()
    print("[refine] counting owners per attribute ...")
    db.executescript("""
        DROP TABLE IF EXISTS attr_stats;
        CREATE TABLE attr_stats (attribute TEXT PRIMARY KEY, owners INTEGER NOT NULL);
        INSERT INTO attr_stats(attribute, owners)
            SELECT attribute, COUNT(DISTINCT subject) FROM edges GROUP BY attribute;
    """)
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM attr_stats").fetchone()[0]
    print(f"[refine] {n:,} attributes scored in {time.time() - t0:.0f}s")

    cols = {r[1] for r in db.execute("PRAGMA table_info(topics)")}
    if "key_folded" not in cols:
        db.execute("ALTER TABLE topics ADD COLUMN key_folded TEXT")
        db.commit()
    if "key_fuzzy" not in cols:
        db.execute("ALTER TABLE topics ADD COLUMN key_fuzzy TEXT")
        db.commit()
    print("[refine] building spelling-insensitive keys ...")
    rows = [(fold(k), fuzzy_key(k), k) for (k,) in db.execute("SELECT key FROM topics")]
    db.executemany("UPDATE topics SET key_folded=?, key_fuzzy=? WHERE key=?", rows)
    db.execute("CREATE INDEX IF NOT EXISTS topics_folded ON topics(key_folded)")
    db.execute("CREATE INDEX IF NOT EXISTS topics_fuzzy ON topics(key_fuzzy)")
    db.commit()
    print(f"[refine] normalised {len(rows):,} keys")

    cols = {r[1] for r in db.execute("PRAGMA table_info(topics)")}
    if "person_alias" not in cols:
        db.execute("ALTER TABLE topics ADD COLUMN person_alias INTEGER NOT NULL DEFAULT 0")
        db.commit()

    t0 = time.time()
    print("[refine] marking person aliases ...")
    aliases: Set[str] = set()
    for (title,) in db.execute("SELECT title FROM topics WHERE person=1"):
        aliases |= name_variants(title)
    aliases = {a for a in aliases if len(a) >= 3}
    print(f"[refine] {len(aliases):,} alias forms")

    db.execute("UPDATE topics SET person_alias=0")
    db.executemany(
        "UPDATE topics SET person_alias=1 "
        "WHERE key=? AND person=0 AND passages <= ?",
        [(a, ALIAS_STUB_MAX_PASSAGES) for a in aliases])
    db.commit()
    marked = db.execute("SELECT COUNT(*) FROM topics WHERE person_alias=1").fetchone()[0]
    print(f"[refine] marked {marked:,} topics as person aliases in {time.time() - t0:.0f}s")

    db.execute("ANALYZE")
    db.commit()
    db.close()
    print("[refine] done, attributes_of() and binding_block() are now filtered")


def stats() -> None:
    ix = EntityIndex()
    db = ix.db
    t = db.execute("SELECT COUNT(*), SUM(person) FROM topics").fetchone()
    e = db.execute("SELECT COUNT(*), SUM(n) FROM edges").fetchone()
    print(f"topics       : {t[0]:,}  ({t[1]:,} person-like)")
    print(f"edges        : {e[0]:,} distinct, {e[1]:,} observations")
    print("\nby frame:")
    for frame, c in db.execute(
            "SELECT frame, COUNT(*) FROM edges GROUP BY frame ORDER BY 2 DESC"):
        print(f"  {frame:<14} {c:,}")
    print("\nby domain (topics):")
    for dom, c in db.execute(
            "SELECT domain, COUNT(*) FROM topics GROUP BY domain ORDER BY 2 DESC"):
        print(f"  {dom:<14} {c:,}")
    print("\ntop subjects by attribute count:")
    for s, c in db.execute(
            "SELECT subject, COUNT(DISTINCT attribute) FROM edges GROUP BY subject "
            "ORDER BY 2 DESC LIMIT 12"):
        print(f"  {ix.title(s):<34} {c}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--domains", nargs="*", default=None)
    b.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    b.add_argument("--keep-partials", action="store_true",
                   help="leave shard partials on disk so a re-run resumes instead of redoing")

    sub.add_parser("stats")
    sub.add_parser("refine")

    o = sub.add_parser("owns")
    o.add_argument("subject")
    o.add_argument("attribute")

    a = sub.add_parser("attrs")
    a.add_argument("subject")

    w = sub.add_parser("owners")
    w.add_argument("attribute")

    args = ap.parse_args(argv)

    if args.cmd == "build":
        build(args.domains, args.workers, args.keep_partials)
    elif args.cmd == "stats":
        stats()
    elif args.cmd == "refine":
        refine()
    elif args.cmd == "owns":
        ix = EntityIndex()
        c = ix.confidence(args.subject, args.attribute)
        exists = ix.has_topic(args.attribute)
        print(f"{args.subject!r} owns {args.attribute!r}? confidence={c:.2f} "
              f"({'YES' if c >= 0.3 else 'NO'})")
        if not exists:
            print("  note: that attribute name does not exist in the corpus at all "
                  "(invention, not misattribution)")
        elif c < 0.3:
            print(f"  it exists, and belongs to: {', '.join(ix.owners_of(args.attribute)[:6])}")
    elif args.cmd == "attrs":
        ix = EntityIndex()
        for t, c in ix.attributes_of(args.subject):
            print(f"  {c:.2f}  {t}")
    elif args.cmd == "owners":
        ix = EntityIndex()
        print("  " + "\n  ".join(EntityIndex().owners_of(args.attribute)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
