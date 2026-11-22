"""one reply, the way Lite needs it: the timer/alarm/list fast path first (free, instant), then
Reedus himself on whichever cloud LLM the account picked, with the bulletin and recalled history
in his prompt, streamed out sentence by sentence so his voice starts before he's done thinking.

the persona text is reedus_companion/serve/persona.py, the same file the robot uses, so both
bodies are one character. the LLM layer is reedus_companion/cloud/llm.py (any provider).
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Callable, List, Optional

from reedus_companion.serve import persona
from reedus_relay import config, memory

LITE_VOICE = (
    "Right now you're speaking out loud through Reedus Lite, your small desk body: a face of "
    "lights, cat ears, a speaker. Everything you write is spoken, so talk, don't type: usually one "
    "to three sentences, longer only when they ask for detail. No lists, no markdown, no emoji, "
    "no stage directions. Timers, alarms, reminders and lists are handled for you before a "
    "message reaches you.\n\n"
    "Your face shows how you feel, so react to what they said. Start every reply with one face "
    "tag in square brackets: [happy], [laugh], [love], [sad], [surprised], [angry], [confused], "
    "[sleepy], [excited], [smug], [shy], [curious], [wink] or [neutral]. For example: "
    "\"[excited] Wait, you got the internship?\" or \"[sad] Oh no, I'm sorry.\" Put a new tag "
    "in front of a later sentence only when your feeling changes. Tags are never spoken."
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
    r"look (?:it |that )?up|search|google|what happened|who is the (?:current|new)|"
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

    def system(self, said: str, conversation: str) -> str:
        bulletin, _ = memory.bulletin(self.account["id"])
        recalled = memory.recall(self.account["id"], said, conversation)
        today = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p").replace(" 0", " ")
        out = persona.build_runtime_system(today=today, bulletin=bulletin, recalled=recalled) \
            + "\n\n" + LITE_VOICE
        fact = self.weather_fact(said)
        if fact:
            out += "\n\n" + fact + " (Live data. Use it if they asked about the weather.)"
        return out

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
              on_face: Optional[Callable[[str], None]] = None) -> str:
        """Speakable reply text; each sentence also goes to on_sentence as soon as it's ready,
        and each face tag ([excited], [sad]…) to on_face just before the sentence it leads."""
        fast = self.fast_path(said)
        if fast:
            on_sentence(speakable(fast))
            self._remember(said, fast)
            return fast
        msgs = [{"role": "system", "content": self.system(said, conversation)}]
        # an empty turn (a reply cut off by "Reedus" before it said anything) makes Gemini
        # refuse the whole request ("Missing text in content"), so those never go in
        msgs += [h for h in self.history[-2 * config.HISTORY_TURNS:] if (h.get("content") or "").strip()]
        msgs.append({"role": "user", "content": said})
        split = SentenceSplitter(on_sentence, on_face=on_face, first_clause=36)
        backend = self.backend()
        searchable = getattr(backend, "_search_default", None)
        if searchable is None and hasattr(backend, "_search"):
            searchable = backend._search_default = bool(backend._search)
        if searchable is not None:
            backend._search = bool(searchable) and needs_search(said) and not self.weather_fact(said)
        text = backend.chat(msgs, REPLY_TOKENS, use_adapter=True, on_token=split.feed,
                            name="persona", cancel=cancel)
        if cancel is not None and cancel.is_set():
            split.buf = ""
        else:
            split.flush()
        # history keeps the tags, so he keeps seeing (and copying) them; what's said and saved doesn't
        self._remember(said, re.sub(r"\s+", " ", _MD.sub("", text or "")).strip())
        return speakable(face_tag(text)[1])

    def _remember(self, said: str, reply: str) -> None:
        if not (said or "").strip() or not (reply or "").strip():
            return
        with self.lock:
            self.history += [{"role": "user", "content": said},
                             {"role": "assistant", "content": reply}]
            self.history = self.history[-2 * config.HISTORY_TURNS:]

    def new_conversation(self) -> None:
        with self.lock:
            self.history = []
