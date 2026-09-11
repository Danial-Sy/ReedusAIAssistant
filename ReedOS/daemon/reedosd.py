#!/usr/bin/env python3
"""reedosd, the ReedOS service layer."""

import asyncio
import json
import logging
import pathlib
import socket
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from robot_bridge import RobotBridge
from routine_engine import RoutineEngine
from chat_bridge import ChatBridge
from brain_link import BrainLink
from auth import COOKIE, COOKIE_MAX_AGE, Passcode
from camera import Camera

log = logging.getLogger("reedosd")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

HERE = pathlib.Path(__file__).parent
SETTINGS_FILE = HERE / "settings.json"

HOST = "0.0.0.0"
PORT = 8765
PICO_HOST = "reedus.local"      # or a fixed IP; overridable in settings.json
PICO_PORT = 8266

app = FastAPI(title="reedosd", version="0.1.0")


class Hub:
    """Tracks connected UI/phone clients and fans events out to them."""

    def __init__(self):
        self.clients: dict[WebSocket, str] = {}   # ws -> client kind

    async def broadcast(self, type_: str, data: dict):
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(json.dumps({"type": type_, "data": data}))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.pop(ws, None)

    def kinds(self):
        return set(self.clients.values())

    async def kick_all(self, why: str = "passcode changed"):
        """Sign every device out. An open socket outlives the token it logged in with."""
        for ws in list(self.clients):
            try:
                await ws.send_text(json.dumps({"type": "locked", "data": {"why": why}}))
                await ws.close(code=4401)
            except Exception:
                pass
            self.clients.pop(ws, None)


hub = Hub()
settings: dict = {}


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings():
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except Exception as e:
        log.warning("could not persist settings: %s", e)


settings = load_settings()

gate = Passcode(HERE / "auth.json")

OPEN_PATHS = ("/api/login", "/api/auth")


@app.middleware("http")
async def passcode_gate(request: Request, call_next):
    path = request.url.path
    guarded = path.startswith("/api/") or path.startswith("/camera")
    if guarded and path not in OPEN_PATHS and not gate.authorised(request):
        return JSONResponse({"error": "passcode required", "locked": True}, status_code=401)
    return await call_next(request)


bridge = RobotBridge(
    host=settings.get("_pico_host", PICO_HOST),
    port=settings.get("_pico_port", PICO_PORT),
)
camera = Camera(device=settings.get("_camera_device", 0))
brain = BrainLink(settings)
chat = ChatBridge(settings, brain)
engine = RoutineEngine(bridge, chat)


def host_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


async def status_payload() -> dict:
    # BrainLink caches this for 2s, so the 3s status pump is one cheap localhost GET
    # and a dead brain fails instantly instead of stalling.
    b = await asyncio.to_thread(brain.state)
    return {
        **b,
        "robot": "on" if bridge.connected else "off",
        "phone": "on" if "phone" in hub.kinds() else "off",
        "chat": "on" if chat.available else "off",
        "cameraUrl": f"http://{host_ip()}:{PORT}/camera/stream" if camera.available else None,
        "hostIp": host_ip(),
        "locked": gate.enabled,
    }


async def push_status():
    await hub.broadcast("status", await status_payload())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if gate.enabled and not gate.authorised(ws):
        await ws.accept()
        await ws.send_text(json.dumps({"type": "locked", "data": {"why": "passcode required"}}))
        await ws.close(code=4401)
        return
    await ws.accept()
    hub.clients[ws] = "ui"
    await ws.send_text(json.dumps({"type": "status", "data": await status_payload()}))
    await hub.broadcast("log", {"level": "info", "msg": "client connected"})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await handle_message(ws, m)
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.pop(ws, None)
        await push_status()


async def handle_message(ws: WebSocket, m: dict):
    t, data, mid = m.get("type"), m.get("data") or {}, m.get("id")

    if t == "hello":
        kind = "phone" if "phone" in str(data.get("client", "")) else "ui"
        hub.clients[ws] = kind
        await push_status()

    elif t == "drive":
        limit = settings.get("speedLimit", 100) / 100.0
        await bridge.drive(
            max(-limit, min(limit, float(data.get("l", 0)))),
            max(-limit, min(limit, float(data.get("r", 0)))),
        )

    elif t == "twist":
        # joystick / arrow keys, forward + turn, PID holds the speed
        limit = settings.get("speedLimit", 100) / 100.0
        await bridge.twist(
            max(-limit, min(limit, float(data.get("fwd", 0)))),
            max(-limit, min(limit, float(data.get("turn", 0)))),
        )

    elif t == "move":
        await bridge.move_mm(float(data.get("mm", 0)))

    elif t == "turn":
        await bridge.turn_deg(float(data.get("deg", 0)))

    elif t == "stop":
        engine.abort()
        await bridge.stop()
        await hub.broadcast("log", {"level": "warn", "msg": "STOP issued"})

    elif t == "settings.set":
        settings.update({k: v for k, v in data.items() if not k.startswith("_")})
        save_settings()
        await bridge.push_config(settings)
        brain.configure(settings)
        chat.configure(settings)
        await hub.broadcast("settings", settings)

    elif t == "chat":
        route = str(data.get("route") or settings.get("chatRoute") or "device")
        want_speech = bool(data.get("speak", settings.get("chatSpeak", False)))
        reply = await chat.ask(str(data.get("text", "")), speak=want_speech and route == "mobo")
        reply["tts"] = bool(want_speech and not reply.get("spoke"))
        reply["route"] = route
        await ws.send_text(json.dumps({"type": "chat.reply", "id": mid, "data": reply}))

    elif t == "listen":
        out = await asyncio.to_thread(brain.listen, bool(data.get("speak", True)))
        if out.get("heard"):
            await hub.broadcast("chat.heard", {"text": out["heard"]})
        await ws.send_text(json.dumps({"type": "chat.reply", "id": mid, "data": {
            "text": out.get("text") or out.get("error") or "",
            "heard": out.get("heard", ""),
            "tts": False,
            "spoke": bool(out.get("spoke")),
            "error": out.get("error"),
        }}))

    elif t == "mode.set":
        out = await asyncio.to_thread(brain.set_mode, str(data.get("mode") or "toggle"))
        if out.get("error") and "accepted" not in out:
            out = {"accepted": False,
                   "why": "Reedus's brain isn't running, start the voice loop on the box.",
                   "detail": out["error"]}
        await hub.broadcast("mode", out)
        if out.get("why"):
            await hub.broadcast("log", {"level": "warn", "msg": f"mode: {out['why']}"})
        elif out.get("accepted"):
            await hub.broadcast("log",
                                {"level": "info", "msg": f"mode → {out.get('pending')} (queued)"})
        await push_status()

    elif t == "routine.run":
        asyncio.create_task(engine.run(data, hub))

    elif t == "selftest":
        asyncio.create_task(run_selftest())


async def run_selftest():
    """Mirrors test_both_motors.py: each wheel fwd/rev, then both together."""
    await hub.broadcast("log", {"level": "info", "msg": "self-test: LEFT forward"})
    for l, r, label in [
        (0.4, 0, "LEFT forward"), (-0.4, 0, "LEFT reverse"), (0, 0, "LEFT stop"),
        (0, 0.4, "RIGHT forward"), (0, -0.4, "RIGHT reverse"), (0, 0, "RIGHT stop"),
        (0.4, 0.4, "BOTH forward"), (0, 0, "done"),
    ]:
        await bridge.drive(l, r)
        await hub.broadcast("log", {"level": "info", "msg": f"self-test: {label}"})
        await asyncio.sleep(1.5)
    await bridge.stop()
    await hub.broadcast("log", {"level": "info", "msg": "self-test complete"})


@app.get("/api/state")
async def api_state():
    return JSONResponse(await status_payload())


@app.post("/api/login")
async def api_login(req: Request):
    who = (req.client.host if req.client else "?")
    wait = gate.locked_out(who)
    if wait:
        return JSONResponse({"error": f"too many tries, wait {wait}s", "retryAfter": wait},
                            status_code=429)
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not gate.check_code(str(body.get("code") or "")):
        gate.record_fail(who)
        return JSONResponse({"error": "wrong passcode"}, status_code=401)
    gate.clear_fails(who)
    token = gate.token
    r = JSONResponse({"ok": True, "token": token, "locked": gate.enabled})
    if token:
        r.set_cookie(COOKIE, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax")
    return r


@app.post("/api/logout")
async def api_logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@app.get("/api/auth")
async def api_auth(req: Request):
    """Is a passcode set, and am I past it? The lock screen asks this first."""
    return JSONResponse({
        "required": gate.enabled,
        "authed": gate.authorised(req),
        "fromEnv": gate.from_env,
    })


@app.post("/api/passcode")
async def api_set_passcode(req: Request):
    """Change it from the Access card. Everyone else is logged out on purpose."""
    if not gate.authorised(req):
        return JSONResponse({"error": "passcode required", "locked": True}, status_code=401)
    if gate.from_env:
        return JSONResponse({"error": "passcode comes from REEDOS_PASSCODE, change it there"},
                            status_code=409)
    try:
        body = await req.json()
    except Exception:
        body = {}
    code = str(body.get("code") or "").strip()
    if code and len(code) < 4:
        return JSONResponse({"error": "use at least 4 characters"}, status_code=400)
    gate.set_code(code)
    await hub.kick_all("passcode changed")
    token = gate.token
    r = JSONResponse({"ok": True, "required": gate.enabled, "token": token})
    if token:
        r.set_cookie(COOKIE, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax")
    else:
        r.delete_cookie(COOKIE)
    return r


@app.get("/api/mode")
async def api_mode():
    return JSONResponse(await asyncio.to_thread(brain.state))


@app.post("/api/mode")
async def api_set_mode(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    out = await asyncio.to_thread(brain.set_mode, str(body.get("mode") or "toggle"))
    await hub.broadcast("mode", out)
    await push_status()
    return JSONResponse(out, status_code=200 if out.get("accepted") else 409)


@app.get("/api/settings")
async def api_settings():
    return JSONResponse(settings)


@app.get("/camera/stream")
async def camera_stream():
    if not camera.available:
        return JSONResponse({"error": "no camera"}, status_code=503)
    return StreamingResponse(
        camera.mjpeg(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


async def telemetry_pump():
    last_status = 0.0
    while True:
        t = bridge.latest_telemetry()
        if t:
            await hub.broadcast("telemetry", t)
        now = time.time()
        if now - last_status > 3.0:
            await push_status()
            last_status = now
        await asyncio.sleep(0.1)


UI_DIR = pathlib.Path(settings.get("_ui_dir") or (HERE.parent / "reedos-app" / "src"))

if UI_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
else:
    log.warning("UI not found at %s, API only. Set \"_ui_dir\" in settings.json.", UI_DIR)


@app.on_event("startup")
async def startup():
    asyncio.create_task(bridge.run_forever(hub))
    asyncio.create_task(telemetry_pump())
    camera.start()
    ip = host_ip()
    log.info("reedosd up, open ReedOS at http://%s:%s/", ip, PORT)
    if gate.enabled:
        log.info("passcode required%s", " (from REEDOS_PASSCODE)" if gate.from_env else "")
    else:
        log.warning("NO PASSCODE SET, anyone on this network can drive Reedus. "
                    "Set one in App settings → Access, or REEDOS_PASSCODE=...")
    if UI_DIR.is_dir():
        log.info("serving the UI from %s", UI_DIR)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
