"""one connected Reedus Lite: the conversation loop that used to run on the Xeon.

    idle ──"hey reedus"──► listening ──pause──► thinking ──► speaking ──► listening (follow-up)
      ▲                        │ nothing said                    │ "hey reedus" = barge-in
      └────────────────────────┴──────── goodbye / silence ◄─────┘

audio up: the S3 sends 20 ms frames only while its speech gate is open, or while the relay has
said `listen` (a conversation is going). audio down: speech frames paced against the device's
playhead, exactly like the run box hub (ReedusLite/PROTOCOL.md), so barge-in stays instant.

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

from reedus_relay import accounts, config, memory, meter, speech
from reedus_relay.brain import Brain, is_goodbye

IDLE, LISTEN, BUSY = "idle", "listen", "busy"
FRAME_S = config.FRAME_SAMPLES / config.RATE
OUT_OF_CREDIT = ("I'm out of cloud credit, so I can't think right now. "
                 "Top up the pool in ReedOS and I'm all yours.")
TROUBLE = "Sorry, my brain hiccuped there. Try me again in a second."
_WAKE_PREFIX = re.compile(r"^\W*(?:hey|hi|okay|ok|yo)?[\s,]*(?:reedus|reed us|reedis|redis)\b[\s,.!?]*", re.I)


def strip_wake(said: str) -> str:
    return _WAKE_PREFIX.sub("", said or "").strip()


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
        self.wake = wake if wake is not None else speech.wake_detector()
        self.ep: Optional[speech.Endpointer] = None
        self.cancel = threading.Event()
        self.task: Optional[asyncio.Task] = None
        self.play_end = 0.0
        self.last_frame = time.monotonic()
        self.conversation = uuid.uuid4().hex
        self.turns_in_conv = 0
        self.empty_turns = 0
        self.info: dict = {}
        self.voice_factory = voice_factory
        self.stt = stt or speech.transcribe
        self._last_clock = self._last_timers = 0.0
        self.closed = False

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
                   "account": self.account.get("name", ""), "device": self.device.get("name", "")})
        self.set_state("idle")
        self.listen(False)
        self.send_clock()
        self.send_timers()
        _, rev = await asyncio.to_thread(memory.bulletin, self.aid)
        if rev and int(self.hello.get("bulletin_rev") or 0) != rev:
            await asyncio.to_thread(self.send_bulletin)
        if not await asyncio.to_thread(accounts.funded, self.aid):
            self.send({"type": "glance", "card": "text", "text": "TOP UP IN REEDOS", "hold": 20})

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
                self.end_conversation()

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
            if ev in ("end", "max"):
                self.begin_turn(self.ep.audio())
            elif ev == "timeout":
                self.end_conversation()
            return
        # idle, or busy thinking/speaking: only the wake word matters (barge-in)
        if self.wake.feed(pcm):
            self.on_wake()

    def on_text(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "gate":
            if msg.get("open") and self.mode == IDLE:
                self.wake.reset()
                if self.wake.gate_opened():
                    self.on_wake()
        elif t in ("status", "battery", "doa", "music"):
            self.info[t] = msg
        elif t == "bulletin_ack":
            self.info["bulletin_rev"] = msg.get("rev")

    def on_wake(self) -> None:
        if self.assist.ringing():
            self.assist.hush()                 # the name quiets a ring, "stop"/"snooze" follow
        if self.mode == BUSY:
            self.barge()
        self.send({"type": "wake"})
        self.start_listening(follow_up=False)

    # ---------- conversation ----------
    def start_listening(self, follow_up: bool) -> None:
        self.mode = LISTEN
        self.ep = speech.Endpointer(start_timeout=config.FOLLOW_UP_S if follow_up
                                    else config.START_TIMEOUT_S)
        self.last_frame = time.monotonic()
        self.listen(True)
        self.set_state("listening")

    def end_conversation(self) -> None:
        self.mode = IDLE
        self.ep = None
        self.listen(False)
        self.set_state("idle")
        self.wake.reset()
        if self.turns_in_conv:
            self._fold_in_background(self.conversation)
        self.brain.new_conversation()
        self.conversation = uuid.uuid4().hex
        self.turns_in_conv = 0
        self.empty_turns = 0

    def barge(self) -> None:
        self.cancel.set()
        self.send({"type": "flush"})
        self.play_end = 0.0
        self.mode = IDLE

    def begin_turn(self, audio: bytes) -> None:
        self.mode = BUSY
        self.ep = None
        self.cancel = threading.Event()
        self.set_state("thinking")
        self.task = asyncio.ensure_future(self.turn(audio, self.cancel))

    def _stale(self, cancel: threading.Event) -> bool:
        return cancel.is_set() or cancel is not self.cancel or self.closed

    async def turn(self, audio: bytes, cancel: threading.Event) -> None:
        m = meter.Meter()
        said, reply = "", ""
        try:
            if not await asyncio.to_thread(accounts.funded, self.aid):
                await self.speak_lines([OUT_OF_CREDIT], m, cancel)
                await self._finish_playback(cancel)
                if not self._stale(cancel):
                    self.end_conversation()
                return

            def _hear():
                with meter.metering(m):
                    return self.stt(audio)
            said = strip_wake(await asyncio.to_thread(_hear))
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
            from reedus_companion.lite import emotion
            react = emotion.react(said)
            if react:
                self.send({"type": "expr", "name": react, "hold": 1.2})

            sentences: asyncio.Queue = asyncio.Queue()

            def _on_sentence(s: str) -> None:
                self.loop.call_soon_threadsafe(sentences.put_nowait, s)

            def _think() -> str:
                with meter.metering(m):
                    try:
                        return self.brain.reply(said, self.conversation, _on_sentence, cancel)
                    finally:
                        self.loop.call_soon_threadsafe(sentences.put_nowait, None)

            think = asyncio.ensure_future(asyncio.to_thread(_think))
            await self.speak_stream(sentences, m, cancel)
            reply = await think
            if self._stale(cancel):
                return
            await self._finish_playback(cancel)
            if self._stale(cancel):
                return
            self.turns_in_conv += 1
            if is_goodbye(said):
                self.send({"type": "expr", "name": "bye", "hold": 2.0})
                self.end_conversation()
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
            if said and reply:
                await asyncio.to_thread(memory.record_turn, self.aid, self.did, self.conversation,
                                        said, reply, usd)

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
                frames: asyncio.Queue = asyncio.Queue()
                task = asyncio.ensure_future(asyncio.to_thread(self._synth, s, frames, m, cancel))
                await pending.put((s, frames, task))

        async def play():
            first = True
            while True:
                item = await pending.get()
                if item is None:
                    return
                s, frames, task = item
                emo, strength = emotion.classify(s)
                if emo != "neutral":
                    self.send({"type": "emotion", "emotion": emo, "strength": strength,
                               "hold": emotion.speech_seconds(s)})
                while True:
                    fr = await frames.get()
                    if fr is None:
                        break
                    if self._stale(cancel):
                        continue
                    if first:
                        self.set_state("speaking")
                        first = False
                    await self._send_speech(fr)
                await task

        await asyncio.gather(dispatch(), play())

    def _synth(self, text: str, frames: asyncio.Queue, m, cancel) -> None:
        try:
            with meter.metering(m):
                for fr in self.voice_factory().frames(text):
                    if self._stale(cancel):
                        break
                    self.loop.call_soon_threadsafe(frames.put_nowait, fr)
        except Exception as e:                                   # noqa: BLE001
            print(f"[relay] voice failed: {type(e).__name__}: {e}", flush=True)
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
        self.play_end += FRAME_S
        self._put(b"\x01" + frame)

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
