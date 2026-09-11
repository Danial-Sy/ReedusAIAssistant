#!/usr/bin/env python3
"""wake-word + session pipeline. no mic, no gguf."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reedus_companion.voice.stt import canon_name                          # noqa: E402
from reedus_companion.voice.wake import (                                 # noqa: E402
    classify_utterance, has_wake, is_stop, is_wake_only, strip_wake,
)
from reedus_companion.voice.session import WakeSession                    # noqa: E402
from reedus_companion.voice.jingle import make_jingle                     # noqa: E402

_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


print("[1/5] wake phrases (incl. Whisper misspellings → Reedus)")
for raw, want_wake in [
    ("Hey Reedus", True),
    ("hey reedus", True),
    ("Reedus", True),
    ("yo Reedus what's up", True),
    ("Okay Reedus, how are you", True),
    ("Oh, hey Reedus.", True),       # filler before hey
    ("oh hey Reedus", True),
    ("Ah, Reedus", True),
    ("I'll hear Reedus.", True),     # whisper garble of Hey Reedus
    ("I hear Reedus", True),
    ("here Reedus", True),
    ("Hey breeders", True),          # canon → Reedus
    ("hey readers what's good", True),
    ("what's going on", False),
    ("talking to you my guy", False),
    ("I've been talking to some readers about the book", False),
]:
    text = canon_name(raw)
    check(has_wake(text) is want_wake, f"has_wake({raw!r}) → {text!r} want {want_wake}")

check(is_wake_only(canon_name("Hey Reedus")), "bare Hey Reedus is wake-only")
check(is_wake_only(canon_name("Reedus!")), "bare Reedus is wake-only")
check(is_wake_only(canon_name("Reedus?")), "Reedus? is wake-only (not a '?' command)")
check(is_wake_only(canon_name("Hey Reedus??")), "Hey Reedus?? is wake-only")
check(is_wake_only(canon_name("Oh, hey Reedus.")), "Oh, hey Reedus. is wake-only")
check(is_wake_only(canon_name("I'll hear Reedus.")), "I'll hear Reedus. is wake-only")
check(not is_wake_only(canon_name("Hey Reedus what's up")),
      "wake+command is not wake-only")
check(classify_utterance(canon_name("Reedus?"))[0] == "wake_only",
      "classify Reedus? → wake_only")
check(strip_wake(canon_name("Reedus?")) == "", "strip Reedus? → empty")
s_q = WakeSession()
ev_q = s_q.on_transcript(canon_name("Reedus?"))
check(ev_q.action == "arm" and ev_q.jingle == "listen" and not ev_q.text,
      f"Reedus? arms and does NOT run a turn (got {ev_q})")
_stripped = strip_wake(canon_name("Hey Reedus what's up"))
check(_stripped == "what's up", f"strip wake → {_stripped!r}")
check(strip_wake(canon_name("yo Reedus, how was your day")) == "how was your day",
      "strip yo Reedus comma")
check(strip_wake(canon_name("Oh, hey Reedus, what's up")) == "what's up",
      "strip Oh, hey Reedus")

k_pre, p_pre = classify_utterance(canon_name("So now I can see. Hey Reedus?"))
check(k_pre == "wake_only" and p_pre == "",
      f"trailing wake arms only (got {k_pre!r} {p_pre!r})")
s_pre = WakeSession()
ev_pre = s_pre.on_transcript(canon_name("So now I can see. Hey Reedus?"))
check(ev_pre.action == "arm" and not ev_pre.text,
      f"trailing Hey Reedus? arms, no turn (got {ev_pre})")
check("ignored talk before wake" in (ev_pre.note or ""),
      f"note explains preamble drop (got {ev_pre.note!r})")
from reedus_companion.voice.wake import from_wake_onward
check(from_wake_onward(canon_name("So now I can see. Hey Reedus?"))
      in ("Hey Reedus?", "hey Reedus?"),
      f"from_wake_onward drops preamble "
      f"({from_wake_onward(canon_name('So now I can see. Hey Reedus?'))!r})")
check(from_wake_onward("what's going on") == "", "no wake → empty gate")

# Idle: "Oh, hey Reedus." must arm, never ignore
s_oh = WakeSession()
ev_oh = s_oh.on_transcript(canon_name("Oh, hey Reedus."))
check(ev_oh.action == "arm" and ev_oh.jingle == "listen" and s_oh.active,
      f"Oh, hey Reedus. arms session (got {ev_oh})")
print("\n[2/5] stop / sleep signals")
for raw in [
    "goodbye", "Goodbye Reedus", "hey Reedus goodbye", "go to sleep",
    "stop listening", "that's all", "Reedus go to sleep", "bye",
    "Oh, goodbye.",          # live Xeon miss
    "oh goodbye",
    "Alright, bye",
    "okay then goodbye",
    "yeah see ya",
    "gotta go",
]:
    check(is_stop(canon_name(raw)), f"stop: {raw!r}")
for raw in ["what's up", "Hey Reedus what's up", "I'm done with that arc",
            "bye for now let's keep talking",  # trailing content, not a clean stop
            ]:
    if raw == "bye for now let's keep talking":
        check(not is_stop(raw), f"not stop (trailing talk): {raw!r}")
        continue
    check(not is_stop(canon_name(raw)), f"not stop: {raw!r}")
check(is_stop("i'm done"), "bare i'm done stops")
check(is_stop("I'm done."), "I'm done. stops")

# "Oh, goodbye." must sleep, never become a corpus turn
s_bye = WakeSession()
s_bye.on_transcript("Hey Reedus")
ev_bye = s_bye.on_transcript("Oh, goodbye.")
check(ev_bye.action == "stop" and ev_bye.jingle == "sleep" and not s_bye.active,
      f"Oh, goodbye. sleeps the session (got {ev_bye})")

print("\n[3/5] classify_utterance")
check(classify_utterance("hey there") == ("idle_noise", "hey there")
      or classify_utterance("hey there")[0] == "turn",
      f"no reedus → not wake ({classify_utterance('hey there')})")
k, p = classify_utterance(canon_name("Hey Reedus"))
check(k == "wake_only", f"Hey Reedus → wake_only (got {k})")
k, p = classify_utterance(canon_name("Hey Reedus what's up"))
check(k == "wake_cmd" and "what's up" in p, f"wake_cmd payload={p!r}")
k, p = classify_utterance("goodbye")
check(k == "stop", f"goodbye → stop (got {k})")

print("\n[4/5] full Alexa session state machine")
s = WakeSession(max_empty=2)
ev = s.on_transcript("what's going on")
check(ev.action == "ignore" and not s.active, "idle ignores non-wake")
ev = s.on_transcript(canon_name("Hey breeders"))
check(ev.action == "arm" and ev.jingle == "listen" and s.active,
      f"wake-only arms session (action={ev.action}, jingle={ev.jingle})")
ev = s.on_transcript("what's going on")
check(ev.action == "turn" and ev.text == "what's going on",
      f"in-session turn (got {ev})")
ev = s.after_reply()
check(ev.jingle == "ready", f"after reply → ready jingle ({ev.jingle})")
ev = s.on_transcript("I'm just talking to you my guy")
check(ev.action == "turn", "chitchat is a normal turn")
ev = s.on_transcript("goodbye")
check(ev.action == "stop" and ev.jingle == "sleep" and not s.active,
      f"goodbye sleeps (got {ev})")
ev = s.on_transcript("still here?")
check(ev.action == "ignore", "after sleep, non-wake ignored")

# wake + command in one breath
s2 = WakeSession()
ev = s2.on_transcript(canon_name("Hey Reedus, how was your day"))
check(ev.action == "turn" and "how was your day" in ev.text and ev.jingle == "listen",
      f"wake+cmd strips and runs (got {ev})")
check(s2.active, "session active after wake+cmd")
ev = s2.on_transcript(canon_name("Reedus go to sleep"))
check(ev.action == "stop" and not s2.active, "Reedus go to sleep ends session")

# empty timeout
s3 = WakeSession(max_empty=2)
s3.on_transcript("Hey Reedus")
s3.on_transcript("")
ev = s3.on_transcript("")
check(ev.action == "stop" and not s3.active, "two empties → auto sleep")

print("\n[5/5] jingles are real audio buffers")
for kind in ("listen", "ready", "sleep"):
    a = make_jingle(kind)
    check(getattr(a, "size", 0) > 1000, f"{kind} jingle has samples ({getattr(a,'size',0)})")
    check(float(a.max()) > 0.01, f"{kind} jingle is audible")

# End-to-end scripted pipeline without GGUF
print("\n[bonus] scripted pipeline with stub brain")
from reedus_companion.voice import voice_loop as VL                      # noqa: E402

class _Stub:
    def __init__(self):
        self.heard = []
    def warm(self): return 0.0
    def reply(self, t):
        self.heard.append(t)
        return f"got:{t}"
    def close(self): pass
    on_token = None

stub = _Stub()
jingles = []
turns = []

def hear_factory(lines):
    q = list(lines)
    def hear():
        if not q:
            raise KeyboardInterrupt
        return q.pop(0)
    return hear

# Monkeypatch jingle play to record kinds
import reedus_companion.voice.jingle as JG                               # noqa: E402
_real_play = JG.play_jingle

def _cap_play(kind="listen", device=None, sr=24000):
    jingles.append(kind)

JG.play_jingle = _cap_play
try:
    script = [
        "noise hello",
        "Hey Reedus",
        "what's up",
        "goodbye",
        "Hey Reedus tell me a joke",
        "stop listening",
    ]
    # Inline a minimal driver matching run_wake_loop semantics
    from reedus_companion.voice.session import WakeSession
    from reedus_companion.voice.stt import canon_name
    sess = WakeSession()
    for line in script:
        text = canon_name(line)
        ev = sess.on_transcript(text)
        if ev.jingle:
            jingles.append(ev.jingle)
        if ev.action == "turn":
            turns.append(ev.text)
            stub.reply(ev.text)
            post = sess.after_reply()
            if post.jingle:
                jingles.append(post.jingle)
finally:
    JG.play_jingle = _real_play

check(jingles[0] == "listen", f"first jingle is listen after wake ({jingles})")
check("ready" in jingles, f"ready jingle after reply ({jingles})")
check(jingles.count("sleep") >= 2, f"sleep jingle on both goodbyes ({jingles})")
check(turns == ["what's up", "tell me a joke"], f"brain saw stripped turns {turns}")
check("Hey Reedus" not in " ".join(turns), "wake word not forwarded to brain")

print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("ALL PASS")
