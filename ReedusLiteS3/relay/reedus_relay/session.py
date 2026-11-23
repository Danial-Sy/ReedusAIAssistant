"""one connected Reedus Lite: the conversation loop that used to run on the Xeon.

    idle ──"Reedus"──► listening ──pause──► thinking ──► speaking ──► listening (follow-up)
      ▲                     │ nothing said                    │ "Reedus" = barge-in
      └─────────────────────┴──────── goodbye / silence ◄─────┘

the wake word: an S3 with the `wake` cap hears "Reedus" itself and sends {"type": "wake"}
(wake.py); for one without it the relay runs openWakeWord on what its speech gate sends.
audio up: the S3 sends 20 ms frames only after its wake word (or while its speech gate is open),
or while the relay has said `listen` (a conversation is going). audio down: speech frames paced
against the device's playhead, exactly like the run box hub (ReedusLite/PROTOCOL.md), so
barge-in stays instant.

every cost in a turn (speech-to-text, LLM, web search, Inworld) is metered and taken from the
account's pool when the turn ends; the memory gets the turn, the bulletin is refreshed once the
conversation is over.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from typing import Optional

from reedus_relay import accounts, config, listening, memory, meter, speech
from reedus_relay.brain import DEEP, Brain, directives, is_goodbye

IDLE, LISTEN, BUSY = "idle", "listen", "busy"
FRAME_S = config.FRAME_SAMPLES / config.RATE
_TTS_SLOTS: Optional[asyncio.Semaphore] = None  # Inworld streams, relay-wide (FIFO: in order)


def _tts_slots() -> asyncio.Semaphore:
    global _TTS_SLOTS
    if _TTS_SLOTS is None:
        _TTS_SLOTS = asyncio.Semaphore(config.TTS_CONCURRENCY)
    return _TTS_SLOTS
OUT_OF_CREDIT = ("I'm out of credit, so I can't think right now. "
                 "Add some in the app and I'm all yours.")
TROUBLE = "Sorry, my brain hiccuped there. Try me again in a second."
THINK_ARMED = ("Okay, I'll really think about it. Go ahead.",
               "Sure, I'll take my time on this one. What's the question?",
               "Alright, thinking cap on. Go for it.")
THINK_FILLER = ("Hmm, let me think about that.", "Okay, give me a second to think.",
                "Hmm, good question. Let me think.")
SEARCH_ARMED = ("Sure, I'll look it up. What's the question?", "Okay, I'll search for it. Go ahead.")
ARMED_BOTH = ("Okay, I'll look it up and really think it through. Go ahead.",
              "Sure, I'll search and take my time on it. What's the question?")
SEARCH_FILLER = ("Let me look that up.", "One sec, I'll look it up.")
BOTH_FILLER = ("Okay, let me look into that and think it over.", "Let me dig into that for a second.")
_WAKE_PREFIX = re.compile(r"^\W*(?:hey|hi|okay|ok|yo)?[\s,]*(?:reedus|reed us|reedis|redis)\b[\s,.!?]*", re.I)


_STOP = re.compile(
    r"^\W*(?:(?:okay|ok|oh|no|hey|alright|uh|um|dude|bro|reedus|stop)[\s,.!]+)*"
    r"(?:(?:can|could|would|will) you (?:please )?|please |you can |just )?"
    r"(?:stop(?: (?:it|that|listening|talking|speaking|now|please))?|never ?mind|forget (?:it|about it)|"
    r"cancel(?: that)?|shut up|be quiet|quiet(?: down)?|shush|hush|zip it|enough|that'?s enough|"
    r"nothing|no thanks|nah|i'?m good|all good|leave it|that'?s it|that'?ll be all|not you|"
    r"wasn'?t talking to you|i wasn'?t talking to you)"
    r"(?:[\s,.!]+(?:please|thanks|thank you|reedus|now|for now|stop))*\W*$", re.I)


def strip_wake(said: str) -> str:
    return _WAKE_PREFIX.sub("", said or "").strip()


# what a sentence that isn't finished ends on: "and", "but", "because", "um", "the", "my"… or a
# trailing comma / dash / ellipsis. kept to words that almost never end a real question
_UNFINISHED = re.compile(
    r"(?:[,\-—…]|\.\.\.)\s*$|\b(?:and|but|or|because|cause|cuz|um+|uh+|er+m?|hmm+|the|a|an|my|your|our|"
    r"their|his|her|i|to|if|plus|also|whether|than)\W*$", re.I)


def looks_unfinished(said: str) -> bool:
    """'so I was thinking about, um' -> True. Wait for the rest before answering."""
    t = (said or "").strip()
    if t.endswith(("...", "…", ",", "-", "—")):
        return True
    t = re.sub(r"[.!?]+$", "", t)          # the transcriber likes to end everything with a full stop
    return bool(t) and len(t.split()) <= 60 and bool(_UNFINISHED.search(t))


def is_stop(said: str) -> bool:
    """"Stop", "never mind", "shut up"…: he stops listening, says nothing, back to idle."""
    return bool(_STOP.match(said or ""))



def _search_note(bt: dict) -> str:
    """the turn log's word on the web search: was it on, and did it really run"""
    if not bt.get("search"):
        return ""
    ran = bt.get("searched")
    note = ", searched" if ran else ", search on but not used" if ran is False else ", search on"
    return note + (" (after promising one)" if bt.get("retried") else "")

class Session:
    def __init__(self, hub, ws, device: dict, account: dict, hello: dict, voice_factory,
                 brain: Optional[Brain] = None, wake=None, assistant=None, stt=None):
        self.hub, self.ws = hub, ws
        self.device, self.account = device, account
        self.did, self.aid = device["id"], account["id"]
        self.hello = hello
        self.loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self.out: asyncio.Queue = asyncio.Queue()
        self.mode = IDLE
        self.state = "idle"
        self.wake = wake if wake is not None else speech.wake_detector(hello.get("caps") or [])
        self.device_wake = bool(getattr(self.wake, "on_device", False))
        self.ep: Optional[speech.Endpointer] = None
        self.cancel = threading.Event()
        self.task: Optional[asyncio.Task] = None
        self.play_end = 0.0
        self.pb: Optional[dict] = None   # this reply's playback report from the Lite
        self.last_frame = time.monotonic()
        self.conversation = uuid.uuid4().hex
        self.turns_in_conv = 0
        self.empty_turns = 0
        self.info: dict = {}
        self.voice_factory = voice_factory
        self.stt = stt or speech.transcribe
        self._last_clock = self._last_timers = 0.0
        self.closed = False
        self.spec = None                 # an early transcript in flight (speech.Endpointer 'pause')
        self.spec_meters: list = []
        self.spec_at = 0.0
        self.t_end = self.t_first_audio = 0.0
        self.idle_since = time.monotonic()
        self.next_glance = time.monotonic() + 3.0
        self.wx_up = False               # the standing weather card is on the device
        self.wx_next = 0.0               # when to send it a fresh reading
        self.art_until = 0.0             # something drawn from the app is on the face
        self.funded_hint = True          # last known: does the pool have money (no early STT if not)
        self.think_next = False          # "think about this before you answer": the next question
        self.rest_until = 0.0            # one question mode: when this conversation counts as over
        self.watch: Optional[speech.Endpointer] = None   # still listening while he thinks
        self.carry_text: Optional[str] = None             # "so I was, um…": what they said so far
        self.continues = 0               # how many times this turn waited for the rest
        self.last_peak = -120.0          # how loud they were, so a TV can't pass for them
        self.stop_ep: Optional[speech.Endpointer] = None  # "stop" said over him while he talks
        self.stop_checks = 0
        self.stop_busy = False
        self.search_next = False         # "search the internet": the next question

        if assistant is None:
            from reedus_relay.assist_store import DbAssistant
            assistant = DbAssistant(self.did)
        self.assist = assistant
        from reedus_companion.assist.commands import Commands
        self.brain = brain or Brain(account, device, Commands(self.assist))
        if getattr(self.brain, "commands", None) is None:
            self.brain.commands = Commands(self.assist)
        self.assist.on_change(lambda kind, item: self.send_timers())
        if hasattr(self.assist, "start"):
            self.assist.start()

    # ---------- outbound (thread-safe) ----------
    def _put(self, item) -> None:
        if self.closed:
            return
        if threading.get_ident() == self._loop_thread:
            self.out.put_nowait(item)
        else:
            self.loop.call_soon_threadsafe(self.out.put_nowait, item)

    def send(self, msg: dict) -> None:
        self._put(json.dumps(msg, separators=(",", ":")))

    def set_state(self, state: str) -> None:
        if state == "idle" and self.state != "idle":
            self.idle_since = time.monotonic()
            self.next_glance = max(self.next_glance, self.idle_since + 20.0)
        self.state = state
        self.send({"type": "state", "state": state})

    def listen(self, open_: bool) -> None:
        self.send({"type": "listen", "open": bool(open_)})

    def send_clock(self) -> None:
        now = time.time()
        local = time.localtime(now)
        h24 = bool(((self.assist.settings or {}).get("glance") or {}).get("h24"))
        self.send({"type": "clock", "t": int(now + local.tm_gmtoff), "h24": h24})
        self._last_clock = time.monotonic()

    def send_timers(self) -> None:
        from reedus_relay.assist_store import timers_msg
        self.send(timers_msg(self.assist))
        self._last_timers = time.monotonic()

    def send_bulletin(self) -> None:
        text, rev = memory.bulletin(self.aid)
        self.send({"type": "bulletin", "rev": rev, "text": text})

    async def sender(self) -> None:
        while True:
            item = await self.out.get()
            await self.ws.send(item)

    # ---------- lifecycle ----------
    async def welcome(self) -> None:
        s = self.device.get("settings") or {}
        self.send({"type": "welcome", "proto": 3, "volume": int(s.get("volume", 70)),
                   "brightness": int(s.get("brightness", 100)),
                   "wake": "device" if self.device_wake else "relay",
                   "wake_cutoff": listening.wake_cutoff(self.account),
                   "head_near_db": listening.head_near_db(self.account),
                   "account": self.account.get("name", ""), "device": self.device.get("name", "")})
        self.set_state("idle")
        self.listen(False)
        self.send_clock()
        self.send_timers()
        _, rev = await asyncio.to_thread(memory.bulletin, self.aid)
        if rev and int(self.hello.get("bulletin_rev") or 0) != rev:
            await asyncio.to_thread(self.send_bulletin)
        self.funded_hint = await asyncio.to_thread(accounts.funded, self.aid)
        if not self.funded_hint:
            self.send({"type": "glance", "card": "text", "text": "ADD CREDIT IN THE APP", "hold": 20})

    async def housekeeping(self) -> None:
        while not self.closed:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if now - self._last_clock > 300:
                self.send_clock()
            if now - self._last_timers > 60:
                self.send_timers()
            # listening but the device went quiet (it should be streaming): don't hang there
            if self.mode == LISTEN and now - self.last_frame > 4.0:
                self.end_conversation(keep=self.single())
            if self.mode == IDLE and self.rest_until and now >= self.rest_until:
                self.end_conversation()          # one question mode: that conversation's over
            if now >= self.next_glance:
                await self.glance_round(now)

    def glance_settings(self) -> dict:
        return ((self.account or {}).get("settings") or {}).get("glance") or {}

    WX_HOLD_S = 900.0        # the device keeps the card this long without hearing from us
    WX_REFRESH_S = 300.0     # ...and we send a fresh reading this often

    WX_FLASH_S = 8.0         # how long it shows each time in the every-N-seconds mode

    async def glance_round(self, now: float) -> None:
        """The idle weather card (turned on in the app), two ways (the app's "how often"):

            every 0 / unset  always: a standing card the device shows whenever he's idle, the
                             whole time, until his name wakes him (and again once the
                             conversation's over). timers, rings and drawings go in front of it
            every N seconds  it pops up for a few seconds every N while nobody's talking

        Either way, not at night, and taken down when it's switched off."""
        self.next_glance = now + 5.0
        g = self.glance_settings()
        want = bool(g.get("weather")) and bool((g.get("place") or "").strip())
        quiet = g.get("night") if isinstance(g.get("night"), list) else [23, 7]
        h = time.localtime().tm_hour
        if want and len(quiet) == 2 and (h >= quiet[0] or h < quiet[1] if quiet[0] > quiet[1]
                                         else quiet[0] <= h < quiet[1]):
            want = False
        try:
            every = float(g.get("every") or 0)
        except (TypeError, ValueError):
            every = 0.0
        always = every <= 0
        if self.wx_up and (not want or not always):     # the standing card has to go
            self.send({"type": "glance", "card": "weather", "temp": 0, "hold": 0})
            self.wx_up = False
            self.info.pop("glance", None)
        if not want or now < self.wx_next:
            return
        if not always:
            # a passing card: only on an idle face that's been idle a moment, never over a
            # timer, a ring or a drawing from the app
            if self.mode != IDLE or self.state != "idle" or now - self.idle_since < 15.0 \
                    or now < self.art_until:
                return
            try:
                if self.assist.ringing() or any(i.get("kind") == "timer" for i in self.assist.active()):
                    return
            except Exception:                                    # noqa: BLE001
                pass
        from reedus_relay import weather
        try:
            w = await asyncio.to_thread(weather.current, g["place"], g.get("unit") or "F")
        except Exception as e:                                   # noqa: BLE001
            print(f"[relay] weather card failed: {type(e).__name__}: {e}", flush=True)
            w = None
        if not w:
            self.wx_next = now + 60.0
            return
        card = {"type": "glance", "card": "weather", "temp": w["temp"], "unit": w["unit"],
                "icon": w["icon"]}
        if always:
            self.send({**card, "hold": self.WX_HOLD_S})
            self.wx_up = True
            self.wx_next = now + self.WX_REFRESH_S
        else:
            self.send({**card, "hold": self.WX_FLASH_S, "once": True})
            self.wx_next = now + max(30.0, every)
        self.info["glance"] = {"card": "weather", **w, "at": time.time()}

    def close(self) -> None:
        self.closed = True
        self.cancel.set()
        if self.task is not None:
            self.task.cancel()
        try:
            self.assist.stop()
        except Exception:                                        # noqa: BLE001
            pass
        if self.turns_in_conv:
            self._fold_in_background(self.conversation)

    # ---------- inbound ----------
    def on_frame(self, pcm: bytes) -> None:
        self.last_frame = time.monotonic()
        if self.mode == LISTEN and self.ep is not None:
            ev = self.ep.feed(pcm)
            if ev == "pause":
                self._speculate(self.ep.audio())
                self.warm()                      # the brain is needed in about a second
            elif ev == "resume":
                self.spec = None                 # they went on: that early transcript is short
            elif ev in ("end", "max"):
                self.t_end = time.monotonic()
                self.last_peak = max(self.ep.peak, -120.0)
                self.carry_text = None           # the whole thing gets transcribed again
                self.begin_turn(self.ep.audio())
            elif ev == "timeout":
                if self.carry_text:              # they trailed off and never went on: answer it
                    text, self.carry_text = self.carry_text, None
                    self.t_end = time.monotonic()
                    self.begin_turn(self.ep.carry, text=text)
                else:
                    self.end_conversation(keep=self.single())
            return
        # thinking, nothing said back yet: if they start talking again they weren't finished.
        # drop the reply and keep listening, with what they'd said so far kept in front
        if self.mode == BUSY and self.watch is not None and not self.t_first_audio:
            if self.watch.feed(pcm) == "start":
                self._keep_listening()
                return
        # he's talking: a short "stop" over him (no name needed) cuts him off
        if self.mode == BUSY and self.t_first_audio and self.stop_ep is not None:
            ev = self.stop_ep.feed(pcm)
            if ev in ("end", "max"):
                audio = self.stop_ep.audio()
                self.stop_ep = self._stop_endpointer()
                self._check_stop(audio)
        # idle, or busy thinking/speaking: only the wake word matters (barge-in)
        if self.wake.feed(pcm):
            self.on_wake()

    def on_text(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "wake":
            # the S3 heard the name. already listening: it's part of what's being said
            self.info["wake"] = msg
            if self.device_wake and self.mode != LISTEN:
                self.on_wake()
        elif t == "gate":
            if msg.get("open") and self.mode == IDLE:
                self.wake.reset()
                if self.wake.gate_opened():
                    self.on_wake()
        elif t == "pb":
            self._on_playback(msg)
        elif t in ("status", "battery", "doa", "music", "cal"):
            self.info[t] = {**msg, "at": time.time()} if t in ("doa", "cal") else msg
        elif t == "ota":                     # a firmware update's progress, for the app
            self.info["ota"] = {**(self.info.get("ota") or {}), **msg, "at": time.time()}
            if msg.get("state") in ("done", "error"):
                print(f"[relay] {self.device.get('name')}: firmware update {msg.get('state')}"
                      f"{': ' + str(msg.get('error')) if msg.get('error') else ''}", flush=True)
        elif t == "draw":                    # the drawing finished playing on its own
            if msg.get("op") == "done":
                self.art_until = 0.0
        elif t == "ring":                    # the Lite's own idea of whether it's ringing
            self.info["ring"] = {"on": bool(msg.get("on")), "id": msg.get("id") or "",
                                 "kind": msg.get("kind") or "", "at": time.time()}
        elif t == "wake_near":               # it almost heard the name (the app shows it)
            self.info["wake_near"] = {"p": float(msg.get("p") or 0), "at": time.time()}
        elif t == "bulletin_ack":
            self.info["bulletin_rev"] = msg.get("rev")

    def on_wake(self) -> None:
        if self.assist.ringing():
            self.assist.hush()                 # the name quiets a ring, "stop"/"snooze" follow
        if self.mode == BUSY:
            self.barge()
        self.send({"type": "wake"})
        self.start_listening(follow_up=False)
        self.warm()

    # ---------- conversation ----------
    def end_silence(self) -> float:
        return listening.end_silence(self.account)

    def _endpointer(self, start_timeout: float, floor_db: float = -120.0) -> speech.Endpointer:
        end_s = self.end_silence()
        return speech.Endpointer(start_timeout=start_timeout, end_silence=end_s,
                                 spec_after=listening.spec_after(end_s), floor_db=floor_db)

    # ---------- "stop" while he's talking ----------
    STOP_CHECKS = 6           # transcriptions per reply at most (each one costs a little)
    STOP_MAX_S = 2.6          # longer than this isn't a "stop", it's talking over him

    def _stop_endpointer(self) -> speech.Endpointer:
        # louder than his own voice coming back through the mic: a little under how loud
        # they spoke to him, no early transcripts, short pauses end it
        return speech.Endpointer(start_timeout=1e9, end_silence=0.45, max_len=self.STOP_MAX_S,
                                 spec_after=0.0, floor_db=self.last_peak - 9.0)

    def _check_stop(self, audio: bytes) -> None:
        secs = len(audio) / 2 / config.RATE
        if self.stop_busy or self.stop_checks >= self.STOP_CHECKS or secs > self.STOP_MAX_S + 0.6:
            return
        self.stop_checks += 1
        self.stop_busy = True
        token = self.cancel

        def _go():
            m = meter.Meter()
            text = ""
            try:
                with meter.metering(m):
                    text = self.stt(audio) or ""
            except Exception as e:                               # noqa: BLE001
                print(f"[relay] stop check failed: {type(e).__name__}", flush=True)
            if m.usd > 0:
                try:
                    accounts.charge(self.aid, m.usd, f"{self.device.get('name', 'Lite')}: {m.summary()}")
                except Exception:                                # noqa: BLE001
                    pass
            self.loop.call_soon_threadsafe(self._stop_heard, text, token)
        threading.Thread(target=_go, name="stop-check", daemon=True).start()

    def _stop_heard(self, text: str, token: threading.Event) -> None:
        self.stop_busy = False
        if token is not self.cancel or self.mode != BUSY:
            return
        said = strip_wake(text)
        if said and is_stop(said):
            print(f"[relay] {self.device.get('name')}: told to stop mid-reply", flush=True)
            self.barge()
            self.send({"type": "expr", "name": "blink", "hold": 0.6})
            self.send({"type": "ring_off"})
            self.end_conversation()

    def start_listening(self, follow_up: bool) -> None:
        self.mode = LISTEN
        self.spec = None
        self.watch = None
        self.stop_ep = None
        self.carry_text = None
        self.continues = 0
        self.ep = self._endpointer(config.FOLLOW_UP_S if follow_up else config.START_TIMEOUT_S)
        if follow_up:
            # the tail of his own voice right after he stops shouldn't open a turn: for the
            # first moment only something nearly as loud as they speak to him counts
            self.ep.guard_s, self.ep.guard_db = 0.4, self.last_peak - 8.0
        self.last_frame = time.monotonic()
        self.listen(True)
        self.set_state("listening")

    def single(self) -> bool:
        """One question mode (the app's Listening card): answer, then back to idle."""
        return listening.mode_id(self.account) == "single"

    REST_S = 180.0    # one question mode: questions this close together are one conversation

    def end_conversation(self, keep: bool = False) -> None:
        """Back to idle. keep=True (one question mode): the conversation isn't over, he just
        stops listening; asked again within REST_S, he still knows what you just talked about,
        and the memory update (an LLM call) runs once at the end instead of after every question."""
        self.mode = IDLE
        self.ep = None
        self.watch = None
        self.stop_ep = None
        self.carry_text = None
        self.listen(False)
        self.set_state("idle")
        self.wake.reset()
        self.empty_turns = 0
        self.think_next = self.search_next = False
        if keep and self.turns_in_conv:
            self.rest_until = time.monotonic() + self.REST_S
            return
        self.rest_until = 0.0
        if self.turns_in_conv:
            self._fold_in_background(self.conversation)
        self.brain.new_conversation()
        self.conversation = uuid.uuid4().hex
        self.turns_in_conv = 0

    def barge(self) -> None:
        self.cancel.set()
        self.send({"type": "flush"})
        self.play_end = 0.0
        self.mode = IDLE

    def begin_turn(self, audio: bytes, text: Optional[str] = None) -> None:
        self.mode = BUSY
        self.ep = None
        self.cancel = threading.Event()
        self.t_first_audio = 0.0
        self.stop_ep = None
        self.stop_checks = 0
        # keep an ear on them while he thinks: louder than LOCK_DB under how they just spoke,
        # so a TV in the background can't pass for them going on
        self.watch = self._endpointer(1e9, floor_db=self.last_peak - config.LOCK_DB - 3.0)
        self.watch.carry = audio
        self.set_state("thinking")
        self.task = asyncio.ensure_future(self.turn(audio, self.cancel, text))

    def _keep_listening(self) -> None:
        """They went on talking before his reply started: they weren't done. Forget the reply,
        keep listening; the watch already has their new words, with the old ones in front."""
        self.cancel.set()
        self.send({"type": "flush"})
        self.play_end = 0.0
        self.mode = LISTEN
        self.ep, self.watch = self.watch, None
        self.ep.start_timeout = 1e9                  # it has already started
        self.spec = None
        self.carry_text = None
        self.set_state("listening")

    def _wait_for_more(self, audio: bytes, said: str) -> None:
        """What they said trails off ("and then I…", "um,"): wait CONTINUE_S for the rest
        before answering. if they go on, it's one sentence; if not, answer what's there."""
        self.continues += 1
        self.mode = LISTEN
        ep = self.watch or self._endpointer(0.0)
        self.watch = None
        ep.carry = audio
        ep.start_timeout = ep.t + config.CONTINUE_S
        self.ep = ep
        self.spec = None
        self.carry_text = said
        self.set_state("listening")

    def _stale(self, cancel: threading.Event) -> bool:
        return cancel.is_set() or cancel is not self.cancel or self.closed

    # ---------- speculative speech-to-text ----------
    def _speculate(self, audio: bytes) -> None:
        """They paused: start transcribing now. If the pause turns out to be the end of the
        sentence (it usually is), the transcript is already on its way when 'end' fires."""
        if len(audio) < speech.FRAME_BYTES * 10 or not self.funded_hint:
            return
        m = meter.Meter()
        self.spec_meters.append(m)

        def _hear():
            with meter.metering(m):
                return self.stt(audio)
        self.spec = asyncio.ensure_future(asyncio.to_thread(_hear))
        self.spec_at = time.monotonic()

    async def _hear(self, audio: bytes, m) -> tuple:
        """(text, how): the early transcript when it's still good, else a fresh one."""
        spec, self.spec = self.spec, None
        spent, self.spec_meters = self.spec_meters, []
        got = None
        if spec is not None:
            try:
                got = (await spec, "early")
            except Exception as e:                               # noqa: BLE001
                print(f"[relay] early transcript failed ({type(e).__name__}), asking again", flush=True)
        for sm in spent:                     # early attempts are paid for either way
            for what, usd in sm.parts:
                m.add(usd, what)
        if got is not None:
            return got

        def _go():
            with meter.metering(m):
                return self.stt(audio)
        return await asyncio.to_thread(_go), "after"

    def warm(self) -> None:
        """Someone said the name, or paused mid-turn: open the voice's and the brain's
        connections and read the bulletin while they talk, so the reply doesn't pay for TLS
        handshakes and a database round trip. At most once every 2 s."""
        now = time.monotonic()
        if now - getattr(self, "_warmed", 0.0) < 2.0:
            return
        self._warmed = now

        def _voice():
            try:
                v = self.voice_factory(self.voice_prefs())
            except TypeError:
                v = self.voice_factory()
            except Exception:                                    # noqa: BLE001
                return
            if hasattr(v, "warm"):
                v.warm()

        def _brain():
            if hasattr(self.brain, "warm"):
                try:
                    self.brain.warm()
                except Exception:                                # noqa: BLE001
                    pass
        threading.Thread(target=_voice, name="voice-warm", daemon=True).start()
        threading.Thread(target=_brain, name="brain-warm", daemon=True).start()

    def voice_prefs(self) -> dict:
        return ((self.account or {}).get("settings") or {}).get("voice") or {}

    async def turn(self, audio: bytes, cancel: threading.Event, text: Optional[str] = None) -> None:
        m = meter.Meter()
        said, reply = "", ""
        t_end = getattr(self, "t_end", 0.0) or time.monotonic()
        self.t_first_audio = 0.0
        try:
            self.funded_hint = await asyncio.to_thread(accounts.funded, self.aid)
            if not self.funded_hint:
                await self.speak_lines([OUT_OF_CREDIT], m, cancel)
                await self._finish_playback(cancel)
                if not self._stale(cancel):
                    self.end_conversation()
                return

            if text is not None:                 # already transcribed (they trailed off, then nothing)
                raw, how = text, "kept"
                spent, self.spec_meters, self.spec = self.spec_meters, [], None
                for sm in spent:
                    for what, usd in sm.parts:
                        m.add(usd, what)
            else:
                raw, how = await self._hear(audio, m)
            said = strip_wake(raw)
            t_heard = time.monotonic()
            if self._stale(cancel):
                return
            if not said:
                # heard noise, not words: one more chance, then back to sleep
                self.empty_turns += 1
                if self.empty_turns >= 2:
                    self.end_conversation()
                else:
                    self.start_listening(follow_up=True)
                return
            self.empty_turns = 0
            if text is None and self.continues < 2 and looks_unfinished(said):
                self._wait_for_more(audio, said)
                return
            if is_stop(said) and not self._assist_waiting():
                # "stop" / "never mind": no reply, no LLM, straight back to idle. (with an alarm
                # ringing or a question of his pending, the fast path handles it instead)
                self.send({"type": "expr", "name": "blink", "hold": 0.6})
                self.send({"type": "ring_off"})  # in case the Lite is ringing on its own
                self.end_conversation()
                return
            # "think about this before you answer" / "search the internet": deep thinking and/or
            # a web search for this one question, either the rest of what they said or (when
            # that's all they said) whatever they ask next
            deep = DEEP if self.think_next else None
            force_search = self.search_next
            self.think_next = self.search_next = False
            want_think, want_search, rest = directives(said)
            if want_think or want_search:
                if len(rest.split()) < 2:
                    self.think_next, self.search_next = want_think, want_search
                    import random
                    lines = (ARMED_BOTH if want_think and want_search
                             else THINK_ARMED if want_think else SEARCH_ARMED)
                    await self.speak_lines([random.choice(lines)], m, cancel)
                    await self._finish_playback(cancel)
                    if not self._stale(cancel):
                        self.start_listening(follow_up=True)
                    return
                said = rest
                deep = DEEP if want_think else deep
                force_search = force_search or want_search
            from reedus_companion.lite import emotion
            react = emotion.react(said)
            if react:
                self.send({"type": "expr", "name": react, "hold": 1.2})

            sentences: asyncio.Queue = asyncio.Queue()

            def _on_sentence(s: str) -> None:
                self.loop.call_soon_threadsafe(sentences.put_nowait, s)

            def _on_face(tag: str) -> None:
                self.loop.call_soon_threadsafe(sentences.put_nowait, {"face": tag})

            extra = {**({"think": deep} if deep else {}), **({"search": True} if force_search else {})}

            def _think() -> str:
                with meter.metering(m):
                    try:
                        return self.brain.reply(said, self.conversation, _on_sentence, cancel,
                                                on_face=_on_face, **extra)
                    finally:
                        self.loop.call_soon_threadsafe(sentences.put_nowait, None)

            if deep or force_search:             # a few seconds of silence otherwise
                import random
                sentences.put_nowait({"face": "curious"})
                sentences.put_nowait(random.choice(BOTH_FILLER if deep and force_search
                                                   else THINK_FILLER if deep else SEARCH_FILLER))
            think = asyncio.ensure_future(asyncio.to_thread(_think))
            await self.speak_stream(sentences, m, cancel)
            reply = await think
            if self.t_first_audio:
                st = speech.last_stt
                bt = dict(getattr(self.brain, "timing", None) or {})

                def _ms(k):
                    return f"{int(bt[k] * 1000)}" if isinstance(bt.get(k), float) else "-"
                phases = ("fast path" if bt.get("fast") else
                          f"prep {_ms('prep')}, first token {_ms('token')}, first sentence "
                          f"{_ms('sentence')}{_search_note(bt)}"
                          f"{', thinking ' + bt['think'] if bt.get('think') not in (None, 'fast') else ''}")
                voice_ms = (int((self.t_first_audio - t_heard - bt['sentence']) * 1000)
                            if isinstance(bt.get("sentence"), float) else None)
                print(f"[relay] {self.device.get('name')}: heard in {int((t_heard - t_end) * 1000)} ms "
                      f"({st.get('model', 'stt')}, {how}), voice started "
                      f"{int((self.t_first_audio - t_end) * 1000)} ms after you stopped "
                      f"(thinking + first sentence {int((self.t_first_audio - t_heard) * 1000)} ms: "
                      f"{phases}{f', voice {voice_ms}' if voice_ms is not None else ''})",
                      flush=True)
            if self._stale(cancel):
                return
            await self._finish_playback(cancel)
            self._log_playback()
            if self._stale(cancel):
                return
            self.turns_in_conv += 1
            if is_goodbye(said):
                self.send({"type": "expr", "name": "bye", "hold": 2.0})
                self.end_conversation()
            elif self.single():
                self.end_conversation(keep=True)  # one question mode: say his name to ask again
            else:
                self.start_listening(follow_up=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                   # noqa: BLE001
            print(f"[relay] {self.device.get('name')}: turn failed: {type(e).__name__}: {e}", flush=True)
            if not self._stale(cancel):
                self.send({"type": "expr", "name": "confused", "hold": 2.0})
                try:
                    await self.speak_lines([TROUBLE], m, cancel)
                    await self._finish_playback(cancel)
                except Exception:                                # noqa: BLE001
                    pass
                if not self._stale(cancel):
                    self.end_conversation()
        finally:
            usd = m.usd
            if usd > 0:
                await asyncio.to_thread(accounts.charge, self.aid, usd,
                                        f"{self.device.get('name', 'Lite')}: {m.summary()}")
                self.funded_hint = await asyncio.to_thread(accounts.funded, self.aid)
            if said and reply and not cancel.is_set():     # a reply nobody heard isn't a turn
                await asyncio.to_thread(memory.record_turn, self.aid, self.did, self.conversation,
                                        said, reply, usd)

    def is_ringing(self) -> bool:
        """Ringing here (the relay's timer store) or on the Lite (it can ring on its own)."""
        try:
            here = bool(self.assist.ringing())
        except Exception:                                        # noqa: BLE001
            here = False
        return here or bool((self.info.get("ring") or {}).get("on"))

    def stop_ringing(self) -> None:
        """End every ring: dismiss it in the timer store, and tell the Lite to go quiet even if
        it's ringing on its own (the relay may not know about that one)."""
        try:
            if self.assist.ringing():
                self.assist.dismiss()
        except Exception as e:                                   # noqa: BLE001
            print(f"[relay] couldn't dismiss the ring: {e!r}", flush=True)
        self.send({"type": "ring_off"})
        self.info["ring"] = {**(self.info.get("ring") or {}), "on": False, "at": time.time()}
        self.send_timers()

    def _assist_waiting(self) -> bool:
        """Something ringing, or a timer/reminder question waiting on an answer."""
        try:
            return bool(self.assist.ringing()) or bool(getattr(self.assist, "pending", None))
        except Exception:                                        # noqa: BLE001
            return False

    # ---------- speaking ----------
    async def speak_lines(self, lines, m, cancel) -> None:
        q: asyncio.Queue = asyncio.Queue()
        for line in lines:
            q.put_nowait(line)
        q.put_nowait(None)
        await self.speak_stream(q, m, cancel)

    async def speak_stream(self, sentences: asyncio.Queue, m, cancel) -> None:
        """Synthesise each sentence as soon as it arrives (in parallel), play them in order."""
        from reedus_companion.lite import emotion
        pending: asyncio.Queue = asyncio.Queue()

        async def dispatch():
            while True:
                s = await sentences.get()
                if s is None or self._stale(cancel):
                    await pending.put(None)
                    if s is not None:            # drain the rest so the thinker isn't stuck
                        while await sentences.get() is not None:
                            pass
                    return
                if isinstance(s, dict):          # a face tag: keep it in line with the speech
                    await pending.put(s)
                    continue
                frames: asyncio.Queue = asyncio.Queue()
                # wait for an Inworld stream, first come first served: sentence 4 can't start
                # before sentence 3. the slot frees when this sentence's audio is all made
                # (faster than he speaks it), so the next one is ready before it's needed
                slots = _tts_slots()
                await slots.acquire()
                if self._stale(cancel):
                    slots.release()
                    await pending.put(None)
                    while await sentences.get() is not None:
                        pass
                    return
                task = asyncio.ensure_future(asyncio.to_thread(self._synth, s, frames, m, cancel))
                task.add_done_callback(lambda _t, sl=slots: sl.release())
                await pending.put((s, frames, task))

        async def play():
            first = True
            mood = None              # what he tagged himself as feeling, until he tags again
            while True:
                item = await pending.get()
                if item is None:
                    return
                if isinstance(item, dict):
                    tag = item.get("face")
                    if tag == "wink":
                        self.send({"type": "expr", "name": "wink", "hold": 1.2})
                    else:
                        mood = None if tag == "neutral" else tag
                        # straight away: the face reacts while his voice is still being made
                        self.send({"type": "emotion", "emotion": tag, "strength": 0.9, "hold": 3.0})
                    continue
                s, frames, task = item
                emo, strength = (mood, 0.9) if mood else emotion.classify(s)
                if emo != "neutral":
                    self.send({"type": "emotion", "emotion": emo, "strength": strength,
                               "hold": emotion.speech_seconds(s)})
                # a few frames per message: the Lite reads one message per pass of its main loop,
                # and 50 tiny ones a second was more than it kept up with during a long reply
                batch = b""
                while True:
                    fr = await frames.get()
                    if fr is None:
                        break
                    if self._stale(cancel):
                        batch = b""
                        continue
                    if first:
                        self.set_state("speaking")
                        self.t_first_audio = time.monotonic()
                        self.stop_ep = self._stop_endpointer()   # listen for "stop" over him
                        self.pb = {"min": None, "under0": None, "under": None, "over": None}
                        first = False
                    batch += fr
                    if len(batch) >= config.SPEECH_BATCH * config.FRAME_SAMPLES * 2:
                        await self._send_speech(batch)
                        batch = b""
                if batch and not self._stale(cancel):
                    await self._send_speech(batch)
                await task

        await asyncio.gather(dispatch(), play())

    def _voice(self):
        try:
            return self.voice_factory(self.voice_prefs())
        except TypeError:                     # a factory that takes no prefs (tests)
            return self.voice_factory()

    def _synth(self, text: str, frames: asyncio.Queue, m, cancel) -> None:
        sent = 0
        try:
            for attempt in range(3):
                if self._stale(cancel):
                    break
                try:
                    with meter.metering(m):
                        for fr in self._voice().frames(text):
                            if self._stale(cancel):
                                break
                            sent += 1
                            self.loop.call_soon_threadsafe(frames.put_nowait, fr)
                    break
                except Exception as e:                       # noqa: BLE001
                    # refused or dropped before any audio (429 at the concurrency limit,
                    # a 5xx): try again. once audio has gone out a retry would repeat words
                    retry = sent == 0 and getattr(e, "retryable", True) and attempt < 2
                    print(f"[relay] voice failed{', retrying' if retry else ''}: "
                          f"{type(e).__name__}: {e}", flush=True)
                    if not retry:
                        break
                    time.sleep(0.25 * (attempt + 1))
        finally:
            self.loop.call_soon_threadsafe(frames.put_nowait, None)

    async def _send_speech(self, frame: bytes) -> None:
        """Keep at most LEAD_S of speech ahead of the device's playhead."""
        while True:
            ahead = self.play_end - time.monotonic()
            if ahead <= config.LEAD_S:
                break
            await asyncio.sleep(min(0.02, ahead - config.LEAD_S + 0.001))
        now = time.monotonic()
        if self.play_end < now:
            self.play_end = now + config.PREBUFFER_S
        self.play_end += len(frame) / 2 / config.RATE
        self._put(b"\x01" + frame)

    def _on_playback(self, msg: dict) -> None:
        """The Lite's own count of what it has queued (firmware 0.3.2+, every 250 ms while he
        talks). If it stalled and restarted, its playhead is behind our clock: move ours back
        so we don't overfill it (a full buffer drops audio, which is the garbling)."""
        buf = msg.get("buf")
        if isinstance(buf, (int, float)) and self.play_end:
            self.play_end = max(self.play_end, time.monotonic() + buf / 1000.0)
        pb = getattr(self, "pb", None)
        if pb is None or self.state != "speaking":
            return
        if isinstance(buf, (int, float)) and msg.get("playing"):
            pb["min"] = buf if pb["min"] is None else min(pb["min"], buf)
        for k, key in (("under", "under"), ("over", "over")):
            v = msg.get(k)
            if isinstance(v, int):
                if pb.get(key + "0") is None:
                    pb[key + "0"] = v
                pb[key] = v

    def _log_playback(self) -> None:
        pb = getattr(self, "pb", None)
        if not pb or pb.get("min") is None:
            return
        self.pb = None
        under = (pb.get("under") or 0) - (pb.get("under0") or 0)
        over = (pb.get("over") or 0) - (pb.get("over0") or 0)
        rssi = (self.info.get("status") or {}).get("rssi")
        bad = under or over
        print(f"[relay] {self.device.get('name')}: playback {'GLITCHED' if bad else 'clean'}: "
              f"{under} dropouts, {over} ms dropped (buffer full), lowest buffer {int(pb['min'])} ms"
              f"{f', wifi {rssi} dBm' if rssi is not None else ''}", flush=True)

    async def _finish_playback(self, cancel) -> None:
        while not self._stale(cancel) and self.play_end - time.monotonic() > 0:
            await asyncio.sleep(0.05)

    # ---------- memory ----------
    def _fold_in_background(self, conversation: str) -> None:
        aid, brain, hub = self.aid, self.brain, self.hub

        def _go():
            m = meter.Meter()
            try:
                with meter.metering(m):
                    new = memory.fold_in(aid, conversation, brain.backend())
            except Exception as e:                               # noqa: BLE001
                print(f"[relay] bulletin fold failed: {e!r}", flush=True)
                return
            if m.usd > 0:
                accounts.charge(aid, m.usd, "memory: bulletin update")
            if new:
                hub.push_bulletin(aid)

        threading.Thread(target=_go, name="bulletin-fold", daemon=True).start()
