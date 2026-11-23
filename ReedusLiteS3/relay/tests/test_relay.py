#!/usr/bin/env python3
"""the relay end to end: a real relay server on localhost and a fake Reedus Lite over a real
WebSocket, with the cloud parts (speech-to-text, LLM, Inworld) faked. covers auth, the wake →
listen → think → speak → follow-up loop, paced speech frames, the timer fast path pushing
`timers`, barge-in, the funding pool (charged per turn, out-of-credit line), memory (turns
recorded, recall in the prompt, bulletin folded and pushed to the device), and the ReedOS app
API (status, llm.set, memory, device.set, admin). no network, no keys.

    python ReedusLiteS3/relay/tests/test_relay.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                     # relay/
sys.path.insert(0, str(HERE.parents[3] / "reedus_llm"))      # the brain
TMP = Path(tempfile.mkdtemp())
os.environ["RELAY_DATA"] = str(TMP)
os.environ["DATABASE_URL"] = ""
os.environ["RELAY_ADMIN_TOKEN"] = "admin-secret"
os.environ["GEMINI_API_KEY"] = "fake-gemini"
os.environ["ANTHROPIC_API_KEY"] = "fake-anthropic"
for k in ("REEDUS_LLM_PROVIDER", "REEDUS_LLM_MODEL", "OPENAI_API_KEY"):
    os.environ.pop(k, None)

import numpy as np

from reedus_relay import __main__ as relay_main

relay_main._setup()
from reedus_relay import accounts, config, memory, meter, speech
from reedus_relay.brain import Brain, SentenceSplitter
from reedus_relay.server import Hub, serve
from reedus_relay.session import strip_wake

config.START_TIMEOUT_S = 1.0
config.FOLLOW_UP_S = 0.6
config.LEAD_S = 0.10
config.PREBUFFER_S = 0.02
config.VAD = "energy"          # the fake audio is sine waves; Silero is checked on real speech below

_fails = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


# ---------- fakes ----------
WAKE_MARK = 1234          # a frame of all 1234s is "hey reedus"
VOICED = (np.sin(np.arange(320) / 3.0) * 9000).astype("<i2").tobytes()
SILENT = np.zeros(320, dtype="<i2").tobytes()
WAKE = np.full(320, WAKE_MARK, dtype="<i2").tobytes()


class FakeWake:
    available = True

    def feed(self, pcm):
        return pcm == WAKE

    def gate_opened(self):
        return False

    def reset(self):
        pass

    def describe(self):
        return "fake"


SCRIPT = []               # what the fake STT "hears", one per utterance
HEARD_AUDIO = []


def fake_stt(audio):
    HEARD_AUDIO.append(len(audio))
    m = meter.current()
    if m is not None:
        m.add(0.0002, "stt")
    return SCRIPT.pop(0) if SCRIPT else ""


class FakeBackend:
    """Streams a canned reply; the bulletin request gets a fact list."""
    CLOUD = True
    model = "fake-model"

    def __init__(self):
        self.calls = []
        self.slow = 0.0

    def chat(self, messages, max_tokens, use_adapter=True, temperature=None, on_token=None,
             tier=None, name="", cancel=None):
        self.calls.append({"messages": messages, "name": name})
        m = meter.current()
        if m is not None:
            m.add(0.001, "llm")
        if name == "bulletin":
            return "- Their name is Sam.\n- They have a physics exam on Friday."
        text = "[excited] Sure thing, mate. That sounds great! [curious] What happens next?"
        for word in text.split(" "):
            if cancel is not None and cancel.is_set():
                break
            if on_token:
                on_token(word + " ")
            if self.slow:
                time.sleep(self.slow)
        return text

    def close(self):
        pass


class FakeVoice:
    def frames(self, text):
        m = meter.current()
        if m is not None:
            m.add(0.0005, "voice")
        for _ in range(4):
            yield SILENT


BACKEND = FakeBackend()


# ---------- a fake Stripe (just the three calls the relay makes) ----------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl


class FakeStripe:
    """Just the Stripe calls the relay makes: balance, customers, Checkout sessions (with the
    saved card when asked), off-session PaymentIntents (with a decline switch), detach. Replays a
    repeated Idempotency-Key like Stripe does."""

    def __init__(self):
        self.sessions = {}
        self.customers = {}
        self.intents = []
        self.detached = []
        self.idem = {}
        self.decline = ""             # "" | card_declined | authentication_required
        self.n = 0
        stripe = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self):
                return self.headers.get("Authorization", "").endswith("sk_test_good")

            def do_GET(self):
                if not self._authed():
                    return self._send(401, {"error": {"message": "Invalid API Key provided: sk_test_***bad"}})
                path, _, query = self.path.partition("?")
                if path == "/v1/balance":
                    return self._send(200, {"object": "balance", "livemode": False})
                sid = path.rsplit("/", 1)[-1]
                s = stripe.sessions.get(sid)
                if not s:
                    return self._send(404, {"error": {"message": "No such checkout.session"}})
                out = {"id": sid, "status": "complete" if s["paid"] else "open",
                       "payment_status": "paid" if s["paid"] else "unpaid",
                       "customer": s["params"].get("customer"),
                       "amount_total": int(s["params"]["line_items[0][price_data][unit_amount]"])}
                pm = {"id": f"pm_{sid[-6:]}", "card": {"brand": "visa", "last4": "4242",
                                                       "exp_month": 12, "exp_year": 2030}}
                if "expand" in query and s["paid"]:
                    out["payment_intent"] = {"id": f"pi_{sid[-6:]}", "payment_method": pm}
                return self._send(200, out)

            def do_POST(self):
                if not self._authed():
                    return self._send(401, {"error": {"message": "Invalid API Key provided"}})
                n = int(self.headers.get("Content-Length") or 0)
                params = dict(parse_qsl(self.rfile.read(n).decode()))
                key = self.headers.get("Idempotency-Key", "")
                if key and key in stripe.idem:
                    return self._send(*stripe.idem[key])
                code, body = self._route(params)
                if key:
                    stripe.idem[key] = (code, body)
                return self._send(code, body)

            def _route(self, params):
                path = self.path
                if path == "/v1/customers":
                    cid = f"cus_{len(stripe.customers) + 1:04d}"
                    stripe.customers[cid] = params
                    return 200, {"id": cid}
                if path == "/v1/payment_intents":
                    stripe.intents.append(params)
                    if stripe.decline:
                        return 402, {"error": {"type": "card_error", "code": stripe.decline,
                                               "decline_code": stripe.decline if stripe.decline == "card_declined" else "",
                                               "message": "Your card was declined."}}
                    return 200, {"id": f"pi_auto_{len(stripe.intents):03d}", "status": "succeeded",
                                 "amount": int(params["amount"])}
                if path.endswith("/detach"):
                    stripe.detached.append(path.split("/")[3])
                    return 200, {"id": path.split("/")[3]}
                stripe.n += 1
                sid = f"cs_test_{stripe.n:04d}abcdef"
                stripe.sessions[sid] = {"params": params, "paid": False}
                return 200, {"id": sid, "url": f"https://checkout.stripe.test/{sid}"}

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def pay(self, sid):
        self.sessions[sid]["paid"] = True


STRIPE = FakeStripe()
os.environ["RELAY_STRIPE_API"] = STRIPE.url
os.environ["RELAY_PUBLIC_URL"] = "https://relay.test"


def make_brain_kwargs():
    return {}


class TestHub(Hub):
    """Injects the fake brain backend into each session's Brain."""

    async def device(self, ws):
        await super().device(ws)


def patch_brain():
    orig = Brain.backend

    def backend(self):
        return BACKEND
    Brain.backend = backend
    return orig


patch_brain()


# ---------- unit bits ----------
print("[1] pieces")
check(strip_wake("Hey Reedus, what's the weather") == "what's the weather", "wake words stripped from the transcript")
from reedus_relay.brain import is_goodbye
check(all(is_goodbye(x) for x in ("goodbye", "okay thanks, goodbye", "alright cool, see you later",
                                   "that's all for now", "I'm heading out, bye")),
      "goodbyes recognised")
check(not any(is_goodbye(x) for x in ("what does goodbye mean in french", "later today I have class",
                                       "say bye to my friend")), "not every 'bye' is a goodbye")
from reedus_relay.session import is_stop
check(all(is_stop(x) for x in ("can you stop talking", "please stop", "you can stop", "that's enough",
                               "zip it", "stop stop", "Reedus, stop.", "could you please stop")),
      "the ways people actually tell him to stop")
check(all(is_stop(x) for x in ("stop", "Stop.", "okay stop", "stop listening", "never mind",
                                "Nevermind, thanks", "shut up", "I wasn't talking to you")),
      "'stop' and friends end the conversation")
check(not any(is_stop(x) for x in ("stop the timer", "stop being weird", "how do I stop procrastinating",
                                    "nothing much, you?")), "...but not sentences that just contain them")
from reedus_relay.brain import apply_think, think_request
check(think_request("think about this before you answer") == (True, "")
      and think_request("Think hard about this: why is the sky blue?") == (True, "why is the sky blue?")
      and think_request("okay take your time with this one, should I take the job") == (True, "should I take the job"),
      "'think about this before you answer' and friends are heard")
check(not any(think_request(x)[0] for x in ("I think it's cool", "what do you think", "think of a number",
                                             "think about it", "do you think before you answer?")),
      "...but not every sentence with 'think' in it")
from reedus_relay.brain import directives, needs_search
check(directives("think about this before you answer and search the internet") == (True, True, "")
      and directives("search the internet for who won the oscars") == (False, True, "who won the oscars")
      and directives("who won the game last night, look it up") == (False, True, "who won the game last night")
      and directives("Think hard and look it up: is coffee bad for you?") == (True, True, "is coffee bad for you?")
      and directives("can you google how tall everest is")[1],
      "'search the internet' / 'look it up' are heard, alone or with thinking")
check(not any(directives(x)[1] or needs_search(x) for x in (
    "my search history is embarrassing", "how do I look it up", "I google everything",
    "tell me about google", "search engines are weird")),
      "...but 'search' or 'google' inside a sentence doesn't make him search")
import types as _types
_g = _types.SimpleNamespace(_think_main="minimal")
_c = _types.SimpleNamespace(_effort="low", _effort_router="low", _think_mode=None)
_o = _types.SimpleNamespace(_effort="low", wire="responses")
apply_think(_g, "high"); apply_think(_c, "medium"); apply_think(_o, "fast")
check(_g._think_main == "high" and (_c._effort, _c._think_mode) == ("medium", "adaptive") and _o._effort == "low",
      "a thinking level reaches Gemini, Claude and OpenAI their own way")
apply_think(_g, "fast"); apply_think(_c, "fast")
check(_g._think_main == "minimal" and _c._think_mode is None, "Fastest is the least each one allows")
from reedus_relay.session import looks_unfinished
check(all(looks_unfinished(x) for x in ("so I was thinking about, um", "I need to buy milk and",
                                        "And then I.", "is it better than", "Hey, so..."))
      and not any(looks_unfinished(x) for x in ("What is the capital of France?", "I think so.",
                                                "what do you mean", "tell me a joke", "where should I eat")),
      "a sentence that trails off is told apart from a finished one")
from reedus_relay import listening
check(listening.end_silence({}) == 1.2 and listening.end_silence({"settings": {"listen": {"patience": "very"}}}) == 2.6
      and listening.wake_cutoff({"settings": {"listen": {"wake": 0.6}}}) == 0.60 and listening.wake_cutoff({}) == 0.0
      and listening.valid_cutoff(0.55) is None and listening.valid_cutoff(0.96) is None,
      "patience and wake sensitivity map to seconds and a cutoff")
from reedus_relay import models_info
from reedus_companion.cloud import gemini_backend as _gb
check(models_info.lowest("gemini", "gemini-3.5-flash-lite") == ("minimal", "known")
      and models_info.lowest("gemini", "gemini-3.7-flash")[0] == "low"
      and models_info.lowest("anthropic", "claude-haiku-5-5") == ("off", "known")
      and models_info.lowest("openrouter", "some/model") == ("unknown", ""),
      "each model's least thinking: known ones say so, the rest 'not tested yet'")
_gb._THINK_LEARNED["gemini-4-hydrogen"] = "low"
check(models_info.lowest("gemini", "gemini-4-hydrogen") == ("low", "learned"),
      "a level the provider turned down is learned and shown")
del _gb._THINK_LEARNED["gemini-4-hydrogen"]
for x in (1.4, 1.6, 9.0):
    models_info.record("gemini", "gemini-test", x)
check(models_info.typical("gemini", "gemini-test") == (1.6, 3)
      and models_info.annotate("gemini", [{"id": "gemini-3.5-flash-lite"}])[0]["thinking_label"] == "minimal thinking",
      "first-word times from real replies (the median), and the model list gets labels")
from reedus_relay.brain import stable_system
check(stable_system().endswith(stable_system()[-40:]) and "Today's date" not in stable_system(),
      "the cached part of the prompt has no clock in it")
check("head swivels" in stable_system() and "no camera" in stable_system(),
      "he knows his own body (the turning head, no camera)")
from reedus_relay.brain import Brain
_past = [{"role": "user", "content": "who won last night"},
         {"role": "assistant", "content": "The Caps, 4-2.", "searched": True},
         {"role": "user", "content": "nice"}, {"role": "assistant", "content": "Right?"}]
check(Brain.searched_before(_past) == ["who won last night"],
      "a turn that really searched is remembered as searched")
got = []
from reedus_relay.brain import face_tag
check(face_tag("[Excited] Whoa! [weird] ok") == ("excited", " Whoa! [weird] ok"),
      "face tags are found (any case) and only known ones are taken out")
faces = []
sp2 = SentenceSplitter(got.append, on_face=faces.append)
sp2.feed("[sad] Oh no, that sounds really rough. ")
sp2.feed("[happy] But hey, you'll get through it, I promise.")
sp2.flush()
check(faces == ["sad", "happy"] and got[-1].startswith("But hey") and "[" not in "".join(got),
      "the splitter hands each tag over before its sentence and never speaks it")
got.clear()
sp = SentenceSplitter(got.append)
for tok in "Well hello there my old friend. How are you doing today? Good.".split(" "):
    sp.feed(tok + " ")
sp.flush()
check(got == ["Well hello there my old friend.", "How are you doing today?", "Good."],
      f"sentences come out as they close ({got})")
ep = speech.Endpointer(start_timeout=1.0, end_silence=0.2)
evs = [ep.feed(SILENT) for _ in range(10)] + [ep.feed(VOICED) for _ in range(10)] + \
      [ep.feed(SILENT) for _ in range(15)]
check("start" in evs and evs.index("start") == 12 and "end" in evs, "endpointer: start after 3 voiced frames, end after silence")
check(len(ep.audio()) >= 10 * 640, "endpointer keeps the speech")
ep2 = speech.Endpointer(start_timeout=0.2)
check([ep2.feed(SILENT) for _ in range(12)][-3:].count("timeout") >= 1, "endpointer times out on silence")
check(speech.tone()[:2] != b"", "tone renders")
from reedus_relay.brain import needs_search
check(needs_search("who won the game last night") and needs_search("what's the latest news")
      and not needs_search("tell me a joke") and not needs_search("how are you doing"),
      "the web search tool only comes out when the sentence needs the live web")
got.clear()
sp3 = SentenceSplitter(got.append, first_clause=20)
for tok in "Oh man, that is a really good question, honestly. Let me think.".split(" "):
    sp3.feed(tok + " ")
sp3.flush()
check(got[0] == "Oh man, that is a really good question," and got[-1].endswith("Let me think."),
      f"the first words can go out at a comma so his voice starts sooner ({got})")
b = Brain({"id": "x", "settings": {}}, {"id": "y"}, backend=BACKEND)
b.history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": ""}]
b._remember("cut off", "")
check(len(b.history) == 2, "a reply cut off before it said anything isn't kept")
check(speech.clean_transcript("Thank you.", b"\0" * 16000) == "" and
      speech.clean_transcript("Thank you so much for the help", b"\0" * 16000) != "" and
      speech.clean_transcript("[background noise]", b"\0" * 64000) == "",
      "what speech-to-text makes up from noise is dropped")
ep3 = speech.Endpointer(start_timeout=2.0, end_silence=0.5, spec_after=0.2)
evs = [ep3.feed(VOICED) for _ in range(10)] + [ep3.feed(SILENT) for _ in range(12)] + \
      [ep3.feed(VOICED) for _ in range(5)] + [ep3.feed(SILENT) for _ in range(30)]
check(evs.count("pause") == 2 and evs.index("pause") < evs.index("resume") < evs.index("end")
      and evs.index("end") > 27,
      "a pause starts an early transcript, talking again throws it away, the real end ends it")
check(strip_wake("Reedus, what's the weather") == "what's the weather", "just the name is stripped too")
from reedus_relay import wake as wake_mod
check(wake_mod.make("device", ["gate", "wake"]).on_device, "RELAY_WAKE=device trusts a Lite that hears the name")
check(not wake_mod.make("device", ["gate"]).on_device, "a Lite without it falls back to openWakeWord")
check(not wake_mod.make("model", ["wake"]).on_device and wake_mod.make("gate", ["wake"]).gate_opened(),
      "RELAY_WAKE=model / gate override the Lite's")
check(wake_mod.status("device")[0] and wake_mod.status("device")[1].startswith("on the Lite"),
      "Setup counts the wake word as ready when the Lites have it")

# ---------- accounts ----------
print("[2] accounts and the pool")
acc = accounts.create_account("Sam", 5.0)
aid, app_token = acc["account"]["id"], acc["token"]
dev = accounts.create_device(aid, "Desk Lite")
did, dev_token = dev["device"]["id"], dev["token"]
check(accounts.account_for_token(app_token)["id"] == aid, "app token signs in")
check(accounts.device_for_token(dev_token)["id"] == did, "device token signs in")
check(accounts.device_for_token("rld_nope") is None, "bad token refused")
check(accounts.charge(aid, 0.25, "test") == 4.75, "charge comes out of the pool")
check(accounts.top_up(aid, 0.25) == 5.0, "top-up goes back in")
dev2_token = accounts.create_device(aid, "Kitchen Lite")["token"]
broke = accounts.create_account("Broke", 0.0)
broke_dev = accounts.create_device(broke["account"]["id"], "Broke Lite")


# ---------- end to end ----------
class Device:
    """A fake Reedus Lite."""

    def __init__(self, ws):
        self.ws = ws
        self.events = []
        self.speech_frames = 0
        self.flushes = 0
        self.task = asyncio.ensure_future(self._recv())

    async def _recv(self):
        try:
            async for m in self.ws:
                if isinstance(m, bytes):
                    if m[:1] == b"\x01":   # counted in 20 ms frames (the relay batches them)
                        self.speech_frames += (len(m) - 1) // 640
                else:
                    msg = json.loads(m)
                    self.events.append(msg)
                    if msg["type"] == "flush":
                        self.flushes += 1
        except Exception:
            pass

    def types(self):
        return [e["type"] for e in self.events]

    def states(self):
        return [e["state"] for e in self.events if e["type"] == "state"]

    def last(self, t):
        for e in reversed(self.events):
            if e["type"] == t:
                return e
        return None

    async def wait_for(self, pred, timeout=5.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return True
            await asyncio.sleep(0.02)
        return False

    async def say(self, voiced=15, silence=90, wake=True):
        await self.ws.send(json.dumps({"type": "gate", "open": True}))
        if wake:
            await self.ws.send(WAKE)
        for _ in range(5):
            await self.ws.send(SILENT)
        for _ in range(voiced):
            await self.ws.send(VOICED)
        for _ in range(silence):
            await self.ws.send(SILENT)


async def main():
    import websockets
    hub = Hub(voice_factory=lambda: FakeVoice(),
              session_kwargs={"wake": None, "stt": fake_stt})
    # every session gets its own fake wake detector
    orig_init = hub.device

    from reedus_relay import session as session_mod
    real_session = session_mod.Session

    class S(real_session):
        def __init__(self, *a, **kw):
            if "wake" not in (a[4].get("caps") or []):    # a Lite that hears "Reedus" keeps its own
                kw["wake"] = FakeWake()
            super().__init__(*a, **kw)
    import reedus_relay.server as server_mod
    server_mod.Session = S

    server = await serve(hub, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}"

    print("[3] device link")
    try:
        async with websockets.connect(url + "/lite") as ws:
            await ws.send(json.dumps({"type": "hello", "proto": 3, "token": "rld_wrong", "rate": 16000}))
            await ws.recv()
        check(False, "bad device token closes the socket")
    except websockets.ConnectionClosed as e:
        check(e.rcvd.code == 4001, "bad device token closes the socket (4001)")

    ws = await websockets.connect(url + "/lite")
    await ws.send(json.dumps({"type": "hello", "proto": 3, "device": "reedus-lite-s3", "fw": "test",
                              "token": dev_token, "rate": 16000, "frame_ms": 20, "bulletin_rev": 0}))
    d = Device(ws)
    ok = await d.wait_for(lambda: {"welcome", "state", "clock", "timers"} <= set(d.types()))
    check(ok, "welcome, state, clock and timers on connect")
    check(d.last("listen") == {"type": "listen", "open": False}, "starts gated (only streams on speech)")
    check(d.last("welcome").get("wake") == "relay", "no wake cap: the relay does the wake word")

    print("[4] a conversation")
    SCRIPT[:] = ["Hey Reedus, I've got a physics exam on Friday"]
    bal0 = accounts.balance(aid)
    await d.say()
    ok = await d.wait_for(lambda: "speaking" in d.states() and d.states()[-1] == "listening", 6)
    check(ok, f"wake → listening → thinking → speaking → listening ({d.states()})")
    check("wake" in d.types(), "wake event sent")
    emos = [e["emotion"] for e in d.events if e["type"] == "emotion"]
    check(emos[:1] == ["excited"] and "curious" in emos and emos.index("curious") > 0,
          f"his own face tags drive the face, in order ({emos})")
    await d.wait_for(lambda: bool(memory.recent(aid, 1)), 3)
    check(memory.recent(aid, 1) and "[" not in memory.recent(aid, 1)[0]["reply"]
          and memory.recent(aid, 1)[0]["reply"].startswith("Sure thing"),
          "the tags are never spoken or saved")
    check(d.last("listen") == {"type": "listen", "open": True}, "listen opened for the conversation")
    check(d.speech_frames >= 8, f"speech frames streamed with the kind byte ({d.speech_frames})")
    check(any(e["type"] == "emotion" for e in d.events), "emotion from the reply")
    sys_prompt = BACKEND.calls[-1]["messages"][0]["content"]
    check("You are Reedus" in sys_prompt and "Reedus Lite" in sys_prompt, "his persona + the Lite voice rules")
    check(BACKEND.calls[-1]["messages"][-1]["content"] == "I've got a physics exam on Friday",
          "the transcript (wake words stripped) is the user turn")
    await d.wait_for(lambda: accounts.balance(aid) < bal0, 2)
    spent = bal0 - accounts.balance(aid)
    check(abs(spent - (0.0002 + 0.001 + 0.0005 * 3)) < 1e-6 or spent > 0.001,
          f"the turn was charged to the pool (${spent:.4f})")
    await d.wait_for(lambda: bool(memory.recent(aid)), 2)
    check(bool(memory.recent(aid)) and memory.recent(aid)[0]["said"].startswith("I've got a physics"),
          "turn saved to history")

    print("[5] follow-up without the wake word, then goodbye")
    SCRIPT[:] = ["what should I study first"]
    n_before = len(BACKEND.calls)
    await d.say(wake=False)
    ok = await d.wait_for(lambda: len(BACKEND.calls) > n_before and d.states()[-1] == "listening", 6)
    check(ok, "a follow-up is answered without saying the name")
    check(any(m["content"].startswith("I've got a physics") for m in BACKEND.calls[-1]["messages"]),
          "the conversation so far is in the prompt")
    SCRIPT[:] = ["okay thanks, goodbye"]
    await d.say(wake=False)
    ok = await d.wait_for(lambda: d.states()[-1] == "idle" and d.last("listen")["open"] is False, 6)
    check(ok, "goodbye ends the conversation and closes listening")
    check(any(e.get("name") == "bye" for e in d.events if e["type"] == "expr"), "waves bye")
    ok = await d.wait_for(lambda: d.last("bulletin") is not None, 4)
    check(ok and "physics exam" in d.last("bulletin")["text"], "bulletin folded and pushed to the S3")
    check(memory.bulletin(aid)[1] == 1, "bulletin saved (rev 1)")

    print("[6] memory recall in a new conversation")
    SCRIPT[:] = ["how do you think my exam will go"]
    await d.say()
    await d.wait_for(lambda: d.states()[-1] == "listening" and len(BACKEND.calls) >= 5, 6)
    persona_calls = [c for c in BACKEND.calls if c["name"] == "persona"]
    sp = persona_calls[-1]["messages"][0]["content"]
    check("physics exam on Friday" in sp, "bulletin facts are in the prompt")
    check("past conversations" in sp and "physics" in sp.split("past conversations", 1)[1],
          "a matching past turn is recalled")
    # let it time out back to idle
    for _ in range(40):
        await ws.send(SILENT)
    await d.wait_for(lambda: d.states()[-1] == "idle", 4)
    check(d.states()[-1] == "idle", "silence after a reply goes back to idle")

    print("[7] timers: the fast path, no LLM")
    SCRIPT[:] = ["set a timer for 5 minutes"]
    n_llm = len([c for c in BACKEND.calls if c["name"] == "persona"])
    await d.say()
    ok = await d.wait_for(lambda: any(e["type"] == "timers" and e["items"] for e in d.events), 6)
    check(ok, "a timers message with the new timer")
    t = [e for e in d.events if e["type"] == "timers" and e["items"]][-1]["items"][0]
    check(abs(t["left"] - 300) < 5, f"5 minutes on the device's countdown ({t['left']})")
    await d.wait_for(lambda: d.states()[-1] == "listening", 4)
    check(len([c for c in BACKEND.calls if c["name"] == "persona"]) == n_llm, "no LLM call for a timer")
    for _ in range(40):
        await ws.send(SILENT)
    await d.wait_for(lambda: d.states()[-1] == "idle", 4)

    print("[7b] 'stop', and 'think about this before you answer'")

    def n_persona():
        return len([c for c in BACKEND.calls if c["name"] == "persona"])
    sess = next(x for x in hub.sessions.values() if x.device.get("name") == "Desk Lite")
    n_llm, n_states = n_persona(), len(d.states())
    SCRIPT[:] = ["stop"]
    await d.say()
    ok = await d.wait_for(lambda: "listening" in d.states()[n_states:] and d.states()[-1] == "idle", 6)
    check(ok and n_persona() == n_llm, "'stop' goes straight back to idle, no reply, no LLM")
    SCRIPT[:] = ["think about this before you answer", "why is the sky blue"]
    await d.say()
    ok = await d.wait_for(lambda: sess.think_next and d.states()[-1] == "listening", 6)
    check(ok and n_persona() == n_llm, "the bare ask arms it: a quick 'go ahead', no LLM yet")
    await d.say(wake=False)
    ok = await d.wait_for(lambda: n_persona() > n_llm and d.states()[-1] == "listening", 8)
    check(ok and BACKEND.calls[-1]["messages"][-1]["content"] == "why is the sky blue"
          and sess.brain.timing.get("think") == "high" and not sess.think_next,
          "...and the next question is answered with deep thinking, just that one")
    n_llm = n_persona()
    SCRIPT[:] = ["think hard about this, what is 17 times 23"]
    await d.say(wake=False)
    ok = await d.wait_for(lambda: n_persona() > n_llm and d.states()[-1] == "listening", 8)
    check(ok and BACKEND.calls[-1]["messages"][-1]["content"] == "what is 17 times 23"
          and sess.brain.timing.get("think") == "high", "asked in the same breath: that question, thought through")
    n_llm = n_persona()
    SCRIPT[:] = ["what do you think about pizza"]
    await d.say(wake=False)
    ok = await d.wait_for(lambda: n_persona() > n_llm and d.states()[-1] == "listening", 8)
    check(ok and BACKEND.calls[-1]["messages"][-1]["content"] == "what do you think about pizza"
          and sess.brain.timing.get("think") == "fast", "plain 'think' in a sentence changes nothing")
    BACKEND._search = True                # pretend this brain can search the web
    n_llm = n_persona()
    SCRIPT[:] = ["think about this before you answer and search the internet", "who won the game last night"]
    await d.say(wake=False)
    ok = await d.wait_for(lambda: sess.think_next and sess.search_next and d.states()[-1] == "listening", 6)
    check(ok and n_persona() == n_llm, "think + search, said alone: both armed, no LLM yet")
    await d.say(wake=False)
    ok = await d.wait_for(lambda: n_persona() > n_llm and d.states()[-1] == "listening", 8)
    check(ok and BACKEND.calls[-1]["messages"][-1]["content"] == "who won the game last night"
          and sess.brain.timing.get("think") == "high" and sess.brain.timing.get("search") is True,
          "...the next question is thought through AND searched")
    n_llm = n_persona()
    SCRIPT[:] = ["what's your favourite colour, look it up"]
    await d.say(wake=False)
    ok = await d.wait_for(lambda: n_persona() > n_llm and d.states()[-1] == "listening", 8)
    check(ok and BACKEND.calls[-1]["messages"][-1]["content"] == "what's your favourite colour"
          and sess.brain.timing.get("search") is True and sess.brain.timing.get("think") == "fast",
          "'look it up' at the end forces a search even when the question wouldn't")
    n_llm = n_persona()
    SCRIPT[:] = ["my search history is so embarrassing"]
    await d.say(wake=False)
    ok = await d.wait_for(lambda: n_persona() > n_llm and d.states()[-1] == "listening", 8)
    check(ok and sess.brain.timing.get("search") is False, "...and 'search' in a sentence doesn't")
    del BACKEND._search
    if hasattr(BACKEND, "_search_default"):
        del BACKEND._search_default
    for _ in range(40):
        await ws.send(SILENT)
    await d.wait_for(lambda: d.states()[-1] == "idle", 4)

    print("[7c] letting them finish")
    sess = next(x for x in hub.sessions.values() if x.device.get("name") == "Desk Lite")
    # they pause, he starts thinking, they carry on before he's said anything
    BACKEND.slow = 0.3
    n_llm, n_states = n_persona(), len(d.states())
    SCRIPT[:] = ["so the thing I wanted to ask you", "so the thing I wanted to ask you is why the sky is blue"]
    await d.say()
    ok = await d.wait_for(lambda: d.states()[-1] == "thinking", 4)
    for _ in range(12):
        await ws.send(VOICED)
    ok2 = await d.wait_for(lambda: d.states()[-1] == "listening" and sess.mode == "listen", 3)
    for _ in range(90):
        await ws.send(SILENT)
    BACKEND.slow = 0.0
    ok3 = await d.wait_for(lambda: n_persona() > n_llm and d.states()[-1] == "listening"
                           and "speaking" in d.states()[n_states:], 10)
    users = [h["content"] for h in sess.brain.history if h["role"] == "user"]
    check(ok and ok2 and ok3 and BACKEND.calls[-1]["messages"][-1]["content"].endswith("why the sky is blue")
          and "so the thing I wanted to ask you" not in users,
          "talking again while he thinks: he drops that reply, keeps listening, answers the whole thing")
    for _ in range(40):
        await ws.send(SILENT)
    await d.wait_for(lambda: d.states()[-1] == "idle", 4)
    # it trails off ("…and"), then they go on
    n_llm = n_persona()
    SCRIPT[:] = ["I need to buy milk and", "I need to buy milk and eggs, what should I make"]
    await d.say()
    ok = await d.wait_for(lambda: sess.carry_text == "I need to buy milk and" and sess.mode == "listen", 4)
    check(ok and n_persona() == n_llm, "a sentence that trails off: he waits for the rest, no reply yet")
    await d.say(wake=False)
    ok = await d.wait_for(lambda: n_persona() > n_llm and d.states()[-1] == "listening", 8)
    check(ok and BACKEND.calls[-1]["messages"][-1]["content"] == "I need to buy milk and eggs, what should I make",
          "...and answers it as one sentence once they finish")
    # it trails off and they don't go on: after a couple of seconds he answers what's there
    n_llm = n_persona()
    SCRIPT[:] = ["tell me something about, um"]
    await d.say(wake=False)
    ok = await d.wait_for(lambda: sess.carry_text is not None, 4)
    for _ in range(130):
        await ws.send(SILENT)
    ok2 = await d.wait_for(lambda: n_persona() > n_llm, 6)
    check(ok and ok2 and BACKEND.calls[-1]["messages"][-1]["content"] == "tell me something about, um",
          "...and if nothing more comes, he answers what they said")
    check("Your last reply ended with a question" in BACKEND.calls[-1]["messages"][0]["content"]
          and "just not on every reply" in BACKEND.calls[-1]["messages"][0]["content"],
          "he asked last time, so this reply is told not to")
    for _ in range(40):
        await ws.send(SILENT)
    await d.wait_for(lambda: d.states()[-1] == "idle", 4)

    print("[7d] ringing always stops")
    sess = next(x for x in hub.sessions.values() if x.device.get("name") == "Desk Lite")
    a = sess.assist
    # the timer clock survives a failed save (it used to die, and then nothing rang or stopped)
    real_save, boom = a.save, []

    def bad_save():
        boom.append(1)
        raise RuntimeError("database went away")
    a.add_timer(0.0, label="pasta")
    a.save = bad_save
    wait = a.tick()
    a.save = real_save
    check(boom and wait == 2.0 and a._thread is not None and a._thread.is_alive(),
          "a database error in the timer clock doesn't kill it")
    a.tick()
    ok = await d.wait_for(lambda: (d.last("timers") or {}).get("ring"), 3)
    check(ok and sess.is_ringing(), "the timer rings")
    n_ro = len([e for e in d.events if e["type"] == "ring_off"])
    r = await hub._device_control("device.ring_off", {"device": sess.did}, sess.aid, True)
    ok = await d.wait_for(lambda: len([e for e in d.events if e["type"] == "ring_off"]) > n_ro
                          and not (d.last("timers") or {}).get("ring"), 3)
    check(r.get("ok") and ok and not a.ringing() and not sess.is_ringing(),
          "the app's Stop button ends it, on the relay and on the Lite")
    # the Lite rings on its own (the relay doesn't know): it shows in the app, "stop" quiets it
    await ws.send(json.dumps({"type": "ring", "on": True, "id": "abc12345", "kind": "timer"}))
    await asyncio.sleep(0.2)
    st = await asyncio.to_thread(hub._status, sess.account)
    me = next(x for x in st["devices"] if x["id"] == sess.did)
    check(me.get("ringing") is True, "a ring the Lite started itself shows in the app")
    n_ro = len([e for e in d.events if e["type"] == "ring_off"])
    SCRIPT[:] = ["stop"]
    await d.say()
    ok = await d.wait_for(lambda: len([e for e in d.events if e["type"] == "ring_off"]) > n_ro, 6)
    check(ok, "...and 'stop' tells the Lite to go quiet, even though the relay wasn't ringing")
    await ws.send(json.dumps({"type": "ring", "on": False, "id": "abc12345", "kind": "timer"}))
    await d.wait_for(lambda: d.states()[-1] == "idle", 4)

    print("[7e] 'stop' while he's talking, no name needed")
    sess = next(x for x in hub.sessions.values() if x.device.get("name") == "Desk Lite")
    BACKEND.slow = 0.25                     # a long reply, sentence by sentence
    SCRIPT[:] = ["tell me a long story", "what a nice day"]
    await d.say()
    ok = await d.wait_for(lambda: d.states()[-1] == "speaking", 8)
    for _ in range(12):                     # they say something over him that isn't "stop"
        await ws.send(VOICED)
    for _ in range(30):
        await ws.send(SILENT)
    await asyncio.sleep(0.4)
    check(ok and sess.mode == "busy" and sess.stop_checks == 1, "talking over him about something else doesn't stop him")
    SCRIPT[:] = ["stop talking"]
    for _ in range(12):
        await ws.send(VOICED)
    for _ in range(30):
        await ws.send(SILENT)
    ok = await d.wait_for(lambda: d.states()[-1] == "idle" and sess.mode == "idle", 4)
    BACKEND.slow = 0.0
    check(ok and any(e["type"] == "flush" for e in d.events[-12:]),
          "'stop talking' over him cuts him off and he goes quiet")
    await asyncio.sleep(0.3)

    print("[7f] one question mode")
    sess = next(x for x in hub.sessions.values() if x.device.get("name") == "Desk Lite")
    saved_acc = sess.account
    sess.account = {**saved_acc, "settings": {**(saved_acc.get("settings") or {}),
                                              "listen": {"mode": "single"}}}
    n_llm, n_states = n_persona(), len(d.states())
    SCRIPT[:] = ["what's the tallest mountain"]
    await d.say()
    ok = await d.wait_for(lambda: n_persona() > n_llm and "speaking" in d.states()[n_states:]
                          and d.states()[-1] == "idle", 8)
    check(ok and "listening" not in d.states()[n_states:][d.states()[n_states:].index("speaking"):],
          "one question mode: he answers and goes straight back to idle, no follow-up listening")
    conv = sess.conversation
    check(sess.rest_until > 0 and sess.turns_in_conv == 1, "...but the conversation isn't over yet")
    n_llm = n_persona()
    SCRIPT[:] = ["and how tall is it"]
    await d.say()
    ok = await d.wait_for(lambda: n_persona() > n_llm and d.states()[-1] == "idle", 8)
    users = [m["content"] for m in BACKEND.calls[-1]["messages"] if m["role"] == "user"]
    check(ok and sess.conversation == conv and "what's the tallest mountain" in users,
          "say his name again soon after: he remembers what you just asked")
    sess.rest_until = time.monotonic() - 1
    ok = await d.wait_for(lambda: sess.conversation != conv and sess.turns_in_conv == 0, 3)
    check(ok and sess.rest_until == 0.0, "a few quiet minutes later that conversation is wrapped up (once)")
    sess.account = saved_acc

    print("[8] barge-in")
    BACKEND.slow = 0.15
    SCRIPT[:] = ["tell me a long story", "never mind"]
    await d.say()
    await d.wait_for(lambda: d.states()[-1] == "speaking", 6)
    flushes = d.flushes
    await ws.send(WAKE)                         # "hey reedus" over his own voice
    ok = await d.wait_for(lambda: d.flushes > flushes and d.states()[-1] == "listening", 3)
    check(ok, "the name while he's talking flushes his speech and listens")
    BACKEND.slow = 0.0
    for _ in range(60):
        await ws.send(SILENT)
    await d.wait_for(lambda: d.states()[-1] == "idle", 4)

    print("[9] ReedOS app API")
    app = await websockets.connect(url + "/app")
    await app.send(json.dumps({"type": "auth", "token": app_token}))
    auth = json.loads(await app.recv())
    check(auth["type"] == "auth.ok" and auth["account"]["name"] == "Sam", "app signs in with the account token")

    async def ask(req):
        req["id"] = int(time.time() * 1000) % 100000
        await app.send(json.dumps(req))
        while True:
            r = json.loads(await app.recv())
            if r.get("id") == req["id"]:
                return r

    st = await ask({"action": "status"})
    check(st["devices"][0]["online"] and st["devices"][0]["name"] == "Desk Lite", "status shows the Lite online")
    check(st["balance"] < 5.0 and {p["id"] for p in st["providers"]} >= {"gemini", "anthropic"},
          "balance and the providers this relay has keys for")
    check("custom" not in {p["id"] for p in st["providers"]},
          "no 'other OpenAI-compatible' brain offered without a base URL")
    r = await ask({"action": "llm.set", "provider": "openai", "model": "gpt-5-mini"})
    check("no OpenAI key" in r.get("error", ""), "can't pick a provider the relay has no key for")
    r = await ask({"action": "llm.set", "provider": "anthropic", "model": "claude-sonnet-5-5"})
    check(r.get("ok") and r["label"] == "Claude Sonnet 5.5", "switch the brain to Claude")
    check(accounts.account(aid)["settings"]["llm"]["provider"] == "anthropic", "saved on the account")
    check(hub.sessions[did].brain.llm_choice() == ("anthropic", "claude-sonnet-5-5"),
          "the live Lite picks it up")
    r = await ask({"action": "device.set", "device": did, "volume": 40})
    ok = await d.wait_for(lambda: d.last("volume") == {"type": "volume", "level": 40}, 2)
    check(r.get("ok") and ok, "volume from the app reaches the device")
    r = await ask({"action": "memory.get"})
    check("physics" in r["bulletin"] and r["recent"], "memory: bulletin and recent turns")
    r = await ask({"action": "memory.set", "bulletin": "- Their name is Sam.\n- Likes chess."})
    ok = await d.wait_for(lambda: "chess" in (d.last("bulletin") or {}).get("text", ""), 2)
    check(r.get("ok") and ok, "editing the bulletin pushes it to the S3")
    r = await ask({"action": "chat", "text": "hello from the app"})
    check(r.get("text", "").startswith("Sure thing"), "typing to Reedus from the app")
    r = await ask({"action": "ledger"})
    check(any(row["usd"] < 0 for row in r["ledger"]) and r["balance"] < 5, "ledger lists the spend")
    r = await ask({"action": "topup", "usd": 10})
    check("error" in r, "users can't top themselves up until payments exist")
    await app.close()

    adm = await websockets.connect(url + "/app")
    await adm.send(json.dumps({"type": "auth", "token": "admin-secret"}))
    check(json.loads(await adm.recv())["admin"], "admin token signs in as admin")
    await adm.send(json.dumps({"id": 1, "action": "topup", "account": aid, "usd": 3}))
    r = json.loads(await adm.recv())
    check(r.get("ok") and r["balance"] > 7, "admin top-up")
    await adm.close()

    print("[10] out of credit")
    ws2 = await websockets.connect(url + "/lite")
    await ws2.send(json.dumps({"type": "hello", "proto": 3, "token": broke_dev["token"], "rate": 16000}))
    d2 = Device(ws2)
    ok = await d2.wait_for(lambda: any(e["type"] == "glance" and "ADD CREDIT" in e.get("text", "") for e in d2.events), 3)
    check(ok, "an empty pool shows ADD CREDIT IN THE APP on the face")
    n_stt = len(HEARD_AUDIO)
    await d2.say()
    ok = await d2.wait_for(lambda: d2.speech_frames > 0 and d2.states()[-1] == "idle", 6)
    check(ok, "he says he's out of credit, then goes idle")
    check(len(HEARD_AUDIO) == n_stt, "no speech-to-text spent on an empty pool")
    await ws2.close()

    print("[11] health check")
    import urllib.request
    body = (await asyncio.to_thread(
        lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3).read())).decode()
    check("ok" in body, "GET /health answers plain HTTP")

    print("[12] the relay serves the website")

    def get(path):
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3)
            return r.status, r.headers.get("Content-Type", ""), r.read()
        except Exception as e:                                   # noqa: BLE001
            return getattr(e, "code", 0), "", b""
    st, ct, html = await asyncio.to_thread(get, "/")
    check(st == 200 and "text/html" in ct and b"Pair a Lite" in html and b"g-setup" in html,
          "/ is the Reedus Lite website")
    check(b"view-reedus" not in html and b"Run box" not in html and b"demo" not in html.lower(),
          "no robot, no run box, no demo on it")
    st, ct, js = await asyncio.to_thread(get, "/app.js")
    check(st == 200 and "javascript" in ct and b"const Relay" in js, "its script is served")
    st, _, rj = await asyncio.to_thread(get, "/relay.json")
    rj = json.loads(rj) if st == 200 else {}
    check(rj.get("relay") is True and rj.get("claimed") is True and rj.get("setup_code") is True,
          "/relay.json: a relay, already set up, setup needs a code")
    st, _, _ = await asyncio.to_thread(get, "/../../ReedusLiteS3/relay/reedus_relay/config.py")
    st2, _, _ = await asyncio.to_thread(get, "/%2e%2e/%2e%2e/reedus_llm/reedus_companion/config.py")
    check(st == 404 and st2 == 404, "nothing outside the app folder is served")

    print("[13] setup: the owner's checklist and keys")
    from reedus_relay import funding, vault
    import reedus_companion.cloud.llm as cloud_llm
    real_llm_test = cloud_llm.test
    cloud_llm.test = lambda pid, model=None, key=None, base=None: (
        {"ok": True, "ms": 420, "reply": "Ready.", "model": "fake"} if key == "sk-ant-good"
        else {"ok": False, "error": "Anthropic Claude rejected the key."})
    check(accounts.is_owner(accounts.account(aid)) and not accounts.is_owner(accounts.account(broke["account"]["id"])),
          "the first account owns the relay, later ones don't")
    app = await websockets.connect(url + "/app")
    await app.send(json.dumps({"type": "auth", "token": app_token}))
    check(json.loads(await app.recv()).get("owner") is True, "sign-in says you're the owner")

    async def ask2(sock, req):
        req["id"] = int(time.time() * 1000) % 100000 + 7
        await sock.send(json.dumps(req))
        while True:
            r = json.loads(await sock.recv())
            if r.get("id") == req["id"]:
                return r

    st = await ask2(app, {"action": "setup.status"})
    items = {i["id"]: i for i in st["items"]}
    check(set(items) == {"brain", "hearing", "voice", "lite", "money", "payments"}, "the owner's checklist")
    check(items["brain"]["done"] and items["hearing"]["done"] and not items["voice"]["done"] and items["lite"]["done"],
          f"brain and hearing ready (env keys), voice missing, a Lite exists ({items['voice']['detail']})")
    check(not st["relay_ready"], "the relay isn't ready without a voice")
    check(items["payments"]["optional"] and not items["payments"]["done"], "payments optional and not set")
    check(any(b["id"] == "anthropic" and b["key"]["set"] and b["key"]["source"] == "railway" for b in st["brains"]),
          "brains list which keys are set and where from")
    check(all("steps" in b and "billing" in b for b in st["brains"]) and
          {s["id"] for s in st["services"]} == {"inworld", "stripe"},
          "every provider and service comes with its get-a-key steps and billing note")
    check("fake-anthropic" not in json.dumps(st), "no whole key ever comes back")
    r = await ask2(app, {"action": "setup.key", "name": "ANTHROPIC_API_KEY", "value": "sk-ant-bad"})
    check(r.get("saved") is False and "rejected" in r["test"]["detail"], "a key that fails its test isn't saved")
    r = await ask2(app, {"action": "setup.key", "name": "ANTHROPIC_API_KEY", "value": "sk-ant-good"})
    check(r.get("saved") and r["source"] == "app" and r["hint"] == "sk-a…good", "a good key is tested and saved")
    check(os.environ["ANTHROPIC_API_KEY"] == "sk-ant-good", "and is live in the relay right away")
    r = await ask2(app, {"action": "setup.forget", "name": "ANTHROPIC_API_KEY"})
    check(os.environ["ANTHROPIC_API_KEY"] == "fake-anthropic" and r["source"] == "railway",
          "forgetting it falls back to the Railway variable")
    r = await ask2(app, {"action": "setup.key", "name": "SOMETHING_ELSE", "value": "x"})
    check("unknown key" in r.get("error", ""), "only known keys can be set")
    r = await ask2(app, {"action": "device.create", "name": "Bedroom Lite", "host": "my-relay.up.railway.app"})
    check(r.get("token", "").startswith("rld_") and r["token"] in r["secrets_h"]
          and '"my-relay.up.railway.app"' in r["secrets_h"] and "RELAY_TLS 1" in r["secrets_h"],
          "adding a Lite hands back its secrets.h, ready to paste")

    other = await websockets.connect(url + "/app")
    await other.send(json.dumps({"type": "auth", "token": broke["token"]}))
    check(json.loads(await other.recv()).get("owner") is False, "a second account isn't an owner")
    r = await ask2(other, {"action": "setup.key", "name": "INWORLD_API_KEY", "value": "abc"})
    check("owner" in r.get("error", ""), "non-owners can't change the relay's keys")
    r = await ask2(other, {"action": "setup.status"})
    check("brains" not in r and "services" not in r and [i["id"] for i in r["items"]] == ["lite", "money"]
          and "relay_ready" in r, "non-owners see their own two steps and whether the relay is ready, not the keys")
    r = await ask2(other, {"action": "device.create", "name": "x"})
    check("owner" in r.get("error", ""), "non-owners can't add Lites")

    print("[14] one-tap top-ups (fake Stripe)")
    r = await ask2(app, {"action": "topup.start", "usd": 10})
    check("aren't set up" in r.get("error", ""), "no Stripe key: top-ups say so")
    r = await ask2(app, {"action": "setup.key", "name": "STRIPE_SECRET_KEY", "value": "sk_test_bad"})
    check(r.get("saved") is False and "Invalid API Key" in r["test"]["detail"], "a bad Stripe key is refused")
    r = await ask2(app, {"action": "setup.key", "name": "STRIPE_SECRET_KEY", "value": "sk_test_good"})
    check(r.get("saved") and "test mode" in r["test"]["detail"], "a test-mode Stripe key is accepted")
    st = await ask2(app, {"action": "setup.status"})
    check(st["payments"] and st["stripe_mode"] == "test" and st["amounts"] == [5, 10, 20, 50], "payments on")
    r = await ask2(app, {"action": "topup.start", "usd": 1})
    check("$5 to $200" in r.get("error", ""), "amounts outside $5–$200 refused")
    bal0 = accounts.balance(aid)
    r = await ask2(app, {"action": "topup.start", "usd": 10, "return_to": "https://evil.example"})
    sid = r.get("session", "")
    sent = STRIPE.sessions.get(sid, {}).get("params", {})
    check(r.get("url", "").startswith("https://checkout.stripe.test/") and sid.startswith("cs_test_"),
          "Top up gives a Stripe Checkout link")
    check(sent.get("line_items[0][price_data][unit_amount]") == "1000" and sent.get("client_reference_id") == aid
          and sent.get("mode") == "payment", "for $10.00, tagged with the account")
    check(sent.get("success_url", "").startswith("https://relay.test/?topup={CHECKOUT_SESSION_ID}"),
          "a foreign return address is ignored, Stripe sends you back to the relay")
    r = await ask2(app, {"action": "topup.confirm", "session": sid})
    check(r.get("credited") is False and r["status"] == "open" and accounts.balance(aid) == bal0,
          "not paid yet: nothing credited")
    STRIPE.pay(sid)
    r = await ask2(app, {"action": "topup.confirm", "session": sid})
    check(r.get("credited") and abs(accounts.balance(aid) - (bal0 + 10)) < 1e-6, "paid: $10 lands in the pool")
    r = await ask2(app, {"action": "topup.confirm", "session": sid})
    check(r.get("already") and abs(accounts.balance(aid) - (bal0 + 10)) < 1e-6, "confirming twice credits once")
    r = await ask2(other, {"action": "topup.confirm", "session": sid})
    check("isn't one this relay started" in r.get("error", ""), "another account can't claim it")
    r = await ask2(app, {"action": "topup.start", "usd": 20, "return_to": "http://192.168.1.20:8765/"})
    sid2 = r["session"]
    check(STRIPE.sessions[sid2]["params"]["success_url"].startswith("http://192.168.1.20:8765/?topup="),
          "ReedOS on your own network gets sent back there")
    STRIPE.pay(sid2)
    n = await asyncio.to_thread(funding.reconcile)
    check(n == 1 and abs(accounts.balance(aid) - (bal0 + 30)) < 1e-6,
          "closed the tab before coming back: the reconciler credits it anyway")
    check(await asyncio.to_thread(funding.reconcile) == 0, "and only once")
    led = await ask2(app, {"action": "ledger"})
    check(sum(1 for row in led["ledger"] if "Stripe top-up" in row["what"]) == 2, "both top-ups in the ledger")
    await ask2(app, {"action": "setup.forget", "name": "STRIPE_SECRET_KEY"})
    cloud_llm.test = real_llm_test
    await app.close()
    await other.close()
    app2 = await websockets.connect(url + "/app")
    await app2.send(json.dumps({"type": "auth", "token": app_token}))
    await app2.recv()
    print("[15] auto top-up from a saved card")
    from reedus_relay import funding as fnd
    await ask2(app2, {"action": "setup.key", "name": "STRIPE_SECRET_KEY", "value": "sk_test_good"})
    bill = await ask2(app2, {"action": "billing.get"})
    check(bill["card"] is None and not bill["auto"]["enabled"], "no card saved yet, auto top-up off")
    r = await ask2(app2, {"action": "billing.set", "enabled": True})
    check("save a card first" in r.get("error", ""), "can't switch it on without a card")
    r = await ask2(app2, {"action": "topup.start", "usd": 10, "save_card": True, "auto": {"below": 2, "amount": 20}})
    sid3 = r["session"]
    p = STRIPE.sessions[sid3]["params"]
    check(p.get("customer", "").startswith("cus_") and p.get("payment_intent_data[setup_future_usage]") == "off_session",
          "save my card: Checkout keeps the card under a Stripe customer for this account")
    check(STRIPE.customers[p["customer"]].get("metadata[account]") == aid, "the customer is tagged with the account")
    STRIPE.pay(sid3)
    r = await ask2(app2, {"action": "topup.confirm", "session": sid3})
    check(r.get("credited") and r["card"]["last4"] == "4242" and r["card"]["brand"] == "Visa",
          "paid: $10 in, and the card is remembered (Visa •••• 4242)")
    bill = await ask2(app2, {"action": "billing.get"})
    check(bill["auto"] == {"enabled": True, "below": 2.0, "amount": 20.0}, f"auto top-up on with your numbers ({bill['auto']})")
    st = await ask2(app2, {"action": "status"})
    check(st["billing"]["card"]["last4"] == "4242", "status carries the card for the Pool card")
    check("pm_" not in json.dumps(st["billing"]["card"].get("brand", "")), "only brand and last four are shown")

    def drain_to(target):
        bal = accounts.balance(aid)
        if bal > target:
            accounts.charge(aid, bal - target, "test spend")

    n_int = len(STRIPE.intents)
    drain_to(3.0)
    await asyncio.sleep(0.6)
    check(len(STRIPE.intents) == n_int, "above $2: no charge")
    drain_to(1.5)
    ok = await d.wait_for(lambda: len(STRIPE.intents) == n_int + 1 and accounts.balance(aid) > 20, 4)
    pi = STRIPE.intents[-1] if STRIPE.intents else {}
    check(ok, f"under $2 after a spend: the card is charged and the pool refilled (${accounts.balance(aid):.2f})")
    check(pi.get("amount") == "2000" and pi.get("off_session") == "true" and pi.get("confirm") == "true"
          and pi.get("payment_method", "").startswith("pm_"), "$20, off-session, from the saved card")
    led = await ask2(app2, {"action": "ledger"})
    check(any("Auto top-up, Visa •••• 4242" in row["what"] for row in led["ledger"]), "the ledger says which card")
    drain_to(1.0)
    await asyncio.sleep(0.6)
    check(len(STRIPE.intents) == n_int + 1, "a second dip minutes later: no second charge (10-minute gap)")
    check(fnd.maybe_auto(aid) == {"skipped": "too soon after the last automatic top-up"}, "and it says why")
    fnd.AUTO_GAP_S = 0
    before = len(STRIPE.intents)
    for _ in range(4):
        drain_to(1.0)
        fnd.maybe_auto(aid)
    check(len(STRIPE.intents) - before == fnd.AUTO_PER_DAY - 1, f"never more than {fnd.AUTO_PER_DAY} a day")
    check(fnd.maybe_auto(aid) == {"skipped": "already 3 automatic top-ups today"}, "the daily cap says why")
    from reedus_relay.db import db as _db
    _db().execute("DELETE FROM topups WHERE kind = 'auto'")
    # a retry with the same Idempotency-Key replays Stripe's answer instead of charging again
    keys = [k for k in STRIPE.idem if k.startswith("auto-")]
    check(len(keys) == len(set(keys)) and all(k.startswith(f"auto-{aid}-") for k in keys),
          "every automatic charge carries its own idempotency key")

    STRIPE.decline = "card_declined"
    drain_to(1.0)
    out = fnd.maybe_auto(aid)
    check(out == {"failed": "the card was declined"}, "a declined card: no money, and it says so")
    bill = await ask2(app2, {"action": "billing.get"})
    check(not bill["auto"]["enabled"] and "declined" in bill["auto_failed"], f"auto top-up switches itself off ({bill['auto']}, {bill['auto_failed']!r})")
    check(fnd.maybe_auto(aid) is None, "and stays off")
    STRIPE.decline = "authentication_required"
    r = await ask2(app2, {"action": "billing.set", "enabled": True, "below": 5, "amount": 10})
    check(r.get("ok") and r["auto"] == {"enabled": True, "below": 5.0, "amount": 10.0} and not r["auto_failed"],
          "switching it back on clears the warning and takes new numbers")
    drain_to(4.0)
    out = fnd.maybe_auto(aid)
    check(out == {"failed": "your bank wants to confirm it's you"}, "the bank wants 3-D Secure: stops, explains")
    STRIPE.decline = ""
    r = await ask2(app2, {"action": "billing.set", "enabled": True, "amount": 500})
    check("auto top-ups are $5 to $200" in r.get("error", ""), "auto amounts stay within $5–$200")
    r = await ask2(app2, {"action": "billing.forget_card"})
    check(r.get("ok") and r["card"] is None and not r["auto"]["enabled"]
          and STRIPE.detached and STRIPE.detached[-1].startswith("pm_"),
          "Remove card: detached at Stripe, forgotten here, auto off")
    check(fnd.maybe_auto(aid) is None, "no card, no charges")
    fnd.AUTO_GAP_S = 600.0
    await ask2(app2, {"action": "setup.forget", "name": "STRIPE_SECRET_KEY"})
    await app2.close()

    print("[16] the wake word on the S3")
    ws2 = await websockets.connect(url + "/lite")
    await ws2.send(json.dumps({"type": "hello", "proto": 3, "device": "kitchen", "fw": "test",
                               "token": dev2_token, "rate": 16000, "frame_ms": 20, "bulletin_rev": 0,
                               "caps": ["gate", "local_ring", "bulletin", "wake"]}))
    d2 = Device(ws2)
    ok = await d2.wait_for(lambda: d2.last("welcome") is not None)
    check(ok and d2.last("welcome").get("wake") == "device", "a Lite with the wake cap is told wake: device")
    await d2.say(wake=True)                     # gate + audio (even the old marker frame), no wake message
    await asyncio.sleep(0.4)
    check("wake" not in d2.types() and d2.states()[-1] == "idle", "audio alone doesn't wake it (no openWakeWord)")
    SCRIPT[:] = ["Reedus, tell me a joke about physics"]
    n_before = len(BACKEND.calls)
    await ws2.send(json.dumps({"type": "wake", "score": 0.95}))
    await d2.say(wake=False)                    # the pre-roll and live audio right behind it
    ok = await d2.wait_for(lambda: len(BACKEND.calls) > n_before and "speaking" in d2.states()
                           and d2.states()[-1] == "listening", 6)
    check(ok, f"the S3's wake → listening → thinking → speaking → listening ({d2.states()})")
    check("wake" in d2.types() and {"type": "listen", "open": True} in d2.events,
          "the relay answers the S3's wake with wake + listen")
    check(BACKEND.calls[-1]["messages"][-1]["content"] == "tell me a joke about physics",
          "the name is stripped from what was said after it")
    BACKEND.slow = 0.15
    SCRIPT[:] = ["tell me a long story"]
    await d2.say(wake=False)
    await d2.wait_for(lambda: d2.states()[-1] == "speaking", 6)
    flushes = d2.flushes
    await ws2.send(json.dumps({"type": "wake", "score": 0.97}))
    ok = await d2.wait_for(lambda: d2.flushes > flushes and d2.states()[-1] == "listening", 3)
    check(ok, "the S3's wake while he's talking is a barge-in")
    BACKEND.slow = 0.0
    n_wakes = d2.types().count("wake")
    await ws2.send(json.dumps({"type": "wake", "score": 0.93}))
    await asyncio.sleep(0.3)
    check(d2.types().count("wake") == n_wakes, "a wake while already listening is ignored")
    for _ in range(60):
        await ws2.send(SILENT)
    await d2.wait_for(lambda: d2.states()[-1] == "idle", 4)
    await ws2.close()

    print("[17] signing in without tokens: setup, owner code, invites, device codes")

    async def first(msg):
        """Open /app, send a sign-in message, return (socket, reply)."""
        sock = await websockets.connect(url + "/app")
        await sock.send(json.dumps(msg))
        return sock, json.loads(await sock.recv())

    sock, r = await first({"type": "setup", "name": "Intruder", "code": "admin-secret"})
    check(r["type"] == "auth.fail" and "already set up" in r["error"], "a relay that's set up can't be set up again")
    await sock.close()
    sock, r = await first({"type": "owner", "code": "wrong"})
    check(r["type"] == "auth.fail", "the owner sign-in needs the real setup code")
    await sock.send(json.dumps({"type": "owner", "code": "admin-secret"}))
    r = json.loads(await sock.recv())
    check(r["type"] == "auth.ok" and r["owner"] and r["account"]["id"] == aid and r["token"].startswith("rla_"),
          "a wrong code can be retried; the setup code signs in as the owner with a fresh token")
    owner_tok = r["token"]
    check(accounts.account_for_token(owner_tok)["id"] == aid and accounts.account_for_token(app_token)["id"] == aid,
          "the new browser token works, and so does the old one")
    inv = await ask2(sock, {"action": "invite.create"})
    login = await ask2(sock, {"action": "login.code"})
    check(len(inv.get("code", "")) == 8 and len(login.get("code", "")) == 6 and login["code"].isdigit(),
          "an 8-character invite and a 6-digit sign-in code")
    r = await ask2(sock, {"action": "members"})
    check({m["name"] for m in r["members"]} >= {"Sam", "Broke"} and any(m["owner"] for m in r["members"]),
          "the owner sees everyone")
    b0 = accounts.balance(broke["account"]["id"])
    r = await ask2(sock, {"action": "credit.add", "account": broke["account"]["id"], "usd": 2})
    check(r.get("ok") and abs(accounts.balance(broke["account"]["id"]) - (b0 + 2)) < 1e-9,
          "the owner adds credit by hand")
    r = await ask2(sock, {"action": "credit.add", "account": broke["account"]["id"], "usd": -1})
    check("error" in r and abs(accounts.balance(broke["account"]["id"]) - (b0 + 2)) < 1e-9, "only positive amounts")

    j, r = await first({"type": "join", "invite": inv["code"].lower(), "name": "Alex"})
    check(r["type"] == "auth.ok" and not r["owner"] and r["account"]["name"] == "Alex" and r["token"],
          "an invite makes a new account (codes aren't case-sensitive)")
    alex_tok = r["token"]
    r = await ask2(j, {"action": "members"})
    check("owner" in r.get("error", ""), "a member can't list everyone")
    r = await ask2(j, {"action": "invite.create"})
    check("owner" in r.get("error", ""), "or invite people")
    r = await ask2(j, {"action": "credit.add", "usd": 50})
    check("owner" in r.get("error", ""), "or add credit for free")
    await j.close()
    j, r = await first({"type": "join", "invite": inv["code"], "name": "Again"})
    check(r["type"] == "auth.fail", "an invite works once")
    await j.close()

    rd, r = await first({"type": "redeem", "code": login["code"]})
    check(r["type"] == "auth.ok" and r["account"]["id"] == aid, "a 6-digit code signs another device in")
    rd_tok = r["token"]
    await ask2(rd, {"action": "signout"})
    await rd.close()
    check(accounts.account_for_token(rd_tok) is None, "Sign out kills that device's token")
    rd, r = await first({"type": "redeem", "code": login["code"]})
    check(r["type"] == "auth.fail", "and a sign-in code works once")
    await rd.close()

    print("[18] pairing: the code on Lite's face")
    lw = await websockets.connect(url + "/lite")
    await lw.send(json.dumps({"type": "hello", "proto": 3, "pair": True, "rate": 16000,
                              "caps": ["gate", "wake", "pair"]}))
    pd = Device(lw)
    ok = await pd.wait_for(lambda: pd.last("pair") is not None and pd.last("glance") is not None, 3)
    code = (pd.last("pair") or {}).get("code", "")
    check(ok and len(code) == 6 and code.isdigit() and code in pd.last("glance")["text"],
          f"an unpaired Lite gets a 6-digit code, scrolled on its face ({(pd.last('glance') or {}).get('text')})")
    st = await ask2(sock, {"action": "status"})
    check(st.get("pairing") == 1, "the website can tell a Lite is waiting")
    wrong = "000000" if code != "000000" else "111111"
    r = await ask2(sock, {"action": "pair", "code": wrong, "name": "Den Lite"})
    check("code" in r.get("error", ""), "a wrong code pairs nothing")
    n_dev = len(accounts.devices(aid))
    r = await ask2(sock, {"action": "pair", "code": f"{code[:3]} {code[3:]}", "name": "Den Lite"})
    ok = await pd.wait_for(lambda: pd.last("paired") is not None, 3)
    tok = (pd.last("paired") or {}).get("token", "")
    check(r.get("ok") and ok and tok.startswith("rld_") and len(accounts.devices(aid)) == n_dev + 1,
          "typing the code pairs it: the Lite is handed its own token")
    await asyncio.sleep(0.8)
    check(pd.task.done(), "and the relay closes the pairing link so it reconnects")
    r = await ask2(sock, {"action": "pair", "code": code, "name": "Twice"})
    check("error" in r, "a code can't be used again")
    lw = await websockets.connect(url + "/lite")
    await lw.send(json.dumps({"type": "hello", "proto": 3, "token": tok, "rate": 16000, "caps": ["gate", "wake", "pair"]}))
    pd = Device(lw)
    ok = await pd.wait_for(lambda: pd.last("welcome") is not None, 3)
    check(ok and pd.last("welcome")["device"] == "Den Lite", "it comes back with that token as Den Lite")
    new_id = next(x["id"] for x in accounts.devices(aid) if x["name"] == "Den Lite")
    r = await ask2(sock, {"action": "device.rename", "device": new_id, "name": "Study Lite"})
    check(r.get("ok") and accounts.device(new_id)["name"] == "Study Lite", "rename a Lite")
    j, _ = await first({"type": "auth", "token": alex_tok})
    r = await ask2(j, {"action": "device.remove", "device": new_id})
    check("no such device" in r.get("error", ""), "nobody else can remove your Lite")
    await j.close()
    r = await ask2(sock, {"action": "device.remove", "device": new_id})
    ok = await pd.wait_for(lambda: pd.last("unpaired") is not None and pd.task.done(), 3)
    check(r.get("ok") and ok and accounts.device(new_id) is None, "Remove: the Lite is told it's unpaired and dropped")
    lw = await websockets.connect(url + "/lite")
    await lw.send(json.dumps({"type": "hello", "proto": 3, "token": tok, "rate": 16000, "caps": ["gate", "wake", "pair"]}))
    pd = Device(lw)
    ok = await pd.wait_for(lambda: pd.last("unpaired") is not None and pd.last("pair") is not None, 3)
    check(ok, "its old token now gets unpaired + a fresh pairing code")
    await lw.close()
    try:
        async with websockets.connect(url + "/lite") as old:
            await old.send(json.dumps({"type": "hello", "proto": 3, "token": tok, "rate": 16000}))
            await old.recv()
        check(False, "older firmware without the pair cap still gets 4001")
    except websockets.ConnectionClosed as e:
        check(e.rcvd.code == 4001, "older firmware without the pair cap still gets 4001")

    print("[19] any model, and who hears")
    from reedus_relay import brain as brain_mod
    from reedus_companion.cloud import llm_settings
    r = await ask2(sock, {"action": "llm.set", "provider": "anthropic", "model": "claude-opus-5-5"})
    check(r.get("ok") and r["label"] == "Claude Opus 5.5", "pick any listed model")
    r = await ask2(sock, {"action": "llm.set", "provider": "anthropic", "model": "claude-some-future-model"})
    st = await ask2(sock, {"action": "status"})
    check(r.get("ok") and st["llm"]["model"] == "claude-some-future-model" and st["llm"]["ready"],
          "or type any model id")
    saved_ant = os.environ.pop("ANTHROPIC_API_KEY")
    st = await ask2(sock, {"action": "status"})
    check(st["llm"]["provider"] == "gemini" and st["llm"]["ready"],
          "the picked provider's key is gone: it says Gemini (which has one) instead of a dead pick")
    saved_gem = os.environ.pop("GEMINI_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = saved_ant
    st = await ask2(sock, {"action": "status"})
    check(st["llm"]["provider"] == "anthropic" and st["llm"]["model"] == "claude-some-future-model",
          "only a Claude key: the account's Claude pick is back")
    acc_x = accounts.create_account("Nobody picked", 0.0, False)["account"]
    check(brain_mod.llm_choice(acc_x)[0] == "anthropic",
          "an account that never picked thinks with whatever has a key, not a keyless Gemini")
    check(not speech.stt_ready() and speech.stt_provider() == "gemini", "no Gemini or OpenAI key: nobody can hear")
    su = await ask2(sock, {"action": "setup.status"})
    check(not {i["id"]: i for i in su["items"]}["hearing"]["done"], "and the checklist says hearing needs one")
    os.environ["OPENAI_API_KEY"] = "fake-openai"
    check(speech.stt_provider() == "openai" and speech.stt_ready(), "an OpenAI key alone: OpenAI does the hearing")
    os.environ["GEMINI_API_KEY"] = saved_gem
    check(speech.stt_provider() == "gemini", "Gemini wins when both are there")
    os.environ.pop("OPENAI_API_KEY")
    real_list = cloud_llm.list_models
    cloud_llm.list_models = lambda pid, key=None, base=None: [
        {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "note": ""},
        {"id": "claude-new-thing", "label": "Claude New Thing", "note": ""}]
    r = await ask2(sock, {"action": "models", "provider": "anthropic"})
    check([m["id"] for m in r.get("models", [])] == ["claude-sonnet-5-5", "claude-new-thing"] and not r["error_note"],
          "the website can list every model the key can use")
    cloud_llm.list_models = real_list
    from reedus_companion.cloud import persona_shots
    persona_shots._CACHE = None
    check("my name is" not in persona_shots.block().lower(), "no name-teaching example in his prompt")

    print("[20] the face, the head, drawings, voices, weather, firmware from the app")
    st = await ask2(sock, {"action": "status"})
    own_aid = st["account"]["id"]
    art_tok = accounts.create_device(own_aid, "Art Lite")["token"]
    ws3 = await websockets.connect(url + "/lite")
    await ws3.send(json.dumps({"type": "hello", "proto": 3, "device": "art", "fw": "0.3.0-s3",
                               "token": art_tok, "rate": 16000, "frame_ms": 20, "bulletin_rev": 0,
                               "caps": ["gate", "bulletin", "wake", "draw", "ota"]}))
    art_frames = []

    class ArtDevice(Device):
        async def _recv(self):
            try:
                async for m in self.ws:
                    if isinstance(m, bytes):
                        if m[:1] == b"\x03":
                            art_frames.append(m)
                    else:
                        self.events.append(json.loads(m))
            except Exception:
                pass
    d3 = ArtDevice(ws3)
    await d3.wait_for(lambda: d3.last("welcome") is not None)
    st = await ask2(sock, {"action": "status"})
    art = next(x for x in st["devices"] if x["name"] == "Art Lite")
    check(art["fw"] == "0.3.0-s3" and "draw" in art["caps"], "status shows each Lite's firmware and what it can do")
    r = await ask2(sock, {"action": "device.expr", "device": art["id"], "name": "love", "hold": 2})
    ok = await d3.wait_for(lambda: (d3.last("expr") or {}).get("name") == "love", 2)
    check(r.get("ok") and ok, "an expression from the app reaches the face")
    r = await ask2(sock, {"action": "device.look", "device": art["id"], "angle": 140})
    ok = await d3.wait_for(lambda: (d3.last("look") or {}).get("angle") == 90, 2)
    check(r.get("ok") and ok, "turning the head from the app (clamped to its 90 degrees)")
    import base64 as b64
    red = b64.b64encode(bytes([255, 0, 0]) * 256).decode()
    blue = b64.b64encode(bytes([0, 0, 255]) * 256).decode()
    r = await ask2(sock, {"action": "device.draw", "device": art["id"], "frames": [red, blue], "fps": 6})
    ok = await d3.wait_for(lambda: len(art_frames) == 2, 2)
    check(r.get("ok") and ok and art_frames[0][:6] == bytes([3, 0, 2, 6, 15, 1])
          and art_frames[1][6:9] == bytes([0, 0, 255]) and len(art_frames[1]) == 6 + 768,
          "a two-frame animation goes down as kind-3 frames (index, count, fps, hold, loop, RGB)")
    r = await ask2(sock, {"action": "device.draw", "device": art["id"], "frames": ["AAAA"]})
    check("16x16" in r.get("error", ""), "a frame that isn't 16x16 RGB is refused")
    r = await ask2(sock, {"action": "device.draw", "device": did, "frames": [red]})
    check("error" in r, "only the Lite's own account can draw on it")
    r = await ask2(sock, {"action": "device.draw.clear", "device": art["id"]})
    ok = await d3.wait_for(lambda: (d3.last("draw") or {}).get("op") == "clear", 2)
    check(r.get("ok") and ok, "back to his face")
    # voices
    os.environ["INWORLD_API_KEY"] = ""
    r = await ask2(sock, {"action": "voices"})
    check(len(r.get("voices", [])) > 5 and r["source"] == "builtin", "a voice list even when Inworld can't be asked")
    r = await ask2(sock, {"action": "voice.set", "voice": "Dennis", "speed": 1.1})
    check(r.get("ok") and accounts.account(own_aid)["settings"]["voice"] == {"id": "Dennis", "speed": 1.1},
          "picking a voice saves it on the account")
    check(hub.sessions[accounts.device_for_token(art_tok)["id"]].voice_prefs()["id"] == "Dennis",
          "the live Lite speaks with it from its next sentence")
    art_sess = hub.sessions[accounts.device_for_token(art_tok)["id"]]
    r = await ask2(sock, {"action": "listen.set", "patience": "patient", "wake": 0.8, "head": "close"})
    ok = await d3.wait_for(lambda: (d3.last("wake_cfg") or {}).get("cutoff") == 0.80
                           and d3.last("wake_cfg").get("head_near_db") == 6.0, 2)
    check(r.get("ok") and r["listen"]["patience"] == "patient" and ok and art_sess.end_silence() == 1.8,
          "Listening settings save, the Lite gets the new wake cutoff live, and waits longer")
    await ws3.send(json.dumps({"type": "wake_near", "p": 0.74}))
    await asyncio.sleep(0.2)
    st = await ask2(sock, {"action": "status"})
    art = next(x for x in st["devices"] if x["name"] == "Art Lite")
    check(abs(art["wake_near"]["p"] - 0.74) < 1e-6 and st["listen"]["wake"] == 0.80,
          "a near miss on the wake word shows in the app")
    r = await ask2(sock, {"action": "listen.set", "wake": 0.4})
    check("error" in r, "a threshold under 60% is refused")
    r = await ask2(sock, {"action": "listen.set", "head": "everyone-in-town"})
    check("error" in r, "a made-up head choice is refused")
    await ask2(sock, {"action": "listen.set", "patience": "normal", "wake": 0.9, "head": "normal"})
    from reedus_companion.cloud import llm as _llm

    class _QuickModel:
        _think_main = "minimal"
        _search = True

        def chat(self, messages, max_tokens, use_adapter=True, on_token=None, name="", **kw):
            time.sleep(0.05)
            on_token("hello")
            return "hello"

        def close(self):
            pass
    _real_build = _llm.build
    _llm.build = lambda pid, model=None, **kw: _QuickModel()
    r = await ask2(sock, {"action": "llm.probe", "provider": "gemini", "model": "gemini-3.5-flash-lite"})
    _llm.build = _real_build
    check(r.get("ok") and 0.04 <= r["first_word_s"] < 2 and r["thinking"] == "minimal"
          and r["info"]["probe"]["first_word_s"] == r["first_word_s"],
          "Test speed: first-word time and the least thinking the model accepted")
    st = await ask2(sock, {"action": "status"})
    gem = next((x for x in st["providers"] if x["id"] == "gemini"), None)
    check("info" in st["llm"] and (gem is None or all("thinking" in m for m in gem["models"])),
          "the app sees each model's thinking label and the current model's speed")
    r = await ask2(sock, {"action": "think.set", "think": "medium"})
    st = await ask2(sock, {"action": "status"})
    check(r.get("ok") and st.get("think") == "medium" and len(st.get("think_choices", [])) == 4,
          "the thinking level is saved and shown in the app")
    check(hub.sessions[accounts.device_for_token(art_tok)["id"]].brain.account["settings"].get("think") == "medium",
          "...and the live Lite picks it up")
    r = await ask2(sock, {"action": "think.set", "think": "galaxy-brain"})
    check("error" in r, "a made-up thinking level is refused")
    await ask2(sock, {"action": "think.set", "think": "fast"})
    r = await ask2(sock, {"action": "voices"})
    check([m["id"] for m in r.get("models", [])] == ["inworld-tts-2", "inworld-tts-2-flash"]
          and r.get("model_default") == "inworld-tts-2", "the app gets the Quality / Fast voice engines")
    r = await ask2(sock, {"action": "voice.set", "voice": "Dennis", "model": "inworld-tts-2-flash"})
    check(accounts.account(own_aid)["settings"]["voice"].get("model") == "inworld-tts-2-flash",
          "picking the Fast engine saves it")
    r = await ask2(sock, {"action": "voice.set", "voice": "Dennis", "model": "some-other-model"})
    check("model" not in r["voice"], "a made-up engine isn't saved")
    await ask2(sock, {"action": "voice.set", "voice": "Dennis", "model": "inworld-tts-2-flash"})
    seen = []
    real_factory = hub._voice_factory
    hub._voice_factory = lambda prefs=None: (seen.append(prefs), FakeVoice())[1]
    hub.sessions[accounts.device_for_token(art_tok)["id"]]._voice()
    hub._voice_factory = real_factory
    check(seen and (seen[0] or {}).get("model") == "inworld-tts-2-flash" and seen[0].get("id") == "Dennis",
          "the Lite's voice is actually built with the account's choice (it used to fall back to the default)")
    check(speech.voice_model({"model": "inworld-tts-2-flash"}) == "inworld-tts-2-flash"
          and speech.voice_model({}) == "inworld-tts-2" and speech.voice_model({"model": "x"}) == "inworld-tts-2",
          "unknown or missing engine means Quality")
    # weather card (Open-Meteo faked)
    from reedus_relay import weather as wmod
    calls = []

    def fake_json(u):
        calls.append(u)
        if "geocoding" in u:
            return {"results": [{"name": "Blacksburg", "admin1": "South Carolina", "latitude": 35.1, "longitude": -81.5},
                                {"name": "Blacksburg", "admin1": "Virginia", "latitude": 37.23, "longitude": -80.41}]}
        return {"current": {"temperature_2m": 51.6, "weather_code": 3, "is_day": 1},
                "daily": {"temperature_2m_max": [58], "temperature_2m_min": [39]}}
    wmod._json = fake_json
    check(wmod.geocode("Blacksburg, VA")[2] == "Blacksburg, Virginia", "'Blacksburg, VA' finds the Virginia one")
    r = await ask2(sock, {"action": "glance.set", "weather": True, "place": "Blacksburg, VA", "unit": "F"})
    check(r.get("ok") and r["now"]["temp"] == 52 and r["now"]["icon"] == "cloud", "turning on the weather card checks the place")
    sess3 = hub.sessions[accounts.device_for_token(art_tok)["id"]]
    sess3.art_until = 0.0
    ok = await d3.wait_for(lambda: (d3.last("glance") or {}).get("card") == "weather", 4)
    check(ok and d3.last("glance")["temp"] == 52, "the weather card shows on the idle face")
    check(d3.last("glance")["hold"] >= 600, "...and it stays up (a standing card), not 8 seconds")
    sess3.account = {**sess3.account, "settings": {**(sess3.account.get("settings") or {}),
                     "glance": {**sess3.glance_settings(), "weather": False}}}
    await sess3.glance_round(time.monotonic())
    ok = await d3.wait_for(lambda: (d3.last("glance") or {}).get("hold") == 0, 2)
    check(ok and not sess3.wx_up, "switching it off takes it down")
    sess3.account = {**sess3.account, "settings": {**(sess3.account.get("settings") or {}),
                     "glance": {**sess3.glance_settings(), "weather": True, "every": 90}}}
    sess3.wx_next, sess3.idle_since = 0.0, time.monotonic() - 60
    await sess3.glance_round(time.monotonic())
    ok = await d3.wait_for(lambda: (d3.last("glance") or {}).get("once") is True, 2)
    check(ok and d3.last("glance")["hold"] < 30 and not sess3.wx_up and sess3.wx_next > time.monotonic() + 60,
          "'every 90 s' still works: a few seconds of weather, then the face again")
    r = await ask2(sock, {"action": "glance.set", "every": 0})
    check(r.get("ok") and r["glance"]["every"] == 0, "'always' saves as 0")
    check("52 degrees" in sess3.brain.weather_fact("what's the weather like"),
          "asking about the weather uses it (no web search)")
    wmod._json = lambda u: None
    wmod._geo.clear()
    r = await ask2(sock, {"action": "glance.set", "place": "Nowhereville Zzz"})
    check("couldn't find" in r.get("error", ""), "a place it can't find is refused")
    # firmware
    # NUL before the version, as in a real image: a random digit there read as "70.3.1-s3"
    fake_fw = bytes([0xE9]) + os.urandom(100 * 1024) + b"\x000.3.1-s3\x00" + os.urandom(1000)
    half = len(fake_fw) // 2
    r1 = await ask2(sock, {"action": "fw.upload", "index": 0, "count": 2,
                           "data": b64.b64encode(fake_fw[:half]).decode()})
    r2 = await ask2(sock, {"action": "fw.upload", "upload": r1["upload"], "index": 1, "count": 2,
                           "data": b64.b64encode(fake_fw[half:]).decode()})
    check(r2.get("fw") and r2["size"] == len(fake_fw) and r2["version"] == "0.3.1-s3",
          "firmware uploads in pieces and its version is read from the image")
    r = await ask2(sock, {"action": "fw.upload", "index": 0, "count": 1, "data": b64.b64encode(b"hello").decode()})
    check("ESP32" in r.get("error", ""), "something that isn't firmware is refused")
    r = await ask2(sock, {"action": "device.ota", "device": art["id"], "fw": r2["fw"]})
    ok = await d3.wait_for(lambda: d3.last("ota") is not None, 2)
    check(r.get("ok") and ok, "the Lite is told to update")
    ota = d3.last("ota")
    import hashlib
    import urllib.request
    path = "/fw/" + ota["url"].split("/fw/")[1]
    body = await asyncio.to_thread(lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}{path}").read())
    check(body == fake_fw and ota["md5"] == hashlib.md5(fake_fw).hexdigest() and ota["size"] == len(fake_fw),
          "it downloads exactly that image from the relay, and the md5 matches")
    try:
        await asyncio.to_thread(lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/fw/nope.bin").read())
        check(False, "a made-up firmware link is refused")
    except Exception:
        check(True, "a made-up firmware link is refused")
    await ws3.send(json.dumps({"type": "ota", "state": "downloading", "pct": 40}))
    await asyncio.sleep(0.2)
    st = await ask2(sock, {"action": "status"})
    art = next(x for x in st["devices"] if x["name"] == "Art Lite")
    check(art["ota"]["pct"] == 40, "the update's progress shows in the app")
    r = await ask2(sock, {"action": "device.ota", "device": did, "fw": r2["fw"]})
    check("error" in r, "another account's Lite can't be updated")
    import requests as _rq
    _real_get, _asked = _rq.get, []

    class _Resp:
        status_code = 200

        def json(self):
            return {"target_commitish": "6c5a42cbac8cf033a550ebe604beaad88b320ac3",
                    "published_at": "2026-11-22T18:02:40Z"}

    _rq.get = lambda url, **kw: (_asked.append(url), _Resp())[1]
    hub._fw_check_cache = None
    r = await ask2(sock, {"action": "fw.check"})
    r2 = await ask2(sock, {"action": "fw.check"})
    _rq.get = _real_get
    check(r.get("sha") == "6c5a42c" and "api.github.com/repos/Danial-Sy/ReedusAIAssistant/releases/tags/lite-fw-latest"
          in _asked[0] and len(_asked) == 1 and r2.get("sha") == r["sha"],
          "the app can ask which commit the latest build is from (no download, cached a minute)")
    await ws3.close()
    await sock.close()

    await ws.close()
    await asyncio.sleep(0.2)
    server.close()


asyncio.run(main())
print()
if _fails:
    print(f"{len(_fails)} FAILED")
    for f in _fails:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
