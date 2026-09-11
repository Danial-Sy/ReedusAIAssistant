#!/usr/bin/env python3
"""barge / cancel plumbing regressions. no mic, no gguf, no audio device."""
from __future__ import annotations
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_fails = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


# [1]
print("[1/8] speaking() tracks audio, not the player thread's lifetime")
from reedus_companion.voice.stream_speaker import StreamSpeaker


class _FakeTTS:
    sample_rate = 24000

    def synth(self, text):
        import numpy as np
        return np.zeros(int(self.sample_rate * 0.05), dtype=np.float32)


class _NoAudioSpeaker(StreamSpeaker):
    """StreamSpeaker with the DAC replaced, the queue/accounting logic is what's under test."""

    def _play_loop(self):
        import queue as _q
        self._pb = object()
        try:
            while not self._abort.is_set():
                try:
                    audio = self._audio_q.get(timeout=0.02)
                except _q.Empty:
                    continue
                if audio is None:
                    return
                self._mark_pending(-1)
        finally:
            self._pb = None


sp = _NoAudioSpeaker(_FakeTTS(), scrub=None)
check(not sp.speaking(), "idle speaker is not speaking")
sp.say("gimme a sec, looking that up now")
check(sp.speaking(), "speaking() True while the ack is queued")
deadline = time.monotonic() + 5.0
while sp.speaking() and time.monotonic() < deadline:
    time.sleep(0.02)
check(not sp.speaking(),
      "speaking() False once the ack drains, this is what re-arms think-barge mid-search")
check(sp._pb is not None, "the player thread is still alive (playing() would still say busy)")
sp.stop()

# [2]
print("\n[2/8] ThinkCancelWatcher prefers speaking() over playing()/DAC lock")
from reedus_companion.voice.barge import ThinkCancelWatcher


class _StuckSpeaker:
    """The old shape: player thread open all turn, nothing actually sounding."""
    def playing(self):
        return True

    def speaking(self):
        return False


w = ThinkCancelWatcher(threading.Event(), speaker=_StuckSpeaker())
check(not w._speaker_busy(), "open-but-silent speaker no longer counts as busy")


class _TalkingSpeaker(_StuckSpeaker):
    def speaking(self):
        return True


check(ThinkCancelWatcher(threading.Event(), speaker=_TalkingSpeaker())._speaker_busy(),
      "audio genuinely on the wire still counts as busy (no self-cancel on ack bleed)")
check(not ThinkCancelWatcher(threading.Event(), speaker=None)._speaker_busy(),
      "no speaker -> not busy")

# [3]
print("\n[3/8] a wake address mid-utterance keeps the command after it")
from reedus_companion.voice.wake import classify_utterance

check(classify_utterance("Hey there Reedus, what do you see?") == ("wake_cmd", "what do you see?"),
      "'Hey there Reedus, what do you see?' -> wake+command (was: armed, question dropped)")
check(classify_utterance("anyway yeah so Reedus what is the weather")[0] == "wake_cmd",
      "filler the wake list doesn't know still yields a command")
check(classify_utterance("So now I can see. Hey Reedus?") == ("wake_only", ""),
      "trailing bare address still arms only, preamble is not a command")
check(classify_utterance("Hey Reedus, what do you see?")[0] == "wake_cmd",
      "the leading-wake case is unchanged")

# [4]
print("\n[4/8] fact-machinery suppression survives the corpus-skip decision")
import inspect
from reedus_companion.serve import core as C

src = inspect.getsource(C.Reedus.reply)
check(src.count("personal = ") == 1,
      "reply() assigns `personal` exactly once (no shadowing of _suppress_fact_machinery)")
check("conv_turn = " in src and "conv_turn=conv_turn" in src,
      "corpus-skip uses its own narrow predicate")
check("conv_turn=personal" not in src and "_corpus_verdict(" in src,
      "the broad _suppress_fact_machinery verdict is not what gates the corpus")
check(C._suppress_fact_machinery("you remember when naruto and sasuke died?"),
      "reminiscing still suppresses fact machinery")
check(C._suppress_fact_machinery("don't search that, just tell me"),
      "an explicit no-search veto still suppresses fact machinery")

# [5]
print("\n[5/8] router JSON extraction does not fail closed on nesting / junk")
check([o for o in C._json_objects('noise {"look": true, "meta": {"a": 1}} tail')],
      "balanced scanner finds an object with a nested one")
obj = C._parse_router_json('{"search": true, "query": "who won {the} final"}')
check(obj is not None and obj.get("search") is True,
      "a brace inside the query no longer truncates the parse")
check(C._parse_router_json('{"verify": true}') is None,
      "an object without the required key is still a failure")


class _Raw:
    """Backend that returns one canned router reply."""
    def __init__(self, raw):
        self.raw = raw

    def chat(self, *a, **k):
        return self.raw

    def close(self):
        pass


def _look(raw):
    r = C.Reedus.__new__(C.Reedus)
    r.history = []
    r._cancel = None
    r.backend = _Raw(raw)
    return C.Reedus._route_look(r, "what am I holding")


check(_look('{"look": true}') is True, "clean JSON -> look")
check(_look('{"look": true, "why": {"cue": "holding"}}') is True,
      "nested object -> look (was: parse fail -> no camera)")
check(_look('here you go: {"look": true') is True,
      "malformed wrapper around a real verdict is recovered, not swallowed")
check(_look('{"look": false}') is False, "an explicit no is still a no")
check(_look('I think you want the web') is False, "genuine non-answer still fails closed")


# [6]
print("\n[6/8] think-barge captures the interrupting utterance")
import types
import numpy as np
from reedus_companion import config as _cfg

_SR = int(_cfg.MIC_SAMPLE_RATE)
_BLK = int(_SR * 0.05)
MARK = 0.9
LOUD = 0.2          # above REEDUS_THINK_BARGE_RMS
_scripted = ([np.zeros(_BLK, dtype=np.float32)] * 5
             + [np.full(_BLK, MARK, dtype=np.float32)]
             + [np.full(_BLK, LOUD, dtype=np.float32)] * 9
             + [np.zeros(_BLK, dtype=np.float32)] * 40)


class _FakeInputStream:
    """Scripted mic driven through PortAudio's CALLBACK contract, like the real one."""
    def __init__(self, blocks, callback):
        self._blocks, self._cb = list(blocks), callback
        self._stop = threading.Event()
        self._t = None

    def start(self):
        def _pump():
            i = 0
            while not self._stop.is_set():
                b = self._blocks[i] if i < len(self._blocks) else np.zeros(_BLK, dtype=np.float32)
                i += 1
                try:
                    self._cb(b.reshape(-1, 1), len(b), None, None)
                except Exception:
                    return
                time.sleep(0.001)
        self._t = threading.Thread(target=_pump, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()

    def close(self):
        self._stop.set()


def _run_watcher(capture: str):
    import os as _os
    _os.environ["REEDUS_THINK_BARGE_DELAY"] = "0"
    _os.environ["REEDUS_THINK_BARGE_RMS"] = "0.05"
    _os.environ["REEDUS_THINK_BARGE_SILENCE_SEC"] = "0.2"
    _os.environ["REEDUS_THINK_BARGE_CAPTURE"] = capture
    fake = types.ModuleType("sounddevice")
    fake.InputStream = lambda **kw: _FakeInputStream(_scripted, kw["callback"])
    saved = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = fake
    try:
        import reedus_companion.voice.barge as B
        ev = threading.Event()
        w = B.ThinkCancelWatcher(ev, speaker=None)
        w.start()
        ev.wait(timeout=10.0)
        w.stop()          # joins, waiting out the capture
        return ev, w
    finally:
        if saved is None:
            sys.modules.pop("sounddevice", None)
        else:
            sys.modules["sounddevice"] = saved


ev, w = _run_watcher("1")
check(ev.is_set(), "sustained speech during think sets the cancel Event")
check(w.hit, "watcher reports the hit")
check(w.audio is not None and w.audio.size > 0, "the interrupting utterance was captured")
if w.audio is not None and w.audio.size:
    check(float(np.max(np.abs(w.audio))) >= MARK - 1e-6,
          "capture includes the FIRST speech block (pre-roll reached back before the cut)")
    check(w.audio.size >= _BLK * 6,
          f"capture continued past the cut ({w.audio.size // _BLK} blocks)")

ev2, w2 = _run_watcher("0")
check(ev2.is_set() and w2.hit, "REEDUS_THINK_BARGE_CAPTURE=0 still cancels")
check(w2.audio is None, "...and captures nothing (stop-only, the old behaviour)")

# [7]
print("\n[7/8] an interruption runs as the next turn")
from reedus_companion.voice import voice_loop as VL
from reedus_companion.voice.session import WakeSession


class _FakeSTT:
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio):
        return self.text


class _Brain:
    def __init__(self):
        self.history = []


def _drive(said, pop_assistant, history=None):
    calls = []
    orig = VL._do_turn
    VL._do_turn = lambda r, sp, t, b, ot: (calls.append(t), ("ok", 0.1, 0.1, None))[1]
    try:
        brain = _Brain()
        brain.history = list(history or [])
        sess = WakeSession()
        sess.active = True
        out = VL._consume_interruption(
            brain, None, _FakeSTT(said), sess, "server", None,
            np.ones(_BLK, dtype=np.float32), False, "interrupted",
            pop_assistant=pop_assistant)
        return out, calls, brain, sess
    finally:
        VL._do_turn = orig


out, calls, _b, _s = _drive("actually just tell me the weather", False)
check(out == "done" and calls == ["actually just tell me the weather"],
      "the replacement instruction becomes the next turn (no repeating yourself)")

out, calls, _b, sess = _drive("goodbye", False)
check(out == "sleep" and not calls and not sess.active,
      "an interruption that says goodbye sleeps instead of answering")

out, calls, _b, _s = _drive("", False)
check(out == "done" and not calls,
      "energy with no words is a false barge, not a turn")

_hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "half-spoken"}]
_o, _c, brain, _s = _drive("wait what", True, _hist)
check(len(brain.history) == 1,
      "speak-barge pops the half-spoken assistant turn")
_o, _c, brain, _s = _drive("wait what", False, _hist)
check(len(brain.history) == 2,
      "think-cancel does NOT pop, a cancelled reply never reached history")

check(VL._consume_interruption.__doc__ and "listen jingle fires FIRST"
      in VL._consume_interruption.__doc__,
      "the listen cue is documented as preceding STT")


# [8]
print("\n[8/8] 'never mind' cancels the request, not the session")
from reedus_companion.voice.wake import classify_utterance as _cls, is_dismiss, is_stop

check(_cls("Hey Reedus, never mind.")[0] == "dismiss",
      "'Hey Reedus, never mind' dismisses (was: matched the stop list and SLEPT)")
for phrase in ("never mind", "nevermind", "forget it", "cancel that", "drop it",
               "I figured it out"):
    check(is_dismiss(phrase), f"dismiss: {phrase!r}")
for phrase in ("goodbye", "see ya", "go to sleep", "gotta go"):
    check(is_stop(phrase) and not is_dismiss(phrase), f"still a real goodbye: {phrase!r}")
check(_cls("never mind the weather what about naruto")[0] == "turn",
      "'never mind X, what about Y' stays a turn")
check(_cls("forget it what about the other one")[0] == "turn",
      "content after the dismissal keeps it a turn")

_s = WakeSession()
_s.active = True
_ev = _s.on_transcript("Hey Reedus, never mind.")
check(_ev.action == "arm" and _s.active,
      "a dismissal keeps the session OPEN and re-arms")
check(_ev.jingle == "ready", "...with an audible cue that he's still listening")
_ev2 = _s.on_transcript("goodbye")
check(_ev2.action == "stop" and not _s.active, "goodbye still sleeps")

_idle = WakeSession()
check(_idle.on_transcript("never mind").action == "ignore" and not _idle.active,
      "a dismissal while idle does not wake him")

_o, _c, _b, _sess = _drive("never mind", False)
check(_o == "sleep" and not _c and _sess.active,
      "interrupting with 'never mind' runs no new turn and stays awake")

print()
if _fails:
    print(f"{len(_fails)} FAILED")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
