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
            return "- Their name is Danial.\n- They have a physics exam on Friday."
        text = "Sure thing, mate. That sounds great! What happens next?"
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
got = []
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

# ---------- accounts ----------
print("[2] accounts and the pool")
acc = accounts.create_account("Danial", 5.0)
aid, app_token = acc["account"]["id"], acc["token"]
dev = accounts.create_device(aid, "Desk Lite")
did, dev_token = dev["device"]["id"], dev["token"]
check(accounts.account_for_token(app_token)["id"] == aid, "app token signs in")
check(accounts.device_for_token(dev_token)["id"] == did, "device token signs in")
check(accounts.device_for_token("rld_nope") is None, "bad token refused")
check(accounts.charge(aid, 0.25, "test") == 4.75, "charge comes out of the pool")
check(accounts.top_up(aid, 0.25) == 5.0, "top-up goes back in")
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
                    if m[:1] == b"\x01":
                        self.speech_frames += 1
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

    async def say(self, voiced=15, silence=50, wake=True):
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

    print("[4] a conversation")
    SCRIPT[:] = ["Hey Reedus, I've got a physics exam on Friday"]
    bal0 = accounts.balance(aid)
    await d.say()
    ok = await d.wait_for(lambda: "speaking" in d.states() and d.states()[-1] == "listening", 6)
    check(ok, f"wake → listening → thinking → speaking → listening ({d.states()})")
    check("wake" in d.types(), "wake event sent")
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
    check(auth["type"] == "auth.ok" and auth["account"]["name"] == "Danial", "app signs in with the account token")

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
    r = await ask({"action": "memory.set", "bulletin": "- Their name is Danial.\n- Likes chess."})
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
    ok = await d2.wait_for(lambda: any(e["type"] == "glance" and "TOP UP" in e.get("text", "") for e in d2.events), 3)
    check(ok, "an empty pool shows TOP UP IN REEDOS on the face")
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

    print("[12] the relay serves ReedOS (works with the Xeon off)")

    def get(path):
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3)
            return r.status, r.headers.get("Content-Type", ""), r.read()
        except Exception as e:                                   # noqa: BLE001
            return getattr(e, "code", 0), "", b""
    st, ct, html = await asyncio.to_thread(get, "/")
    check(st == 200 and "text/html" in ct and b"view-standalone" in html, "/ is the ReedOS app")
    st, ct, js = await asyncio.to_thread(get, "/js/relay.js")
    check(st == 200 and "javascript" in ct and b"const Relay" in js, "its scripts are served")
    st, _, rj = await asyncio.to_thread(get, "/relay.json")
    check(st == 200 and json.loads(rj).get("relay") is True, "/relay.json tells the app it's on a relay")
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
    check(set(items) == {"brain", "voice", "lite", "wake", "payments", "pool"}, "a six-step checklist")
    check(items["brain"]["done"] and not items["voice"]["done"] and items["lite"]["done"],
          f"brain ready (env keys), voice missing, a Lite exists ({items['voice']['detail']})")
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
    check("brains" not in r and "services" not in r and len(r["items"]) == 6,
          "non-owners see the checklist, not the keys")
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
