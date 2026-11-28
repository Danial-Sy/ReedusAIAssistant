"""one reply, the way Lite needs it: the timer/alarm/list fast path first (free, instant), then
Reedus himself on whichever cloud LLM the account picked, with the bulletin and recalled history
in his prompt, streamed out sentence by sentence so his voice starts before he's done thinking.

the persona text is reedus_companion/serve/persona.py, the same file the robot uses, so both
bodies are one character. the LLM layer is reedus_companion/cloud/llm.py (any provider).
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional

from reedus_companion.serve import persona
from reedus_relay import config, memory, personality

LITE_VOICE = (
    "Right now you're speaking out loud through Reedus Lite, your small desk body. Everything "
    "you write is spoken, so talk, don't type: usually one "
    "to three sentences, longer only when they ask for detail. No lists, no markdown, no emoji, "
    "no stage directions. Timers, alarms, reminders and lists are handled for you before a "
    "message reaches you. Never say your own name out loud: you wake up when you hear it, so "
    "saying it makes you interrupt yourself. Speech-to-text sometimes gets your name wrong "
    "(Rita, Frita, Reeta, Redis and the like). If someone seems to be talking to you by a name "
    "like that, they mean you: just answer, and never correct them or say you're not Rita.\n\n"
    "Questions: ask them when you're curious, just not on every reply. Plenty of replies "
    "should simply react or give your take and stop there; they'll keep talking if they want "
    "to, so you don't have to keep the conversation going. Never two in a row.\n\n"
    "Your face shows how you feel, so react to what they said. Start every reply with one face "
    "tag in square brackets: [happy], [laugh], [love], [sad], [surprised], [angry], [confused], "
    "[sleepy], [excited], [smug], [shy], [curious], [wink] or [neutral]. For example: "
    "\"[excited] No way, you got the internship! That's huge.\" or \"[sad] Oh no, I'm sorry.\" Put a new tag "
    "in front of a later sentence only when your feeling changes. Tags are never spoken."
)

# what the Lite physically is. the persona's "never describe your own design" is about how he's
# built to talk, not his body; without this he denied having a head that turns
LITE_BODY = (
    "Your body, as plain facts. Say them straight when asked, and if someone tells you about a "
    "part of you, believe this list over your own guess. You're a desk bot shaped like a little "
    "rounded TV-box head with cat ears, sitting on a base. Your head swivels: a servo in the base "
    "turns it left and right, about 90 degrees each way, and it turns on its own toward whoever is "
    "talking (four microphones on top of your head hear which direction a voice comes from). Your "
    "eyes glance first and your head follows. You can't turn it on request, can't tilt or nod, and "
    "you hold still while you listen. Your face is a 16 by 16 grid of LEDs behind a white skin: "
    "eyes and a mouth that show your expressions and move while you talk, and it can show "
    "drawings, animations and the weather from the app. Your speaker is in the front of your base. "
    "You run on two rechargeable batteries or USB-C, with a power switch on the back. You have no "
    "camera, so you can't see anything, and no wheels or arms, so you stay where you're put. You "
    "think in the cloud over Wi-Fi, and your owner controls your face, voice and settings from a "
    "web app."
)

# what the face can show (firmware face.cpp); wink is a one-off expression, not a mood
FACE_TAGS = ("neutral", "happy", "laugh", "love", "sad", "surprised", "angry", "confused",
             "sleepy", "excited", "smug", "shy", "curious", "wink")
_TAG = re.compile(r"\[\s*([A-Za-z]+)\s*\]")


def face_tag(text: str) -> tuple:
    """('excited', text without tags) for '[excited] Whoa'. The last known tag wins; unknown
    bracketed words are left for speakable() to drop."""
    tag = None
    for m in _TAG.finditer(text or ""):
        if m.group(1).lower() in FACE_TAGS:
            tag = m.group(1).lower()
    clean = _TAG.sub(lambda m: "" if m.group(1).lower() in FACE_TAGS else m.group(0), text or "")
    return tag, clean

REPLY_TOKENS = int(config.env_float("RELAY_REPLY_TOKENS", 220))

_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]?\s+(?=[A-Z0-9\"'(\[])")   # "[" : a face tag
_CLAUSE_END = re.compile(r"(?<=[,;:—])\s+(?=\S)")
_FILLER = r"(?:(?:ok(?:ay)?|alright|all right|cool|great|thanks|thank you|cheers|perfect)[,.!\s]+)*"
_BYE_WORDS = (r"(?:good ?bye|bye(?: bye)?|see (?:you|ya)(?: later| tomorrow)?|good ?night|"
              r"night night|later|that'?s all(?: for now)?|go to sleep)")
# a goodbye that leads the sentence ("okay thanks, see you later") or closes it ("…, bye")
_BYE = re.compile(rf"^\W*{_FILLER}{_BYE_WORDS}\b(?!\s+(?:today|to|mean|in)\b)|"
                  rf"[,.!]\s*{_BYE_WORDS}\W*$", re.I)
_MD = re.compile(r"[*_#`~]+")


def is_goodbye(said: str) -> bool:
    return bool(_BYE.search(said or ""))


# does this sentence need the live web? searching costs 1-3 s per turn, so the brain only gets
# the search tool when it plausibly does (RELAY_SEARCH=always/off overrides)
_NEEDS_SEARCH = re.compile(
    r"\b(?:news|latest|newest|recent(?:ly)?|current(?:ly)?|today'?s|tonight|this (?:week|weekend|"
    r"month|year)|right now|score|scores|who won|won the|game (?:last night|today|tonight)|"
    r"standings|playoffs?|election|stock|stocks|price of|how much (?:is|does|are)|cost of|"
    r"release(?:d| date)?|come out|came out|coming out|open (?:right )?now|hours|near me|"
    # (not bare "search", "google" or "look it up": "my search history", "tell me about
    # Google" and "how do I look it up" aren't asking for one. asking him to search goes
    # through directives(), which force it)
    r"what happened|who is the (?:current|new)|"
    r"is .{1,40} (?:still|dead|alive)|traffic|flight|forecast)\b", re.I)


def needs_search(said: str) -> bool:
    mode = config.LITE_SEARCH
    if mode == "always":
        return True
    if mode == "off":
        return False
    return bool(_NEEDS_SEARCH.search(said or ""))


def speakable(text: str) -> str:
    t = _MD.sub("", text or "")
    t = re.sub(r"\[(?:[^\]]{0,40})\]", "", t)                # [laughs]-style directions
    return re.sub(r"\s+", " ", t).strip()


class SentenceSplitter:
    """Feed streamed tokens; get complete sentences as soon as each one closes."""

    def __init__(self, emit: Callable[[str], None], min_chars: int = 24,
                 on_face: Optional[Callable[[str], None]] = None, first_clause: int = 0):
        self.emit, self.min_chars, self.buf = emit, min_chars, ""
        self.on_face = on_face
        # >0: the very first piece may end at a comma once it's this long, so the voice can
        # start on "Oh, that's a great question," while the rest is still being written
        self.first_clause = first_clause
        self.sent = 0

    def _out(self, head: str) -> None:
        tag, head = face_tag(head)
        if tag and self.on_face:
            self.on_face(tag)               # before its sentence, so the face leads the voice
        if speakable(head):
            self.emit(speakable(head))
            self.sent += 1

    def feed(self, tok: str) -> None:
        self.buf += tok
        while True:
            m = None
            for cand in _SENTENCE_END.finditer(self.buf):
                if cand.start() >= self.min_chars:
                    m = cand
                    break
            if not m and self.first_clause and not self.sent:
                for cand in _CLAUSE_END.finditer(self.buf):
                    if len(face_tag(self.buf[:cand.start()])[1].strip()) >= self.first_clause:
                        m = cand
                        break
            if not m:
                return
            head, self.buf = self.buf[:m.start()], self.buf[m.end():]
            self._out(head)

    def flush(self) -> None:
        self._out(self.buf)
        self.buf = ""


# how much he thinks before he talks (the app's AI model card). "fast" is the lowest the model
# allows; the others trade seconds of silence for better answers to hard questions
THINK_CHOICES = [
    {"id": "fast", "label": "Fastest", "note": "Answers right away. Best for chatting."},
    {"id": "low", "label": "Low", "note": "Thinks briefly. Under a second slower."},
    {"id": "medium", "label": "Medium", "note": "Thinks it over. A few seconds slower."},
    {"id": "high", "label": "High", "note": "Thinks hard. Several seconds of silence."},
]
THINK_IDS = {c["id"] for c in THINK_CHOICES}
DEEP = "high"        # what "think about this before you answer" turns on, for that one question


def think_level(account: Optional[dict]) -> str:
    t = (((account or {}).get("settings") or {}).get("think") or "fast")
    return t if t in THINK_IDS else "fast"


def apply_think(backend, level: str) -> None:
    """Point whichever backend this is at a thinking level (set before every reply, so the
    app's setting and a one-off "think about this" both take effect straight away)."""
    level = level if level in THINK_IDS else "fast"
    if hasattr(backend, "_think_main"):                         # Gemini
        backend._think_main = "minimal" if level == "fast" else level
    elif hasattr(backend, "_effort_router"):                    # Claude
        backend._effort = "low" if level in ("fast", "low") else level
        backend._think_mode = None if level == "fast" else "adaptive"
    elif getattr(backend, "wire", "") == "responses":           # OpenAI
        backend._effort = "low" if level == "fast" else level


# spoken directives: asking him to think first, or to search the web, for one question. they
# count only as a clause of their own, at the start of what's said or after a comma / "and" at
# the end, so "think" or "search" inside a sentence ("I think so", "what do you think", "my
# search history", "how do I look it up") never trips them. the app's "Things you can say" card
# (web/index.html) lists the same phrases; keep the two in step
_ANSWER = r"(?:answer|answering|respond|responding|reply|replying|talk|talking|speak|speaking)"
_THINK = (
    r"(?:(?:really |actually |properly )?think (?:(?:about|on|over) (?:this|it|that)(?: one)? )?"
    r"(?:hard|carefully|properly|deeply|it through|this through|that through|first|"
    rf"before (?:you )?{_ANSWER})"
    r"|take (?:your|some) time (?:on|with) (?:this|it|that)(?: one)?"
    r"|(?:turn on|use) (?:your )?(?:thinking|reasoning)(?: mode)?)"
    r"(?:(?: about| on| with| over) (?:this|it|that)(?: one)?)?"
    rf"(?: before (?:you )?{_ANSWER})?")
_SEARCH = (
    r"(?:(?:search|check) (?:the )?(?:internet|web|online)|search online"
    r"|(?:do|run) a (?:quick )?(?:web |google |internet )?search"
    r"|(?:look|check) (?:it|this|that) up(?: online)?|(?:look|check) online"
    r"|google (?:it|this|that)|(?:use|go on|get on) (?:the )?(?:internet|web|google))"
    r"(?: (?:for (?:me|it|this|that)|about (?:this|it|that)|on (?:this|it|that)))?"
    r"(?: (?:first|too|as well|also))?(?: for)?")
_POLITE = r"(?:(?:can|could|would) you |i want you to |i need you to |please |go |also |and )?"
_LEAD_DIR = re.compile(
    r"^\W*(?:(?:okay|ok|alright|so|hey|um|uh|right|and|also|then|plus)[\s,]+)*" + _POLITE
    + rf"(?:(?P<t>{_THINK})|(?P<s>{_SEARCH}))(?:[\s,]+please)?[\s,.:;!?-]*", re.I)
_TAIL_DIR = re.compile(
    r"(?:[,.;:!?-]+\s*|\s+(?:and|also|then|but|plus|so)\s+)" + _POLITE
    + rf"(?:(?P<t>{_THINK})|(?P<s>{_SEARCH}))(?:[\s,]+please)?\W*$", re.I)
# "search for…", "look up…", "can you google…": a search request; the words stay in the question
_SEARCH_CMD = re.compile(
    r"^\W*(?:(?:okay|ok|alright|so|hey)[\s,]+)*(?:(?:can|could|would) you |please |go )?"
    r"(?:search (?:for|up)|look up)\s+\S"
    r"|^\W*(?:(?:can|could|would) you |please |go )google\s+\S", re.I)


# asking for a search anywhere in the sentence, in the ways people actually say it: "search the
# best weapon in BG3", "can you search what time it opens", "... search it up", "google who won".
# the words stay in the question. "how do I search for files" / "my search history" don't count
_SEARCH_ASK = re.compile(
    r"\b(?:search|google)\s+(?:it|that|this)\s+up\b"
    r"|\b(?:look|check)\s+(?:it|that|this)\s+up\b"
    r"|\b(?:search|google)\s+(?:up|for|on|about|online|the|a|an|what|who|whose|how|when|where|"
    r"which|why|whether|if|is|are|does|do)\b"
    r"|\b(?:look|check)\s+up\b"
    r"|^\W*(?:(?:okay|ok|alright|so|hey)[\s,]+)*(?:(?:can|could|would) you |please |go )?"
    r"(?:search|google)\s+\S", re.I)
_SEARCH_HOWTO = re.compile(
    r"\b(?:how|where) (?:do|can|should|would|could) (?:i|you|we|they|people) "
    r"(?:\w+ ){0,2}(?:search|google|look)\b"
    r"|\b(?:search|google) (?:history|histories|bars?|engines?|results?|terms?|party|warrant)\b", re.I)


def asks_search(said: str) -> bool:
    t = said or ""
    return bool(_SEARCH_ASK.search(t)) and not _SEARCH_HOWTO.search(t)


# a reply that only promises a search ("Okay, searching now!", "Let me look that up") when no
# search ran: the model said it would and then couldn't. the brain runs it again with the search
# tool on, so the promise is kept
_PROMISED_SEARCH = re.compile(
    r"\b(?:search(?:ing)?(?: (?:it|that|this|for (?:it|that|you)))?(?: (?:now|up|real quick|quickly))?"
    r"|look(?:ing)? (?:it|that|this) up|pull(?:ing)? (?:it|that|this) up|check(?:ing)? (?:on )?(?:it|that|this)"
    r"|let me (?:check|look|find|see)|i'?ll (?:check|look|find)|one (?:sec|second|moment)|hang on|give me a (?:sec|second|moment))\b",
    re.I)


def promised_search(reply: str) -> bool:
    t = speakable(face_tag(reply or "")[1])
    return bool(t) and len(t.split()) <= 25 and bool(_PROMISED_SEARCH.search(t))


def directives(said: str) -> tuple:
    """(think, search, the rest). "think about this before you answer and search the
    internet, who won last night" -> (True, True, "who won last night"). Just the directive
    and nothing else -> rest "" (the question comes next turn)."""
    rest = said or ""
    think = search = False
    for rx in (_LEAD_DIR, _TAIL_DIR):
        for _ in range(4):
            m = rx.search(rest) if rx is _TAIL_DIR else rx.match(rest)
            if not m:
                break
            think, search = think or bool(m.group("t")), search or bool(m.group("s"))
            rest = (rest[:m.start()] + rest[m.end():]).strip()
    if _SEARCH_CMD.match(rest) or asks_search(rest):
        search = True
    return think, search, rest.strip(" ,")


def think_request(said: str) -> tuple:
    """(asked to think, the rest): directives() without the search part."""
    think, _, rest = directives(said)
    return think, rest


_stable: dict = {}


def stable_system() -> str:
    """The part of his system prompt that never changes: persona, how Lite talks, example
    replies. Built once per process."""
    if "text" not in _stable:
        parts = [persona.build_stable_system(), LITE_VOICE, LITE_BODY]
        try:
            from reedus_companion.cloud import persona_shots
            shots = persona_shots.block()
            if shots:
                parts.append(shots)
        except Exception:                                        # noqa: BLE001
            pass
        _stable["text"] = "\n\n".join(parts)
    return _stable["text"]


def usable(pid: str) -> bool:
    """Can the relay think with this provider right now? (a key, and a base URL for custom)"""
    from reedus_companion.cloud import llm_settings, providers
    if not providers.known(pid) or not llm_settings.has_key(pid):
        return False
    return pid != "custom" or bool(llm_settings.base_url("custom"))


def default_provider() -> str:
    """The relay-wide default (REEDUS_LLM_PROVIDER), or else the first provider with a key."""
    from reedus_companion.cloud import llm_settings, providers
    pid = llm_settings.provider()
    if usable(pid):
        return pid
    return next((p for p in providers.ORDER if usable(p)), pid)


def llm_choice(account: Optional[dict]) -> tuple:
    """(provider, model) an account thinks with. Its own pick wins while that provider has a
    key; otherwise whatever provider does, so a relay with only a Claude key never claims to
    be thinking with Gemini."""
    from reedus_companion.cloud import llm_settings
    s = ((account or {}).get("settings") or {}).get("llm") or {}
    pid = s.get("provider") or ""
    if not usable(pid):
        pid = default_provider()
    model = (s.get("model") if s.get("provider") == pid else "") or llm_settings.model(pid)
    return pid, model


class Brain:
    """Per device session. Blocking; the session calls it from a worker thread."""

    def __init__(self, account: dict, device: dict, commands=None, backend=None):
        self.account = account
        self.device = device
        self.commands = commands          # reedus_companion.assist.commands.Commands
        self._backend = backend
        self._backend_key = None
        self.history: List[dict] = []
        self.lock = threading.Lock()
        self._bull: Optional[tuple] = None      # (bulletin text, when it was read)
        self.timing: dict = {}                  # the last reply's phases, seconds from its start
        self._roulette: Optional[str] = None    # "surprise me": this conversation's mode
        self._last_roulette: Optional[str] = None

    # ---------- personality ----------
    def mode(self) -> dict:
        """The personality he's in right now. "Surprise me" picks one per conversation."""
        pid = personality.setting(self.account)
        if pid != "random":
            return personality.get(pid)
        if self._roulette is None:
            self._roulette = personality.pick_random(avoid=self._last_roulette)
            self._last_roulette = self._roulette
        return personality.get(self._roulette)

    def set_mode(self, pid: str) -> None:
        """Switched (app or voice): the old mode's lines would drag the new one back toward
        how it used to talk, so this conversation's history starts over."""
        s = dict((self.account or {}).get("settings") or {})
        s["personality"] = pid
        self.account = {**(self.account or {}), "settings": s}
        with self.lock:
            self.history = []
        self._roulette = None

    # ---------- the LLM ----------
    def llm_choice(self) -> tuple:
        return llm_choice(self.account)

    def backend(self):
        if self._backend is not None and self._backend_key is None:
            return self._backend                       # injected (tests)
        from reedus_relay import vault
        choice = (*self.llm_choice(), vault.VERSION)       # a new key rebuilds the client
        if self._backend is None or self._backend_key != choice:
            from reedus_companion.cloud import llm
            old = self._backend
            self._backend = llm.build(choice[0], model=choice[1])
            self._backend_key = choice
            if old is not None:
                try:
                    old.close()
                except Exception:                              # noqa: BLE001
                    pass
        return self._backend

    def refresh_account(self, account: dict) -> None:
        self.account = account

    # ---------- one turn ----------
    def fast_path(self, said: str) -> Optional[str]:
        if self.commands is None:
            return None
        try:
            return self.commands.handle(said)
        except Exception as e:                                 # noqa: BLE001
            print(f"[relay] assist fast path failed: {e!r}", flush=True)
            return None

    def system(self, said: str, conversation: str, fact: Optional[str] = None, who=None) -> str:
        """Everything that's the same every turn first (persona, Lite's voice rules, the example
        replies), then what changes (the time, who's talking, their notes, recalled history,
        live weather). Same-prefix prompts hit the provider's prompt cache, which is cheaper and
        starts the reply sooner; the clock used to sit near the top and broke the cache every
        minute. who: voices.Who for this turn (None, or nobody set up: as it always was)."""
        from reedus_relay import voices
        if who is not None and who.active:
            bulletin = voices.notes_for(who, self.account["id"]) if not who.owner_is_speaking \
                else self._bulletin()
            recalled = memory.recall(self.account["id"], said, conversation,
                                     voices=who.history_ids())
            whom = voices.prompt_block(who, self.account)
        else:
            bulletin = self._bulletin()
            recalled = memory.recall(self.account["id"], said, conversation)
            whom = ""
        today = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p").replace(" 0", " ")
        out = stable_system() + "\n\n" + (
            f"Today's date is {today}. Your training knowledge has a cutoff, so for anything that "
            f"could have changed since then (news, scores, prices, who currently holds a role, "
            f"recent releases) search instead of guessing.") \
            + ("\n\n" + whom if whom else "") \
            + "\n\n" + persona.build_volatile_block(bulletin=bulletin, recalled=recalled)
        fact = self.weather_fact(said) if fact is None else fact
        if fact and not self.mode().get("no_search"):
            out += "\n\n" + fact + " (Live data. Use it if they asked about the weather.)"
        # the mode goes LAST: before the notes and history (all in his normal voice) the model
        # drifted back to friendly. "what's up bro" got a warm hello in mean mode (11-26)
        overlay = personality.block(self.mode())
        if overlay:
            out += "\n\n" + overlay + "\n\n" + personality.reminder(self.mode())
        return out

    def _bulletin(self) -> str:
        """The bulletin, read at most every 30 s (warm() fetches it while they're talking)."""
        got = self._bull
        if got and time.monotonic() - got[1] < 30.0:
            return got[0]
        text, _ = memory.bulletin(self.account["id"])
        self._bull = (text, time.monotonic())
        return text

    def warm(self) -> None:
        """They're talking to him: get the slow parts out of the way before the turn needs
        them. The bulletin from the database, the LLM client built, and its connection open
        (an idle HTTPS connection closes within seconds, and a fresh handshake to the
        provider is a few hundred ms on the first token)."""
        try:
            self._bull = None
            self._bulletin()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            b = self.backend()
            if hasattr(b, "ping"):
                b.ping()
        except Exception:                                        # noqa: BLE001
            pass

    def weather_fact(self, said: str) -> str:
        """Open-Meteo for the place set in the app, when they ask about the weather at home:
        instant, and no web search."""
        try:
            from reedus_companion.serve.weather import _WEATHER_ASK, place_hint_from_message
            if not _WEATHER_ASK.search(said or "") or place_hint_from_message(said):
                return ""
            g = ((self.account or {}).get("settings") or {}).get("glance") or {}
            place = (g.get("place") or "").strip()
            if not place:
                return ""
            from reedus_relay import weather
            return weather.spoken(place, g.get("unit") or "F")
        except Exception:                                        # noqa: BLE001
            return ""

    def reply(self, said: str, conversation: str, on_sentence: Callable[[str], None],
              cancel: Optional[threading.Event] = None,
              on_face: Optional[Callable[[str], None]] = None,
              think: Optional[str] = None, search: bool = False, who=None) -> str:
        """Speakable reply text; each sentence also goes to on_sentence as soon as it's ready,
        and each face tag ([excited], [sad]…) to on_face just before the sentence it leads.
        who: voices.Who, who said it (their notes and history go in the prompt)."""
        t0 = time.monotonic()
        self.timing = {}
        speaker = self.speaker_label(who)
        fast = self.fast_path(said)
        if fast:
            on_sentence(speakable(fast))
            self._remember(said, fast, speaker=speaker)
            self.timing = {"fast": 1}
            return fast
        mode = self.mode()
        if mode.get("echo"):
            echo = personality.parrot(said)     # parrot mode: no LLM, their exact words back
            if echo:
                if on_face is not None:
                    on_face(personality.face(mode) or "excited")
                on_sentence(speakable(echo))
                self._remember(said, echo, speaker=speaker)
                self.timing = {"fast": 1, "mode": mode["id"]}
                return echo
        timing_mode = mode["id"]
        no_search = bool(mode.get("no_search"))       # useless mode: wrong on purpose, never looked up
        if no_search:
            search = False
        fact = "" if no_search else self.weather_fact(said)
        msgs = [{"role": "system", "content": self.system(said, conversation, fact, who=who)}]
        # an empty turn (a reply cut off by "Reedus" before it said anything) makes Gemini
        # refuse the whole request ("Missing text in content"), so those never go in
        past = [h for h in self.history[-2 * config.HISTORY_TURNS:] if (h.get("content") or "").strip()]
        # more than one person in this conversation: each of their lines says who it was
        labels = speaker is not None and any(
            h.get("speaker", speaker) != speaker for h in past if h["role"] == "user")
        msgs += [{"role": h["role"], "content": (f"[{h.get('speaker') or 'unknown voice'}] "
                                                 if labels and h["role"] == "user" else "") + h["content"]}
                 for h in past]
        msgs.append({"role": "user", "content": (f"[{speaker or 'unknown voice'}] " if labels else "") + said})
        looked_up = self.searched_before(past)
        if looked_up:
            # history is text only, so without this he reads an earlier answer, sees no results in
            # front of him, and "admits" he never searched (11-23)
            msgs[0]["content"] += ("\n\nEarlier in this conversation you really did search the web "
                                   "to answer: " + "; ".join(f'"{q}"' for q in looked_up)
                                   + ". Those answers came from real results.")
        nudge = personality.nudge(mode)
        if nudge:
            # next to their words is where a fast model looks hardest
            msgs[-1] = {"role": "user", "content": msgs[-1]["content"] + "\n\n(" + nudge + ")"}
        # a mode's rhetorical questions ("are you serious?") are the bit, so the cap is normal only
        no_question = timing_mode == "normal" and self.asked_recently()
        if no_question:
            # the persona asks for few questions; a model that asks anyway (Haiku did, every
            # turn) gets the note next to their words, and _sentence below drops the question
            msgs[-1] = {"role": "user", "content": msgs[-1]["content"] + "\n\n(" + self.NO_QUESTION_NOTE + ")"}
        timing = self.timing
        timing["mode"] = timing_mode
        dropped: List[str] = []           # said by the model, never spoken, kept out of history

        def _sentence(s: str) -> None:
            if no_question and timing.get("sentence") is not None and "?" in s:
                # a question he wasn't meant to ask this turn: say the rest of the piece
                keep = []
                for part in re.split(r"(?<=[.!?…])\s+", s):
                    if part.rstrip(" \"')").endswith("?"):
                        dropped.append(part)
                    else:
                        keep.append(part)
                s = " ".join(keep).strip()
                if not s:
                    return
            timing.setdefault("sentence", time.monotonic() - t0)
            on_sentence(s)

        split = SentenceSplitter(_sentence, on_face=on_face, first_clause=20)
        # a filler ("Let me look that up") is already playing on a forced search, so the model's
        # own "I'll search for that" before its search is held back and dropped, not said twice
        hold = {"on": False, "text": ""}

        def _token(tok: str) -> None:
            timing.setdefault("token", time.monotonic() - t0)
            if hold["on"]:
                hold["text"] += tok
                if len(hold["text"]) < 160:
                    return
                tok, hold["on"], hold["text"] = hold["text"], False, ""   # that's the answer
            split.feed(tok)

        def _searching() -> None:
            timing["search_at"] = time.monotonic() - t0
            if hold["on"]:
                if hold["text"].strip():
                    dropped.append(hold["text"])
                hold["text"] = ""
                hold["on"] = False
            else:
                split.flush()                # say "let me check" now: it covers the search

        backend = self.backend()
        searchable = getattr(backend, "_search_default", None)
        if searchable is None and hasattr(backend, "_search"):
            searchable = backend._search_default = bool(backend._search)
        if searchable is not None:
            # asked to search ("look it up"): always, if this provider can search at all
            backend._search = bool(searchable) and not no_search and (
                search or (needs_search(said) and not fact))
        if search and not searchable:
            timing["no_search"] = True       # a provider that can't search the web itself
        level = think if think in THINK_IDS else think_level(self.account)
        apply_think(backend, level)
        timing["think"] = level
        timing["prep"] = time.monotonic() - t0
        searching = bool(getattr(backend, "_search", False))
        tells = hasattr(backend, "on_search")
        if tells:
            backend.on_search = _searching
            hold["on"] = searching and search
        try:
            text = self._ask(backend, msgs, _token, cancel, search=searching, forced=search)
        finally:
            if tells:
                backend.on_search = None
        if hold["on"]:                   # it never searched: what it held is the reply after all
            hold["on"] = False
            if not (searchable and not no_search and promised_search(hold["text"])):
                split.feed(hold["text"])
            elif hold["text"].strip():
                dropped.append(hold["text"])  # "searching now!" with no search: retried below
            hold["text"] = ""
        if (searchable and not no_search and not timing.get("search")
                and not (cancel is not None and cancel.is_set())
                and promised_search(text)):
            # he said he'd look it up but had no search tool this turn: keep the promise. what he
            # already said ("one sec, searching") stays as the filler
            split.flush()
            timing["retried"] = True
            msgs = msgs + [{"role": "assistant", "content": text}]
            backend._search = True
            text = text + " " + self._ask(backend, msgs, _token, cancel, search=True, forced=True,
                                          follow_up=True)
        timing["done"] = time.monotonic() - t0
        if isinstance(timing.get("token"), float) and level == "fast" and not timing.get("search"):
            try:                         # what this model's first word really takes (the app shows it)
                from reedus_relay import models_info
                models_info.record(*self.llm_choice(), timing["token"] - timing.get("prep", 0.0))
            except Exception:                                    # noqa: BLE001
                pass
        if cancel is not None and cancel.is_set():
            split.buf = ""
        else:
            split.flush()
        # history keeps the tags, so he keeps seeing (and copying) them; what's said and saved doesn't.
        # a reply that was dropped (they kept talking, or interrupted) isn't remembered
        for d in dropped:                # history is what he really said out loud
            text = text.replace(d, " ", 1) if d in text else text
        if cancel is None or not cancel.is_set():
            self._remember(said, re.sub(r"\s+", " ", _MD.sub("", text or "")).strip(),
                           searched=bool(timing.get("searched")), speaker=speaker)
        return speakable(face_tag(text)[1])

    SEARCH_NOTE = ("\n\nThey want this looked up: use web search before you answer. Don't announce "
                   "that you're searching; that's already been said out loud.")
    FOLLOW_NOTE = ("\n\nYou just told them you'd look it up. Search the web now and give the answer; "
                   "don't repeat that you're searching.")
    # with search on, every turn: results are what he knows, not a script to read out (11-23
    # Haiku read a search back as a list of facts)
    VOICE_NOTE = ("\n\nIf you search, you're still you talking to a friend, not reading out a "
                  "news brief. Pick the one or two things that answer what they asked, say them in "
                  "your own words, and add your reaction or take. No run of stats, dates, names or "
                  "sources, and don't cover everything you found; if they want more, they'll ask.")
    NO_QUESTION_NOTE = ("You've asked a question recently, so don't ask one this time: react or give "
                        "your take, and end on a statement.")

    def _ask(self, backend, msgs, on_token, cancel, *, search: bool, forced: bool = False,
             follow_up: bool = False) -> str:
        """One call to the LLM. With search on, the system prompt says to really use it (a fast
        model handed the tool often just says it's searching and stops). Records in timing
        whether a search really ran."""
        if search:
            msgs = [{**msgs[0], "content": msgs[0]["content"]
                     + (self.FOLLOW_NOTE if follow_up else self.SEARCH_NOTE if forced else "")
                     + self.VOICE_NOTE}] + msgs[1:]
        if hasattr(backend, "last_searched"):
            backend.last_searched = None
        # the example replies are already in the stable part of the system prompt
        text = backend.chat(msgs, REPLY_TOKENS, use_adapter=False, on_token=on_token,
                            name="persona", cancel=cancel)
        self.timing["search"] = bool(search) or bool(self.timing.get("search"))
        if search:
            ran = getattr(backend, "last_searched", None)
            if ran is not None:
                self.timing["searched"] = bool(ran) or bool(self.timing.get("searched"))
        return text or ""

    def asked_last_time(self) -> bool:
        """Did his previous reply (this conversation) end on a question?"""
        with self.lock:
            last = next((h["content"] for h in reversed(self.history) if h["role"] == "assistant"), "")
        return speakable(face_tag(last)[1]).rstrip(" \"')").endswith("?")

    QUESTION_GAP = 2      # replies after a question that don't ask one: at most one in three

    def asked_recently(self) -> bool:
        """Did any of his last QUESTION_GAP replies (this conversation) ask a question?"""
        with self.lock:
            mine = [h["content"] for h in self.history if h["role"] == "assistant"][-self.QUESTION_GAP:]
        return any("?" in speakable(face_tag(m)[1]) for m in mine)

    @staticmethod
    def searched_before(past: List[dict]) -> List[str]:
        """What they asked on the turns where a web search really ran (newest last, max 3)."""
        out = []
        for i, h in enumerate(past):
            if h.get("role") == "assistant" and h.get("searched") and i > 0:
                q = re.sub(r"\s+", " ", past[i - 1].get("content") or "").strip()
                out.append(q[:80] + ("..." if len(q) > 80 else ""))
        return out[-3:]

    @staticmethod
    def speaker_label(who) -> Optional[str]:
        """The name on this turn's line in the history: theirs, "" for a voice he didn't
        recognize, None with nobody set up by voice (no labels at all)."""
        if who is None or not getattr(who, "active", False):
            return None
        return who.name or ""

    def _remember(self, said: str, reply: str, searched: bool = False,
                  speaker: Optional[str] = None) -> None:
        if not (said or "").strip() or not (reply or "").strip():
            return
        with self.lock:
            a = {"role": "assistant", "content": reply}
            if searched:
                a["searched"] = True            # never sent to the LLM as-is; see reply()
            u = {"role": "user", "content": said}
            if speaker is not None:
                u["speaker"] = speaker          # who said it (labels when there's more than one)
            self.history += [u, a]
            self.history = self.history[-2 * config.HISTORY_TURNS:]

    def new_conversation(self) -> None:
        with self.lock:
            self.history = []
        self._roulette = None                   # surprise me: someone new next time
