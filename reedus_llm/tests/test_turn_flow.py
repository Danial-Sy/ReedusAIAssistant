#!/usr/bin/env python3
"""end-to-end turn flow with a stub backend. no gguf, no camera, no network."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reedus_companion.serve import core as C   # noqa: E402

_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


class StubBackend:
    """Records every call and replies with canned text."""

    def __init__(self, reply: str = "Sure thing!"):
        self.calls: list[dict] = []
        self.reply = reply
        self.streamed: list[str] = []

    def chat(self, messages, max_tokens, use_adapter=True, temperature=None, on_token=None,
             tier=None, name="", cancel=None):
        self.calls.append({
            "system": (messages[0].get("content", "") if messages else ""),
            "last": (messages[-1].get("content", "") if messages else ""),
            "streamed": on_token is not None,
            "name": name,
            "tier": tier,
            "use_adapter": use_adapter,
        })
        if on_token is not None:
            for piece in self.reply.split(" "):
                self.streamed.append(piece)
                on_token(piece + " ")
        return self.reply


def make(reply="Sure thing!"):
    be = StubBackend(reply)
    r = C.Reedus(be)
    r.mem = _NullMemory()
    return r, be


class _NullMemory:
    """Memory stub, no sqlite, no embeddings, no onnx download."""
    embedder = None
    def read_bulletin(self, *a, **k): return ""
    def recall(self, *a, **k): return ""
    def add_turn(self, *a, **k): return None
    def close(self, *a, **k): return None


# --- failed look is honest ---
print("[1/4] a FAILED look must be admitted, not improvised")
r, be = make("I can't see anything right now, my camera isn't hooked up.")
statuses: list[str] = []
r.on_status = statuses.append
r._vision = False                       # bridge unavailable

answer = r.reply("what do you see")

sysmsgs = " ".join(c["last"] for c in be.calls)
check(len(be.calls) == 1, f"one model call, not a search pipeline (got {len(be.calls)})")
check("COULDN'T" in sysmsgs or "not available at all" in sysmsgs,
      "the model was told the camera failed")
check("do NOT describe or guess" in sysmsgs,
      "the model was explicitly forbidden from inventing a scene")
check(bool(statuses), f"an acknowledgement was emitted before looking: {statuses!r}")

# --- ack fires before work ---
print("\n[2/4] speak-first acknowledgement")
check(all(isinstance(s, str) and s for s in statuses), "status lines are non-empty strings")
seen = set()
for _ in range(12):
    seen.add(C._ack(C._LOOK_ACKS))
check(len(seen) > 1, f"acknowledgements vary rather than repeating one line ({len(seen)} distinct)")
prev = C._ack(C._SEARCH_ACKS)
repeats = sum(1 for _ in range(30) if (cur := C._ack(C._SEARCH_ACKS)) == prev or (prev := cur) and False)
check(repeats == 0, "never the same acknowledgement twice in a row")

# a hook that explodes must not kill the turn
r2, be2 = make()
r2._vision = False
def _boom(_): raise RuntimeError("ui exploded")
r2.on_status = _boom
try:
    r2.reply("what do you see")
    check(True, "a raising on_status hook does not break the turn")
except Exception as e:
    check(False, f"a raising on_status hook broke the turn: {e!r}")

# --- streaming policy ---
print("\n[3/4] streaming policy: everywhere EXCEPT when a verified fact is in play")
r3, be3 = make("Looks like a mug.")
r3._vision = False
r3.on_token = lambda t: None
r3.reply("what do you see")
check(be3.calls[-1]["streamed"], "vision turn streams")

r4, be4 = make("Not much, just hanging out")
r4.on_token = lambda t: None
r4.reply("hey whats up")
check(be4.calls[-1]["streamed"],
      "plain chit-chat STREAMS (this is most turns; withholding it looked broken)")

r5, be5 = make("Hello!")
r5.reply("hey whats up")
check(not be5.calls[-1]["streamed"], "no on_token hook -> never streams")

r6, be6 = make("It is Monday.")
r6.on_token = lambda t: None
r6.reply("what day is it today")
check(not be6.calls[-1]["streamed"],
      "a grounded turn does NOT stream (digit guard may regenerate it)")

# --- no regressions ---
print("\n[4/4] a failed look must not turn into a web search")
r7, be7 = make("no eyes right now")
r7._vision = False
searched = {"v": False}
r7.reply("what do you see")
check(not any("web search" in c["last"].lower() for c in be7.calls),
      "no search grounding was injected on a failed look")

# --- spoken text must match the cleaned text ---
print("\n[5/5] streamed speech says the SCRUBBED sentence, not the raw draft")
import numpy as np                                                        # noqa: E402


class _FakeOutputStream:
    def __init__(self, samplerate=None, channels=1, dtype="float32",
                 device=None, blocksize=0):
        self.writes = []
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        _FakeSd.streams.append(self)

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1

    def close(self):
        self.close_calls += 1

    def write(self, data):
        self.writes.append(np.asarray(data))


class _FakeSd:
    streams: list = []
    play_calls = 0

    class _Default:
        device = [0, 0]
    default = _Default()

    @staticmethod
    def play(*_a, **_k):
        _FakeSd.play_calls += 1

    @staticmethod
    def wait():
        pass

    @staticmethod
    def query_devices(_dev=None):
        return {"default_samplerate": 24000}

    OutputStream = _FakeOutputStream


sys.modules["sounddevice"] = _FakeSd
from reedus_companion.voice.stream_speaker import StreamSpeaker            # noqa: E402
from reedus_companion.voice.tts import PlaybackStream                     # noqa: E402


class _FakeTTS:
    sample_rate = 24000
    def __init__(self): self.said = []
    def synth(self, t):
        self.said.append(t)
        return np.zeros(240, dtype="float32")


RAW = "Hey hey, what’s good. Name check, yourself, right?"
FINAL = C._clean_reply(RAW)
check("Name check" not in FINAL, "the scrubber removes the tic from the text")

_tts = _FakeTTS()
_sp = StreamSpeaker(_tts, scrub=C.scrub)
for _tok in RAW.split(" "):
    _sp.feed(_tok + " ")
_sp.finish(RAW)
_spoken = " ".join(_tts.said)
check("Name check" not in _spoken, f"...and from the SPEECH too (spoke: {_spoken!r})")
check("what" in _spoken and "good" in _spoken, "the good part still gets spoken")

check(C.scrub("Name check, yourself, right?").strip() == "",
      "a sentence that is entirely a tic scrubs to nothing (stays silent)")
check(C.scrub("Hey hey, what’s good.") != "", "a clean sentence survives scrubbing")

# --- the ack must not suppress the actual reply ---
print("\n[6/6] a spoken acknowledgement is not a reply")
_tts2 = _FakeTTS()
_sp2 = StreamSpeaker(_tts2, scrub=C.scrub)
_sp2.say("hang on, let me look that up")          # grounded turn, ack spoken, nothing streamed
_sp2.finish("Spain won it, 1-0 over Argentina.")
_said = " ".join(_tts2.said)
check("Spain" in _said, f"the verified answer IS spoken after the ack (said: {_tts2.said})")
check("hang on" in _said, "the ack was spoken too")

_tts3 = _FakeTTS()
_sp3 = StreamSpeaker(_tts3, scrub=C.scrub)
for _tok in "All good here. Just chilling. ".split(" "):
    _sp3.feed(_tok + " ")
_sp3.finish("All good here. Just chilling.")
check(sum("chilling" in s.lower() for s in _tts3.said) == 1,
      f"a streamed reply is not spoken twice (said: {_tts3.said})")

print("\n[6b] deferred synth: Kokoro waits until finish()")
_tts_d = _FakeTTS()
_sp_d = StreamSpeaker(_tts_d, scrub=C.scrub, overlap=False)
for _tok in "Hey there buddy. What's good? ".split(" "):
    _sp_d.feed(_tok + " ")
check(_tts_d.said == [], f"no synth during feed when overlap=False (said={_tts_d.said})")
_sp_d.finish("Hey there buddy. What's good?")
check(len(_tts_d.said) >= 1, f"synth runs in finish() (said={_tts_d.said})")
check("Hey" in " ".join(_tts_d.said), "deferred sentences still speak")

_tts_o = _FakeTTS()
_sp_o = StreamSpeaker(_tts_o, scrub=C.scrub, overlap=True)
for _tok in "Hey there buddy. What's good? ".split(" "):
    _sp_o.feed(_tok + " ")
import time as _time
_time.sleep(0.15)          # workers need a beat
check(len(_tts_o.said) >= 1, f"overlap=True synths during feed (said={_tts_o.said})")
_sp_o.finish("Hey there buddy. What's good?")

print("\n[7/7] greeting: absence tic is silent, Hey is spoken")
GREET = "Hey, what's going on? Been a while! How's life treatin' ya? 🙌"
GREET_FINAL = C._clean_reply(GREET, personal=True)
check("Been a while" not in GREET_FINAL,
      f"absence clause is scrubbed from the final text ({GREET_FINAL!r})")
check("🙌" not in GREET_FINAL, "emoji is stripped from the final text")
check("Hey" in GREET_FINAL and "going on" in GREET_FINAL,
      f"Hey and the check-in survive ({GREET_FINAL!r})")

_tts4 = _FakeTTS()
_sp4 = StreamSpeaker(_tts4, scrub=lambda t: C.scrub(t, personal=True))
for _tok in ["Hey", " —", " what's", " going", " on?", " Been", " a", " while!",
             " How's", " life", " treatin'", " ya?", " 🙌"]:
    _sp4.feed(_tok)
_sp4.finish(GREET)
_gsaid = " ".join(_tts4.said)
check("been a while" not in _gsaid.lower(),
      f"absence clause is not spoken ({_gsaid!r})")
check("Hey" in _gsaid, f"Hey is queued for speech ({_gsaid!r})")
check("going on" in _gsaid.lower(), f"the check-in is spoken ({_gsaid!r})")

print("\n[8/8] USB playback stays open across sentences")
_FakeSd.streams.clear()
_FakeSd.play_calls = 0
_pb = PlaybackStream(24000)
_pb.write(np.ones(80, dtype="float32"))
_pb.write(np.ones(80, dtype="float32") * 0.5)
_pb.close()
check(_FakeSd.play_calls == 0, "PlaybackStream does not call sd.play")
check(len(_FakeSd.streams) == 1, f"one OutputStream for two clips, got {len(_FakeSd.streams)}")
_st = _FakeSd.streams[0]
check(_st.start_calls == 1, f"DAC started once, not per sentence (start={_st.start_calls})")
_nonsilent = [w for w in _st.writes if np.max(np.abs(w)) > 0.01]
check(len(_nonsilent) == 2, f"both clips written to the same stream ({len(_nonsilent)} audible writes)")
check(_st.close_calls == 1, "stream closed at end of turn")

print("\n[9/9] a SUCCESSFUL look must forbid inventing attributes")
r8, be8 = make("I see a person in the center.")
class _FakeVision:
    def grounding_for(self, _msg):
        return {
            "count": 1,
            "grounding": "Most prominent: a person (center), high confidence.",
            "most_prominent": "person",
            "groups": ["general_common"],
            "text": "I can see a person (center).",
        }
r8._vision = _FakeVision()
r8.reply("what do you see right now")
look_msg = " ".join(c["last"] for c in be8.calls)
check("I can see a person (center)." in look_msg or "a person (center)" in look_msg,
      "detector inventory was injected for the model")
check("Do NOT invent" in look_msg or "do NOT invent" in look_msg,
      "fabrication of clothing/scene details is forbidden")
check("stick to the detector" in look_msg.lower() or "stop there" in look_msg.lower(),
      "model told to speak only listed labels")
check(not any("web search" in c["last"].lower() for c in be8.calls),
      "successful look does not web-search")

print("\n[10/10] reset after barge stop must not leave a poisoned queue")
_tts_b = _FakeTTS()
_sp_b = StreamSpeaker(_tts_b, scrub=C.scrub, overlap=False)
_sp_b.finish("First line spoken fully.")
_time.sleep(0.05)
_sp_b.stop()                              # injects None sentinels
_sp_b.reset()                             # must recreate queues
_tts_b.said.clear()
ok = _sp_b.finish("Second reply after the interrupt.")
_time.sleep(0.1)
check(ok, "finish after stop+reset reports spoken")
check(any("Second reply" in s for s in _tts_b.said),
      f"second turn is spoken after barge (said={_tts_b.said})")

print("\n[11] spoken 'what you see' is a look turn (no LLM look-router / no web)")
r9, be9 = make("I can't see anything right now, my camera isn't hooked up.")
r9._vision = False
r9.reply("Awesome, and can you tell me what you see right now?")
names9 = [c["name"] for c in be9.calls]
check("look-router" not in names9,
      f"LOOK_Q fast-path skips look-router (names={names9})")
check("search-router" not in names9,
      f"look turn never opens search-router (names={names9})")
check(any("COULDN'T" in c["last"] or "not available at all" in c["last"] for c in be9.calls),
      "failed look grounding still injected")

print("\n[12] think-barge cancel aborts mid-search before persona")
import threading
from unittest.mock import patch

class _SearchStub(StubBackend):
    def chat(self, messages, max_tokens, use_adapter=True, temperature=None, on_token=None,
             tier=None, name="", cancel=None):
        self.calls.append({
            "system": (messages[0].get("content", "") if messages else ""),
            "last": (messages[-1].get("content", "") if messages else ""),
            "streamed": on_token is not None,
            "name": name,
            "tier": tier,
            "use_adapter": use_adapter,
        })
        if name == "search-router":
            return '{"search": true, "query": "Spain Portugal final score"}'
        if name == "extract":
            return "NOT FOUND"
        if on_token is not None:
            on_token(self.reply)
        return self.reply

be12 = _SearchStub("should never speak this")
r12 = C.Reedus(be12)
r12.mem = _NullMemory()
cancel12 = threading.Event()
r12._cancel = cancel12

def _fake_web(q, max_results=5):
    cancel12.set()          # barge while the network call is "in flight"
    return "1. junk, junk title with no score"

with patch("reedus_companion.serve.core.web_search", _fake_web):
    with patch("reedus_companion.serve.core.PAGE_FETCH", 0):
        out12 = r12.reply("what was the Spain Portugal final score yesterday")
check(out12 == "", f"cancelled search returns empty (got {out12!r})")
check(r12.cancelled, "cancelled flag set after barge during search")
check("persona" not in [c["name"] for c in be12.calls],
      f"persona skipped after cancel (names={[c['name'] for c in be12.calls]})")

print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("ALL PASS")
