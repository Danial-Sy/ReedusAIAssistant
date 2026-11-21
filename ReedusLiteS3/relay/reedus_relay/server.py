"""the relay's front door: one websockets server on Railway's $PORT.

    wss://<relay>/lite    Reedus Lite (S3) devices. hello carries the device token, or asks
                          to pair: the relay shows a 6-digit code on Lite's face
    wss://<relay>/app     the website: sign in first (auth / setup / join / redeem / owner),
                          then {"id","action",…}
    GET /                 the website (ReedusLiteS3/web)
    GET /relay.json       is this relay set up yet, does setup need a code
    GET /health           Railway's health check

see ReedusLiteS3/PROTOCOL.md for every action.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import threading
import time
from http import HTTPStatus
from typing import Callable, Dict, Optional

from reedus_relay import accounts, config, memory, meter, speech
from reedus_relay.session import Session

INVITE_TTL_S = 7 * 86400
LOGIN_TTL_S = 10 * 60
PAIR_REFRESH_S = 20.0
_MAX_BAD_CODES = 6          # wrong codes per connection before it's dropped


class Hub:
    def __init__(self, voice_factory: Optional[Callable] = None, session_kwargs: Optional[dict] = None):
        self.sessions: Dict[str, Session] = {}
        self.pairing: Dict[str, dict] = {}       # 6-digit code -> a Lite waiting to be paired
        self._vt = threading.local()
        self._voice_factory = voice_factory
        self.session_kwargs = session_kwargs or {}
        self.started = time.time()
        self._bad_codes: list = []               # times of wrong codes, relay-wide (throttle)

    def voice(self):
        if self._voice_factory is not None:
            return self._voice_factory()
        from reedus_relay import vault
        v = getattr(self._vt, "v", None)
        if v is None or getattr(self._vt, "ver", None) != vault.VERSION:
            v = self._vt.v = speech.Voice()        # one Inworld session per worker thread
            self._vt.ver = vault.VERSION
        return v

    def account_sessions(self, aid: str):
        return [s for s in list(self.sessions.values()) if s.aid == aid]

    def push_bulletin(self, aid: str) -> None:
        for s in self.account_sessions(aid):
            s.send_bulletin()

    def refresh_account(self, aid: str) -> None:
        acc = accounts.account(aid)
        if not acc:
            return
        for s in self.account_sessions(aid):
            s.account = acc
            s.brain.refresh_account(acc)

    # ---------- devices ----------
    async def device(self, ws) -> None:
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=8.0))
        except Exception:                                        # noqa: BLE001
            await ws.close(4000, "expected hello")
            return
        if hello.get("type") != "hello":
            await ws.close(4000, "expected hello")
            return
        token = str(hello.get("token") or "")
        dev = await asyncio.to_thread(accounts.device_for_token, token) if token else None
        if not dev:
            if hello.get("pair") or "pair" in (hello.get("caps") or []):
                if token:       # removed from its account (or a new database): forget the old token
                    await ws.send(json.dumps({"type": "unpaired"}))
                await self.pair_wait(ws, hello)
                return
            await ws.close(4001, "bad token")
            return
        if int(hello.get("rate") or config.RATE) != config.RATE:
            await ws.close(4002, f"rate must be {config.RATE}")
            return
        acc = await asyncio.to_thread(accounts.account, dev["account_id"])
        old = self.sessions.get(dev["id"])
        if old is not None:
            old.close()
            try:
                await old.ws.close(4003, "replaced")
            except Exception:                                    # noqa: BLE001
                pass
        session = Session(self, ws, dev, acc, hello, self.voice, **self.session_kwargs)
        self.sessions[dev["id"]] = session
        await asyncio.to_thread(accounts.touch, dev["id"])
        print(f"[relay] {dev['name']} ({acc['name']}) connected, fw {hello.get('fw', '?')}", flush=True)
        await session.welcome()
        sender = asyncio.ensure_future(session.sender())
        chores = asyncio.ensure_future(session.housekeeping())
        try:
            async for frame in ws:
                if isinstance(frame, (bytes, bytearray, memoryview)):
                    session.on_frame(bytes(frame))
                else:
                    try:
                        session.on_text(json.loads(frame))
                    except ValueError:
                        pass
        except Exception as e:                                   # noqa: BLE001
            print(f"[relay] {dev['name']} link dropped: {type(e).__name__}", flush=True)
        finally:
            sender.cancel()
            chores.cancel()
            session.close()
            if self.sessions.get(dev["id"]) is session:
                del self.sessions[dev["id"]]
            await asyncio.to_thread(accounts.touch, dev["id"])
            print(f"[relay] {dev['name']} disconnected", flush=True)

    # ---------- pairing: a Lite with no token shows a code, the website claims it ----------
    def _new_pair_code(self) -> str:
        while True:
            code = f"{secrets.randbelow(10 ** 6):06d}"
            if code not in self.pairing:
                return code

    async def pair_wait(self, ws, hello: dict) -> None:
        code = self._new_pair_code()
        done = asyncio.get_running_loop().create_future()
        self.pairing[code] = {"ws": ws, "hello": hello, "done": done, "since": time.time()}
        print(f"[relay] a Lite is waiting to pair, code {code}", flush=True)

        async def drain():              # status, battery, a stray frame: ignored until it's paired
            async for _ in ws:
                pass

        reader = asyncio.ensure_future(drain())
        try:
            while not done.done() and not reader.done():
                await ws.send(json.dumps({"type": "pair", "code": code}))
                await ws.send(json.dumps({"type": "glance", "card": "text",
                                          "text": f"PAIR CODE {code}", "hold": PAIR_REFRESH_S + 5}))
                await asyncio.wait([done, reader], timeout=PAIR_REFRESH_S,
                                   return_when=asyncio.FIRST_COMPLETED)
            if done.done():
                dev, token = done.result()
                await ws.send(json.dumps({"type": "paired", "token": token, "name": dev["name"],
                                          "device": dev["id"]}))
                await asyncio.sleep(0.5)
                await ws.close(4004, "paired, reconnect with your token")
        except Exception:                                        # noqa: BLE001
            pass
        finally:
            reader.cancel()
            if self.pairing.get(code, {}).get("done") is done:
                del self.pairing[code]

    def _throttled(self) -> bool:
        now = time.time()
        self._bad_codes = [t for t in self._bad_codes if now - t < 600]
        return len(self._bad_codes) >= 60

    def _bad_code(self) -> None:
        self._bad_codes.append(time.time())

    async def claim_pair(self, aid: str, code: str, name: str) -> dict:
        code = "".join(ch for ch in str(code or "") if ch.isdigit())
        if self._throttled():
            return {"error": "too many wrong codes, wait a few minutes"}
        entry = self.pairing.get(code)
        if not entry or entry["done"].done():
            self._bad_code()
            return {"error": "no Lite is showing that code. Check the digits on its face"}
        out = await asyncio.to_thread(accounts.create_device, aid, (name or "").strip()[:40] or "Reedus Lite")
        entry["done"].set_result((out["device"], out["token"]))
        print(f"[relay] {out['device']['name']} paired", flush=True)
        return {"ok": True, "device": out["device"]}

    # ---------- the website ----------
    async def app(self, ws) -> None:
        bad = 0
        while True:
            try:
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=120.0))
            except Exception:                                    # noqa: BLE001
                await ws.close(4000, "expected auth")
                return
            got = await self.sign_in(first)
            if got.get("ok"):
                break
            bad += 1
            await ws.send(json.dumps({"type": "auth.fail", "error": got.get("error") or "bad token"}))
            if first.get("type") == "auth" or bad >= _MAX_BAD_CODES:
                await ws.close(4001, "bad token")
                return
        acc, admin, token = got["account"], got["admin"], got.get("token") or ""
        hello = {"type": "auth.ok", "admin": admin, "owner": admin or accounts.is_owner(acc),
                 "account": _public_account(acc) if acc else None}
        if got.get("new_token"):
            hello["token"] = got["new_token"]      # the browser keeps this one
            token = got["new_token"]
        await ws.send(json.dumps(hello))
        async for raw in ws:
            try:
                req = json.loads(raw)
            except ValueError:
                continue
            try:
                if req.get("action") == "signout" and acc:
                    await asyncio.to_thread(accounts.drop_app_token, token)
                    out = {"ok": True}
                else:
                    out = await self.app_action(req, acc, admin)
            except Exception as e:                               # noqa: BLE001
                out = {"error": f"{type(e).__name__}: {e}"}
            out["id"] = req.get("id")
            await ws.send(json.dumps(out))
            if acc:
                acc = await asyncio.to_thread(accounts.account, acc["id"]) or acc

    async def sign_in(self, m: dict) -> dict:
        """The first message on /app. {"ok", "account", "admin", "new_token"?} or {"error"}.

            auth    {"token"}            an app token (or RELAY_ADMIN_TOKEN: admin tools)
            setup   {"name", "code"?}    a brand-new relay: the first account, its owner
            owner   {"code"}             the owner, signing in with RELAY_ADMIN_TOKEN
            join    {"invite", "name"}   someone the owner invited makes their account
            redeem  {"code"}             a 6-digit code from a browser that's already signed in
        """
        kind = m.get("type") or ""
        if kind == "auth":
            token = str(m.get("token") or "")
            if config.ADMIN_TOKEN and hmac.compare_digest(token, config.ADMIN_TOKEN):
                return {"ok": True, "account": None, "admin": True}
            acc = await asyncio.to_thread(accounts.account_for_token, token)
            if not acc:
                return {"error": "this browser's sign-in isn't valid any more, sign in again"}
            return {"ok": True, "account": acc, "admin": False, "token": token}
        if self._throttled():
            return {"error": "too many wrong codes, wait a few minutes"}
        if kind == "setup":
            if await asyncio.to_thread(accounts.claimed):
                return {"error": "this relay is already set up, sign in instead"}
            if config.ADMIN_TOKEN and not hmac.compare_digest(str(m.get("code") or "").strip(),
                                                              config.ADMIN_TOKEN):
                self._bad_code()
                return {"error": "that setup code doesn't match RELAY_ADMIN_TOKEN on Railway"}
            name = str(m.get("name") or "").strip()[:40]
            if not name:
                return {"error": "type a name for your account"}
            out = await asyncio.to_thread(accounts.create_account, name, 0.0, True)
            token = await asyncio.to_thread(accounts.new_app_token, out["account"]["id"])
            print(f"[relay] set up by a new owner account", flush=True)
            return {"ok": True, "account": out["account"], "admin": False, "new_token": token}
        if kind == "owner":
            if not config.ADMIN_TOKEN or not hmac.compare_digest(str(m.get("code") or "").strip(),
                                                                 config.ADMIN_TOKEN):
                self._bad_code()
                return {"error": "that isn't this relay's setup code"}
            acc = await asyncio.to_thread(accounts.owner_account)
            if not acc:
                return {"error": "this relay has no owner yet, set it up instead"}
            token = await asyncio.to_thread(accounts.new_app_token, acc["id"])
            return {"ok": True, "account": acc, "admin": False, "new_token": token}
        if kind == "join":
            inviter = await asyncio.to_thread(accounts.use_code, "invite", str(m.get("invite") or ""), True)
            if not inviter:
                self._bad_code()
                return {"error": "that invite link has expired or was already used. Ask for a new one"}
            name = str(m.get("name") or "").strip()[:40]
            if not name:
                return {"error": "type your name"}
            await asyncio.to_thread(accounts.use_code, "invite", str(m.get("invite") or ""))
            out = await asyncio.to_thread(accounts.create_account, name, 0.0, False)
            token = await asyncio.to_thread(accounts.new_app_token, out["account"]["id"])
            return {"ok": True, "account": out["account"], "admin": False, "new_token": token}
        if kind == "redeem":
            aid = await asyncio.to_thread(accounts.use_code, "login", str(m.get("code") or ""))
            acc = await asyncio.to_thread(accounts.account, aid) if aid else None
            if not acc:
                self._bad_code()
                return {"error": "that code didn't work. Codes last 10 minutes"}
            token = await asyncio.to_thread(accounts.new_app_token, acc["id"])
            return {"ok": True, "account": acc, "admin": False, "new_token": token}
        return {"error": "sign in first"}

    async def app_action(self, req: dict, acc: Optional[dict], admin: bool) -> dict:
        a = req.get("action") or ""
        owner = admin or accounts.is_owner(acc)
        if a.startswith("setup."):
            return await asyncio.to_thread(self._setup, a, req, acc, owner)
        if admin:
            return await asyncio.to_thread(self._admin, a, req)
        # fresh every request: balance, billing and settings change behind this socket's back
        # (turns being charged, an auto top-up, another device)
        acc = await asyncio.to_thread(accounts.account, acc["id"]) or acc
        aid = acc["id"]
        if a == "pair":
            return await self.claim_pair(aid, req.get("code"), str(req.get("name") or ""))
        if a == "device.rename":
            dev = await asyncio.to_thread(accounts.device, req.get("device") or "")
            if not dev or dev["account_id"] != aid:
                return {"error": "no such device"}
            await asyncio.to_thread(accounts.rename_device, dev["id"], str(req.get("name") or ""))
            return {"ok": True}
        if a == "device.remove":
            dev = await asyncio.to_thread(accounts.device, req.get("device") or "")
            if not dev or dev["account_id"] != aid:
                return {"error": "no such device"}
            await asyncio.to_thread(accounts.remove_device, dev["id"])
            s = self.sessions.get(dev["id"])
            if s:
                s.send({"type": "unpaired"})
                await asyncio.sleep(0.2)
                try:
                    await s.ws.close(4005, "removed")
                except Exception:                                # noqa: BLE001
                    pass
            return {"ok": True}
        if a == "login.code":
            code = await asyncio.to_thread(accounts.make_code, "login", aid, LOGIN_TTL_S, True)
            return {"code": code, "expires_in": int(LOGIN_TTL_S)}
        if a == "invite.create":
            if not owner:
                return {"error": "only the relay's owner can invite people"}
            code = await asyncio.to_thread(accounts.make_code, "invite", aid, INVITE_TTL_S)
            return {"code": code, "expires_in": int(INVITE_TTL_S)}
        if a == "members":
            if not owner:
                return {"error": "only the relay's owner can see everyone"}
            return {"members": await asyncio.to_thread(accounts.members)}
        if a == "credit.add":
            if not owner:
                return {"error": "only the relay's owner can add credit by hand"}
            try:
                usd = round(float(req.get("usd") or 0), 2)
            except (TypeError, ValueError):
                usd = 0
            if not 0 < usd <= 1000:
                return {"error": "pick an amount between $0.01 and $1000"}
            target = str(req.get("account") or aid)
            if not await asyncio.to_thread(accounts.account, target):
                return {"error": "no such account"}
            bal = await asyncio.to_thread(accounts.top_up, target, usd, "added by the owner")
            return {"ok": True, "balance": round(bal, 4)}
        if a == "models":
            from reedus_companion.cloud import llm, providers
            pid = str(req.get("provider") or "")
            if not providers.known(pid):
                return {"error": f"unknown provider {pid!r}"}
            rows = await asyncio.to_thread(llm.list_models, pid)
            err = next((r.get("error") for r in rows if r.get("error")), "")
            return {"provider": pid, "models": [r for r in rows if r.get("id")], "error_note": err}
        if a == "device.create":
            if not owner:
                return {"error": "only the relay's owner can add a Lite"}
            out = await asyncio.to_thread(accounts.create_device, aid, req.get("name") or "Reedus Lite")
            return {"device": out["device"], "token": out["token"],
                    "secrets_h": _secrets_h(out["token"], req.get("host") or "")}
        if a == "topup.start":
            from reedus_relay import funding
            auto = req.get("auto") if isinstance(req.get("auto"), dict) else None
            return await asyncio.to_thread(funding.start, acc, req.get("usd"), str(req.get("return_to") or ""),
                                           bool(req.get("save_card")), auto)
        if a == "billing.get":
            from reedus_relay import funding
            return funding.billing_status(acc)
        if a == "billing.set":
            from reedus_relay import funding
            return await asyncio.to_thread(funding.set_auto, acc, bool(req.get("enabled")),
                                           req.get("below"), req.get("amount"))
        if a == "billing.forget_card":
            from reedus_relay import funding
            return await asyncio.to_thread(funding.forget_card, acc)
        if a == "topup.confirm":
            from reedus_relay import funding
            return await asyncio.to_thread(funding.confirm, str(req.get("session") or ""), aid)
        if a == "status":
            return await asyncio.to_thread(self._status, acc)
        if a == "llm.set":
            from reedus_companion.cloud import llm_settings, providers
            from reedus_relay.brain import usable
            pid = req.get("provider") or ""
            if not providers.known(pid):
                return {"error": f"unknown provider {pid!r}"}
            if not usable(pid):
                return {"error": f"this relay has no {providers.get(pid)['name']} key yet"}
            model = (req.get("model") or "").strip() or llm_settings.model(pid)
            await asyncio.to_thread(accounts.set_account_settings, aid,
                                    {"llm": {"provider": pid, "model": model}})
            self.refresh_account(aid)
            return {"ok": True, "provider": pid, "model": model,
                    "label": providers.label(pid, model)}
        if a == "memory.get":
            text, rev = await asyncio.to_thread(memory.bulletin, aid)
            turns = await asyncio.to_thread(memory.recent, aid, int(req.get("limit") or 30))
            return {"bulletin": text, "rev": rev, "recent": turns}
        if a == "memory.set":
            rev = await asyncio.to_thread(memory.set_bulletin, aid, str(req.get("bulletin") or ""))
            self.push_bulletin(aid)
            return {"ok": True, "rev": rev}
        if a == "memory.forget":
            await asyncio.to_thread(memory.forget_all, aid)
            self.push_bulletin(aid)
            return {"ok": True}
        if a == "device.set":
            did = req.get("device") or ""
            dev = await asyncio.to_thread(accounts.device, did)
            if not dev or dev["account_id"] != aid:
                return {"error": "no such device"}
            patch = {k: max(0, min(100, int(req[k]))) for k in ("volume", "brightness") if k in req}
            await asyncio.to_thread(accounts.set_device_settings, did, patch)
            s = self.sessions.get(did)
            if s:
                for k, v in patch.items():
                    s.send({"type": k, "level": v})
            return {"ok": True, **patch}
        if a == "ledger":
            return {"ledger": await asyncio.to_thread(accounts.ledger, aid, int(req.get("limit") or 50)),
                    "balance": await asyncio.to_thread(accounts.balance, aid)}
        if a == "chat":
            return await asyncio.to_thread(self._chat, acc, str(req.get("text") or ""))
        if a == "topup":
            return {"error": "use topup.start (Stripe Checkout), or ask the relay owner to top up"}
        return {"error": f"unknown action {a!r}"}

    def _status(self, acc: dict) -> dict:
        from reedus_companion.cloud import providers
        from reedus_relay.brain import llm_choice, usable
        aid = acc["id"]
        pid, model = llm_choice(acc)
        devs = []
        for d in accounts.devices(aid):
            live = self.sessions.get(d["id"])
            devs.append({"id": d["id"], "name": d["name"], "online": live is not None,
                         "state": live.state if live else None, "settings": d["settings"],
                         "last_seen": d["last_seen"],
                         "battery": (live.info.get("battery") if live else None),
                         "status": (live.info.get("status") if live else None)})
        _, rev = memory.bulletin(aid)
        bal = accounts.balance(aid)
        from reedus_relay import funding, vault
        return {"account": _public_account(acc), "balance": round(bal, 4), "billing": funding.billing_status(acc),
                "low": bal < config.LOW_BALANCE_USD, "devices": devs,
                "llm": {"provider": pid, "model": model, "label": providers.label(pid, model),
                        "ready": usable(pid)},
                "providers": [p for p in providers.public() if usable(p["id"])],
                "payments": funding.enabled(), "stripe_mode": vault.stripe_mode(),
                "amounts": funding.AMOUNTS, "pairing": len(self.pairing),
                "bulletin_rev": rev, "stt": speech.stt_provider()}

    def _chat(self, acc: dict, text: str) -> dict:
        """Type to Reedus from the website: same brain and memory, no voice."""
        from reedus_relay.brain import Brain
        if not text.strip():
            return {"error": "empty text"}
        if not accounts.funded(acc["id"]):
            return {"error": "the pool is empty, top up first"}
        m = meter.Meter()
        brain = Brain(acc, {"id": "app", "name": "the website"})
        convo = f"app-{acc['id']}-{int(time.time() // 1800)}"   # half-hour chat windows
        t0 = time.perf_counter()
        with meter.metering(m):
            reply = brain.reply(text, convo, lambda s: None)
        accounts.charge(acc["id"], m.usd, f"Chat: {m.summary()}")
        memory.record_turn(acc["id"], "app", convo, text, reply, m.usd)
        return {"text": reply, "usd": round(m.usd, 5), "ms": int((time.perf_counter() - t0) * 1000)}

    # ---------- Setup: the checklist, keys, tests ----------
    def wake_status(self) -> tuple:
        if not hasattr(self, "_wake"):
            try:
                self._wake = speech.wake_status()
            except Exception as e:                               # noqa: BLE001
                self._wake = (False, f"NONE: {e}")
        return self._wake

    def _setup(self, a: str, req: dict, acc: Optional[dict], owner: bool) -> dict:
        from reedus_relay import funding, vault
        if a == "setup.status":
            return self._setup_status(acc, owner)
        if not owner:
            return {"error": "only the relay's owner can change its keys"}
        name = str(req.get("name") or "")
        if not vault.known(name):
            return {"error": f"unknown key {name!r}"}
        if a == "setup.key":
            value = str(req.get("value") or "").strip()
            if not value:
                return {"error": "paste the key first"}
            res = vault.test(name, value)
            if not res["ok"] and not req.get("force"):
                return {"ok": False, "saved": False, "test": res}
            vault.set_key(name, value)
            return {"ok": True, "saved": True, "test": res, **vault.describe(name)}
        if a == "setup.test":
            return {"ok": True, "test": vault.test(name)}
        if a == "setup.forget":
            vault.set_key(name, "")
            return {"ok": True, **vault.describe(name)}
        return {"error": f"unknown setup action {a!r}"}

    def _setup_status(self, acc: Optional[dict], owner: bool) -> dict:
        """What this relay still needs, in plain words. The owner sees every step; anyone else
        sees pairing their Lite and their credit, plus whether the relay itself is ready."""
        from reedus_companion.cloud import llm_settings, providers
        from reedus_relay import funding, vault
        from reedus_relay.brain import llm_choice, usable
        brains = []
        for p in providers.public():
            if p["id"] == "custom":
                continue
            brains.append({**p, "key": vault.describe(p["env"]) if owner else {"set": usable(p["id"])}})
        have_brain = any(usable(b["id"]) for b in brains)
        stt = speech.stt_provider()
        hearing = speech.stt_ready()
        voice = bool(os.environ.get("INWORLD_API_KEY"))
        devs = accounts.devices(acc["id"]) if acc else []
        online = sum(1 for d in devs if d["id"] in self.sessions)
        bal = accounts.balance(acc["id"]) if acc else 0.0
        pid, model = llm_choice(acc)
        relay_items = [
            {"id": "brain", "title": "AI key", "done": have_brain,
             "detail": f"thinking with {providers.label(pid, model)}" if have_brain
             else "Gemini, Claude, GPT… any one works"},
            {"id": "hearing", "title": "Hearing", "done": hearing,
             "detail": f"{providers.get(stt)['short']} turns speech into text" if hearing
             else "needs a Gemini or OpenAI key (Claude and the rest can't hear)"},
            {"id": "voice", "title": "Voice", "done": voice,
             "detail": "Inworld" if voice else "an Inworld key, so Lite can talk"},
        ]
        mine = [
            {"id": "lite", "title": "Pair your Lite", "done": bool(devs),
             "detail": (f"{len(devs)} paired, {online} online" if devs
                        else "type the code on Lite's face")},
            {"id": "money", "title": "Add credit", "done": bal > 0,
             "detail": f"${bal:.2f} left" if bal > 0 else "every reply costs about a cent"},
        ]
        relay_ready = all(i["done"] for i in relay_items)
        items = (relay_items if owner else []) + mine
        if owner:
            items.append({"id": "payments", "title": "Card payments", "done": funding.enabled(),
                          "optional": True,
                          "detail": f"Stripe, {vault.stripe_mode()} mode" if funding.enabled()
                          else "a Stripe key, so people can top up with a card"})
        out = {"owner": owner, "items": items, "relay_ready": relay_ready,
               "ready": all(i["done"] for i in items if not i.get("optional")) and relay_ready,
               "payments": funding.enabled(), "stripe_mode": vault.stripe_mode(),
               "amounts": funding.AMOUNTS, "public_url": funding.public_base()}
        if owner:
            out["brains"] = brains
            out["services"] = [{**svc, "env": name, "key": vault.describe(name)}
                               for name, svc in vault.SERVICES.items()]
        return out

    def _admin(self, a: str, req: dict) -> dict:
        if a == "account.create":
            out = accounts.create_account(req.get("name") or "Reedus", float(req.get("balance") or 0))
            return {"account": _public_account(out["account"]), "token": out["token"]}
        if a == "device.create":
            out = accounts.create_device(req.get("account") or "", req.get("name") or "Reedus Lite")
            return {"device": out["device"], "token": out["token"]}
        if a == "topup":
            bal = accounts.top_up(req.get("account") or "", float(req.get("usd") or 0),
                                  req.get("what") or "top-up")
            return {"ok": True, "balance": bal}
        if a == "accounts":
            from reedus_relay.db import db
            return {"accounts": db().all("SELECT id, name, balance_usd, spent_usd FROM accounts")}
        return {"error": f"unknown admin action {a!r}"}


def _secrets_h(token: str, host: str) -> str:
    """firmware/src/secrets.h for a new Lite, ready to paste (Wi-Fi left for you)."""
    from reedus_relay import funding
    h = (host or funding.public_base()).replace("https://", "").replace("http://", "").strip("/") \
        or "your-relay.up.railway.app"
    local = h.split(":")[0] in ("localhost", "127.0.0.1") or h.startswith(("192.168.", "10."))
    port = int(h.split(":")[1]) if ":" in h else (8080 if local else 443)
    return (
        "// ReedusLiteS3/firmware/src/secrets.h\n"
        "#pragma once\n\n"
        "#define WIFI_SSID \"your-network-name\"   // 2.4 GHz\n"
        "#define WIFI_PASS \"your-password\"\n\n"
        f"#define RELAY_HOST \"{h.split(':')[0]}\"\n"
        f"#define RELAY_PORT {port}\n"
        f"#define RELAY_TLS {0 if local else 1}\n\n"
        f"#define LITE_TOKEN \"{token}\"\n")


def _public_account(acc: Optional[dict]) -> Optional[dict]:
    if not acc:
        return None
    return {"id": acc["id"], "name": acc["name"], "balance": round(float(acc["balance_usd"]), 4),
            "spent": round(float(acc["spent_usd"]), 4), "owner": accounts.is_owner(acc)}


_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
          ".json": "application/json", ".webmanifest": "application/manifest+json",
          ".ico": "image/x-icon", ".woff2": "font/woff2"}


def _response(body: bytes, ctype: str):
    from websockets.datastructures import Headers
    from websockets.http11 import Response
    return Response(200, "OK", Headers([("Content-Type", ctype), ("Content-Length", str(len(body))),
                                        ("Cache-Control", "no-cache"),
                                        ("X-Content-Type-Options", "nosniff")]), body)


def _file_response(path: str):
    """A file of the website (config.UI_DIR), or None. No listings, no escaping the dir."""
    root = config.UI_DIR
    if not root.is_dir():
        return None
    rel = path.lstrip("/") or "index.html"
    try:
        target = (root / rel).resolve()
        target.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        return None
    return _response(target.read_bytes(), _TYPES.get(target.suffix.lower(), "application/octet-stream"))


async def serve(hub: Hub, host: str = config.HOST, port: int = config.PORT):
    from websockets.asyncio.server import serve as ws_serve

    def process_request(connection, request):
        path = request.path.split("?")[0]
        upgrade = request.headers.get("Upgrade", "").lower() == "websocket"
        if path.rstrip("/") in ("/lite", "/app") and upgrade:
            return None
        if path.rstrip("/") == "/health":
            return connection.respond(HTTPStatus.OK, "reedus relay ok\n")
        if path == "/relay.json":
            # the website asks: is this relay set up yet, and does setup need a code?
            return _response(json.dumps({"relay": True, "name": config.RELAY_NAME, "app": "/app",
                                         "claimed": accounts.claimed(),
                                         "setup_code": bool(config.ADMIN_TOKEN)}).encode(),
                             "application/json")
        if not upgrade:
            got = _file_response(path)
            if got is not None:
                return got
            if path in ("/", ""):
                return connection.respond(HTTPStatus.OK, "reedus relay ok\n")
        return connection.respond(HTTPStatus.NOT_FOUND, "not here\n")

    async def handler(ws):
        path = ws.request.path.split("?")[0].rstrip("/")
        if path == "/lite":
            await hub.device(ws)
        elif path == "/app":
            await hub.app(ws)

    return await ws_serve(handler, host, port, process_request=process_request,
                          max_size=2 ** 20, ping_interval=20, ping_timeout=20)
