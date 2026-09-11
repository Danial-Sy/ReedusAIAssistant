"""the conversation core: routing, retrieval, search, grounding, scrubbing. backend-agnostic."""
from __future__ import annotations
import os
import random
import re
import sys
import json
import datetime
import math
import unicodedata
from typing import Callable, List, Dict, Protocol, Optional

from reedus_companion import config
from reedus_companion.router_stats import note as _note_json
from reedus_companion.serve.persona import (build_runtime_system, build_stable_system,
                                            build_volatile_block)
from reedus_companion.serve.memory import Memory
from reedus_companion.serve import tools as toolmod
from reedus_companion.serve.websearch import clean_query, web_search, web_search_pages

DEBUG = bool(os.environ.get("REEDUS_DEBUG"))
MAX_TOOL_HOPS = 3

PAGE_FETCH = os.environ.get("REEDUS_PAGE_FETCH", "0") == "1"
PAGE_FETCH_N = int(os.environ.get("REEDUS_PAGE_FETCH_N", "1"))

# volatile blocks (passages, recall, bulletin) go AFTER history, not in the system message,
# so a changed passage doesn't invalidate the kv cache for all twelve history messages.
PERSONA_SPLIT = os.environ.get("REEDUS_PERSONA_SPLIT", "1") == "1"

# long passages get compressed by the 1.7B before the persona sees them: ~450 tokens of wiki
# -> ~60 of bullets, several seconds of 4B prefill saved for ~0.5s on the small model.
KNOWLEDGE_COMPRESS = os.environ.get("REEDUS_KNOWLEDGE_COMPRESS", "1") == "1"
KNOWLEDGE_COMPRESS_MIN_CHARS = int(os.environ.get("REEDUS_KNOWLEDGE_COMPRESS_MIN", "500"))
# weak injects aren't compressed (summarising noise reads as confident) but they get reused
# every turn under session lock, so cap them or follow-ups crawl.
WEAK_STICKY_CHARS = int(os.environ.get("REEDUS_WEAK_STICKY_CHARS", "320"))

HISTORY_WINDOW = int(os.environ.get("REEDUS_HISTORY_WINDOW", "12"))
HISTORY_CHUNK = int(os.environ.get("REEDUS_HISTORY_CHUNK", "4"))

# search router prompt, two versions: full is 551 tokens (~6.3s on every routed turn) with worked
# examples that keep a 4B emitting clean JSON; compact says the same in ~180. REEDUS_ROUTER_PROMPT.
ROUTER_SYSTEM_FULL = (
    "You are the search router for a chat assistant. Read the whole conversation and decide "
    "whether answering the user's LAST message correctly needs a LIVE web search, and if so, "
    "what to search for.\n\n"
    "Use context to build the query. If the user says 'what was the score again' or 'look it "
    "up', figure out WHAT from earlier in the chat and write a full standalone query (e.g. "
    "'USA vs Turkey World Cup 2026 final score').\n"
    "Make queries CURRENT: for anything time-sensitive (sports, news, prices, who-holds-a-role), "
    "include the current year and assume the user means the MOST RECENT occurrence unless they "
    "say otherwise. Never write a bare query that could match an old event.\n\n"
    "THE PRINCIPLE: if the user is asking for a FACT, any who/what/when/where/how-many/which "
    "question with an objectively right answer, the default is YES, search it. Only skip the "
    "search when the answer is trivially timeless and universally known (basic math, capitals, "
    "definitions, settled history like 'when was WW2'). When in doubt, YES.\n"
    "ALSO YES when: the answer depends on today's actual date (anniversaries, 'how many years "
    "since', never trust the model's memory of the current year); the user disputes a factual "
    "claim the assistant made; or the reply would require specific knowledge of a niche domain "
    "(a game's mechanics, a show's plot, a product's specs) that a small model likely knows "
    "poorly, even for an OPINION question, if forming the opinion needs those facts, search "
    "them first (e.g. 'what's your favorite Baldur's Gate 3 class' -> search the classes).\n"
    "If the user explicitly says search / look up / google / find / check, it's ALWAYS yes.\n"
    "Answer NO only for: pure opinions and preferences about everyday things, advice, "
    "emotional support, jokes, greetings, chit-chat, and questions about the user themselves "
    "or this conversation.\n\n"
    "Examples:\n"
    "- (after chatting about USA vs Turkey) user: 'what was the score again?' -> "
    '{"search": true, "query": "USA vs Turkey World Cup 2026 final score"}\n'
    "- user: 'whats the capital of japan' -> {\"search\": false, \"query\": \"\"}\n"
    "- (after discussing France) user: 'can you search' -> "
    '{"search": true, "query": "current population of France"}\n'
    "- user: 'i had a rough day' -> {\"search\": false, \"query\": \"\"}\n\n"
    "Reply with ONLY a JSON object, nothing before or after:\n"
    '{"search": true or false, "query": "standalone query, or empty string if no search"}'
)

ROUTER_SYSTEM_COMPACT = (
    "Decide whether answering the user's LAST message needs a LIVE web search, and write the "
    "query. Use the whole conversation for context.\n"
    "YES if it asks for a LIVE fact, scores, news, prices, who currently holds a role, "
    "today's date/weather, product availability, or if the user says search / look up / "
    "google / check, or disputes a claim the assistant made. When in doubt on LIVE facts, YES.\n"
    "NO for: chatting about a game, show, book, or character (plot opinions, 'been playing X', "
    "'what about Y though'), those are conversation, not live lookups. Also NO for timeless "
    "universal knowledge (basic math, capitals, definitions, settled history), pure opinions "
    "and preferences, advice, emotional support, jokes, greetings, chit-chat, and questions "
    "about the user or this conversation.\n"
    "The query must stand alone and be CURRENT: resolve references from the chat ('what was "
    "the score again' -> 'USA vs Turkey World Cup 2026 final score'), add the current year for "
    "anything time-sensitive, and assume the most recent occurrence.\n"
    "Reply with ONLY a JSON object, nothing before or after:\n"
    '{"search": true or false, "query": "standalone query, or empty string if no search"}'
)

ROUTER_SYSTEM = (ROUTER_SYSTEM_FULL
                 if os.environ.get("REEDUS_ROUTER_PROMPT", "compact").lower() == "full"
                 else ROUTER_SYSTEM_COMPACT)

VERIFY_DRAFTS = os.environ.get("REEDUS_VERIFY", "1") != "0"

DRAFT_CHECK_SYSTEM = (
    "You review a chat assistant's DRAFT reply before it is sent. Decide if the draft asserts "
    "any specific real-world fact that could be wrong or outdated, because it depends on the "
    "current date, or could have changed since the model was trained, or is a precise claim "
    "(number, date, year, name, score, price, version, record, status) stated without a source.\n"
    "Chit-chat, opinions, advice, emotional support, pure math the draft shows its work for, "
    "and timeless general knowledge (capitals, definitions, settled history) do NOT need "
    "verification.\n"
    "If verification is needed, write one standalone, current web-search query that would "
    "confirm or correct the claim (include the current year for time-sensitive things).\n"
    "Reply with ONLY a JSON object:\n"
    '{"verify": true or false, "query": "search query, or empty string"}'
)

# risky-claim detector: ordinals, years, prices, percents. a match forces verification, the LLM
# only writes the query (SKIP is the escape hatch for "first of all"). agreement percentages
# ("100% that combo is peak") are excluded, they were buying a 9.6s verify turn for "yes".
# condense word count is a RANGE: "under n words" let an 81-word draft come back as 25.
CONDENSE_SYSTEM = (
    "Rewrite the message shorter, between {lo} and {hi} words. Aim for {hi}; going under {lo} "
    "is a failure, not a bonus.\n"
    "Keep the SAME voice, slang, punctuation and energy. Keep the concrete details (names, "
    "techniques, versions) and keep the closing question if there is one. Cut hedging, "
    "repetition, and any point that is not the main one.\n"
    "Write COMPLETE SENTENCES. Do not compress into telegraphic fragments, do not turn "
    "statements into short rhetorical questions, and do not drop the words that connect ideas "
    "- 'he'd need to survive losing his eye, and Sage Mode might have destabilised him' must "
    "not become 'Survives eye loss? Sage Mode unstable?'. Every sentence must still read as "
    "something a person would say out loud.\n"
    "Do NOT add any new claim, name or detail that isn't already there. Do not explain, do not "
    "add a preamble, reply with the shortened message and nothing else."
)

def _stub_questions(t: str) -> int:
    """Short rhetorical question fragments, the signature of over-compression."""
    return sum(1 for s in _sentences(t or "")
               if s.strip().endswith("?") and not _DIRECTED_Q.search(s)
               and len(s.split()) <= 5)


CONDENSE_RETRY = (
    "\nYour previous attempt was too short and lost the meaning. This time cut ONLY whole "
    "sentences that are not the main point, and leave the sentences you keep exactly as they "
    "are. Do not rephrase them."
)


def _known_topic(store, name: str) -> bool:
    """Is this the name of something the corpus holds? Tries the obvious spelling variants."""
    import unicodedata
    try:
        ix = _entity_index()
        if ix is not None and (ix.has_topic(name) or ix.has_topic_prefix(name)):
            return True
    except Exception:                                            # noqa: BLE001
        pass
    plain = "".join(c for c in unicodedata.normalize("NFKD", name)
                    if not unicodedata.combining(c))
    for cand in (name, plain, name.replace("-", " "), plain.replace("-", " ")):
        if store.has_topic(cand):
            return True
    return False


# unknown names before a reply is suspect. one odd capitalisation is normal english, the
# fabricated reply had ten.
MIN_UNKNOWN_NAMES = int(os.environ.get("REEDUS_MIN_UNKNOWN_NAMES", "2"))

_AGREEMENT_PCT = re.compile(
    r"(?:^|[\s\-—(])(?:0|100|110|1000|10000)\s?%(?=[\s,.!?—-]|$)(?!\s*of\b)")

_RISKY_CLAIM = re.compile(
    r"\$\s?\d|\b\d+(\.\d+)?\s?%|\b\d+(\.\d+)?\s*per\s?cent\b"
    r"|\b(1[5-9]|20)\d{2}\b|\b\d+(st|nd|rd|th)\b|\banniversar\w*"
    r"|\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|one|two|three|four|five|six|"
    r"seven|eight|nine)[- ](first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\b"
    r"|\b(twentieth|thirtieth|fortieth|fiftieth|sixtieth|seventieth|eightieth|ninetieth"
    r"|hundredth|thousandth)\b", re.I)

# extractor prompt: messy snippets -> one clean fact. module-level so warm() can prime it (6.45s
# uncached). keep it example-free, the model echoed a sample score verbatim when results were thin.
EXTRACT_SYSTEM = (
    "You read web search results and answer the user's question in ONE short factual "
    "sentence, or, if the question needs a set of options (classes, models, cast, "
    "features), ONE compact list sentence naming them, using ONLY information "
    "written in the results below. Copy names and numbers exactly as they appear; "
    "use the FINAL result, not live/partial scores. "
    "No explanation, no hedging, no outside knowledge.\n"
    "If, and ONLY if, the results do not clearly contain the specific answer, or "
    "they look like an error message, ads, or unrelated pages, reply with exactly "
    "NOT FOUND and nothing else. NEVER write an answer and then add NOT FOUND after "
    "it: your reply must be EITHER one factual sentence OR the exact text NOT FOUND, "
    "never both."
)


_TOPIC_CUE = re.compile(
    r"\b(play(?:ing|in|ed|through)?|replay(?:ing)?|watch(?:ing|in|ed)?|rewatch(?:ing)?|"
    r"read(?:ing|in)?|listen(?:ing)?\s+to|into|been\s+on|using|tried|trying|bought|"
    r"got\s+into|started|grind(?:ing)?|farming|running|binge(?:ing|d|-?watching)?|"
    r"beat(?:ing)?|streaming|obsessed\s+with|hooked\s+on|deep\s+in|addicted\s+to)\b",
    re.I)

_NEW_ENGAGE = re.compile(
    r"(?i)(?:^|[\s,])(?:(?:yo|hey|hi|anyway|ngl|so)\s+)*"
    r"(?:i(?:'?ve|'m|m)\s+)?(?:been\s+|just\s+|currently\s+|still\s+|lately\s+)"
    r"(?:play(?:ing|in|ed|through)?|replay(?:ing)?|watch(?:ing|in|ed)?|rewatch(?:ing)?|"
    r"read(?:ing|in)?|grind(?:ing)?|farming|binge(?:ing|d|-?watching)?|"
    r"got\s+into|hooked\s+on|obsessed\s+with|into)\b"
)

TOPIC_ROUTER_SYSTEM = (
    "Pull the TITLE out of the user's message: a specific named work, product, game, show, film, "
    "album, or tool that someone would have to look up to follow the conversation.\n"
    "Give an empty topic for generic nouns (work, the gym, my job, music, games, a movie), for "
    "people in the user's life, and for household words everyone already knows.\n"
    "Also empty when the message is only naming a character ability, mode, arc, or technique "
    "inside a franchise already under discussion (e.g. 'sage mode', 'amaterasu', 'bankai'), "
    "those are not new titles to look up.\n"
    "Examples:\n"
    "- 'just playing some naruto ultimate ninja storm 4 its so PEAK' -> "
    '{"topic": "Naruto Shippuden: Ultimate Ninja Storm 4", "query": "Naruto Shippuden Ultimate '
    'Ninja Storm 4 video game"}\n'
    "- 'been watching severance lately' -> "
    '{"topic": "Severance", "query": "Severance TV series"}\n'
    "- 'i finally got a steam deck' -> "
    '{"topic": "Steam Deck", "query": "Valve Steam Deck handheld"}\n'
    "- 'been playing video games all weekend' -> {\"topic\": \"\", \"query\": \"\"}\n"
    "- 'i was watching a movie with my brother' -> {\"topic\": \"\", \"query\": \"\"}\n"
    "- 'sage mode naruto might beat amaterasu' -> {\"topic\": \"\", \"query\": \"\"}\n"
    "- 'mementos grinding is a slog sometimes' -> {\"topic\": \"\", \"query\": \"\"}\n"
    "Reply with ONLY a JSON object, nothing before or after:\n"
    '{"topic": "the exact title, or empty string", "query": "a search query to learn what it is, '
    'or empty string"}'
)

_TOPIC_FILLER = frozenset("""
some the a an this that my your our their his her its
rn lately recently again now today tonight atm currently still just really rly
so very pretty kinda sorta bit little lot lots much many new old
like ngl fr tbh honestly literally basically all
game games movie movies show shows series anime tv film films music songs
stuff things thing something anything everything
weekend week weeknight morning afternoon evening night day days
work gym school class job
is are was were be been being am do does did can could will would should
one ones thing that which who what when where why how transform into
man bro dude bruh fam guys mate lol lmao haha
competitive casual ranked online offline multiplayer singleplayer
sometimes always never maybe probably possibly usually often rarely
already also even only ever instead maybe somehow anyway whatever
""".split())


def _is_plausible_topic_title(topic: str) -> bool:
    """Reject filler/adverb 'titles' before they retarget the session lock."""
    raw = (topic or "").strip()
    if len(raw) < 3:
        return False
    words = [w for w in re.split(r"\W+", raw.lower()) if w]
    if not words:
        return False
    content = [w for w in words if w not in _TOPIC_FILLER and len(w) > 1]
    if not content:
        return False
    if len(content) == 1 and content[0] in {
        "sometimes", "always", "never", "maybe", "probably", "instead", "anyway",
        "something", "anything", "everything", "stuff", "things", "grinding", "farming",
    }:
        return False
    return True


def _topic_candidate(msg: str, min_words: int = 2) -> str:
    """Deterministic fallback: take the noun phrase after the engagement verb."""
    m = _TOPIC_CUE.search(msg or "")
    if not m:
        return ""
    tail = msg[m.end():]
    tail = re.split(r"[,.!?;:]|\b(?:and|but|so|because|cuz|coz|its|it'?s|thats|that'?s|which|"
                    r"while|when|with|for|from|lately|recently|rn)\b", tail, maxsplit=1)[0]
    tail = re.sub(r"^(?:\s*(?:on|to|at|in|into|through|with|about|out|up|off|down|back|"
                  r"the|some|a|an)\b)+\s*", " ", tail, flags=re.I)
    raw_words = re.findall(r"[\w'’\-]+", tail)
    words: list[str] = []
    for i, w in enumerate(raw_words):
        wl = w.lower()
        if wl in _TOPIC_FILLER:
            if (wl == "one" and i + 1 < len(raw_words)
                    and raw_words[i + 1].lower() not in _TOPIC_FILLER
                    and len(raw_words[i + 1]) > 2):
                words.append(w)
            continue
        words.append(w)
    if len(words) < min_words:
        return ""
    while words and words[-1].lower() in _TOPIC_FILLER:
        words.pop()
    if len(words) < min_words:
        return ""
    return " ".join(words[:7]).strip()

TOPIC_BRIEF_SYSTEM = (
    "From these search results, write 2 or 3 short factual sentences describing what this thing "
    "IS: what kind of thing it is, who made it, roughly when, and one or two distinctive details "
    "someone would need in order to follow a casual conversation about it. Use ONLY what is "
    "written in the results. No preamble, no opinions, no bullet points.\n"
    "If the results do not actually describe it, reply with exactly NOT FOUND."
)

_DOMAIN_ROUTER_SYSTEM = (
    "Pick the knowledge domain the user's message is about.\n"
    "Choose from: games, anime_manga, film_tv, music, sport, tech, food_drink, hobbies.\n"
    'Reply with ONLY a JSON object: {"domain": "one of the above, or empty string"}'
)

_TOPIC_ACKS = (
    "hm, not sure I know that one, gimme a sec",
    "I don't think I know that one, let me look it up quick",
    "haven't heard of that, hang on",
    "new one on me, one sec",
)

MAX_TOPICS_INJECTED = 3          # keeps the injected block under ~150 tokens
MAX_TOPICS_STORED = 60


FORCED_QUERY_SYSTEM = (
    "The assistant's DRAFT reply contains a specific factual claim (a number, year, ordinal, "
    "price, percentage, or anniversary). Write ONE standalone, current web-search query that "
    "would confirm or correct the MAIN factual claim in the draft, include the current year "
    "if the claim is time-sensitive. If the flagged text is genuinely not a checkable "
    "real-world fact (a casual phrase, a joke, pure math), reply with exactly SKIP.\n"
    "Reply with ONLY the search query, or SKIP. No explanation, no quotes."
)

_DATE_Q = re.compile(
    r"\bwhat\s+(day|date|time)\s+is\s+(it|today)\b"
    r"|\bwhat('?s|\s+is)\s+(the\s+)?(date|day|time)\s+(today|right\s+now)\b"
    r"|\bwhat('?s|\s+is)\s+the\s+date\s*\??\s*$"
    r"|\btoday'?s\s+date\b",
    re.I)

_SEARCH_VERB = re.compile(
    r"\b(searche?|serach|saerch|seach|searh|sarch|google|googel|"
    r"look (it )?up|lookup|look into|pull up|find out|find me)\b", re.I)
_NO_SEARCH = re.compile(
    r"\b(don'?t|do not|stop|quit|no need to|never|not|without|won'?t need to|"
    r"you don'?t (have|need) to)\s+(\w+\s+){0,3}"        # {0,3} reaches 'need you to search'
    r"(searche?\w*|google\w*|look(ing)?\s*(it\s*)?up|check(ing)?)\b", re.I)
_PERSONAL_Q = re.compile(
    r"\b(what|how|where|who)\s+(do|does|would|did|will|are|is)\s+(you|u|ya)\b"
    r"|\bwhat\s+(you|u|ya)\s+(want|think|feel|like|prefer|wish|imagine|choose|be)\b"
    r"|\bdo you (want|like|prefer|think|feel|wish|imagine)\b"
    r"|\byour (?:\w+\s+)?(opinion|take|favorite|favourite|body|style|personality|name|"
    r"choice|pick|vibe|look|design)\b",
    re.I)
_CURRENT_INFO = re.compile(
    r"\b(who won|what.?s the score|the score|how much (is|are|does|do)|price of|weather|"
    r"forecast|latest|the news|headlines|stock price|exchange rate)\b"
    r"|\bwho(?:'?s| is| are|'?re)\s+(?:the\s+)?(?:current\s+|new\s+|sitting\s+)?"
    r"(?:president|prime\s+minister|pm|chancellor|monarch|king|queen|pope|ceo|owner|"
    r"manager|head\s+coach|coach|captain|champion|title\s+holder|record\s+holder|"
    r"leader|mayor|governor|senator|number\s+one)\b"
    r"|\bwho(?:'?s| is| are|'?re)\b[^?]{0,40}?\b(?:right\s+now|currently|these\s+days|"
    r"at\s+the\s+moment|nowadays|as\s+of\s+now|now)\b", re.I)
_LEAD = re.compile(r"^(hey|yo|ok|okay|so|and|please|could you|can you|would you|will you|reedus)[\s,]*", re.I)


_ENTITY = re.compile(
    r"\b[A-Z][\w']+(?:\s+(?:of|the|and|&)\s+|\s+)[A-Z0-9][\w']*"   # Two Capitalized Words
    r"|\b[A-Za-z][\w']{2,}\s?\d+\b"                                 # iPhone 17
    r"|\b[A-Za-z]{2,4}\d+\b"                                        # bg3, ps5, gta6
    r"|\b[A-Z]{3,}\b")                                              # ALLCAPS acronym
_SHOUT_WORDS = frozenset("""
WHAT WHY HOW WHO WHEN WHERE WHICH THIS THAT THESE THOSE THERE THEN THAN
YES NO NAH YEAH YEP YUP NOPE OK OKAY SURE FINE REALLY VERY SO SUCH JUST
LOL LMAO LMFAO OMG OMFG WTF IDK IDC IMO IMHO TBH NGL FR SMH BRUH BRO DUDE MAN BUD
WOW WOAH WHOA DAMN DANG SHIT FUCK HELL GOD JESUS
RIGHT EXACTLY TRUE TOTALLY DEFINITELY ABSOLUTELY HONESTLY SERIOUSLY LITERALLY ACTUALLY FINALLY
PEAK FIRE GOATED GOAT BASED CRINGE MID SICK DOPE LIT WILD CRAZY INSANE NUTS
HUGE MASSIVE TINY BIG SMALL BEST WORST GOOD BAD GREAT AWFUL AMAZING TERRIBLE
LOVE HATE NEED WANT LIKE STOP GO HELP PLEASE THANKS THANK SORRY
NEVER ALWAYS EVERY ALL NONE NOTHING EVERYTHING ANYTHING SOMETHING
AND BUT OR THE YOU YOUR MY ME IT IS ARE WAS WERE BE AM DO DID CAN WILL
DONT CANT WONT ISNT ARENT ITS IM IVE ILL WE US OUR THEY THEM HIS HER
NOW SOON LATER TODAY YESTERDAY TOMORROW AGAIN STILL EVEN ONLY MORE LESS
WAY OH HOLY HECK FRICK LEGIT FACTS CAP NAW BET DEADASS LOWKEY HIGHKEY
MUCH TOO REAL DEAD SAME BROO BROOO AYO YO HEY HUH WELL OOF EW UGH
""".split())


_NOT_A_PRODUCT = frozenset("""
what whats whatis which who whos how why when where is are was were be been
the a an this that these those there here it its and or but plus minus times
over under about around near before after within past next last
me you we they them him her us my your our their his hers
got get gets need needs want wants take takes give gives say says said
all only just like about maybe some any each every both few many much more most
in on at by for of to from with without into onto off out up down
number no minute minutes hour hours day days week weeks month months year years
episode episodes season seasons chapter chapters page pages part parts level
top best worst first second third round game games time times
watch watched watching play played playing see saw seen read bought buy
make made do did does done have has had finish finished start started
beat win won lose lost spend spent run ran eat ate sleep slept miss missed
add added skip skipped try tried use used need needed
""".split())


def _entity_search(text: str):
    """_ENTITY.search, except shouting is not a named entity."""
    if not text:
        return None
    lower_words = sum(1 for w in re.findall(r"[A-Za-z]{2,}", text) if w.islower())

    def _shouted(w: str) -> bool:
        return (w.upper() in _SHOUT_WORDS
                or (w.isupper() and w.isalpha() and len(w) >= 5 and lower_words >= 3))

    for m in _ENTITY.finditer(text):
        words = [w for w in re.split(r"[^A-Za-z0-9']+", m.group(0)) if w]
        if words and all(_shouted(w) for w in words):
            continue
        if " " in m.group(0) and re.search(r"\d", m.group(0)) and all(
                w.lower().replace("'", "") in _NOT_A_PRODUCT
                for w in words if not w.isdigit()):
            continue                     # arithmetic, not a product name
        return m
    return None


_QUESTIONISH = re.compile(
    r"\?|^\s*(what|who|when|where|which|how|why|is|are|was|were|does|do|did|can|could)"
    r"(?:'?s)?\b", re.I)
_LOOKUP_PHRASE = re.compile(
    r"\b(tell me about|fill me in on|catch me up on|"
    r"(?:can you |could you |please )?(?:check|look up|find out|search)(?:\s+(?:the|on|for))?)\b",
    re.I)
_WIN_ASK = re.compile(
    r"\b(?:did|does|do|has|have|who)\b.{0,48}\b(?:win|won|beat|beaten|lose|lost)\b",
    re.I)
_OPINIONY = re.compile(
    r"\b(favorite|favourite|best|worst|better|prefer|rank|opinion|take on|thoughts on|"
    r"do you (like|think|feel)|would you)\b"
    r"|\b(?:you|u|ya)\s+(?:main|run|play|rock|use|watch|watched|seen|read|into)\b"
    r"|\bwho(?:'?s| is) your\b"
    r"|\bwho\s+(?:would\s+)?win(?:s)?\b|\bwins\s+(?:in\s+)?a\s+(?:fight|match|duel)\b"
    r"|\b(?:vs\.?|versus)\b|\bstronger\b|\bweaker\b|\bstronger\s+than\b|\bmid\s?diff\b"
    r"|\bpower[\s-]?scal|\bbe\s+honest\b|\bhot\s+take\b|\bcould\s+\w+\s+beat\b", re.I)


def _kw_wants_search(msg: str) -> bool:
    return bool(_SEARCH_VERB.search(msg) or _CURRENT_INFO.search(msg)
                or (_entity_search(msg) and _QUESTIONISH.search(msg)))


_QUERY_STOP = frozenset("""
a an the this that these those my your our their his her its
i me my you your he she it we they them us him her
is are was were be been being am s re m ve ll
do does did done doing have has had having get got
can could will would shall should may might must
what whats what's which who who's whos whose when where why how
hows how's wheres where's whens when's whys why's thats that's theres there's heres here's
there here oh ah um uh
about at by for from in into of on onto to with without
and or but so if then than as too also just still even
no not nah yeah yep yes ok okay
""".split())


_RETRIEVAL_FILLER = frozenset("""
rn lately recently atm currently still just really rly very pretty kinda sorta bit
ngl fr tbh honestly literally basically actually
man bro dude bruh fam guys guy mate lol lmao haha yo yea yeah nah sup wsp
gonna gotta wanna lemme kinda dunno idk imo btw
stuff things thing something anything everything
watching watched watch watchingin playing played playin chilling chillin
talk talking talks chat chatting speak speaking
particular because think thinking thought feels feeling vibe vibes
fun peak mean ive im i've i'm
""".split())


def _content_words(text: str) -> list:
    """The words in a message that could plausibly match a document, in order."""
    return [w for w in re.findall(r"[\w'’\-]+", text or "")
            if w.lower() not in _QUERY_STOP and w.lower() not in _RETRIEVAL_FILLER
            and len(w) > 1]


def _has_query_content(text: str) -> bool:
    """Anything a search engine could match on? A digit counts, stop words don't."""
    if re.search(r"\d", text or ""):
        return True
    return any(w.strip("'") .lower() not in _QUERY_STOP
               for w in re.findall(r"[\w'’\-]+", text or ""))


_PIVOT = re.compile(r"\b(?:what|how)'?s?\s+about\b", re.I)


def _strip_lead_reaction(msg: str) -> str:
    """Drop an opening REACTION so the query starts at the actual question."""
    m = (msg or "").strip()
    if not m:
        return msg
    piv = _PIVOT.search(m)
    if piv and piv.start() > 0:
        head, rest = m[:piv.start()], m[piv.start():]
    else:
        c = m.find(",")
        if c <= 0:
            return msg
        head, rest = m[:c], m[c + 1:]
    head, rest = head.strip(" ,"), rest.strip(" ,")
    if piv is None and len(head.split()) > 2:
        return msg
    if not head or re.search(r"\d", head) or any(t[:1].isupper() for t in head.split()):
        return msg
    if len(rest.split()) < 3 or not _has_query_content(rest):
        return msg
    return rest


def _kw_query(msg: str) -> str:
    """Turn a message into a search query when the forced gate overrides the router."""
    q = _SEARCH_VERB.sub("", _LEAD.sub("", _strip_lead_reaction(msg).strip()))
    q = re.sub(r"\s+", " ", q).strip(" ?.!,")
    try:
        from reedus_companion.serve.websearch import clean_query
        cleaned = clean_query(q)
    except Exception:                                             # noqa: BLE001
        cleaned = ""
    return cleaned or q or msg


_LOOK_Q = re.compile(
    r"\bwhat\s+(?:do|can|are)\s+you\s+see(?:ing)?\b"
    r"|\bwhat\s+you(?:'re| are)?\s+see(?:ing)?\b"
    r"|\b(?:tell|show|describe)\s+me\s+what\s+you\s+see(?:ing)?\b"
    r"|\bwhat('?s| is| are)\s+(?:this|that|these|those|in front of you|on the|in your hand)\b"
    r"|\bwhat('?s| is| are)\s+(?:on|in)\s+(?:my|the|this|that)\s+\w+"
    r"|\bwhat\s+am\s+i\s+(?:holding|showing|pointing at|pointing to|looking at)\b"
    r"|\b(?:look|looking)\s+(?:at\s+(?:this|that|these|those|here)|around|here|there)\b"
    r"|\btake a look\b|\bhave a look\b"
    r"|\b(?:can|could|do)\s+you\s+see\s+(?:this|that|it|me|anything|what\s+i)\b"
    r"|\bsee\s+what\s+i(?:'?m| am)?\s+(?:pointing|holding|showing|looking)\b"
    r"|\bidentify\s+(?:this|that|these|those|it|what)\b"
    r"|\b(?:read|scan)\s+(?:this|that|it)\b"
    r"|\bcheck\s+(?:this|it)\s+out\b"
    r"|\b(?:see|look at)\s+(?:this|that|these|those)\b"
    r"|\bwhat\s+(?:color|colour)\s+is\s+(?:this|that|it)\b",
    re.I)


def _wants_look(msg: str) -> bool:
    """Deterministic look-intent. A search verb ('look it up') wins over a look verb."""
    if _SEARCH_VERB.search(msg):
        return False
    return bool(_LOOK_Q.search(msg))


_LOOK_HINT = re.compile(
    r"\b(see|seeing|look(?:ing)?|watch|show(?:ing)?|point(?:ing)?|holding|hold up|"
    r"in front|what colou?r|read this|scan this|camera|your eyes|this thing|that thing|"
    r"in my hand|over here)\b", re.I)


LOOK_ROUTER_SYSTEM = (
    "You decide if the user's LAST message is asking the assistant to LOOK through its camera / "
    "eyes RIGHT NOW and identify or describe something physically in front of it.\n"
    "YES, needs the camera: 'what is this', 'what am I holding', 'what am I pointing at', "
    "'what am I looking at', 'what do you see', 'what you see right now', 'tell me what you see', "
    "'can you tell me what you see', 'can you see what I'm pointing at', 'what colour "
    "is this', 'look at this', 'read this label', 'is there a mug in front of you', 'check this "
    "out'. Use the conversation to resolve 'this'/'that' to a real physical thing.\n"
    "NO, does not need the camera: web/lookup questions (scores, news, prices, facts), feelings, "
    "opinions, small talk, coding help, or anything not about physically seeing the here-and-now.\n"
    "Reply with ONLY a JSON object, nothing else:\n"
    '{"look": true or false}'
)


def _positive_search_cmd(msg: str) -> bool:
    """An explicit 'search / look it up / google' that is NOT negated ('don't search' is a veto)."""
    return bool(_SEARCH_VERB.search(msg)) and not _NO_SEARCH.search(msg)


def _entity_question(msg: str) -> bool:
    """A question naming a real entity (game/product/show/org), a small model's weak spot."""
    return bool(_entity_search(msg) and _QUESTIONISH.search(msg))


# companion meta-chat: talking ABOUT talking to reedus, not naming a topic. "i'm just talking
# to you because it's fun, my guy" passed every gate and still paid 5.7s for subjects + a
# "talking" -> Ko-Guy entity hit.
_COMPANION_CHAT = re.compile(
    r"\b(?:talk(?:ing)?|chatt(?:ing)?|speak(?:ing)?)\s+to\s+(?:you|u|ya|reedus)\b"
    r"|\b(?:fun|enjoy|like|love)\s+to\s+(?:talk|chat|speak)\b"
    r"|\b(?:just|only)\s+(?:here\s+)?(?:to\s+)?(?:talk|chat|speak)\b"
    r"|\btalking\s+to\s+you\s+is\b"
    r"|\bi'?m\s+just\s+(?:here|talking|chatting)\b",
    re.I)

# first-person frame for speaker stance (not aimed at reedus)
_FIRST_PERSON = re.compile(r"\b(i|i'?m|i'?ve|i'?d|i'?ll|my)\b", re.I)

_USER_STANCE_EVAL = re.compile(
    r"\b(?:feel(?:ing)?\s+like|i\s+think|i\s+reckon|i\s+guess|i\s+prefer|"
    r"i\s+like|i\s+love|i\s+hate|i\s+enjoy|i'?m\s+into|my\s+(?:favorite|favourite))\b"
    r"|\b(?:is|are|was|were|'s|'re)\s+(?:pretty\s+|really\s+|so\s+|super\s+|kinda\s+|kind of\s+)?"
    r"(?:good|great|bad|mid|fire|peak|dope|solid|meh|ok|okay|nice|cool|crazy|wild|"
    r"better|worse|underrated|overrated)\b"
    r"|\b(?:better|worse)\s+than\b"
    r"|\b(?:pretty|so|really)\s+good\b|\bso\s+peak\b|\bwas\s+mid\b",
    re.I)

_CANON_STRUCTURE = re.compile(
    r"\b(arc|episode|chapter|season|saga|jutsu|bankai|quest|raid|dungeon|"
    r"manga|anime|lore|canon|filler|ending|finale|dlc|mod|speedrun|ost)\b",
    re.I)

_STANCE_LIVE_CLAIM = re.compile(
    r"\b(last night|this morning|yesterday|just now|breaking|tonight|this afternoon)\b"
    r"|\b(won|lost|beat|defeated|scored)\b",
    re.I)

_DISCOURSE_REPAIR = re.compile(
    r"\b(?:sorry|apologize|my bad)\b.{0,48}\b(?:interrupt|cut you off|cut\s+in)\b"
    r"|\b(?:didn'?t|did not)\s+mean\s+to\s+(?:interrupt|cut)\b"
    r"|\b(?:please\s+)?(?:continue|go\s+on|keep\s+going|finish\s+(?:that|your)\s+thought)\b"
    r"|\bdo you (?:want to )?continue\b"
    r"|\bwhere\s+were\s+we\b",
    re.I)


def _is_user_stance(msg: str) -> bool:
    """Speaker preference / everyday take. Conversation, not encyclopedia."""
    if not (msg or "").strip():
        return False
    if (_positive_search_cmd(msg) or _CURRENT_INFO.search(msg) or _SCOREISH.search(msg)
            or _DATE_Q.search(msg) or _LOOKUP_PHRASE.search(msg) or _WIN_ASK.search(msg)
            or _TOPIC_CUE.search(msg) or _entity_question(msg) or _CANON_STRUCTURE.search(msg)
            or _STANCE_LIVE_CLAIM.search(msg)):
        return False
    if _entity_search(msg):
        return False
    if not _USER_STANCE_EVAL.search(msg):
        return False
    return bool(_FIRST_PERSON.search(msg)) or not _QUESTIONISH.search(msg)


def _is_personal_turn(msg: str) -> bool:
    """Self-directed / emotional / roleplay turn ('what do YOU want to be', 'how are you')."""
    if _positive_search_cmd(msg) or _CURRENT_INFO.search(msg):
        return False
    if _is_checkin(msg):
        return True
    if _OPINIONY.search(msg) and _SELF_DIRECTED.search(msg):
        return True
    if _entity_question(msg):
        return False
    # meta-chat about the conversation itself has to beat the corpus path. pattern-scoped on
    # purpose: "any self-directed statement" personalised "can you check the standings for me".
    if _COMPANION_CHAT.search(msg or "") and _SELF_DIRECTED.search(msg or ""):
        if not (_entity_search(msg) or _TOPIC_CUE.search(msg or "")
                or _positive_search_cmd(msg)):
            return True
    if _DISCOURSE_REPAIR.search(msg or ""):
        if not (_entity_search(msg) or _TOPIC_CUE.search(msg or "")
                or _positive_search_cmd(msg) or _CURRENT_INFO.search(msg)):
            return True
    if _is_user_stance(msg):
        return True
    return bool(_PERSONAL_Q.search(msg))


def _is_reminiscing(msg: str) -> bool:
    """Shared-memory talk about fiction, not a fact request."""
    return bool(_REMINISCE.search(msg or "")) and not _positive_search_cmd(msg)


def _suppress_fact_machinery(msg: str) -> bool:
    """True when no fact machinery should run: user vetoed searching, personal turn, or reminiscing."""
    return (bool(_NO_SEARCH.search(msg)) or _is_personal_turn(msg)
            or _is_reminiscing(msg))


_SCOREISH = re.compile(
    r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|nil|\d+)\s*(?:to|[-–:])\s*"
    r"(zero|one|two|three|four|five|six|seven|eight|nine|ten|nil|\d+)\b", re.I)


_REMINISCE = re.compile(
    r"\b(you remember|remember (?:when|that|the|how)|do you remember|recall when|"
    r"that (?:part|bit|scene|moment) (?:where|when)|the (?:part|bit|scene) (?:where|when)|"
    r"back when|thinking about (?:when|that)|wasn'?t there a)\b", re.I)


def _looks_factual(msg: str) -> bool:
    """The turn genuinely reaches for an external fact, so 'I couldn't find it' is an honest answer."""
    if _REMINISCE.search(msg or "") and not _positive_search_cmd(msg):
        return False
    return bool(_positive_search_cmd(msg) or _CURRENT_INFO.search(msg)
                or _entity_question(msg) or _SCOREISH.search(msg)
                or _DATE_Q.search(msg) or _TIME_SENSITIVE.search(msg))


def _is_live_fact_search(msg: str, query: str = "") -> bool:
    """A router-yes that should displace the corpus (weather/scores/news/prices), not a fandom question."""
    hay = f"{msg or ''} {query or ''}"
    return bool(_CURRENT_INFO.search(hay) or _SCOREISH.search(hay) or _DATE_Q.search(hay)
                or _positive_search_cmd(msg) or _entity_question(msg)
                or (_TIME_SENSITIVE.search(hay) and (
                    _WH_ANY.search(hay) or _QUESTIONISH.search(hay) or re.search(r"\d", hay))))


# --- cheap-turn gates ---
# a warm chitchat turn was ~4.2s and over half was guard calls (router 27%, draft-check 25%)
# that couldn't have changed anything. skip them only when there's a total absence of signal;
# any hint the world or camera might matter runs the call as before.
# REEDUS_ROUTER_SKIP=0 / REEDUS_DRAFT_CHECK_SKIP=0 force them back on.

_SMALLTALK = re.compile(
    r"^(hey|hi|hello|yo|sup|hiya|heya|come\s+on|c'?mon|what'?s\s+up|wassup|"
    r"what'?s\s+(?:good|going\s+on|happenin[g']?|poppin(?:g)?)|"
    r"how'?s\s+it\s+going|how\s+are\s+(you|u|ya)|how\s+you\s+doin[g']?|"
    r"good\s+(morning|afternoon|evening|night)|"
    r"thanks|thank\s+you|ty|thx|lol|lmao|haha|hehe|nice|cool|sweet|awesome|damn|"
    r"ok|okay|kk|got\s+it|gotcha|sure|yeah|yep|yup|nah|nope|true|fair|"
    r"nvm|never\s*mind|brb|bye|later|see\s+ya|cya|goodnight|gn|night)"
    r"(?:[\s,]+(?:(?:with\s+)?(?:you|u|ya)|(?:my\s+)?(?:man|bro|bruh|dude|bud|buddy|homie|fam|guy)))*"
    r"[\s,.!?]*$", re.I)

_GREETING_Q = re.compile(
    r"(?is)^\s*(?:(?:hey|hi|hello|yo|sup|ok|okay|so)[\s,]+)*"
    r"(?:"
    r"what'?s\s+(?:up|good|going\s+on|happenin[g']?|poppin(?:g)?)|"
    r"how'?s\s+it\s+(?:going|hanging|hangin)|"
    r"how\s+(?:are\s+)?(?:you|u|ya)\s+(?:doin[g']?|been)|"
    r"how\s+you\s+(?:doin[g']?|been)"
    r")"
    r"(?:[\s,]+(?:(?:with\s+)?(?:you|u|ya)|(?:my\s+)?(?:man|bro|bruh|dude|bud|buddy|homie|fam|guy)))*"
    r"[\s,.!?]*$"
)

_CONTENTLESS = frozenset("""
hi hey heyy heyyy hello helloo hallo hullo halloo howdy yo yoo sup wsp wassup whassup
hiya heya hola aye ayo greetings morning afternoon evening night
whats what s up good hows how is it going goin doing doin hanging hangin on
happening happenin poppin popping new things thing
are was were been being am do did
you your yours u ya we us our
with around
thanks thank ty thx bye later please
bud buddy bro brotha bruv bruh dude man mate friend friendo guy guys homie fam pal chief reedus
there again ok okay so well hmm ah oh uh um lol haha my the a and just
""".split())

_CHECKIN_LEFTOVER_OK = frozenset("""
day week night morning afternoon evening weekend today tonight
""".split())


_SQUASH_RUN = re.compile(r"(.)\1+")


def _squash(t: str) -> str:
    """Collapse repeated letters: "hellloooo" and "hello" both -> "helo"."""
    return _SQUASH_RUN.sub(r"\1", t or "")


_CONTENTLESS_SQUASHED = frozenset(_squash(w) for w in _CONTENTLESS)
_CHECKIN_LEFTOVER_OK_SQUASHED = frozenset(_squash(w) for w in _CHECKIN_LEFTOVER_OK)


_FOLLOWUP_SHAPE = re.compile(
    r"^\s*(and|but|ok|okay|oh|so|what about|how about|hows about)?\s*"
    r"(.{0,40}?\b("
    r"(at|around|by)\s+(like|about|around|maybe|say)?\s*\d{1,2}(:\d{2})?\s*(am|pm)?"
    r"|^\s*\d{1,2}(:\d{2})?\s*(am|pm)\b"          # a bare time
    r"|tomorrow|tonight|later|this (evening|afternoon|morning|weekend)"
    r"|next (week|hour|day)|then|after that|earlier"
    r")\b)", re.I)

_HAS_PROPER = re.compile(r"\b[A-Z][a-z]{2,}")

_CARRY_SKIP = re.compile(
    r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*$"
    r"|^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*$"
    r"|^(?:today|tonight|tomorrow|yesterday)$"
    r"|^(?:current|currently|latest|live|now|recent|recently)$"
    r"|^(?:am|pm)$"
    r"|^\d{4}$", re.I)


def _is_search_followup(msg: str) -> bool:
    """Continuation of the previous search rather than a new question?"""
    m = (msg or "").strip()
    if not m or len(m.split()) > 12 or _HAS_PROPER.search(m):
        return False
    sh = _FOLLOWUP_SHAPE.search(m)
    if not sh:
        return False
    rest = _strip_lead_reaction(m)
    if sh.group(3):
        rest = rest.replace(sh.group(3), " ")
    rest = re.sub(r"[^\w\s'’\-]", " ", rest)
    try:
        from reedus_companion.serve.websearch import _SEARCH_NOISE
        rest = _SEARCH_NOISE.sub(" ", rest)
    except Exception:                                             # noqa: BLE001
        pass
    own = [w for w in re.findall(r"[\w'’\-]+", rest) if w.lower() not in _QUERY_STOP]
    return len(own) <= 1


def _followup_constraint(msg: str) -> str:
    """The only new information in a follow-up: the time reference."""
    m = _FOLLOWUP_SHAPE.search(msg or "")
    if not m:
        return ""
    c = re.sub(r"\b(?:like|about|around|maybe|say)\s+(?=\d)", "", m.group(3) or "", flags=re.I)
    return re.sub(r"\s+", " ", c).strip()


def _carry_subject(last_query: str) -> str:
    """The WHAT of the previous search with the WHEN stripped."""
    lq = last_query or ""
    m = _FOLLOWUP_SHAPE.search(lq)
    if m and m.group(3):
        lq = f"{lq[:m.start(3)]} {lq[m.end(3):]}"
    out = []
    for w in lq.split():
        t = w.strip(",.?!'\"")
        if not t or _CARRY_SKIP.match(t):
            continue
        if t[:1].isupper() or t.lower() not in _QUERY_STOP:
            out.append(t)
    return " ".join(out[:5])


def _norm_toks(msg: str) -> list:
    """Lowercased tokens with elongations collapsed and apostrophes stripped."""
    out = []
    for t in re.findall(r"[a-z']+", (msg or "").lower()):
        n = _squash(t.replace("'", "")).strip("'")
        if n:
            out.append(n)
    return out


def _is_contentless(msg: str) -> bool:
    """Bare greeting, no topic, nothing to recall on."""
    toks = _norm_toks(msg)
    # collapse elongations on BOTH sides so "hellloooo" matches "hello". before this a bare
    # greeting ran the whole retrieval stack and its best hit was Niko Bellic/Quotes.
    return bool(toks) and all(t in _CONTENTLESS_SQUASHED for t in toks)


def _is_checkin(msg: str) -> bool:
    """Short social ping with no topic: hello, how's it going, you good."""
    if not (msg or "").strip():
        return False
    if (_positive_search_cmd(msg) or _CURRENT_INFO.search(msg) or _SCOREISH.search(msg)
            or _DATE_Q.search(msg)):
        return False
    if _LOOK_Q.search(msg) or _LOOK_HINT.search(msg):
        return False
    if re.search(r"\d", msg or ""):
        return False
    if _is_contentless(msg):
        return True
    if _GREETING_Q.search(msg or "") or _SMALLTALK.match(_LEAD.sub("", msg or "").strip()):
        return True
    leftover = [t for t in _norm_toks(msg) if t not in _CONTENTLESS_SQUASHED]
    if leftover and all(t in _CHECKIN_LEFTOVER_OK_SQUASHED for t in leftover):
        if _SELF_DIRECTED.search(msg or "") and not _entity_search(msg or ""):
            return True
    return False


def _history_window(history: List[Dict[str, str]],
                    capacity: int = HISTORY_WINDOW,
                    chunk: int = HISTORY_CHUNK) -> List[Dict[str, str]]:
    """Return a suffix of `history` that advances in chunks of `chunk`."""
    n = len(history)
    if n <= capacity:
        return list(history)
    if chunk < 1:
        return list(history[-capacity:])
    excess = n - capacity
    drop = (excess // chunk) * chunk
    return list(history[drop:])


_SELF_DIRECTED = re.compile(r"\b(you|your|yours|u|ya|yourself|reedus|we|us|our)\b", re.I)
_WH = re.compile(r"(?:^|[.!?,;]\s*)(what|who|when|where|which|why|how)(?:'?s)?\b", re.I)

_DRAFT_FACTUAL_SHAPE = re.compile(
    r"(?:\b(?:is|are|was|were|has|have|had)|'s|'re)\s+(the\s+)?(first|last|only|biggest|largest|"
    r"smallest|oldest|newest|best|most|least|fastest|richest)\b"
    r"|\bthe\s+(biggest|largest|smallest|oldest|newest|fastest|richest|first|last|only)\b"
    r"|\b(invented|founded|created|released|launched|discovered|won|beat|defeated|died|born|"
    r"located|situated|headquartered|acquired|merged|published|directed|starred|premiered|"
    r"developed|designed|owns|owned)\b"
    r"|\b(according\s+to|in\s+fact|actually,)\b"
    r"|\b(capital|population|founded\s+in|based\s+in)\b"
    r"|\b(last|this|next)\s+(year|month|week|season|summer|winter)\b"
    r"|\b(recently|currently|nowadays|these\s+days)\b"
    r"|(?:\b(?:is|are|was|were)|'s|'re)\s+(?:the\s+)?(?:current|latest|newest|most\s+recent)\b",
    re.I)

_PRAISE_SUPERLATIVE = re.compile(
    r"\b(?:you|u|ya)\s*(?:'re|\s+are|\s+r)?\s+the\s+"
    r"(?:best|greatest|coolest|smartest|funniest|sweetest|goat|man)\b", re.I)

# does this message continue the previous topic? fires only on a demonstrative, pronoun or
# reaction, something pointing BACK. otherwise "anywho how you doing" after a world cup chat
# ran the router for nothing.
_CONTINUATION = re.compile(
    r"\b(that|thats|those|these|this|it|its|he|she|they|them|him|her|his|their|"
    r"same|too|also|either|both)\b"
    r"|\b(real|really|so|pretty|actually|definitely|honestly|deadass|fr|ngl|tbh)\b"
    r"|\b(better|worse|best|worst|good|bad|great|crazy|wild|insane|nuts|solid|clean|neat)\b",
    re.I)

_ROUTER_SKIP_MAX_WORDS = int(os.environ.get("REEDUS_ROUTER_SKIP_MAX_WORDS", "45"))


_WH_ANY = re.compile(r"\b(what|whats|who|whos|when|where|which|why|how|hows)\b", re.I)


def _warm_subject_shape():
    """The knowledge subject-detection prompt, as a (name, messages) pair for warm()."""
    try:
        from reedus_companion.knowledge.retrieve import SUBJECT_SYSTEM
    except Exception:                                            # noqa: BLE001
        return ()
    return (("subjects", [{"role": "system", "content": SUBJECT_SYSTEM},
                          {"role": "user", "content": "hello"}]),)


RUN = "run"                                # the router call has to happen
R_CHECKIN = "r0-checkin"
R_ARITHMETIC = "r1-arithmetic"
R_SMALLTALK = "r3-smalltalk"
R_PIVOT = "r4-pivot"                       # "what about madara though" -> corpus
R_SELF_DIRECTED = "r5-self-directed"       # "how was your day"
R_STATEMENT = "r6-statement"               # short, no entity/digit/time/visual/question
R_ENGAGEMENT = "r6-engagement"             # same but with a title cue -> corpus


def _router_skip_reason(msg: str) -> str:
    """Which rule lets the search router be skipped, or RUN when none do."""
    if os.environ.get("REEDUS_ROUTER_SKIP", "1") != "1":
        return RUN

    if _is_checkin(msg):
        return R_CHECKIN

    if _is_pure_arithmetic_turn(msg):
        return R_ARITHMETIC

    ents = _entity_search(msg)
    has_digit = bool(re.search(r"\d", msg or ""))
    ask = bool(_QUESTIONISH.search(msg or ""))
    lookup = bool(_LOOKUP_PHRASE.search(msg or "") or _WIN_ASK.search(msg or ""))
    live = bool(_kw_wants_search(msg) or _positive_search_cmd(msg) or _CURRENT_INFO.search(msg)
                or _SCOREISH.search(msg) or _DATE_Q.search(msg) or lookup)
    titleish = bool(_TOPIC_CUE.search(msg or "")) and not ask and not lookup
    if live or (ents and ask) or (ents and lookup) or (has_digit and ask and not titleish):
        return RUN
    if ask and (ents or has_digit) and not titleish:
        return RUN
    if _TIME_SENSITIVE.search(msg) and (ents or has_digit or _WH_ANY.search(msg)):
        return RUN

    if _LOOK_Q.search(msg) or _LOOK_HINT.search(msg):
        return RUN

    core = _LEAD.sub("", msg).strip()

    # 3) fixed greetings/acks are safe whatever the punctuation
    if _is_checkin(msg) or _SMALLTALK.match(core):
        return R_SMALLTALK

    if _PIVOT.search(msg) and not live and not _TIME_SENSITIVE.search(msg):
        return R_PIVOT

    if "?" in msg or _WH.search(core):
        if len(core.split()) > _ROUTER_SKIP_MAX_WORDS:
            return RUN
        if _SELF_DIRECTED.search(msg) or _PERSONAL_Q.search(msg):
            return R_SELF_DIRECTED
        return RUN

    return R_ENGAGEMENT if titleish else R_STATEMENT


def _skip_search_router(msg: str) -> bool:
    """True only when the router call provably can't change the outcome."""
    return _router_skip_reason(msg) != RUN


def _draft_needs_llm_check(draft: str, user_msg: str) -> bool:
    """Is the non-risky branch of _verify_draft worth an LLM call."""
    if os.environ.get("REEDUS_DRAFT_CHECK_SKIP", "1") != "1":
        return True
    claims = _declarative_only(draft)
    shape = bool(_DRAFT_FACTUAL_SHAPE.search(claims))
    if shape and _PRAISE_SUPERLATIVE.search(claims):
        shape = False                     # "you're the best" isn't a world claim
    asked_a_fact = bool(_looks_factual(user_msg) and _QUESTIONISH.search(user_msg))
    answered_with_entity = bool(_QUESTIONISH.search(user_msg) and _entity_search(claims))
    return bool(
        asked_a_fact
        or answered_with_entity
        or _RISKY_CLAIM.search(claims)
        or _SCOREISH.search(claims)
        or (_entity_search(claims) and _OUTCOME_CLAIM.search(claims))   # X beat/released/died
        or shape
    )


# dialogue put in a character's mouth is the highest-precision fabrication signal and nothing
# else catches it (no numbers, no outcome verbs). verifying it would cost ~6s, so quoted spans
# get removed exactly instead.
_QUOTED_SPEECH = re.compile(r"[\"“”]\s*([A-Za-z][^\"“”]{2,80}?)\s*[\"“”]")

_NOT_SPEECH = re.compile(r"^\s*(?:[\w'’-]+|[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)*)\s*$")


def _has_invented_speech(draft: str) -> bool:
    """Does this draft quote someone? Only spans that read like an utterance count."""
    return any(not _NOT_SPEECH.match(m.group(1)) for m in _QUOTED_SPEECH.finditer(draft or ""))


def _strip_ungrounded_speech(draft: str) -> str:
    """Remove quoted dialogue from a reply that had nothing to ground it."""
    sents = _sentences(draft or "")
    if not sents:
        return draft
    kept = [s for s in sents if not _has_invented_speech(s)]
    if not kept or len(" ".join(kept).split()) < 6:
        return draft
    out = re.sub(r"\s+", " ", " ".join(kept)).strip()
    if DEBUG and len(kept) != len(sents):
        print(f"\n[debug] dropped {len(sents) - len(kept)} ungrounded quoted line(s) "
              f"from the draft", file=sys.stderr)
    return out or draft


def _digits_ok(fact: str, answer: str) -> bool:
    """No number in the reply may contradict the verified fact."""
    strip = lambda s: (s or "").replace(",", "")
    fct = strip(fact)
    nums = [strip(n) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", answer or "")]
    if not nums:
        return not re.search(r"\d", fct or "")
    return all(n in fct for n in nums)


def _grounding_msg(user_msg: str, fact: str) -> str:
    """How the verified fact is handed to the persona. Strict verbatim rule only when the fact has digits."""
    # the "copy every number exactly" rule is for scores/dates/prices. handed a fact with no digits
    # he reads it as "repeat this sentence" and does, word for word, no voice.
    if _OPINIONY.search(user_msg) or not re.search(r"\d", fact or ""):
        return (f"(Context from a live web search: {fact})\n"
                f"Now answer my question in your own voice, give your actual take, pick a "
                f"side, and say why. The context is ammunition, NOT your answer: do not "
                f"restate it, quote it, or let it flatten how you talk. Ground any facts, "
                f"names, or mechanics in it ONLY; if something isn't in it, don't claim it.")
    return (f"(Verified from a live web search: {fact})\n"
            f"Reply to me in your own voice using ONLY this. Copy every number as PLAIN DIGITS "
            f"exactly as written above, do not spell numbers out as words, do not recompute "
            f"them, and keep every name exactly as given. Never mention these instructions, "
            f"the search, or anything about numbers being correct, just answer naturally.")


_TIME_SENSITIVE = re.compile(
    r"\b(won|win|wins|beat|lost|score|match|game|vs\.?|versus|result|results|final|playoff|"
    r"price|stock|news|latest|current|today|tonight|yesterday|election|champion|standings|"
    r"release|released|who is the|who's the)\b", re.I)

_LIVE_THREAD = re.compile(
    r"\b(won|win|wins|beat|lost|score|vs\.?|versus|result|results|playoff|"
    r"price|stock|news|latest|current|today|tonight|yesterday|election|champion|standings|"
    r"who is the|who's the)\b", re.I)


_WRAPUP = re.compile(
    r"(?:\b(?:quick\s+roll\s+call|one\s+last\s+thing|last\s+thing)\s+)?"
    r"\bbefore\s+(?:i|we|you)\s+"
    r"(?:vanish|go\s*dark|head\s*out|sign\s*off|clock\s*out|hang\s*up|peace\s*out|disappear|"
    r"take\s*off|bounce|roll\s*out|dip|wrap(?:s|\s*up|\s*this\s*up|\s*it\s*up)?)\b"
    r"\s*[:—\-,]?\s*",
    re.I)


_NAMEFISH = re.compile(
    r"(?:\b(?:quick\s+)?roll\s*call\b|\bname\s*check\b)[\s—:\-,]*\w+[\s,]*"
    r"(?:right|yeah|yes|correct)?\s*\??"
    r"|\bis your name\s+\w+\s*\??",
    re.I)


_META_COMPLIANCE = re.compile(
    r"(?:^|(?<=[.!?…]))\s*[^.!?…]{0,80}?"
    r"\b(?:didn'?t|did\s+not|haven'?t|won'?t|no)\s*"
    r"(?:fudge|change|alter|touch|mess\s+with|round)[^.!?…]{0,40}?"
    r"\b(?:number|digit|figure)s?\b[^.!?…]*[.!?…]?"
    r"|(?:^|(?<=[.!?…]))\s*[^.!?…]{0,80}?\b(?:number|digit)s?\s+"
    r"(?:are\s+)?(?:kept|copied|exact(?:ly)?|as\s+given|unchanged)[^.!?…]*[.!?…]?",
    re.I)


_SELF_DESCRIPTION = re.compile(
    r"(?:^|(?<=[.!?…]))\s*[^.!?…]{0,100}?"
    r"\b(?:"
    r"(?:talk|talking|chat|chatting|speak|speaking)\s+(?:to\s+you\s+|with\s+you\s+)?"
    r"like\s+(?:we'?re\s+|you'?re\s+|a\s+|old\s+|close\s+)*friends?"
    r"|(?:customer|client)\s+(?:and|&|vs\.?|or)\s+(?:service|support|provider)"
    r"|not\s+(?:like\s+)?(?:an?\s+)?(?:assistant|ai|bot|robot|program|service|machine)\b"
    r"|i'?m\s+(?:not\s+)?(?:your|a|an)\s+(?:companion|assistant|ai|bot|program)\b"
    r")[^.!?…]*[.!?…]?",
    re.I)


_SELF_CORRECTION = re.compile(
    r"(?:^|(?<=[.!?…]))\s*[^.!?…]{0,100}?"
    r"\b(?:"
    r"i\s+(?:almost|nearly)\s+(?:said|told|wrote|answered|guessed)"
    r"|i\s+was\s+about\s+to\s+(?:say|tell|guess)"
    r"|my\s+brain\s+(?:was|is|had)\b"
    r"|(?:instead\s+of|rather\s+than)\s+(?:me\s+)?(?:guess|guessing|making\s+it\s+up)"
    r"|(?:old|stale|outdated)\s+headlines?\b"
    r"|i\s+(?:had|got)\s+(?:it|that)\s+wrong\s+(?:at\s+first|before|earlier)"
    r")[^.!?…]*[.!?…]?",
    re.I)


# search-narration tic, stripped on PERSONAL turns only: he picked up "i looked and the results
# weren't convincing" from the grounding prompts. never on a factual turn, a real "couldn't find
# it" has to survive.
_SEARCH_NARRATION = re.compile(
    r"(?:^|(?<=[.!?…]))\s*"
    r"(?:[^.!?…]*?\b(?:i|we)\b[^.!?…]*?\b(?:searche?d?|searching|look(?:ed|ing)?\s*(?:it\s*)?up|"
    r"verif(?:y|ied)|couldn'?t\s+(?:find|verify)|check(?:ed)?\s+everywhere)\b[^.!?…]*"
    r"|[^.!?…]*?\bthe\s+results?\s+(?:were|weren'?t|came\s+back|returned|looked)[^.!?…]*"
    r"|after\s+searching[^.!?…]*"
    r"|[^.!?…]*?\bmade-?up\s+numbers?\b[^.!?…]*"
    r"|[^.!?…]*?\bno\s+(?:clean|clear|definitive|solid)\s+answer\b[^.!?…]*)"
    r"[.!?…]?",
    re.I)


# --- speak-first acks ---
# a search turn is ~29s and 29s of silence reads as frozen, "hang on, let me look that up" at
# second one doesn't. wired via reedus.on_status = fn; unset = silent.
_SEARCH_ACKS = (
    "hang on, let me look that up",
    "one sec, checking now",
    "let me check that real quick",
    "gimme a second, looking it up",
    "hold on, pulling that up",
)
_LOOK_ACKS = (
    "hang on, let me look",
    "one sec, taking a look",
    "let me see what's there",
    "hold on, checking my eyes",
)
_CORPUS_ACKS = (
    "gimme a sec, checking my notes",
    "one sec, looking that up",
    "hang on, let me check",
    "hold on, digging into that",
)
_last_ack = {"v": ""}


def _ack(pool: tuple) -> str:
    """Pick an ack, never the same one twice in a row (that's what makes canned lines feel canned)."""
    import random
    choices = [p for p in pool if p != _last_ack["v"]] or list(pool)
    pick = random.choice(choices)
    _last_ack["v"] = pick
    return pick


_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0001F900-\U0001F9FF]+")


_LEAKED_REF = re.compile(
    r"\(?\s*Reference material for your reply.*?(?:\)|$)", re.I | re.DOTALL)
_LEAKED_TOOL = re.compile(
    r"""(?xis)
    \(?\s*</?tool_call>\s*\)?                      # stray tags
  | \(?\s*[a-z_]{3,20}\s*\(\s*(?:arguments\s*=\s*)?\{.*?\}\s*\)\s*\)?   # web_search(arguments={...})
  | \{\s*"name"\s*:\s*"[a-z_]+"\s*,\s*"arguments"\s*:.*?\}\s*\}?           # bare JSON call
    """)


def scrub(text: str, personal: bool = False) -> str:
    """Strip the tics from a reply, or from any complete sentence of one."""
    out = _LEAKED_REF.sub(" ", text or "")
    out = _LEAKED_TOOL.sub(" ", out)
    out = _EMOJI.sub(" ", out)            # persona forbids them, models emit them anyway
    out = _MARKDOWN.sub(r"\1", out)
    out = _FILLER_OPENER.sub(" ", out)    # "Honestly," "Man," "I'll be honest,"
    out = _FILLER_MID.sub(", ", out)      # "...to say hi, and honestly, I was hoping..."
    out = _strip_absence(out)             # "didn't mean to vanish", "just popped in"
    out = _WRAPUP.sub(" ", out)
    out = _NAMEFISH.sub(" ", out)
    out = _META_COMPLIANCE.sub(" ", out)
    out = _SELF_DESCRIPTION.sub(" ", out)
    out = _SELF_CORRECTION.sub(" ", out)
    if personal:
        out = _SEARCH_NARRATION.sub(" ", out)       # kill search-talk that leaked into chat
    out = re.sub(r"\s+([.!?,;:])", r"\1", out)      # no space before punctuation
    return re.sub(r"\s{2,}", " ", out).strip(" -—:,;")


MAX_QUESTIONS = int(os.environ.get("REEDUS_MAX_QUESTIONS", "1"))

QUESTION_GAP_MIN = int(os.environ.get("REEDUS_QUESTION_GAP_MIN", "1"))
QUESTION_GAP_MAX = int(os.environ.get("REEDUS_QUESTION_GAP_MAX", "3"))
SOFT_WORDS = int(os.environ.get("REEDUS_SOFT_WORDS", "60"))

MAX_SENTENCES = int(os.environ.get("REEDUS_MAX_SENTENCES", "14"))
MAX_WORDS = int(os.environ.get("REEDUS_MAX_WORDS", "110"))

_FILLER_OPENER = re.compile(
    r"(?:(?<=^)|(?<=[.!?…]\s))\s*(?:(?:honestly|i'?ll be honest|look|listen|man|okay so|ok so|"
    r"i mean|to be fair|not gonna lie|ngl)\s*[,—-]+\s*)+", re.I)
_ABSENCE_CLAUSE = re.compile(
    r"^\s*(?:and\s+|but\s+|so\s+|oh\s+|yeah\s+|also\s+)?"
    r"(?:(?:i\s+)?(?:did\s?n'?t|didnt)\s+mean\s+to\s+(?:vanish|disappear|ghost|bail|go\s+quiet)"
    r"|sorry\s+(?:for|about)\s+(?:the\s+)?(?:delay|wait|disappearing|vanishing|being\s+gone|ghosting)"
    r"|sorry\s+i\s+(?:vanished|disappeared|ghosted|was\s+gone|went\s+quiet)"
    r"|(?:it'?s\s+|its\s+)?(?:has\s+|have\s+)?been\s+(?:a\s+)?(?:while|minute|hot\s+minute|ages)"
    r"|long\s+time\s+no\s+see"
    r"|(?:i'?m\s+|im\s+|we'?re\s+)?back\s+again"
    r"|just\s+(?:popped|dropped|swung|stopped)\s+(?:in|by)(?:\s+to\s+say\s+h(?:i|ello))?"
    r"|just\s+got\s+(?:here|back)"
    r"|(?:i'?ve\s+)?been\s+waiting\s+for\s+(?:you|ya)"
    r"|glad\s+(?:you'?re|youre|you\s+are|ur)\s+back)"
    r"\s*(?:man|bro|dude|buddy|bud|mate|lol|haha|tho|though)?\s*$", re.I)

_CLAUSE_SPLIT = re.compile(r"\s*(,|—|–|\s-\s)\s*")


def _strip_absence(text: str) -> str:
    """Drop clauses that are purely a claim about having been away, keep everything else."""
    out = []
    for sent in _sentences(text or ""):
        body = sent.rstrip()
        tail = ""
        while body and body[-1] in ".!?…":
            tail = body[-1] + tail
            body = body[:-1]
        pieces = [p for p in _CLAUSE_SPLIT.split(body) if p and not _CLAUSE_SPLIT.fullmatch(p)]
        kept = [p for p in pieces if not _ABSENCE_CLAUSE.match(p)]
        if not kept:
            continue
        if len(kept) == len(pieces):
            out.append(sent)              # nothing removed, keep original spacing
        else:
            out.append(", ".join(kept) + (tail or "."))
    return " ".join(s.strip() for s in out).strip() or text

_FILLER_MID = re.compile(
    r",\s*(?:and\s+)?(?:honestly|to be fair|not gonna lie|ngl|i mean|like)\s*,", re.I)
_SENT_START = re.compile(r"(^|[.!?]\s+)([a-z])")

_MARKDOWN = re.compile(r"\*{1,3}([^*\n]+?)\*{1,3}|_{2}([^_\n]+?)_{2}")

_SENTENCE = re.compile(r"[^.!?…]+(?:[.!?…]+|$)")


def _sentences(text: str) -> list:
    """Split into sentences; a trailing ellipsis is a pause, not an end."""
    parts = _SENTENCE.findall(text or "")
    out: list = []
    for p in parts:
        starts_closing_quote = p.lstrip()[:1] in ('"', "”", "’", "'")
        if out and (out[-1].rstrip().endswith(("…", "..."))
                    or _ends_abbrev(out[-1])
                    or starts_closing_quote):
            out[-1] += p
        else:
            out.append(p)
    return out


_ABBREV = frozenset(
    "vs v mr mrs ms dr prof st jr sr no vol ch pt fig approx est feat ft dept inc ltd "
    "jan feb mar apr jun jul aug sept sep oct nov dec e.g i.e etc al".split())


def _ends_abbrev(part: str) -> bool:
    s = part.rstrip()
    if not s.endswith("."):
        return False
    last = s[:-1].split()[-1].lower() if s[:-1].split() else ""
    return last in _ABBREV or (len(last) == 1 and last.isalpha())
_DIRECTED_Q = re.compile(r"\b(you|your|you'?re|you'?ve|you'?d|u|ur|yours|yourself)\b", re.I)
_SENT_CONTINUATION = re.compile(
    r"^\s*(?:also|plus|and|but|so|then|it|its|it'?s|they|them|their|that|this|these|those|which|"
    r"added|adds|includes?|included|features?|featured|comes?|came|brings?|brought|"
    r"launched|shipped|dropped|introduced)\b", re.I)


def _cap_questions(text: str, limit: int = MAX_QUESTIONS) -> str:
    """Keep at most `limit` question sentences, keep all statements."""
    if not text or limit < 0:
        return text
    parts = _sentences(text)
    if len(parts) < 2:
        return text
    kept, n_q, n_any = [], 0, 0
    for p in parts:
        s = p.strip()
        if s.endswith("?"):
            n_any += 1
            if _DIRECTED_Q.search(s):
                n_q += 1
            if n_q > limit or n_any > limit + 1:
                break            # truncate, don't filter
        kept.append(p)
    out = "".join(kept).strip()
    return out or text


def _drop_trailing_question(text: str) -> str:
    """Remove a final question aimed at the user, keeping everything before it."""
    parts = _sentences(text)
    if len(parts) < 2:
        return text                      # a bare question is the whole reply, leave it
    while parts:
        s = parts[-1].strip()
        if s.endswith("?") and _DIRECTED_Q.search(s):
            parts.pop()
            continue
        break
    out = "".join(parts).strip()
    return out or text


# opening sentences with no information: discourse markers, hype, restating the question.
# not the same regex as scrub()'s _FILLER_OPENER, reusing it ripped "Hey" off "Hey, what's going on?"
_FILLER_SENTENCE = re.compile(
    r"^\W*(aight|alright|okay|ok|oh|ah+|yo|hey|man|bro|dude|damn|honestly|truthfully|"
    r"look|listen|see|well|so|right|true|facts|yeah|yep|nah|hmm+|wow|woah|whoa|"
    r"let'?s|lets|now we|we'?re|here'?s the thing|good question|great question|"
    r"real talk|no cap|straight up|for real|buckle up|deep dive|diving deep)\b",
    re.I)

_MIN_AFTER_TRIM = 3

_CONTENT_SENTENCE_WORDS = int(os.environ.get("REEDUS_CONTENT_SENTENCE_WORDS", "5"))

# warn | on | off: what _grounded_prefix does with a name the corpus doesn't have. warn by
# default because live it produced four false cuts and zero true ones ("Naruto's Sage Mode",
# "Ninja Jesus", "Eternal Mangekyō Sharingan", "And Madara"). absent from the corpus is a noisy
# proxy for invented, and the failure mode is silently deleting a right answer.
GROUNDED_CUT = os.environ.get("REEDUS_GROUNDED_CUT", "warn").lower()

MIN_CONDENSE_RATIO = float(os.environ.get("REEDUS_MIN_CONDENSE_RATIO", "0.45"))

# hard ceiling on reply generation, the single biggest cost on this box. at ~7.5 tok/s a
# 184-token reply was 24s and then got condensed to 60 words anyway; "under 60 words" doesn't
# hold on a base model. 128 tokens (~90 words) never touches a well-behaved reply and caps the
# bad case at ~16s. _drop_partial_tail removes the sentence a hard cap can sever.
REPLY_MAX_TOKENS = int(os.environ.get("REEDUS_REPLY_TOKENS", "128"))


def _drop_partial_tail(text: str) -> str:
    """Remove a trailing sentence that never finished (max_tokens cut)."""
    t = (text or "").strip()
    if not t or t[-1] in ".!?…\"'”’)":
        return t
    parts = _sentences(t)
    if len(parts) < 2:
        return t
    kept = re.sub(r"\s+", " ", " ".join(parts[:-1])).strip()
    return kept if len(kept.split()) >= 6 else t

# results on the first pass of a search; widens to SEARCH_MAX_RESULTS only on NOT FOUND.
# each result is ~90 tokens of extractor prefill.
SEARCH_NARROW = int(os.environ.get("REEDUS_SEARCH_NARROW", "2"))


def _has_substance(s: str) -> bool:
    """Does this sentence carry something fact-shaped: a name, a number, a quote?"""
    body = s.strip()
    if not body:
        return False
    if any(ch.isdigit() for ch in body):
        return True
    return bool(re.search(r"(?<!^)(?<![.!?]\s)\b[A-Z][\wÀ-ÿ'’\-]{2,}", body))


_cap_subjects: List[str] = []

_cap_context: str = ""

_EI_CACHE: List = []          # [] unset, [None] unavailable, [index] ready


def _entity_index():
    """Lazy entity index for the reply scrubbers. Never fatal."""
    if not _EI_CACHE:
        try:
            from reedus_companion.knowledge.entity_index import EntityIndex
            ix = EntityIndex()
            ix.db
            _EI_CACHE.append(ix)
        except Exception:                                        # noqa: BLE001
            _EI_CACHE.append(None)
    return _EI_CACHE[0]


def _grounded_prefix(parts: List[str], subjects: List[str]) -> List[str]:
    """Keep the reply up to where the corpus stops supporting it."""
    if GROUNDED_CUT == "off" or not parts:
        return parts
    ix = _entity_index()
    if ix is None:
        return parts
    try:
        from reedus_companion.knowledge.entity_index import drop_subspans
        from reedus_companion.scripts.eval_grounding import proper_nouns
    except Exception:                                            # noqa: BLE001
        return parts
    for i, p in enumerate(parts):
        try:
            for n in drop_subspans(proper_nouns(p), ix.has_topic):
                if len(n.split()) < 2:
                    continue
                if n.lower() in _cap_context:
                    continue
                if ix.has_topic(n) or ix.has_topic_prefix(n):
                    continue
                # qualified variants of real things aren't inventions: real names get qualified at the
                # front ("Eternal Mangekyō Sharingan"), invented ones bolt nonsense on the end. a suffix
                # that is itself a topic clears the name.
                toks = n.split()
                if any(ix.has_topic(" ".join(toks[k:]))
                       for k in range(1, max(1, len(toks) - 1))):
                    continue
                if DEBUG:
                    print(f"\n[debug] grounded cut{' (WARN ONLY)' if GROUNDED_CUT == 'warn' else ''}"
                          f" at sentence {i + 1}: {n!r} is not in the corpus", file=sys.stderr)
                if GROUNDED_CUT == "warn":
                    return parts
                return parts[:i] or parts[:1]
        except Exception:                                        # noqa: BLE001
            return parts
    return parts


def _drop_filler_opening(parts: List[str], max_drop: int = 3) -> List[str]:
    """Strip leading sentences that say nothing so the length cap is spent on content."""
    if len(parts) <= _MIN_AFTER_TRIM:
        return parts
    i, dropped = 0, 0
    while i < len(parts) - _MIN_AFTER_TRIM and dropped < max_drop:
        s = parts[i].strip()
        if s.endswith("?") and not _DIRECTED_Q.search(s):
            i += 1
            dropped += 1
            continue
        if _FILLER_SENTENCE.match(s) and not _has_substance(s):
            i += 1
            dropped += 1
            continue
        break
    return parts[i:] if i else parts


def _cap_length(text: str, limit: int = MAX_SENTENCES) -> str:
    """Hard ceiling on reply length, applied after the question cap."""
    if not text or limit < 1:
        return text
    parts = _sentences(text)
    if not parts:
        return text

    parts = _grounded_prefix(parts, _cap_subjects)

    total_words = sum(len(p.split()) for p in parts)
    if len(parts) <= limit and total_words <= MAX_WORDS:
        return "".join(parts).strip() or text

    parts = _drop_filler_opening(parts)

    q_idx, q_words = None, 0
    for i in range(len(parts) - 1, 0, -1):
        s = parts[i].strip()
        if s.endswith("?") and _DIRECTED_Q.search(s):
            if len(s.split()) <= MAX_WORDS // 2:
                q_idx, q_words = i, len(s.split())
            break

    budget = MAX_WORDS - q_words
    head, used, cost = [], 0, 0
    for i, p in enumerate(parts):
        if q_idx is not None and i >= q_idx:
            break
        w = len(p.split())
        c = 1 if w >= _CONTENT_SENTENCE_WORDS else 0
        if head and (cost + c > limit or len(head) >= limit * 3 or used + w > budget):
            break
        head.append(p)
        used += w
        cost += c
        if not head:
            head.append(p)

    while len(head) > 1:
        s = head[-1].strip()
        if s.endswith("?") and not _DIRECTED_Q.search(s):
            head.pop()
            continue
        break

    out = "".join(head)
    if q_idx is not None:
        out += (" " if not out.endswith((" ", "\n")) else "") + parts[q_idx].strip()
    return out.strip() or text


def _clean_reply(text: str, personal: bool = False) -> str:
    if not text:
        return text
    out = scrub(text, personal)
    out = _cap_length(out)
    out = _cap_questions(out)
    if not out:
        return "Ha. What's good?"                   # whole reply was a tic
    out = _SENT_START.sub(lambda m: m.group(1) + m.group(2).upper(), out)
    return out[0].upper() + out[1:]


def _add_recency(query: str) -> str:
    import datetime as _dt
    if _TIME_SENSITIVE.search(query) and not re.search(r"\b(19|20)\d{2}\b", query):
        return f"{query} {_dt.date.today().year}".strip()
    return query


_FOLLOWUP = re.compile(
    r"\b(again|recap|remind me|what was (that|it)|same (one|game|match|thing)|once more)\b", re.I)


def _strip_notfound_hedge(fact: str) -> str:
    """The extractor sometimes emits a real answer and then appends 'NOT FOUND' as a hedge. Drop the hedge."""
    cleaned = re.sub(r"\s*NOT FOUND\.?\s*$", "", (fact or "").strip(), flags=re.I).strip()
    return cleaned if len(cleaned) >= 12 else (fact or "").strip()


_EXTRACT_META = re.compile(
    r"\b(the\s+results?\s+(are|were|seem|look|don'?t|do\s+not)"
    r"|one\s+says|another\s+(one\s+)?(says|that|claims)|some\s+say"
    r"|i'?ll\s+(resolve|answer|go\s+with)|let\s+me\s+resolve"
    r"|hasn'?t\s+happened|has\s+not\s+happened|not\s+yet\s+(occurred|happened|taken\s+place)"
    r"|as\s+of\s+(my|the\s+)?(\w+\s+)?\d{4}|training\s+(data|cutoff)|knowledge\s+cutoff"
    r"|unfinished|upcoming\s+event"
    r"|(cannot|can'?t|unable\s+to)\s+determine|it'?s\s+unclear|is\s+ambiguous"
    r"|results?\s+are\s+(mixed|conflicting|contradictory))\b", re.I)


_OUTCOME_CLAIM = re.compile(
    r"\b(won|wins|winning|beat|beats|defeated|lost|loses|eliminated|knocked\s+out|"
    r"scored|conceded|drew|tied|"
    r"released|launched|came\s+out|dropped|premiered|announced|discontinued|"
    r"died|passed\s+away|retired|signed|traded|fired|elected|became|took\s+over|"
    r"champion|champions|runner-?up|record\s+holder)\b", re.I)


def _declarative_only(text: str) -> str:
    """Drop questions, only assertions can be factually wrong."""
    sents = re.split(r"(?<=[.!?…])\s+", (text or "").strip())
    return " ".join(s for s in sents if not s.rstrip().endswith("?"))


_EXTRACT_MAX_WORDS = int(os.environ.get("REEDUS_EXTRACT_MAX_WORDS", "24"))


def _extract_noncompliant(fact: str) -> bool:
    """The extractor deliberated instead of answering. Treat as NOT FOUND."""
    f = (fact or "").strip()
    if not f:
        return True
    if _EXTRACT_META.search(f):
        return True
    # sentence count alone fails on fragments: "Hot. High near 95F. Winds SW at 5 to 10 mph." is
    # four sentences and exactly what was asked for. deliberation runs ~36 words before a claim,
    # so sentence count needs a word budget behind it.
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", f) if s.strip()]
    return len(sentences) > 2 and len(f.split()) > _EXTRACT_MAX_WORDS


# the extractor saying it found nothing, in prose. a pattern not a literal list: it once
# wrote "DO NOT PROVIDE" instead of "NOT PROVIDED", that scored as a verified fact, and the
# user got the extractor's error message as reedus's answer.
_NO_ANSWER = re.compile(
    r"\b(do(es)?\s*n[o']t\s+(provide|contain|mention|specify|include|show|list|state|give)"
    r"|not\s+(provided|mentioned|specified|included|listed|stated|available|explicitly)"
    r"|no\s+(specific|definitive|information|data|mention|details?|results?)"
    r"|couldn'?t\s+find|could\s+not\s+find|cannot\s+determine|can'?t\s+determine"
    r"|unable\s+to\s+(find|determine)|there\s+is\s+no\s+|isn'?t\s+any\s+"
    r"|search\s+results\s+(do|don'?t|does)|no\s+clear\s+answer)\b", re.I)


def _is_not_found(fact: str) -> bool:
    """Did the extractor fail, however it phrased it?"""
    raw = (fact or "").strip()
    f = _strip_notfound_hedge(raw)
    fu = f.upper()
    if not f:
        return True
    if re.search(r"\bNOT FOUND\b", raw, re.I) and _NO_ANSWER.search(f):
        return True
    return (fu == "NOT FOUND" or fu.startswith("NOT FOUND")
            or bool(_NO_ANSWER.search(f))
            or "NOT EXPLICITLY" in fu or "NO SPECIFIC" in fu
            or "NOT PROVIDED" in fu or "DO NOT SPECIFY" in fu or "DON'T SPECIFY" in fu
            or "COULDN'T FIND" in fu or "CANNOT DETERMINE" in fu or "NO DEFINITIVE" in fu)


def _fact_is_speakable(fact: str) -> bool:
    """Reject title/SEO junk that must never be spoken."""
    f = (fact or "").strip()
    if not f or _is_not_found(f):
        return False
    if " | " in f:
        return False
    if re.search(
        r"\b(local forecast|live weather radar|weatherbug|accuweather|"
        r"weather channel|10[- ]day forecast|hourly forecast provides)\b",
        f, re.I,
    ):
        return False
    if re.search(r"\s[-–—]\s*(Wikipedia|WeatherBug|AccuWeather|The Weather Channel)\s*$",
                 f, re.I):
        return False
    return True


def _json_objects(text: str):
    """Every balanced {...} span in `text`, outermost first."""
    s = text or ""
    i, n = 0, len(s)
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield s[i:j + 1]
                    break
            j += 1
        i = (j + 1) if j < n else n


def _parse_router_json(text: str, required: str = "search",
                       call: str = "") -> Optional[dict]:
    """Pull the JSON object out of a router reply."""
    for candidate in _json_objects(text):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and required in obj:
            _note_json(call, True)
            return obj
    _note_json(call, False)
    return None


class Backend(Protocol):
    def chat(self, messages: List[Dict[str, str]], max_tokens: int,
             use_adapter: bool = True, temperature: Optional[float] = None,
             on_token: Optional[Callable[[str], None]] = None,
             tier: Optional[str] = None, name: str = "",
             cancel=None) -> str: ...


# --- corpus gate: is there anything here an encyclopedia could have an article about? ---
# the corpus is wiki/fandom passages, its subjects are named and specific. a turn can be full of
# words with nothing lookupable, and each of those cost ~3.7s of subjects + a 60-candidate search
# ("two plus two real quick" -> Magik programming language). two cheap signals, either is enough:
# a named entity, or a long content word ("volcanoes"). positive tiebreak only, a hit can't block
# a turn. REEDUS_CORPUS_GATE=0 restores always-retrieve.
_CORPUS_GATE = os.environ.get("REEDUS_CORPUS_GATE", "1") == "1"

CORPUS_PROBE = os.environ.get("REEDUS_CORPUS_PROBE", "1") == "1"
CORPUS_PROBE_MIN = float(os.environ.get("REEDUS_CORPUS_PROBE_MIN", "0.75"))

_ARITH_NUMBER = re.compile(
    r"(?<!\w)(?:\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million)"
    r"(?!\w)",
    re.I,
)
_ARITH_BINARY_OP = re.compile(
    r"(?:[+*/^]|\b(?:plus|minus|times|multiplied\s+by|divided\s+by|over|mod(?:ulo)?|"
    r"to\s+the\s+power\s+of)\b|(?<=\d)\s*-\s*(?=\d))",
    re.I,
)
_ARITH_UNARY_OP = re.compile(
    r"\b(?:square|cube)\s+root\s+of\b|\b(?:squared|cubed|factorial)\b",
    re.I,
)


def _is_arithmetic_turn(msg: str) -> bool:
    """The turn contains a self-contained arithmetic expression."""
    text = msg or ""
    numbers = list(_ARITH_NUMBER.finditer(text))
    if not numbers:
        return False
    if _ARITH_UNARY_OP.search(text):
        return True
    if len(numbers) < 2:
        return False
    between_operands = text[numbers[0].end():numbers[-1].start()]
    return bool(_ARITH_BINARY_OP.search(between_operands))


def _is_pure_arithmetic_turn(msg: str) -> bool:
    """Arithmetic with no lookup, vision or named-subject instruction attached."""
    text = msg or ""
    return bool(
        _is_arithmetic_turn(text)
        and not _positive_search_cmd(text)
        and not _LOOK_Q.search(text)
        and not _LOOK_HINT.search(text)
        and not _entity_search(text)
    )


_ARITH_ATOM_VALUES = {
    "zero": 0.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0,
    "five": 5.0, "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0,
    "ten": 10.0, "eleven": 11.0, "twelve": 12.0, "thirteen": 13.0,
    "fourteen": 14.0, "fifteen": 15.0, "sixteen": 16.0, "seventeen": 17.0,
    "eighteen": 18.0, "nineteen": 19.0, "twenty": 20.0, "thirty": 30.0,
    "forty": 40.0, "fifty": 50.0, "sixty": 60.0, "seventy": 70.0,
    "eighty": 80.0, "ninety": 90.0, "hundred": 100.0, "thousand": 1000.0,
    "million": 1_000_000.0,
}


def _arithmetic_atom(token: str) -> Optional[float]:
    raw = (token or "").strip().lower()
    if raw in _ARITH_ATOM_VALUES:
        return _ARITH_ATOM_VALUES[raw]
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _format_arithmetic(value: float) -> Optional[str]:
    if not math.isfinite(value) or abs(value) > 1e15:
        return None
    if abs(value - round(value)) < 1e-10:
        return str(int(round(value)))
    return f"{value:.10g}"


def _arithmetic_answer(msg: str) -> Optional[str]:
    """Fast answer for one simple arithmetic expression, else None."""
    text = msg or ""
    if not _is_pure_arithmetic_turn(text):
        return None

    numbers = list(_ARITH_NUMBER.finditer(text))
    unary = _ARITH_UNARY_OP.search(text)
    if unary and len(numbers) == 1:
        value = _arithmetic_atom(numbers[0].group(0))
        if value is None:
            return None
        op = unary.group(0).lower()
        if "square root" in op:
            if value < 0:
                return "That doesn't have a real square root."
            result = math.sqrt(value)
        elif "cube root" in op:
            result = math.copysign(abs(value) ** (1.0 / 3.0), value)
        elif "squared" in op:
            result = value * value
        elif "cubed" in op:
            result = value * value * value
        elif "factorial" in op and value.is_integer() and 0 <= value <= 100:
            result = float(math.factorial(int(value)))
        else:
            return None
        shown = _format_arithmetic(result)
        return f"That's {shown}." if shown is not None else None

    if len(numbers) != 2:
        return None
    left = _arithmetic_atom(numbers[0].group(0))
    right = _arithmetic_atom(numbers[1].group(0))
    if left is None or right is None:
        return None
    between = text[numbers[0].end():numbers[1].start()]
    operator = _ARITH_BINARY_OP.search(between)
    if operator is None:
        return None
    op = operator.group(0).strip().lower()
    if op in {"+", "plus"}:
        result = left + right
    elif op in {"-", "minus"}:
        result = left - right
    elif op in {"*", "times", "multiplied by"}:
        result = left * right
    elif op in {"/", "over", "divided by"}:
        if right == 0:
            return "You can't divide by zero."
        result = left / right
    elif op in {"mod", "modulo"}:
        if right == 0:
            return "You can't divide by zero."
        result = left % right
    elif op in {"^", "to the power of"}:
        if abs(right) > 12 or abs(left) > 1e6:
            return None
        result = left ** right
    else:
        return None
    shown = _format_arithmetic(result)
    if shown is None:
        return None
    left_s = numbers[0].group(0).lower()
    right_s = numbers[1].group(0).lower()
    verb = {"+": "plus", "-": "minus", "*": "times", "/": "divided by",
            "^": "to the power of"}.get(op, op)
    return f"{left_s.capitalize()} {verb} {right_s} is {shown}."


_ASKS_ABOUT = re.compile(
    r"\b(?:what(?:'s|s| is| are| was| were)?|who(?:'s|s| is| are)?|how (?:do|does|did|is|are)"
    r"|why (?:do|does|did|is|are)|when (?:did|was|is|are)|where (?:is|are|did|was)"
    r"|tell me about|explain|describe|define|meaning of|difference between)\b", re.I)
_LONG_BUT_COMMON = frozenset("""
actually anything anymore around because before being better between british
buddy checking company completely different english especially everyone
everything exactly getting really something sometimes somebody someone
speaking started starting suppose supposed talking telling thanks things
thinking thought together tomorrow tonight understand usually walking wanted
watching whatever wondering working writing yesterday another awesome
basically certain course honestly instead literally maybe morning myself
normally obviously perfect please pretty probably question quickly remember
second serious simple special stuff surely though through without would could
should always never people insane pressure little around behind normal
asking answer answered saying seeing coming giving making taking having
looking trying wanting needing calling reading sending showing turning
playing sitting standing running moving living leaving putting getting
holding meaning matter matters better worse enough almost mostly rather
simply hardly barely nearly plenty bunch couple bit lot lots
""".split())


_MID_SENTENCE_CAP = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z][\w']*)", re.M)
_CAP_NOT_A_NAME = frozenset("i i'm i've i'd i'll ok okay oh ah yeah yep nah".split())


_COMMON_STARTERS = frozenset("""
about after again alright anyway avoid based being bring check could does doing
even ever every first found fromggoing going great guess hello here hows just
kinda like listen looks maybe might much never nice okay only other over play
playing please pretty really right same seems since some sorry sound sounds
still such sure take talk tell thank thanks that thats their them then there
these they thing think this those thought took true trying turn very want
watch weird well were what whats when where which while will with wonder would
yeah your youre
""".split())


def _proper_noun_ish(msg: str) -> bool:
    for m in _MID_SENTENCE_CAP.finditer(msg or ""):
        if m.group(1).lower() not in _CAP_NOT_A_NAME:
            return True
    # leading capital of the message / of each sentence
    for w in re.findall(r"(?:^|[.!?]\s+)([A-Z][a-z][\w']*)", msg or ""):
        base = re.sub(r"'(?:s|re|ve|ll|d|m|t)$", "", w.lower())
        base = base.replace("'", "")
        if len(base) >= 5 and base not in _COMMON_EN \
                and base not in _CAP_NOT_A_NAME:
            return True
    return False


_COMMON_EN = frozenset("""
about above after again against all almost alone along already also although
always among another answer any anyone anything anyway are around ask asked
asking away back bad because become been before began begin behind being believe
below best better between big both bring brought build call called came can
cannot come comes coming could country course did didnt different does doesnt
doing done dont down during each early easy either else end enough even ever
every everyone everything example eyes face fact family far feel feeling felt
few find fine first following food form found four friend from front full game
games gave general get gets getting girl give given giving goes going gone good
got great group grow guess had half hand happen happened happy hard has have
having head hear heard help her here high him himself his hold home hope hour
house how however idea important into isnt issue its itself job just keep kept
kind knew know known lack land large last late later laugh lead learn least
leave left less let lets life light like liked likely line list listen little
live long look looked looking looks lose lost lot love low made make makes
making man many may maybe mean means meant men might mind mine minute miss
moment money month more morning most mother move much must myself name near
need needs never new next nice night nobody none nor not note nothing now
number off often okay old once one only open opinion order other others our out
over own part particular people perhaps person pick place plan play played
playing please point possible power present pretty probably problem program put
question quick quickly quite rather reach read ready real really reason remember
right room round run running said same saw say saying says school second see
seem seen sense set several shall she should show showed side simple since sit
small some somebody someone something sometimes soon sorry sound sounds speak
special spend start started state stay still stop story stuff such sure system
take taken taking talk talked talking tell telling ten than thank thanks that
thats the their them themselves then there these they thing things think
thinking third this those though thought three through time times today
together told tomorrow tonight too took top toward town tried true try trying
turn turned two under understand until upon use used using usually very want
wanted wants war watch watched watching water way week weird well went were
what whatever when where whether which while white who whole whom whose why
wide will win with within without woman women wonder word words work worked
working world worry would wrong yeah year years yes yet you young your yours
yourself
""".split())


def _corpus_candidates(msg: str) -> list:
    """Spans that could name something, ranked by how name-like they are."""
    strong, weak = [], []
    seen = set()
    def add(bucket, t):
        t = (t or "").strip(" .,!?'\"")
        if len(t) >= 4 and t.lower() not in seen:
            seen.add(t.lower()); bucket.append(t)
    for m in re.finditer(r"\b[A-Z][\w']*(?:\s+[A-Z][\w']*){1,3}", msg or ""):
        add(strong, m.group(0))
    for w in re.findall(r"\b[A-Za-z][\w']{3,}\b", msg or ""):
        if w.lower().replace("'", "") not in _COMMON_EN:
            add(weak, w)
    return (strong + weak)[:16]


def _corpus_has_target(msg: str) -> bool:
    """Could the corpus plausibly have something for this turn?"""
    if not _CORPUS_GATE:
        return True
    if _entity_search(msg) is not None:
        return True
    # 2. a single proper noun ("...playing Fortnite recently").
    if _proper_noun_ish(msg):
        return True
    ask = _ASKS_ABOUT.search(msg or "")
    if not ask:
        return False
    ask_clause = (msg or "")[ask.start():]
    for w in re.findall(r"[A-Za-z]{5,}", ask_clause):
        if w.lower() not in _COMMON_EN:
            return True
    return False


CORPUS_YES = "yes"
CORPUS_NO = "no"
CORPUS_MAYBE = "maybe"

_CORPUS_BY_REASON = {
    R_CHECKIN: CORPUS_NO,
    R_ARITHMETIC: CORPUS_NO,
    R_SMALLTALK: CORPUS_NO,
    R_SELF_DIRECTED: CORPUS_NO,
    "personal": CORPUS_NO,
    "cloud": CORPUS_NO,               # corpus is off in cloud mode
    "cancelled": CORPUS_NO,
    R_PIVOT: CORPUS_YES,
    R_ENGAGEMENT: CORPUS_YES,
    "demoted-to-corpus": CORPUS_YES,  # router said yes but it's not a live fact
    "forced-entity": CORPUS_YES,
    "no-search-veto": CORPUS_YES,
    "router-no": CORPUS_MAYBE,
    "router-yes": CORPUS_MAYBE,
    "kw-fallback": CORPUS_MAYBE,
    R_STATEMENT: CORPUS_MAYBE,        # "been reading about volcanoes" and "man that's crazy" are both this rule
}


def _corpus_verdict(decision: dict, *, date_q: bool, looked: bool, look_failed: bool,
                    conv_turn: bool, weather_fact: bool, user_msg: str = "") -> str:
    """CORPUS_YES / CORPUS_NO / CORPUS_MAYBE for this turn."""
    if decision.get("search") or date_q or looked or look_failed or weather_fact:
        return CORPUS_NO
    if conv_turn:
        return CORPUS_NO
    if _is_arithmetic_turn(user_msg):
        return CORPUS_NO
    if user_msg:
        try:
            from reedus_companion import mode as _mode
            if _mode.switch_request(user_msg):
                return CORPUS_NO
        except Exception:                                        # noqa: BLE001
            pass
    return _CORPUS_BY_REASON.get(decision.get("why") or "", CORPUS_MAYBE)


class Reedus:
    def __init__(self, backend: "Backend"):
        self.backend = backend
        self.mem = Memory()
        self.tools = toolmod.ToolRegistry(memory=self.mem)
        self.history: List[Dict[str, str]] = []
        self.last_search_query: Optional[str] = None
        self.last_fact: Optional[str] = None
        self._vision = None
        self.topics: dict = self._load_topics()
        self._knowledge = None
        self._last_anchor = ""              # the conversation's established subject
        self._asked_last_turn = False       # so questions alternate instead of every turn
        self._question_cooldown = 0
        self._draft_words: List[int] = []
        self._skip_corpus = False
        self._corpus_unsure = False
        # sticky retrieved-passage block, keyed by subject. rebuilding it every turn cost ~450
        # tokens of persona prefill even when nothing changed. weak blocks are never "known" on reuse.
        self._sticky_knowledge: str = ""
        self._sticky_subject: str = ""
        self._sticky_weak: bool = False
        self._session_subject: str = ""
        # optional hook, called the moment slow work starts so the UI/TTS can say "hang on"
        # instead of going silent for 29 seconds
        self.on_status: Optional[Callable[[str], None]] = None
        self.on_token: Optional[Callable[[str], None]] = None
        self.on_replaced: Optional[Callable[[str, str], None]] = None
        self._corrections = None
        self._pending_fix: Optional[int] = None
        self._cancel = None
        self.cancelled = False

    def warm(self, persona_only: bool = False) -> float:
        """Pre-prefill the prompt shapes so the first question doesn't pay for them."""
        import time
        today = datetime.date.today().strftime("%A, %B %d, %Y")
        shapes = [("persona", self._messages("hello"))]
        if not persona_only:
            shapes += [
                ("search-router",
                 [{"role": "system", "content": ROUTER_SYSTEM + f"\nToday is {today}."},
                  {"role": "user", "content": "hello"}]),
                ("extract", [{"role": "system", "content": EXTRACT_SYSTEM},
                             {"role": "user", "content": "Question: hello\n\nSearch results:\n-"}]),
                ("draft-check",
                 [{"role": "system", "content": DRAFT_CHECK_SYSTEM + f"\nToday is {today}."},
                  {"role": "user", "content": "User asked: hello\n\nDRAFT reply: hi"}]),
                ("look-router",
                 [{"role": "system", "content": LOOK_ROUTER_SYSTEM},
                  {"role": "user", "content": "hello"}]),
                ("topic-router",
                 [{"role": "system", "content": TOPIC_ROUTER_SYSTEM},
                  {"role": "user", "content": "hello"}]),
                ("forced-query",
                 [{"role": "system", "content": FORCED_QUERY_SYSTEM + f"\nToday is {today}."},
                  {"role": "user", "content": "User asked: hello\n\nDRAFT reply: hi\n\n"
                                              "(flagged text: 'hi')"}]),
                # subjects went 4.6s cold -> 0.97s warm, so it belongs here. imported lazily because
                # retrieve pulls numpy/fastembed and warm() must never stop a box without them from starting.
                *_warm_subject_shape(),
            ]
        t0 = time.time()
        for name, msgs in shapes:
            try:
                self.backend.chat(msgs, 1, use_adapter=(name == "persona"), temperature=0.0,
                                  name=name)
            except Exception as e:                      # noqa: BLE001, warming is best-effort
                if DEBUG:
                    print(f"\n[debug] warm({name}) failed (harmless): {e!r}", file=sys.stderr)
        # first look on the xeon paid ~35s for the YOLO-World+CLIP cold load, so do it at boot
        if not persona_only and os.environ.get("REEDUS_VISION", "1") != "0":
            try:
                vb = self._vision_bridge()
                if vb is not None:
                    print("Warming vision…", end=" ", flush=True)
                    print(f"{vb.warm():.1f}s")
            except Exception as e:                      # noqa: BLE001
                if DEBUG:
                    print(f"\n[debug] warm(vision) failed (harmless): {e!r}", file=sys.stderr)
        return time.time() - t0

    def _context_is_factual(self, lookback: int = 4) -> bool:
        """Is the recent conversation about checkable real-world facts?"""
        recent = " ".join(m.get("content", "") for m in self.history[-lookback:])
        if not recent:
            return False
        return bool(_entity_search(recent) and _LIVE_THREAD.search(recent))

    def _continues_factual_topic(self, user_msg: str) -> bool:
        """Factual thread and this message is still about it."""
        return bool(self._context_is_factual() and _CONTINUATION.search(user_msg)
                    and not _is_checkin(user_msg))

    def _status(self, msg: str) -> None:
        """Fire the status hook. A UI callback must never break a turn."""
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception as e:                      # noqa: BLE001
                if DEBUG:
                    print(f"\n[debug] on_status hook raised: {e!r}", file=sys.stderr)

    def _check_cancel(self) -> bool:
        """True if think-barge set the cancel Event; marks the turn cancelled so reply() aborts."""
        cancel = self._cancel
        if cancel is not None and cancel.is_set():
            self.cancelled = True
            if DEBUG:
                print("\n[debug] turn cancelled (barge-in)", file=sys.stderr)
            return True
        return False

    def _vision_bridge(self):
        """Lazily build the camera/detector bridge on the first look turn. None if vision is off."""
        if self._vision is False:
            return None
        if self._vision is None:
            try:
                from reedus_companion.serve.vision_bridge import VisionBridge, vision_enabled
                self._vision = VisionBridge() if vision_enabled() else False
            except Exception:
                self._vision = False
        return self._vision or None

    # --- context ---
    def _system_parts(self, user_msg: str) -> tuple:
        """(stable system text, volatile block). One call because building twice would run retrieval twice."""
        today = datetime.date.today().strftime("%A, %B %d, %Y")
        recalled = "" if _is_contentless(user_msg) else self.mem.recall(user_msg)
        topics = self._knowledge_for(user_msg)
        tools = toolmod.render_tools_block()
        vol = dict(bulletin=self._bulletin_for(user_msg), recalled=recalled, topics=topics,
                   weak_knowledge=getattr(self, "_knowledge_weak", False))
        if PERSONA_SPLIT:
            return build_stable_system(today=today, tools_block=tools), build_volatile_block(**vol)
        return build_runtime_system(today=today, tools_block=tools, **vol), ""

    def _system(self, user_msg: str) -> str:
        """Back-compat single-string form of the prompt."""
        stable, vol = self._system_parts(user_msg)
        return f"{stable}\n\n{vol}" if vol else stable

    def _retrieval_query(self, user_msg: str) -> str:
        """The text we actually search the corpus with."""
        parts: List[str] = []
        prev = [m.get("content", "") for m in self.history[-3:] if m.get("role") == "user"]
        prev_msg = prev[-2] if len(prev) >= 2 else (prev[-1] if prev else "")
        haystack = (" ".join(prev) + " " + user_msg).lower()

        haystack_words = set(re.findall(r"[a-z0-9']+", haystack))
        anchor = ""
        best_len = -1
        for name in self.topics:
            sig = [w for w in re.split(r"\W+", name.lower())
                   if len(w) > 3 and w not in _TOPIC_FILLER]
            if not sig:
                continue
            need = 1 if len(sig) == 1 else max(2, (len(sig) + 1) // 2)
            if sum(w in haystack_words for w in sig) < need:
                continue
            if len(name) > best_len:
                anchor = name
                best_len = len(name)
        if not anchor:
            kb = self._knowledge if self._knowledge not in (None, False) else None
            if kb is not None and getattr(kb, "last_topics", None):
                anchor = kb.last_topics[0]

        self._last_anchor = anchor
        if anchor:
            parts.append(anchor)

        own = _content_words(user_msg)
        if len(own) < 3 and prev_msg:
            own = _content_words(prev_msg) + own
        parts.extend(own[-24:])
        q = " ".join(p for p in parts if p.strip())
        if DEBUG:
            print(f"[debug] retrieval query -> {q[:150]!r}", file=sys.stderr)
        return q

    def _knowledge_for(self, user_msg: str) -> str:
        """Retrieved passages if the corpus has anything, the old topic briefs otherwise."""
        # skip the corpus when there's nothing to look up (a bare greeting paid 1.1s to retrieve
        # nothing) or when the turn is grounded by something fresher (web / camera).
        skip = _is_contentless(user_msg) or getattr(self, "_skip_corpus", False)
        self._knowledge_weak = False
        if skip:
            return self._topics_for(user_msg)

        unsure = getattr(self, "_corpus_unsure", False)
        needs_evidence = unsure and not _corpus_has_target(user_msg)

        kb = self._kb()
        if kb is not None:
            try:
                rq = self._retrieval_query(user_msg)
                hint = getattr(self, "_learned_topic", "") or getattr(self, "_last_anchor", "")
                subject_key = (hint or "").strip().lower()
                reuse_key = (getattr(self, "_session_subject", "") or subject_key).strip().lower()
                session_locked = bool(getattr(self, "_session_subject", ""))
                if (reuse_key and reuse_key == self._sticky_subject
                        and self._sticky_knowledge
                        and (not self._sticky_weak or session_locked)):
                    if DEBUG:
                        print(f"[debug] sticky knowledge reuse -> {self._sticky_subject!r} "
                              f"({len(self._sticky_knowledge)} chars"
                              f"{', weak-but-session-locked' if self._sticky_weak else ''})",
                              file=sys.stderr)
                    self._knowledge_weak = bool(self._sticky_weak) if session_locked else False
                    return self._sticky_knowledge

                probe = getattr(kb, "probe", None) if CORPUS_PROBE else None
                if needs_evidence and probe is not None:
                    best = probe(rq, context=getattr(self, "_last_anchor", ""))
                    if best < CORPUS_PROBE_MIN:
                        if DEBUG:
                            print(f"[debug] corpus probe {best:.2f} < {CORPUS_PROBE_MIN:.2f} "
                                  f"-> nothing to retrieve, skipping (silently)",
                                  file=sys.stderr)
                        return self._topics_for(user_msg)
                    if DEBUG:
                        print(f"[debug] corpus probe {best:.2f} -> retrieving", file=sys.stderr)

                # speak before subjects + retrieve, otherwise a 5-15s corpus turn feels crashed
                self._status(_ack(_CORPUS_ACKS))

                # weak sticky or subject change: refresh from the corpus
                ctx = kb.context_for(rq, context=getattr(self, '_last_anchor', ''),
                                     hint=hint or "")
                self._knowledge_weak = bool(getattr(kb, "last_weak", False))
                if self._knowledge_weak:
                    # weak passages are a licence to invent AND they bust the persona prefix
                    # cache (~12s extra prefill). inject nothing, talk from the persona.
                    if DEBUG:
                        print("[debug] nothing confidently anchored (nothing injected) "
                              "-> flagging the turn as ungrounded", file=sys.stderr)
                    return ""
                if ctx:
                    # prefer kb's reported topic if the hint was empty this turn
                    if not subject_key and getattr(kb, "last_topics", None):
                        subject_key = (kb.last_topics[0] or "").strip().lower()
                    if (KNOWLEDGE_COMPRESS
                            and len(ctx) >= KNOWLEDGE_COMPRESS_MIN_CHARS):
                        ctx = self._compress_knowledge(ctx) or ctx
                    store_key = (getattr(self, "_session_subject", "")
                                 or subject_key).strip().lower()
                    if store_key:
                        self._sticky_knowledge = ctx
                        self._sticky_subject = store_key
                        self._sticky_weak = False
                    return ctx
            except Exception as e:                      # noqa: BLE001
                if DEBUG:
                    print(f"\n[debug] knowledge recall failed: {e!r}", file=sys.stderr)
        return self._topics_for(user_msg)

    def _update_session_subject(self, topic: Optional[str]) -> None:
        """Lock sticky reuse to a titled engagement until the next one."""
        new = (topic or "").strip().lower()
        if not new or not _is_plausible_topic_title(new):
            return
        old = getattr(self, "_session_subject", "") or ""
        if new == old:
            return
        if DEBUG and old:
            print(f"[debug] session subject {old!r} -> {new!r} (sticky cleared)",
                  file=sys.stderr)
        self._session_subject = new
        self._sticky_knowledge = ""
        self._sticky_subject = ""
        self._sticky_weak = False

    def _compress_knowledge(self, passages: str) -> str:
        """Router-tier brief of retrieved wiki prose so the main model doesn't re-prefill it."""
        msgs = [
            {"role": "system", "content":
             "Rewrite as <=3 short bullets. Keep owner labels. No preamble. <=40 words."},
            {"role": "user", "content": passages[:1800]},
        ]
        try:
            out = self.backend.chat(msgs, 40, use_adapter=False, temperature=0.0,
                                    name="knowledge-brief", cancel=self._cancel).strip()
        except Exception as e:                              # noqa: BLE001
            if DEBUG:
                print(f"[debug] knowledge compress failed: {e!r}", file=sys.stderr)
            return ""
        # strip accidental think / fence wrappers from the small model
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
        out = re.sub(r"^```\w*\n?|\n?```$", "", out).strip()
        if not out or len(out) < 20:
            return ""
        if len(out) > 420:
            out = out[:420].rsplit(" ", 1)[0]
        if DEBUG:
            print(f"[debug] knowledge compressed {len(passages)} -> {len(out)} chars",
                  file=sys.stderr)
        return out

    # --- topic learning ---
    def _kb(self):
        """Lazy knowledge base. None if unavailable, retrieval is never load-bearing."""
        try:
            from reedus_companion import mode as _mode
            if not _mode.knowledge_enabled():
                return None
        except Exception:
            pass
        if self._knowledge is False:
            return None
        if self._knowledge is None:
            try:
                from reedus_companion.knowledge import Knowledge
                self._knowledge = Knowledge(
                    embed=self.mem.embedder.embed,
                    max_hot=config.KNOWLEDGE_MAX_HOT,
                    llm_route=lambda t: self.backend.chat(
                        [{"role": "system", "content": _DOMAIN_ROUTER_SYSTEM},
                         {"role": "user", "content": t}], 40,
                        use_adapter=False, temperature=0.0, name="domain-router",
                        cancel=self._cancel),
                    llm_chat=lambda m, n: self.backend.chat(
                        m, n, use_adapter=False, temperature=0.0, name="subjects",
                        cancel=self._cancel),
                    debug=DEBUG)
            except Exception as e:                      # noqa: BLE001
                if DEBUG:
                    print(f"\n[debug] knowledge unavailable: {e!r}", file=sys.stderr)
                self._knowledge = False
                return None
        return self._knowledge

    def _load_topics(self) -> dict:
        try:
            if config.TOPICS_PATH.exists():
                data = json.loads(config.TOPICS_PATH.read_text())
                if isinstance(data, dict):
                    return data
        except Exception as e:                          # noqa: BLE001
            if DEBUG:
                print(f"\n[debug] topics load failed: {e!r}", file=sys.stderr)
        return {}

    def _save_topics(self) -> None:
        try:
            config.TOPICS_PATH.parent.mkdir(parents=True, exist_ok=True)
            # keep the most recent N, dicts preserve insertion order
            items = list(self.topics.items())[-MAX_TOPICS_STORED:]
            self.topics = dict(items)
            config.TOPICS_PATH.write_text(json.dumps(self.topics, indent=1, ensure_ascii=False))
        except Exception as e:                          # noqa: BLE001
            if DEBUG:
                print(f"\n[debug] topics save failed: {e!r}", file=sys.stderr)

    def _topics_for(self, user_msg: str) -> str:
        """Only the briefs the current conversation is actually about."""
        if not self.topics:
            return ""
        recent = (user_msg + " " + " ".join(
            m.get("content", "") for m in self.history[-6:])).lower()
        hits = []
        for name, brief in reversed(list(self.topics.items())):
            key = name.lower()
            words = [w for w in re.split(r"\W+", key) if len(w) > 3]
            if key in recent or (words and sum(w in recent for w in words) >= max(1, len(words) // 2)):
                hits.append(f"- {name}: {brief}")
            if len(hits) >= MAX_TOPICS_INJECTED:
                break
        return "\n".join(hits)

    def _engagement_nudge(self) -> str:
        """One-off reminder when the conversation has gone one-sided."""
        if os.environ.get("REEDUS_NUDGE", "1") != "1":
            return ""
        replies = [m.get("content", "") for m in self.history if m.get("role") == "assistant"]
        recent = replies[-3:]
        if len(recent) < 2 or any("?" in r for r in recent):
            return ""
        return ("(Your last few replies have all been reactions, no question, no opinion of "
                "your own. It's going one-sided. In THIS reply either say what you actually "
                "think about what they said, or ask one real question about it. If you don't "
                "know the thing, ask them about it instead of saying you don't know and "
                "stopping. Don't mention this note.)")

    def _length_nudge(self) -> str:
        """Pull the length rule out of the system prompt and put it where it gets obeyed."""
        # off by default: replies got shorter (37 -> 25 words) but "unknown handled" fell
        # 100% -> 50%, he stopped asking about things he didn't know. REEDUS_LENGTH_NUDGE=1.
        if os.environ.get("REEDUS_LENGTH_NUDGE", "0") != "1":
            return ""
        recent = self._draft_words[-2:]
        if len(recent) < 2:
            return ""
        if sum(recent) / len(recent) <= SOFT_WORDS:
            return ""
        return ("(Your last couple of replies ran long. Keep this one to two or three "
                "sentences, say the one thing that matters and stop. Don't mention this note.)")

    def _topic_already_known(self, text: str) -> Optional[str]:
        """Fuzzy topic match on significant words. The known topic key, or None."""
        def sig(s: str) -> set:
            s = "".join(c for c in unicodedata.normalize("NFKD", s or "")
                        if not unicodedata.combining(c))
            return {w for w in re.split(r"\W+", s.lower())
                    if len(w) > 2 and w not in _TOPIC_FILLER}

        want = sig(text)
        if not want:
            return None
        for known in self.topics:
            have = sig(known)
            if not have:
                continue
            overlap = len(want & have)
            if overlap < 2:
                continue
            if not (overlap / len(have) >= 0.6 or overlap / len(want) >= 0.6):
                continue
            if len(have) >= len(want) + 2 and overlap / len(have) < 0.6:
                continue
            return known
        return None

    def _corpus_resolve_topic(self, topic: str) -> Optional[str]:
        """Map a router title onto a name the corpus already holds."""
        topic = (topic or "").strip()
        if not topic:
            return None
        kb = self._kb()
        if kb is None:
            return None
        try:
            if kb.has_topic(topic):
                return topic
        except Exception:                                        # noqa: BLE001
            return None
        words = [w for w in re.split(r"\W+", topic) if w]
        if len(words) < 2:
            return None
        best = ""
        for i in range(len(words) - 1, 0, -1):
            for span in (" ".join(words[:i]), " ".join(words[i:])):
                if len(span) < 3 or len(span) <= len(best):
                    continue
                try:
                    if kb.has_topic(span):
                        best = span
                except Exception:                                # noqa: BLE001
                    pass
        return best or None

    def _maybe_learn_topic(self, user_msg: str) -> Optional[str]:
        try:
            from reedus_companion import mode as _mode
            if not _mode.local_search_enabled():
                return None
        except Exception:
            pass
        """If the user named something reedus doesn't know, look it up once and keep a brief.

        Returns the topic name if one was learned (or already known) this turn, else None. A
        known topic suppresses the search router so "been playing Naruto Storm 4" doesn't
        re-search the web every time.
        """
        if os.environ.get("REEDUS_TOPIC_LEARNING", "1") != "1":
            return None
        if not _TOPIC_CUE.search(user_msg):
            return None
        if (getattr(self, "_session_subject", "")
                and not _NEW_ENGAGE.search(user_msg or "")):
            if DEBUG:
                print(f"[debug] topic cue ignored, session locked to "
                      f"{self._session_subject!r}", file=sys.stderr)
            return None
        known = self._topic_already_known(user_msg)
        if known:
            if DEBUG:
                print(f"[debug] topic already known -> {known!r} (skipping learn + search)",
                      file=sys.stderr)
            return known

        # cheap title check before the router: if the corpus already has it there's nothing to decide
        cheap = _topic_candidate(user_msg) or _topic_candidate(user_msg, min_words=1)
        if cheap:
            resolved = self._corpus_resolve_topic(cheap)
            if resolved:
                if DEBUG:
                    print(f"[debug] topic from corpus ({cheap!r} -> {resolved!r}) "
                          f"- skipped topic-router", file=sys.stderr)
                return resolved

        router_user = user_msg
        anchor = (getattr(self, "_session_subject", "")
                  or getattr(self, "_sticky_subject", "")
                  or getattr(self, "_last_anchor", "")
                  or "").strip()
        if anchor:
            router_user = (f"(Conversation is already about: {anchor}. Abilities, modes, arcs, "
                           f"locations, characters and techniques inside that are NOT new titles "
                           f"- return empty topic for those. Never return a lone adverb or filler "
                           f"word as a topic.)\n{user_msg}")

        raw = self.backend.chat(
            [{"role": "system", "content": TOPIC_ROUTER_SYSTEM},
             {"role": "user", "content": router_user}],
            80, use_adapter=False, temperature=0.0, name="topic-router",
            cancel=self._cancel)
        if self._check_cancel():
            return None
        obj = _parse_router_json(raw, required="topic", call="topic-router") or {}
        topic = (obj.get("topic") or "").strip()
        query = (obj.get("query") or "").strip() or topic
        from_regex = False
        if DEBUG:
            print(f"\n[debug] topic router -> {obj or raw[:120]!r}", file=sys.stderr)
        if not topic or len(topic) < 3:
            topic = _topic_candidate(user_msg)
            query = topic
            from_regex = True
            if DEBUG and topic:
                print(f"[debug] topic router empty -> regex candidate {topic!r}", file=sys.stderr)
            if not topic:
                return None
        if not _is_plausible_topic_title(topic):
            if DEBUG:
                print(f"[debug] topic {topic!r} rejected as non-title", file=sys.stderr)
            return None
        known2 = self._topic_already_known(topic)
        if known2:
            return known2

        # resolve against the corpus before any "new one on me" ack. "Sage Mode Naruto" isn't a
        # wiki title, "Sage Mode" is, and a topic-brief for the compound was ~8s wasted.
        resolved = self._corpus_resolve_topic(topic)
        if resolved:
            if not _is_plausible_topic_title(resolved):
                return None
            if DEBUG:
                print(f"[debug] topic {topic!r} resolved to corpus {resolved!r} "
                      f"(skipping learn + web brief)", file=sys.stderr)
            return resolved

        if getattr(self, "_session_subject", "") and from_regex:
            if DEBUG:
                print(f"[debug] regex candidate {topic!r} ignored, session already "
                      f"{self._session_subject!r}", file=sys.stderr)
            return None

        self._status(_ack(_TOPIC_ACKS))

        learned_anything = False
        already_known = False
        if self._check_cancel():
            return None
        kb = self._kb()
        if kb is not None:
            try:
                n = kb.learn(topic)
                learned_anything = bool(n)
                if n and DEBUG:
                    print(f"[debug] knowledge +{n} passages for {topic!r}", file=sys.stderr)
                elif not n:
                    already = already_known = kb.has_topic(topic)
                    if already and DEBUG:
                        print(f"[debug] {topic!r} already in the corpus", file=sys.stderr)
                    elif not already:
                        print(f"[knowledge] nothing ingested for {topic!r}, no article found, "
                              f"or the fetch failed. Try: python -m reedus_companion.scripts."
                              f"knowledge_cli learn {topic!r}", file=sys.stderr)
            except Exception as e:                      # noqa: BLE001
                print(f"[knowledge] ingest FAILED for {topic!r}: {type(e).__name__}: {e}",
                      file=sys.stderr)

        if from_regex and not learned_anything:
            if DEBUG:
                print(f"[debug] regex candidate {topic!r} has no encyclopedia article, "
                      f"not searching, not storing", file=sys.stderr)
            return None

        # already in the corpus -> stop. a brief is the fallback for when retrieval finds
        # nothing; buying one for a topic the corpus holds is paying ~7s for a worse copy.
        if already_known:
            if DEBUG:
                print(f"[debug] {topic!r} is in the corpus, skipping the web brief "
                      f"(retrieval covers it)", file=sys.stderr)
            return topic

        if self._check_cancel():
            return topic if topic in self.topics else None
        results = web_search(query)
        if self._check_cancel():
            return topic if topic in self.topics else None
        if results and not results.lstrip().startswith("[web_search"):
            brief = self.backend.chat(
                [{"role": "system", "content": TOPIC_BRIEF_SYSTEM},
                 {"role": "user", "content": f"Thing: {topic}\n\nSearch results:\n{results}"}],
                200, use_adapter=False, temperature=0.0, name="topic-brief",
                cancel=self._cancel).strip()
            if brief and not _is_not_found(brief) and not _EXTRACT_META.search(brief):
                self.topics[topic] = brief
                self._save_topics()
                if DEBUG:
                    print(f"\n[debug] LEARNED {topic!r}: {brief[:120]}", file=sys.stderr)
            elif DEBUG:
                print(f"\n[debug] brief unusable for {topic!r} (corpus ingest unaffected)",
                      file=sys.stderr)

        return topic if (learned_anything or topic in self.topics) else None

    def _bulletin_for(self, user_msg: str) -> str:
        """Bulletin with the pinned-facts section relevance-gated. User facts always go in."""
        text = self.mem.read_bulletin() or ""
        if not text.strip():
            return ""
        head, sep, pinned = text.partition("## Pinned")

        user_facts = [l.rstrip() for l in head.splitlines() if l.strip().startswith("-")]
        world_facts: List[str] = []
        if sep:
            pin_body = pinned.partition("\n")[2]
            pin_lines = [l.rstrip() for l in pin_body.splitlines() if l.strip().startswith("-")]
            # check-ins must not pull world pins: risks blurting a footballer into small talk
            # and changes the volatile block so the persona prefix misses the cache (~3s).
            if pin_lines and not _is_checkin(user_msg):
                world_facts = self._gate_pins(user_msg, pin_lines)
            elif DEBUG and pin_lines and _is_checkin(user_msg):
                print("\n[debug] bulletin gate -> skipped (check-in)", file=sys.stderr)

        out = []
        if user_facts:
            out.append("About the user:\n" + "\n".join(user_facts))
        if world_facts:
            out.append(
                "Settled facts about the WORLD, background knowledge only. These describe "
                "other people and events; NONE of them describe the user, and the user is not "
                "any person named here:\n" + "\n".join(world_facts))
        return "\n\n".join(out)

    def _gate_pins(self, user_msg: str, lines: List[str]) -> List[str]:
        """Only the pinned world facts the conversation is actually near."""
        if not lines:
            return []
        try:
            from reedus_companion.serve.memory import _cosine
            context = user_msg + " " + " ".join(
                m["content"] for m in self.history[-4:] if m["role"] == "user")
            vecs = self.mem.embedder.embed([context] + lines)
            keep = [l for l, v in zip(lines, vecs[1:]) if _cosine(vecs[0], v) >= 0.45]
            if DEBUG and len(keep) != len(lines):
                print(f"\n[debug] bulletin gate -> {len(keep)}/{len(lines)} pins relevant",
                      file=sys.stderr)
        except Exception:
            keep = lines                      # embedder hiccup, fail open
        return keep

    def _messages(self, user_msg: str) -> List[Dict[str, str]]:
        stable, volatile = self._system_parts(user_msg)
        msgs = [{"role": "system", "content": stable}]
        msgs += _history_window(self.history)
        # the prefill fix: volatile blocks go HERE, after history and right before the user's turn.
        # llama.cpp reuses a common prefix and re-evaluates everything after the first difference,
        # so with passages in the system message all twelve history messages re-prefilled every
        # grounded turn (~8s of a 10.2s call). sent as `user` not `system`: several chat formatters
        # only take the system message from messages[0] and would drop this block silently.
        if volatile:
            msgs.append({"role": "user", "content":
                         "(Reference material for your reply, not something I said. Use it "
                         "naturally, don't recite it or mention it.)\n\n" + volatile})
        msgs.append({"role": "user", "content": user_msg})
        return msgs

    # --- look router ---
    def _route_look(self, user_msg: str) -> bool:
        """LLM decision: does this turn want the camera? Only called when the fast regex is unsure."""
        msgs = [{"role": "system", "content": LOOK_ROUTER_SYSTEM}]
        msgs += self.history[-4:]
        msgs.append({"role": "user", "content": user_msg})
        raw = self.backend.chat(msgs, 40, use_adapter=False, temperature=0.0,
                                name="look-router", cancel=self._cancel)
        for candidate in _json_objects(raw):
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("look"), bool):
                if DEBUG:
                    print(f"\n[debug] look-router -> {obj['look']}", file=sys.stderr)
                _note_json("look-router", True)
                return obj["look"]
        m = re.search(r'"?look"?\s*[:=]\s*(true|false)\b', raw or "", re.I)
        if m:
            verdict = m.group(1).lower() == "true"
            if DEBUG:
                print(f"\n[debug] look-router -> {verdict} (recovered from malformed JSON)",
                      file=sys.stderr)
            _note_json("look-router", True)
            return verdict
        _note_json("look-router", False)
        return False

    def _wants_look_turn(self, user_msg: str) -> bool:
        """Fast regex first, LLM router only on a hint. A search verb always wins."""
        if _SEARCH_VERB.search(user_msg):
            return False
        if _LOOK_Q.search(user_msg):
            return True
        if _LOOK_HINT.search(user_msg):
            return self._route_look(user_msg)
        return False

    # --- search router ---
    def _route_search(self, user_msg: str) -> dict:
        """{"search", "query", "why"}."""
        try:
            from reedus_companion import mode as _mode
            if not _mode.local_search_enabled():
                if DEBUG:
                    print("\n[debug] cloud mode -> search routing left to Gemini",
                          file=sys.stderr)
                return {"search": False, "query": "", "why": "cloud"}
        except Exception:
            pass
        # pure arithmetic never needs the router, and this has to come BEFORE it: "4+4" was
        # spending 5.5s asking for a verdict that couldn't change the answer
        if _is_pure_arithmetic_turn(user_msg):
            if DEBUG:
                print("\n[debug] arithmetic turn -> search router bypassed", file=sys.stderr)
            return {"search": False, "query": "", "why": "arithmetic"}
        if _NO_SEARCH.search(user_msg):
            if DEBUG:
                print("\n[debug] no-search veto -> not searching", file=sys.stderr)
            return {"search": False, "query": "", "why": "no-search-veto"}
        if _is_personal_turn(user_msg) or _is_checkin(user_msg):
            if DEBUG:
                print("\n[debug] personal turn -> search suppressed", file=sys.stderr)
            return {"search": False, "query": "", "why": "personal"}
        followup = bool(self.last_search_query and _is_search_followup(user_msg))
        if followup:
            subject = _carry_subject(self.last_search_query)
            if subject:
                built = f"{subject} {_followup_constraint(user_msg)}".strip()
                if DEBUG:
                    print(f"\n[debug] follow-up (last: {self.last_search_query!r}) -> built "
                          f"{built!r} without the router", file=sys.stderr)
                return {"search": True, "query": built, "why": "followup"}
        # cheap-turn gate reads the MESSAGE, but a message isn't a conversation: after a world cup
        # exchange "thats real spain was better" skipped the router and he invented a result. a
        # short reaction inside a factual thread still routes. pivots ("what about madara though")
        # are the exception, they want the corpus not the web.
        skip_reason = _router_skip_reason(user_msg)
        skip = skip_reason != RUN
        if skip and not (self._continues_factual_topic(user_msg) and not _PIVOT.search(user_msg)):
            if DEBUG:
                print(f"\n[debug] router skipped ({skip_reason}) -> not searching",
                      file=sys.stderr)
            return {"search": False, "query": "", "why": skip_reason}
        if skip and DEBUG:
            print("\n[debug] skip gate overridden (continues factual thread) -> running router",
                  file=sys.stderr)
        today = datetime.date.today().strftime("%A, %B %d, %Y")
        router_msgs = [{"role": "system", "content": ROUTER_SYSTEM + f"\nToday is {today}."}]
        router_msgs += self.history[-6:]
        router_msgs.append({"role": "user", "content": user_msg})
        raw = self.backend.chat(router_msgs, 120, use_adapter=False, temperature=0.0,
                                name="search-router", cancel=self._cancel)
        if self._check_cancel():
            return {"search": False, "query": "", "why": "cancelled"}
        obj = _parse_router_json(raw, call="search-router")
        if obj is not None and isinstance(obj.get("search"), bool):
            decision = {"search": obj["search"], "query": (obj.get("query") or "").strip(),
                        "why": "router-yes" if obj["search"] else "router-no"}
        else:
            # json parse failed, fall back to the keyword heuristic
            decision = {"search": _kw_wants_search(user_msg), "query": _kw_query(user_msg),
                        "why": "kw-fallback"}
        router_said_search = decision["search"]
        if followup and DEBUG:
            print(f"\n[debug] follow-up to a search turn, no carryable subject "
                  f"(last: {self.last_search_query!r})", file=sys.stderr)
        forced = _positive_search_cmd(user_msg) or _entity_question(user_msg) or followup
        if forced:
            decision["search"] = True
            decision["why"] = "forced-entity" if not router_said_search else "router-yes"
            if not router_said_search:
                decision["query"] = _kw_query(user_msg)
            if DEBUG and not router_said_search:
                print(f"\n[debug] forced search (verb/entity gate) -> {decision['query']!r}",
                      file=sys.stderr)
        if decision["search"] and not decision["query"]:
            decision["query"] = _kw_query(user_msg)

        q = decision.get("query") or ""
        subject = _carry_subject(self.last_search_query) if self.last_search_query else ""
        if (decision["search"] and subject and not router_said_search
                and not _entity_search(q) and len(q.split()) <= 8):
            # dates aren't subjects. "August" is capitalised so it got carried into the follow-up
            # query and ranked monthly forecast pages over current conditions. carry the place and
            # the topic, let the new message supply the time.
            decision["query"] = f"{subject} {q}".strip()
            if DEBUG:
                print(f"\n[debug] query had no subject -> carried "
                      f"{subject!r} from the last search", file=sys.stderr)
        if DEBUG:
            print(f"\n[debug] router -> {decision}", file=sys.stderr)
        return decision

    def _extract_fact(self, user_msg: str, results: str) -> str:
        """Pull one clean factual answer out of messy search results, or NOT FOUND."""
        if not results or results.lstrip().startswith("[web_search"):
            return "NOT FOUND"
        msgs = [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": f"Question: {user_msg}\n\nSearch results:\n{results}"},
        ]
        out = self.backend.chat(msgs, 160, use_adapter=False, temperature=0.0,
                                name="extract", cancel=self._cancel).strip()
        if self._check_cancel():
            return "NOT FOUND"
        if _extract_noncompliant(out):
            if DEBUG:
                print(f"\n[debug] extractor broke contract -> NOT FOUND\n  raw: {out[:180]!r}",
                      file=sys.stderr)
            return "NOT FOUND"
        return out

    # --- main ---
    def reply(self, user_msg: str) -> str:
        # snapshot the reply being objected to, free. classification happens AFTER the answer
        # (_close_correction); running it first was ~1.3k tokens of prefill on every turn.
        self._prev_reply = next((m["content"] for m in reversed(self.history)
                                 if m.get("role") == "assistant"), "")
        self._pending_fix = None
        self.cancelled = False

        # closed-form fast path: `2+2` was still 8.4s of persona generation. only expressions
        # this can prove locally; explanatory math falls through to the persona.
        arithmetic = _arithmetic_answer(user_msg)
        if arithmetic is not None:
            self._skip_corpus = True
            self._corpus_unsure = False
            self._knowledge_weak = False
            self._learned_topic = ""
            if self._question_cooldown > 0:
                self._question_cooldown -= 1
            self._asked_last_turn = self._question_cooldown > 0
            self.history += [{"role": "user", "content": user_msg},
                             {"role": "assistant", "content": arithmetic}]
            if DEBUG:
                print(f"\n[debug] arithmetic fast path -> {arithmetic!r}", file=sys.stderr)
            return arithmetic

        date_q = bool(_DATE_Q.search(user_msg))
        personal = _suppress_fact_machinery(user_msg)
        factual = _looks_factual(user_msg)

        looked = False
        look_failed = False
        vision_info = None
        if not date_q and self._wants_look_turn(user_msg):
            if self._check_cancel():
                return ""
            self._status(_ack(_LOOK_ACKS))          # speak before the work
            if self._check_cancel():
                return ""
            vb = self._vision_bridge()
            vision_info = vb.grounding_for(user_msg) if vb is not None else None
            looked = vision_info is not None
            look_failed = not looked
            if DEBUG:
                print(f"\n[debug] look turn -> {vision_info}"
                      f"{' (LOOK FAILED - camera unavailable)' if look_failed else ''}",
                      file=sys.stderr)
            if self._check_cancel():
                return ""

        if self._check_cancel():
            return ""

        learned = None
        if not (date_q or looked or look_failed):
            learned = self._maybe_learn_topic(user_msg)
        # subject hint for retrieval. the topic router just named it, asking a second model to
        # name it again was 2.2s. reset every turn so a stale topic can't steer a new subject.
        self._learned_topic = learned or ""
        if learned:
            self._update_session_subject(learned)

        # weather goes to open-meteo BEFORE the search router; router+web invented usa-wide queries
        weather_fact = None
        if not (date_q or looked or look_failed or learned):
            try:
                from reedus_companion.serve.weather import is_weather_ask, fetch_weather_fact
                if is_weather_ask(user_msg):
                    self._status(_ack(_SEARCH_ACKS))
                    weather_fact = fetch_weather_fact(user_msg)
                    if DEBUG:
                        print(f"\n[debug] weather API -> {weather_fact or '(miss)'}",
                              file=sys.stderr)
            except Exception as e:                              # noqa: BLE001
                if DEBUG:
                    print(f"\n[debug] weather short-circuit failed: {e!r}", file=sys.stderr)
                weather_fact = None

        decision = ({"search": False, "query": "", "why": "grounded-elsewhere"}
                    if (date_q or looked or look_failed or learned or weather_fact)
                    else self._route_search(user_msg))
        if self._check_cancel():
            return ""

        # live vs encyclopedia: the router's "game/show details -> YES" bias threw away the corpus
        # for "what about Madara" and paid ~5s for web that came back NOT FOUND. keep web for
        # weather/scores/news/prices/explicit lookups, demote the rest to the corpus.
        if decision["search"] and not _is_live_fact_search(user_msg, decision.get("query", "")):
            if DEBUG:
                print(f"\n[debug] router yes demoted to corpus "
                      f"(not a live-fact ask; query={decision.get('query', '')!r})",
                      file=sys.stderr)
            decision = {"search": False, "query": "", "why": "demoted-to-corpus"}

        # turn mode, decided here because _messages below triggers retrieval: live fact -> web
        # only (the corpus offered Woodbridge High School for a weather question), camera -> what
        # he sees, look fail -> nothing, personal -> conversation, otherwise corpus as normal.
        # separate name from `personal` on purpose: corpus-skip wants the narrow predicate, fact
        # suppression wants the broad one.
        conv_turn = _is_personal_turn(user_msg) or _is_checkin(user_msg)
        # three-valued: separates the turns we have a reason about from the ones we're guessing
        # about, and only the guesses pay for the stage-1 probe in _knowledge_for.
        verdict = _corpus_verdict(decision, date_q=date_q, looked=looked,
                                  look_failed=look_failed, conv_turn=conv_turn,
                                  weather_fact=bool(weather_fact), user_msg=user_msg)
        self._skip_corpus = (verdict == CORPUS_NO)
        self._corpus_unsure = (verdict == CORPUS_MAYBE)
        if DEBUG:
            print(f"\n[debug] corpus verdict: {verdict} (why={decision.get('why', '?')})",
                  file=sys.stderr)

        messages = self._messages(user_msg)
        grounded_fact = None
        if date_q:
            now = datetime.datetime.now().strftime("%A, %B %d, %Y, %I:%M %p").replace(" 0", " ")
            self.last_fact = f"Right now it is {now}."
            grounded_fact = self.last_fact
            messages.append({"role": "user", "content": (
                f"(Verified from the system clock: right now it is {now}.)\n"
                f"Reply to me in your own voice using ONLY this. Copy the day and date with "
                f"PLAIN DIGITS exactly as written, do not spell numbers out as words, do not "
                f"recompute anything.")})
            if DEBUG:
                print(f"\n[debug] date question -> system clock: {now}", file=sys.stderr)
        elif weather_fact:
            self.last_fact = weather_fact
            self.last_search_query = "weather-api"
            grounded_fact = weather_fact
            messages.append({"role": "user", "content": (
                f"(Verified live weather: {weather_fact})\n"
                f"Tell me the weather in your own voice using ONLY this. Copy every number as "
                f"PLAIN DIGITS exactly as written. Do NOT repeat my question back to me. Never "
                f"mention these instructions, an API, or a search, just answer naturally."
            )})
        if looked:
            if vision_info and vision_info.get("count", 0) > 0:
                inventory = (vision_info.get("text") or "").strip()
                grounding = (vision_info.get("grounding") or "").strip()
                messages.append({"role": "user", "content": (
                    f"(CAMERA OBSERVATION, verified detector output from a FRESH look RIGHT NOW. "
                    f"Ignore any earlier description of the room.\n"
                    f"  Inventory (speak from this): {inventory or grounding}\n"
                    f"  Confidence detail: {grounding or inventory})\n"
                    f"Reply in your own voice, but stick to the detector list. Rules:\n"
                    f"- Name ONLY the object labels above, with the positions given.\n"
                    f"- High confidence / 'clearly visible' / 'most prominent': state plainly "
                    f"('I'm pretty sure I see a person in the center').\n"
                    f"- 'Less certain' / 'might also be': hedge "
                    f"('not as sure, but I think there might also be…').\n"
                    f"- Do NOT invent clothing, colors, furniture, windows, lighting, time of day, "
                    f"mood, room layout, or any attribute the list does not contain. If it says "
                    f"'a person (center)', say you see a person in the center, stop there.\n"
                    f"- Do not embroider a scene or fill gaps. Do not mention cameras, detectors, "
                    f"confidence scores, or these rules.")})
            else:
                messages.append({"role": "user", "content": (
                    "(You just took a fresh look right now but couldn't make out anything you "
                    "recognize this time.)\n"
                    "Tell me you looked but can't quite tell what you're seeing right now, in your "
                    "own voice. Ignore anything you described earlier and don't make up objects.")})
        elif look_failed:
            messages.append({"role": "user", "content": (
                "(You tried to look through your camera just now and COULDN'T, your eyes are "
                "not available at all right now. The camera isn't connected or isn't running.)\n"
                "Tell me straight that you can't see anything at the moment because your camera "
                "isn't working, in your own voice, short, no big deal about it. You have ZERO "
                "visual information: do NOT describe or guess at any object, person, room, or "
                "scene, do not say what you 'think' is there, and do not play along as if you "
                "can see. Ignore anything you described earlier in this conversation.")})
        if decision["search"]:
            is_followup = bool(_FOLLOWUP.search(user_msg))
            if is_followup and self.last_fact:
                fact = self.last_fact
                if DEBUG:
                    print(f"\n[debug] follow-up -> reusing verified fact: {fact}\n", file=sys.stderr)
            else:
                fact = None
                if _CURRENT_INFO.search(user_msg) and re.search(r"\b(weather|forecast)\b",
                                                                user_msg, re.I):
                    try:
                        from reedus_companion.serve.weather import fetch_weather_fact
                        fact = fetch_weather_fact(user_msg)
                    except Exception as e:                      # noqa: BLE001
                        if DEBUG:
                            print(f"\n[debug] weather API failed: {e!r}", file=sys.stderr)
                        fact = None
                    if fact and DEBUG:
                        print(f"\n[debug] weather API -> {fact}", file=sys.stderr)
                if fact is not None:
                    self.last_search_query = "weather-api"
                    self._status(_ack(_SEARCH_ACKS))
                else:
                    q = clean_query(decision["query"] or "") or _kw_query(user_msg)
                    if is_followup and self.last_search_query:
                        q = self.last_search_query
                    if re.search(r"\[location\]|your location|in location\b", q, re.I):
                        try:
                            from reedus_companion.serve.weather import (
                                fetch_weather_fact, place_hint_from_message, resolve_place,
                            )
                            fact = fetch_weather_fact(user_msg)
                            if fact is None:
                                place = resolve_place(place_hint_from_message(user_msg))
                                if place:
                                    q = f"current weather {place[2]}"
                        except Exception:
                            pass
                    if fact is not None:
                        self.last_search_query = "weather-api"
                        self._status(_ack(_SEARCH_ACKS))
                    else:
                        q = _add_recency(q)
                        self.last_search_query = q
                        self._status(_ack(_SEARCH_ACKS))    # speak before the ~12s of search+extract
                        if self._check_cancel():
                            return ""
                        results = web_search(q, max_results=SEARCH_NARROW)
                        if self._check_cancel():
                            return ""
                        fact = self._extract_fact(user_msg, results)
                        if self._check_cancel():
                            return ""
                        if DEBUG:
                            print(f"\n[debug] web_search({q!r}, {SEARCH_NARROW} results) ->\n{results}\n",
                                  file=sys.stderr)
                            print(f"[debug] extracted fact -> {fact}\n", file=sys.stderr)
                        if _is_not_found(fact) or not _fact_is_speakable(fact):
                            if DEBUG:
                                print(f"[debug] narrow search missed -> widening to "
                                      f"{config.SEARCH_MAX_RESULTS} results", file=sys.stderr)
                            results = web_search(q, max_results=config.SEARCH_MAX_RESULTS)
                            if self._check_cancel():
                                return ""
                            fact = self._extract_fact(user_msg, results)
                            if self._check_cancel():
                                return ""
                            if DEBUG:
                                print(f"[debug] extracted fact (wide) -> {fact}\n", file=sys.stderr)
                        if (_is_not_found(fact) or not _fact_is_speakable(fact)) and PAGE_FETCH:
                            self._status("still digging, let me open the page")
                            if self._check_cancel():
                                return ""
                            if DEBUG:
                                print("[debug] snippets exhausted -> fetching page text",
                                      file=sys.stderr)
                            pages = web_search_pages(q, n_pages=PAGE_FETCH_N)
                            if self._check_cancel():
                                return ""
                            if pages:
                                fact = self._extract_fact(user_msg, pages)
                                if self._check_cancel():
                                    return ""
                                if DEBUG:
                                    print(f"[debug] extracted fact (page text, {len(pages)} chars) "
                                          f"-> {fact}\n", file=sys.stderr)
                            elif DEBUG:
                                print("[debug] page fetch returned nothing -> keeping the miss",
                                      file=sys.stderr)

            if self._check_cancel():
                return ""
            if _is_not_found(fact) or not _fact_is_speakable(fact or ""):
                if factual:
                    messages.append({"role": "user", "content": (
                        "(A web search didn't turn up a clear answer.) Tell me you couldn't find "
                        "it, in your voice. Do NOT state any number, date, name, or score from "
                        "memory, you have no verified answer, so there is nothing to report but "
                        "the miss.")})
                elif DEBUG:
                    print("\n[debug] NOT FOUND on non-factual turn -> no miss narration",
                          file=sys.stderr)
            else:
                fact = _strip_notfound_hedge(fact)
                self.last_fact = fact
                grounded_fact = fact
                messages.append({"role": "user", "content": _grounding_msg(user_msg, fact)})

        gen_temp = 0.3 if (decision["search"] or date_q or looked or look_failed
                           or weather_fact) else None

        if not (decision["search"] or date_q or looked or look_failed or weather_fact):
            nudge = self._engagement_nudge()
            if nudge:
                messages.append({"role": "user", "content": nudge})
                if DEBUG:
                    print("\n[debug] engagement nudge injected", file=sys.stderr)

        lnudge = self._length_nudge()
        if lnudge:
            messages.append({"role": "user", "content": lnudge})
            if DEBUG:
                print("\n[debug] length nudge injected (recent replies ran long)",
                      file=sys.stderr)

        may_be_replaced = (VERIFY_DRAFTS and not decision["search"] and not date_q
                           and not personal and not looked and not look_failed
                           and not weather_fact)

        # streaming: what matters for speech is whether the words can change after they leave
        # his mouth, and only the digit guard (verified fact in play) can regenerate. withhold
        # streaming there, stream everywhere else; on_replaced lets the caller correct out loud.
        will_ground = bool(decision["search"] or date_q or weather_fact)
        stream_cb = self.on_token if (self.on_token and not will_ground) else None
        cancel = self._cancel
        if self._check_cancel():
            return ""
        if stream_cb is not None:
            _sink, _filt = stream_cb, toolmod.StreamFilter()

            def stream_cb(piece: str, _s=_sink, _f=_filt) -> None:   # noqa: F811
                out = _f.feed(piece)
                if out:
                    _s(out)

        raw = ""
        for _ in range(MAX_TOOL_HOPS):
            raw = self.backend.chat(messages, REPLY_MAX_TOKENS, temperature=gen_temp,
                                    on_token=stream_cb, name="persona", cancel=cancel)
            if cancel is not None and cancel.is_set():
                self.cancelled = True
                if DEBUG:
                    print("\n[debug] persona generation cancelled (barge-in)", file=sys.stderr)
                return ""
            calls = toolmod.parse_tool_calls(raw)
            if not calls:
                break
            # model-driven tools, mainly `remember`
            messages.append({"role": "assistant", "content": raw})
            for call in calls:
                result = self.tools.run(call["name"], call.get("arguments", {}))
                messages.append({"role": "user",
                                 "content": f"[tool result, {call['name']}]\n{result}\n"
                                            f"Now answer me directly using this, in one message."})

        global _cap_subjects, _cap_context
        _cap_subjects = []
        _cap_context = " ".join(
            m["content"] for m in self.history[-8:]
            if m.get("role") == "user" and m.get("content")).lower() + " " + user_msg.lower()
        try:
            ix = _entity_index()
            if ix is not None:
                from reedus_companion.knowledge.entity_index import subjects_in
                recent = " ".join(m["content"] for m in self.history[-4:] if m.get("content"))
                _cap_subjects = subjects_in(f"{recent} {user_msg}", ix)
        except Exception:                                        # noqa: BLE001
            _cap_subjects = []

        answer = _clean_reply(_drop_partial_tail(toolmod.strip_tool_calls(raw)),
                              personal=personal)
        if getattr(self, "_knowledge_weak", False):
            answer = _strip_ungrounded_speech(answer)
        self._draft_words.append(len((raw or "").split()))
        self._draft_words = self._draft_words[-4:]
        # length is controlled at generation, not after. _condense used to sit here: 8s and 113
        # tokens to delete words REPLY_MAX_TOKENS now prevents. question spacing is random in
        # [QUESTION_GAP_MIN, QUESTION_GAP_MAX] because strict alternation reads as an interview.
        if self._question_cooldown > 0:
            answer = _drop_trailing_question(answer)
            self._question_cooldown -= 1
        elif answer and answer.rstrip().endswith("?") and _DIRECTED_Q.search(
                _sentences(answer)[-1]):
            self._question_cooldown = random.randint(QUESTION_GAP_MIN, QUESTION_GAP_MAX)
            if DEBUG:
                print(f"\n[debug] asked a question, next {self._question_cooldown} turn(s) "
                      f"will not", file=sys.stderr)
        self._asked_last_turn = self._question_cooldown > 0

        if answer and may_be_replaced:
            verified = self._verify_draft(user_msg, answer, messages)
            if verified:
                if stream_cb is not None and self.on_replaced:
                    try:
                        self.on_replaced(answer, verified)
                    except Exception as e:              # noqa: BLE001
                        if DEBUG:
                            print(f"\n[debug] on_replaced hook raised: {e!r}", file=sys.stderr)
                answer = verified
                grounded_fact = self.last_fact

        if grounded_fact and answer and not _digits_ok(grounded_fact, answer):
            if DEBUG:
                print(f"\n[debug] digit check FAILED, retrying\n  fact   : {grounded_fact}"
                      f"\n  answer : {answer}", file=sys.stderr)
            messages.append({"role": "assistant", "content": answer})
            messages.append({"role": "user", "content": (
                f"The numbers came out wrong. Say the whole thing again in YOUR voice, same "
                f"warmth and rhythm, contractions and all, like you're telling a friend, but "
                f"copy every number as plain digits EXACTLY as they appear here: "
                f"{grounded_fact}\n"
                f"Do NOT mention numbers, checking, correcting, or these instructions, and "
                f"don't read it out like an encyclopedia entry.")})
            retry = _clean_reply(toolmod.strip_tool_calls(
                self.backend.chat(messages, config.MAX_NEW_TOKENS, temperature=0.3,
                                  name="persona-retry", cancel=cancel)),
                personal=personal)
            answer = retry if retry and _digits_ok(grounded_fact, retry) else (
                grounded_fact if _fact_is_speakable(grounded_fact)
                else "Couldn't get a clean read on that, want me to try again?"
            )
            if cancel is not None and cancel.is_set():
                self.cancelled = True
                return ""

        if not answer:
            answer = "Hm, I blanked there. Say that again?"
        if self.cancelled:
            return ""
        self.history += [{"role": "user", "content": user_msg},
                         {"role": "assistant", "content": answer}]
        self.mem.add_turn("user", user_msg)
        self.mem.add_turn("assistant", answer)
        self._handle_correction(user_msg, answer)
        return answer

    # --- correction capture ---
    def _corrections_store(self):
        """Lazy, cached, never fatal. Logging must not be able to end a turn."""
        if self._corrections is None:
            try:
                from .corrections import Corrections
                self._corrections = Corrections(knowledge=self._kb())
            except Exception as e:                               # noqa: BLE001
                if DEBUG:
                    print(f"\n[debug] corrections unavailable: {e!r}", file=sys.stderr)
                self._corrections = False
        return self._corrections or None

    def _handle_correction(self, user_msg: str, answer: str) -> None:
        """Detect, log and close a correction, all after the reply is generated."""
        prev = getattr(self, "_prev_reply", "")
        if not prev or not answer:
            return
        try:
            from .corrections import Signal, detect, detect_contradiction, detect_llm
            signal = detect(user_msg)
            via = "pattern"
            if signal is None:
                signal = detect_contradiction(user_msg, prev)
                via = "contradiction"
            # off by default: the semantic classifier is ~1.3k tokens of prefill, a 5-6s stall after
            # the reply lands to decide whether to write a log row. the regex fast path catches
            # blunt corrections free. REEDUS_CORRECTION_LLM=1 for a data-gathering session.
            if signal is None and os.environ.get("REEDUS_CORRECTION_LLM", "0") == "1":
                signal = detect_llm(
                    user_msg, prev,
                    lambda m, n: self.backend.chat(m, n, use_adapter=False, temperature=0.0,
                                                   name="correction"))
                via = "llm"
            if signal is None:
                return

            store = self._corrections_store()
            if store is None:
                return
            context = "\n".join(f"{m['role']}: {m['content']}" for m in self.history[-8:-2])
            row = store.log(signal, context, prev, user_msg)
            if DEBUG:
                print(f"\n[debug] correction captured ({signal}, via {via}) row={row}",
                      file=sys.stderr)

            if signal == Signal.WRONG:
                store.resolve(row, user_msg)
                try:
                    from ..knowledge.entity_index import EntityIndex
                    from ..scripts.eval_grounding import proper_nouns
                    ix = EntityIndex()
                    names = proper_nouns(prev)
                    for p in [n for n in names if ix.is_person(n)]:
                        bad = ix.misattributions(p, [n for n in names if n != p])
                        if bad:
                            store.note_misattribution(row, p, bad[0])
                            break
                except Exception:                                # noqa: BLE001
                    pass

            ok, reason = store.set_corrected(row, answer, context)
            if DEBUG:
                print(f"[debug] correction fix {'ACCEPTED' if ok else 'REJECTED'} ({reason})",
                      file=sys.stderr)
        except Exception as e:                                   # noqa: BLE001
            if DEBUG:
                print(f"\n[debug] correction handling failed: {e!r}", file=sys.stderr)

    # capitalised multi-word runs, candidate proper nouns in a draft. two+ tokens because one
    # capitalised word is a sentence start. only ever a verification trigger, so over-matching
    # costs a web search rather than deleted text.
    _NAME_RUN = re.compile(
        r"\b([A-Z][\w\u00C0-\u024F'\u2019\-]+(?:\s+[A-Z][\w\u00C0-\u024F'\u2019\-]+){1,4})\b")

    def _unknown_names(self, draft: str) -> List[str]:
        """Capitalised name-runs in the draft the corpus has never heard of."""
        kb = self._kb()
        if kb is None:
            return []
        out, seen = [], set()
        for m in self._NAME_RUN.finditer(draft or ""):
            name = m.group(1).strip()
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                if not _known_topic(kb.store, name):
                    out.append(name)
            except Exception:
                return []
        return out

    @staticmethod
    def _drop_unverified(draft: str) -> str:
        """Remove the sentences that assert checkable specifics, keep the conversation."""
        if not draft:
            return draft
        kept = []
        for sent in _sentences(draft):
            s_ = sent.strip()
            if not s_:
                continue
            checkable = bool(
                re.search(r"\d", s_)
                or _RISKY_CLAIM.search(s_)
                or (_entity_search(s_) and _OUTCOME_CLAIM.search(s_))
            )
            if not checkable:
                kept.append(sent)
        out = "".join(kept).strip()
        return out or ""

    def _verify_draft(self, user_msg: str, draft: str, messages) -> Optional[str]:
        """Check an unsearched draft for checkable claims; if found, search and regenerate grounded."""
        today = datetime.date.today().strftime("%A, %B %d, %Y")
        # blank out agreement percentages first so "oh 100%" can't buy a 9s verification
        risky = _RISKY_CLAIM.search(_AGREEMENT_PCT.sub(" ", draft))
        if not risky:
            claims = _declarative_only(draft)
            ent = _entity_search(claims)
            if ent and _OUTCOME_CLAIM.search(claims):
                risky = ent
            else:
                unknown = self._unknown_names(claims)
                if len(unknown) >= MIN_UNKNOWN_NAMES:
                    if DEBUG:
                        print(f"\n[debug] {len(unknown)} name(s) not in the corpus -> "
                              f"verifying: {', '.join(unknown[:5])}", file=sys.stderr)
                    risky = re.search(re.escape(unknown[0]), claims) or ent
        if risky:
            check_msgs = [
                {"role": "system", "content": FORCED_QUERY_SYSTEM + f"\nToday is {today}."},
                {"role": "user", "content": f"User asked: {user_msg}\n\nDRAFT reply: {draft}\n\n"
                                            f"(flagged text: {risky.group(0)!r})"},
            ]
            # this decides the query that verifies a number-shaped claim. if verification quality
            # drops with the router split, add "forced-query" to REEDUS_ROUTER_KEEP_ON_MAIN first.
            raw = self.backend.chat(check_msgs, 80, use_adapter=False, temperature=0.0,
                                    name="forced-query", cancel=self._cancel)
            if self._check_cancel():
                return None
            query = (raw or "").strip().strip('"')
            if DEBUG:
                print(f"\n[debug] draft-verify FORCED by {risky.group(0)!r} -> query {query!r}",
                      file=sys.stderr)
            if not query or query.upper().startswith("SKIP"):
                query = user_msg if "?" in user_msg or len(user_msg.split()) > 2 else ""
            if not query:
                return None
        else:
            # no number-shaped claim: only spend a model call if the draft asserts something
            # checkable or the question reached for a fact. that's ~25% of a warm turn reclaimed.
            if not _draft_needs_llm_check(draft, user_msg):
                if DEBUG:
                    print("\n[debug] draft-check skipped (nothing checkable in draft)",
                          file=sys.stderr)
                return None
            check_msgs = [
                {"role": "system", "content": DRAFT_CHECK_SYSTEM + f"\nToday is {today}."},
                {"role": "user", "content": f"User asked: {user_msg}\n\nDRAFT reply: {draft}"},
            ]
            raw = self.backend.chat(check_msgs, 120, use_adapter=False, temperature=0.0,
                                    name="draft-check", cancel=self._cancel)
            if self._check_cancel():
                return None
            required = ("verify" if os.environ.get("REEDUS_DRAFT_CHECK_FIX") == "1"
                        else "search")
            obj = _parse_router_json(raw, required=required, call="draft-check")
            if DEBUG:
                print(f"\n[debug] draft-check -> {raw.strip()[:160]!r}", file=sys.stderr)
            if not obj or not obj.get("verify"):
                return None
            query = (obj.get("query") or "").strip() or user_msg
        q = _add_recency(clean_query(query) or query)
        if self._check_cancel():
            return None
        results = web_search(q)
        if self._check_cancel():
            return None
        fact = self._extract_fact(user_msg, results)
        if self._check_cancel():
            return None
        if DEBUG:
            print(f"\n[debug] draft-verify -> web_search({q!r})\n[debug] extracted fact -> {fact}\n",
                  file=sys.stderr)
        if _is_not_found(fact):
            if risky:
                trimmed = self._drop_unverified(draft)
                if trimmed and trimmed != draft.strip() and len(trimmed.split()) >= 4:
                    if DEBUG:
                        print(f"[debug] unverifiable claim dropped ({risky.group(0)!r} could "
                              f"not be confirmed) -> {trimmed[:90]!r}", file=sys.stderr)
                    return _clean_reply(trimmed)
            return None                      # can't verify, keep the draft
        fact = _strip_notfound_hedge(fact)
        self.last_search_query, self.last_fact = q, fact
        messages.append({"role": "user", "content": _grounding_msg(user_msg, fact)})
        redo = self.backend.chat(messages, config.MAX_NEW_TOKENS, temperature=0.3,
                                 name="persona-redo", cancel=self._cancel)
        if self._check_cancel():
            return None
        redo = _clean_reply(toolmod.strip_tool_calls(redo))
        return redo or None

    def close(self):
        if self._vision:
            try:
                self._vision.close()
            except Exception:
                pass
        self.mem.close()
