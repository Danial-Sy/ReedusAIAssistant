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
    "message reaches you."
)

REPLY_TOKENS = int(config.env_float("RELAY_REPLY_TOKENS", 220))

_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]?\s+(?=[A-Z0-9\"'(])")
_FILLER = r"(?:(?:ok(?:ay)?|alright|all right|cool|great|thanks|thank you|cheers|perfect)[,.!\s]+)*"
_BYE_WORDS = (r"(?:good ?bye|bye(?: bye)?|see (?:you|ya)(?: later| tomorrow)?|good ?night|"
              r"night night|later|that'?s all(?: for now)?|go to sleep)")
# a goodbye that leads the sentence ("okay thanks, see you later") or closes it ("…, bye")
_BYE = re.compile(rf"^\W*{_FILLER}{_BYE_WORDS}\b(?!\s+(?:today|to|mean|in)\b)|"
                  rf"[,.!]\s*{_BYE_WORDS}\W*$", re.I)
_MD = re.compile(r"[*_#`~]+")


def is_goodbye(said: str) -> bool:
    return bool(_BYE.search(said or ""))


def speakable(text: str) -> str:
    t = _MD.sub("", text or "")
    t = re.sub(r"\[(?:[^\]]{0,40})\]", "", t)                # [laughs]-style directions
    return re.sub(r"\s+", " ", t).strip()


class SentenceSplitter:
    """Feed streamed tokens; get complete sentences as soon as each one closes."""

    def __init__(self, emit: Callable[[str], None], min_chars: int = 24):
        self.emit, self.min_chars, self.buf = emit, min_chars, ""

    def feed(self, tok: str) -> None:
        self.buf += tok
        while True:
            m = None
            for cand in _SENTENCE_END.finditer(self.buf):
                if cand.start() >= self.min_chars:
                    m = cand
                    break
            if not m:
                return
            head, self.buf = self.buf[:m.start()], self.buf[m.end():]
            if speakable(head):
                self.emit(speakable(head))

    def flush(self) -> None:
        if speakable(self.buf):
            self.emit(speakable(self.buf))
        self.buf = ""


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
        from reedus_companion.cloud import llm_settings
        s = (self.account.get("settings") or {}).get("llm") or {}
        provider = s.get("provider") or llm_settings.provider()
        model = s.get("model") or llm_settings.model(provider)
        return provider, model

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
        return persona.build_runtime_system(today=today, bulletin=bulletin, recalled=recalled) \
            + "\n\n" + LITE_VOICE

    def reply(self, said: str, conversation: str, on_sentence: Callable[[str], None],
              cancel: Optional[threading.Event] = None) -> str:
        """Speakable reply text; each sentence also goes to on_sentence as soon as it's ready."""
        fast = self.fast_path(said)
        if fast:
            on_sentence(speakable(fast))
            self._remember(said, fast)
            return fast
        msgs = [{"role": "system", "content": self.system(said, conversation)}]
        msgs += self.history[-2 * config.HISTORY_TURNS:]
        msgs.append({"role": "user", "content": said})
        split = SentenceSplitter(on_sentence)
        text = self.backend().chat(msgs, REPLY_TOKENS, use_adapter=True, on_token=split.feed,
                                   name="persona", cancel=cancel)
        if cancel is not None and cancel.is_set():
            split.buf = ""
        else:
            split.flush()
        text = speakable(text)
        self._remember(said, text)
        return text

    def _remember(self, said: str, reply: str) -> None:
        with self.lock:
            self.history += [{"role": "user", "content": said},
                             {"role": "assistant", "content": reply}]
            self.history = self.history[-2 * config.HISTORY_TURNS:]

    def new_conversation(self) -> None:
        with self.lock:
            self.history = []
