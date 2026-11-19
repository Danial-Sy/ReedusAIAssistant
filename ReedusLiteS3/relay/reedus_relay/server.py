"""the relay's front door: one websockets server on Railway's $PORT.

    wss://<relay>/lite    Reedus Lite (S3) devices. hello carries the device token
    wss://<relay>/app     ReedOS: {"type":"auth","token":…} first, then {"id","action",…}
    GET /health           Railway's health check

app actions (account token): status, llm.set, memory.get, memory.set, memory.forget,
device.set, ledger, chat. admin actions (RELAY_ADMIN_TOKEN): account.create, device.create,
topup, accounts. see ReedusLiteS3/PROTOCOL.md.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import time
from http import HTTPStatus
from typing import Callable, Dict, Optional

from reedus_relay import accounts, config, memory, meter, speech
from reedus_relay.session import Session


class Hub:
    def __init__(self, voice_factory: Optional[Callable] = None, session_kwargs: Optional[dict] = None):
        self.sessions: Dict[str, Session] = {}
        self._vt = threading.local()
        self._voice_factory = voice_factory
        self.session_kwargs = session_kwargs or {}
        self.started = time.time()

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
        dev = await asyncio.to_thread(accounts.device_for_token, str(hello.get("token") or ""))
        if not dev:
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

    # ---------- ReedOS ----------
    async def app(self, ws) -> None:
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
        except Exception:                                        # noqa: BLE001
            await ws.close(4000, "expected auth")
            return
        token = str(first.get("token") or "")
        admin = bool(config.ADMIN_TOKEN) and hmac.compare_digest(token, config.ADMIN_TOKEN)
        acc = None if admin else await asyncio.to_thread(accounts.account_for_token, token)
        if not admin and not acc:
            await ws.send(json.dumps({"type": "auth.fail", "error": "bad token"}))
            await ws.close(4001, "bad token")
            return
        await ws.send(json.dumps({"type": "auth.ok", "admin": admin,
                                  "owner": admin or accounts.is_owner(acc),
                                  "account": _public_account(acc) if acc else None}))
        async for raw in ws:
            try:
                req = json.loads(raw)
            except ValueError:
                continue
            try:
                out = await self.app_action(req, acc, admin)
            except Exception as e:                               # noqa: BLE001
                out = {"error": f"{type(e).__name__}: {e}"}
            out["id"] = req.get("id")
            await ws.send(json.dumps(out))
            if acc:
                acc = await asyncio.to_thread(accounts.account, acc["id"]) or acc

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
            pid = req.get("provider") or ""
            if not providers.known(pid):
                return {"error": f"unknown provider {pid!r}"}
            if not llm_settings.has_key(pid):
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
        from reedus_companion.cloud import llm_settings, providers
        aid = acc["id"]
        s = (acc.get("settings") or {}).get("llm") or {}
        pid = s.get("provider") or llm_settings.provider()
        model = s.get("model") or llm_settings.model(pid)
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
        from reedus_relay import funding
        return {"account": _public_account(acc), "balance": round(bal, 4), "billing": funding.billing_status(acc),
                "low": bal < config.LOW_BALANCE_USD, "devices": devs,
                "llm": {"provider": pid, "model": model, "label": providers.label(pid, model)},
                "providers": [p for p in providers.public() if llm_settings.has_key(p["id"])
                              and (p["id"] != "custom" or llm_settings.base_url("custom"))],
                "bulletin_rev": rev, "stt": speech.stt_provider()}

    def _chat(self, acc: dict, text: str) -> dict:
        """Type to Reedus from ReedOS: same brain and memory, no voice."""
        from reedus_relay.brain import Brain
        if not text.strip():
            return {"error": "empty text"}
        if not accounts.funded(acc["id"]):
            return {"error": "the pool is empty, top up first"}
        m = meter.Meter()
        brain = Brain(acc, {"id": "app", "name": "ReedOS"})
        convo = f"app-{acc['id']}-{int(time.time() // 1800)}"   # half-hour chat windows
        t0 = time.perf_counter()
        with meter.metering(m):
            reply = brain.reply(text, convo, lambda s: None)
        accounts.charge(acc["id"], m.usd, f"ReedOS chat: {m.summary()}")
        memory.record_turn(acc["id"], "app", convo, text, reply, m.usd)
        return {"text": reply, "usd": round(m.usd, 5), "ms": int((time.perf_counter() - t0) * 1000)}

    # ---------- Setup: the checklist, keys, tests ----------
    def wake_status(self) -> tuple:
        if not hasattr(self, "_wake_desc"):
            try:
                desc = speech.wake_detector().describe()
            except Exception as e:                               # noqa: BLE001
                desc = f"NONE: {e}"
            self._wake_desc = desc
        return (not self._wake_desc.startswith("NONE")), self._wake_desc

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
        from reedus_companion.cloud import llm_settings, providers
        from reedus_relay import funding, vault
        brain_keys = vault.brain_keys()
        brains = []
        for p in providers.public():
            if p["id"] == "custom":
                continue
            brains.append({**p, "key": vault.describe(p["env"]) if owner else {"set": llm_settings.has_key(p["id"])}})
        have_brain = [b for b in brains if b["key"]["set"]]
        devs = accounts.devices(acc["id"]) if acc else []
        online = sum(1 for d in devs if d["id"] in self.sessions)
        wake_ok, wake_desc = self.wake_status()
        bal = accounts.balance(acc["id"]) if acc else 0.0
        llm = {}
        if acc:
            s = (acc.get("settings") or {}).get("llm") or {}
            pid = s.get("provider") or llm_settings.provider()
            llm = {"provider": pid, "label": providers.label(pid, s.get("model") or llm_settings.model(pid))}
        items = [
            {"id": "brain", "title": "Brain", "done": bool(have_brain),
             "detail": (llm["label"] if llm and llm_settings.has_key(llm["provider"]) else "ready")
             if have_brain else "add a key"},
            {"id": "voice", "title": "Voice", "done": bool(os.environ.get("INWORLD_API_KEY")),
             "detail": "Inworld" if os.environ.get("INWORLD_API_KEY") else "add a key"},
            {"id": "lite", "title": "Lite", "done": bool(devs),
             "detail": f"{len(devs)} added, {online} online" if devs else "add one"},
            {"id": "wake", "title": "Wake word", "done": wake_ok,
             "detail": "ready" if wake_ok else "no model yet"},
            {"id": "payments", "title": "Top-ups", "done": funding.enabled(), "optional": True,
             "detail": f"Stripe ({vault.stripe_mode()} mode)" if funding.enabled() else "add a Stripe key"},
            {"id": "pool", "title": "Pool", "done": bal > 0,
             "detail": f"${bal:.2f}" if acc else "sign in"},
        ]
        out = {"owner": owner, "items": items, "ready": all(i["done"] for i in items if not i.get("optional")),
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
            "spent": round(float(acc["spent_usd"]), 4)}


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
    """A file of the ReedOS app (config.UI_DIR), or None. No listings, no escaping the dir."""
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
            # ReedOS loads from the relay when the Xeon is off; this tells it where it is
            return _response(json.dumps({"relay": True, "name": config.RELAY_NAME,
                                         "app": "/app"}).encode(), "application/json")
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
