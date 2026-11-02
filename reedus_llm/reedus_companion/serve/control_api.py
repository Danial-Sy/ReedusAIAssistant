#!/usr/bin/env python3
"""the one http door into a running reedus (ReedOS talks to this)."""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from reedus_companion import mode as mode_mod

DEFAULT_HOST = os.environ.get("REEDUS_API_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("REEDUS_API_PORT", "8766"))
TOKEN = (os.environ.get("REEDUS_API_TOKEN") or "").strip()

# how long a typed message queues behind someone talking to him out loud before we say "busy".
# the wait is for the turn lock, not the turn itself.
TURN_WAIT_S = float(os.environ.get("REEDUS_API_TURN_WAIT", "90"))


def budget_info() -> dict:
    """Cloud spend, or {} when the ledger isn't importable."""
    try:
        from reedus_companion.cloud import budget
        return {
            "spent": round(budget.spent(), 2),
            "cap": round(budget.cap(), 2),
            "exhausted": bool(budget.exhausted()),
        }
    except Exception:
        return {}


def cloud_ready() -> dict:
    ok, why = mode_mod.preflight(mode_mod.CLOUD)
    return {"ok": bool(ok), "why": why}


class Hooks:
    """The host's side of the door. Everything returns plain dicts."""

    def snapshot(self) -> dict:
        raise NotImplementedError

    def chat(self, text: str, speak: bool) -> dict:
        raise NotImplementedError

    def say(self, text: str) -> dict:
        raise NotImplementedError

    def listen(self, speak: bool) -> dict:
        raise NotImplementedError

    def set_mode(self, target: str) -> dict:
        raise NotImplementedError

    def lite(self, req: dict) -> dict:
        return {"error": "this brain has no Reedus Lite (start it with run_lite.sh)"}


def _resolve_target(target: str) -> tuple[str, str]:
    """('cloud'|'local', '') or ('', reason)."""
    t = (target or "").strip().lower()
    if t == "toggle":
        t = mode_mod.LOCAL if mode_mod.current() == mode_mod.CLOUD else mode_mod.CLOUD
    if t not in (mode_mod.LOCAL, mode_mod.CLOUD):
        return "", f"unknown mode {target!r}, want local, cloud or toggle"
    return t, ""


class VoiceLoopHooks(Hooks):
    """Embedded in the running voice loop; the runtime dict is voice_loop._RUNTIME."""

    def __init__(self, runtime: dict):
        self.rt = runtime

    def snapshot(self) -> dict:
        rt = self.rt
        return {
            "ok": True,
            "embedded": True,
            "mode": mode_mod.current(),
            "pending": rt.get("pending_mode"),
            "switching": bool(rt.get("switching")),
            "backend": rt.get("backend"),
            "brain": rt.get("reedus") is not None,
            "voice": rt.get("speaker") is not None,
            "busy": rt.get("busy"),
            "micOwner": "lite" if rt.get("lite") is not None else "voiceloop",
            "lite": rt["lite"].snapshot() if rt.get("lite") is not None else None,
            "budget": budget_info(),
            "cloud": cloud_ready(),
        }

    def chat(self, text: str, speak: bool) -> dict:
        rt = self.rt
        reedus = rt.get("reedus")
        if reedus is None:
            return {"error": "the brain is still loading", "retry": True}
        lock = rt.get("turn_lock")
        if lock is None or not lock.acquire(timeout=TURN_WAIT_S):
            return {"error": "he's mid-conversation, try again in a moment", "retry": True}
        try:
            rt["busy"] = "chat"
            t0 = time.perf_counter()
            reply = reedus.reply(text)
            spoke = False
            if speak:
                spoke = _speak_now(rt.get("speaker"), reply)
            return {
                "text": reply,
                "spoke": spoke,
                "mode": mode_mod.current(),
                "ms": int((time.perf_counter() - t0) * 1000),
            }
        except Exception as e:                                   # noqa: BLE001
            traceback.print_exc()
            return {"error": f"{type(e).__name__}: {e}"}
        finally:
            rt["busy"] = None
            lock.release()

    def say(self, text: str) -> dict:
        return {"spoke": _speak_now(self.rt.get("speaker"), text)}

    def listen(self, speak: bool) -> dict:
        return {
            "error": "Reedus already has the mic, just talk to him.",
            "micOwner": "voiceloop",
        }

    def set_mode(self, target: str) -> dict:
        rt = self.rt
        want, why = _resolve_target(target)
        if not want:
            return {"accepted": False, "mode": mode_mod.current(), "why": why}
        current = mode_mod.current()
        if want == current and not rt.get("pending_mode"):
            return {"accepted": False, "mode": current, "why": f"already in {current} mode"}
        # checked here on the UI's thread so a missing key is an error now, not a toggle that
        # silently springs back in 20 seconds
        ok, reason = mode_mod.preflight(want)
        if not ok:
            return {"accepted": False, "mode": current, "why": reason}
        rt["pending_mode"] = want
        return {
            "accepted": True,
            "mode": current,
            "pending": want,
            "note": "queued, applies before his next turn",
        }

    def lite(self, req: dict) -> dict:
        hub = self.rt.get("lite")
        if hub is None:
            return {"error": "the brain is driving the robot right now, not Reedus Lite"}
        out = hub.control(req or {})
        out["lite"] = hub.snapshot()
        return out


class StandaloneHooks(Hooks):
    """No voice loop: this process owns the brain. Same mode-switch code path as the voice loop."""

    def __init__(self, args):
        self.args = args
        self.rt = {"reedus": None, "speaker": None, "stt": None,
                   "backend": args.backend, "busy": None, "switching": False}
        self.lock = threading.RLock()

    def _ensure(self):
        if self.rt["reedus"] is not None:
            return
        from reedus_companion.serve.core import Reedus
        from reedus_companion.voice import voice_loop as vl
        args = self.args
        if mode_mod.current() == mode_mod.CLOUD and mode_mod.preflight(mode_mod.CLOUD)[0]:
            args.backend = "gemini"
        if args.speak and self.rt["speaker"] is None:
            from reedus_companion.voice.tts import TTS
            from reedus_companion.voice.stream_speaker import StreamSpeaker
            from reedus_companion.serve.core import scrub
            tts = TTS()
            tts.warm()
            self.rt["speaker"] = StreamSpeaker(tts, debug=False, scrub=scrub)
        vl._RUNTIME["args"] = args
        vl._RUNTIME["local_backend"] = getattr(args, "local_backend", "gguf")
        reedus = Reedus(vl.build_backend(args.backend, args))
        try:
            reedus.warm()
        except Exception:
            pass
        self.rt["reedus"] = reedus
        self.rt["backend"] = args.backend

    def snapshot(self) -> dict:
        return {
            "ok": True,
            "embedded": False,
            "mode": mode_mod.current(),
            "pending": None,
            "switching": bool(self.rt.get("switching")),
            "backend": self.rt.get("backend"),
            "brain": self.rt.get("reedus") is not None,
            "voice": self.rt.get("speaker") is not None,
            "busy": self.rt.get("busy"),
            "micOwner": "api" if self.args.mic else None,
            "budget": budget_info(),
            "cloud": cloud_ready(),
        }

    def chat(self, text: str, speak: bool) -> dict:
        if not self.lock.acquire(timeout=TURN_WAIT_S):
            return {"error": "busy", "retry": True}
        try:
            self.rt["busy"] = "chat"
            self._ensure()
            t0 = time.perf_counter()
            reply = self.rt["reedus"].reply(text)
            spoke = _speak_now(self.rt.get("speaker"), reply) if speak else False
            return {"text": reply, "spoke": spoke, "mode": mode_mod.current(),
                    "ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as e:                                   # noqa: BLE001
            traceback.print_exc()
            return {"error": f"{type(e).__name__}: {e}"}
        finally:
            self.rt["busy"] = None
            self.lock.release()

    def say(self, text: str) -> dict:
        return {"spoke": _speak_now(self.rt.get("speaker"), text)}

    def listen(self, speak: bool) -> dict:
        if not self.args.mic:
            return {"error": "this service was started without --mic"}
        if not self.lock.acquire(timeout=TURN_WAIT_S):
            return {"error": "busy", "retry": True}
        try:
            self.rt["busy"] = "listen"
            if self.rt["stt"] is None:
                from reedus_companion.voice.stt import STT
                self.rt["stt"] = STT()
            from reedus_companion.voice.voice_loop import record_until_silence
            audio = record_until_silence()
            heard = (self.rt["stt"].transcribe(audio) or "").strip()
            if not heard:
                return {"heard": "", "text": "", "note": "nothing said"}
            want = mode_mod.switch_request(heard)
            if want:
                out = self.set_mode(want)
                out["heard"] = heard
                return out
            self._ensure()
            reply = self.rt["reedus"].reply(heard)
            spoke = _speak_now(self.rt.get("speaker"), reply) if speak else False
            return {"heard": heard, "text": reply, "spoke": spoke,
                    "mode": mode_mod.current()}
        except Exception as e:                                   # noqa: BLE001
            traceback.print_exc()
            return {"error": f"{type(e).__name__}: {e}"}
        finally:
            self.rt["busy"] = None
            self.lock.release()

    def set_mode(self, target: str) -> dict:
        want, why = _resolve_target(target)
        if not want:
            return {"accepted": False, "mode": mode_mod.current(), "why": why}
        current = mode_mod.current()
        if want == current:
            return {"accepted": False, "mode": current, "why": f"already in {current} mode"}
        ok, reason = mode_mod.preflight(want)
        if not ok:
            return {"accepted": False, "mode": current, "why": reason}
        if not self.lock.acquire(timeout=TURN_WAIT_S):
            return {"accepted": False, "mode": current, "why": "busy"}
        try:
            self.rt["switching"] = True
            self._ensure()
            from reedus_companion.voice import voice_loop as vl
            speaker, name = vl._live_switch(
                self.rt["reedus"], self.rt.get("speaker"), self.rt["backend"], want)
            self.rt["speaker"], self.rt["backend"] = speaker, name
            return {"accepted": True, "mode": mode_mod.current(), "pending": None}
        except Exception as e:                                   # noqa: BLE001
            traceback.print_exc()
            return {"accepted": False, "mode": mode_mod.current(),
                    "why": f"{type(e).__name__}: {e}"}
        finally:
            self.rt["switching"] = False
            self.lock.release()


def _speak_now(speaker, text: str) -> bool:
    """Say it and wait. False (never raises) when there is no voice."""
    if speaker is None or not (text or "").strip():
        return False
    try:
        speaker.reset()
    except Exception:
        pass
    try:
        speaker.say(text)
        speaker.finish(timeout=180.0)
        return True
    except Exception:
        traceback.print_exc()
        return False


INDEX = """Reedus control API, he is {mode} right now.

  GET  /api/health                 state snapshot
  GET  /api/mode                   current + pending mode
  POST /api/mode    {{"mode":"cloud"|"local"|"toggle"}}
  POST /api/chat    {{"text":"...","speak":false}}
  POST /api/speak   {{"text":"..."}}
  POST /api/listen  {{"speak":false}}
  GET  /api/lite                   Reedus Lite link, battery, face
  POST /api/lite    {{"volume":70,"brightness":80,"look":30,"expr":"wink"}}
  GET  /api/connections            Spotify + MCP servers (Composio, Zapier, Home Assistant…)
  POST /api/connections {{"action":"add","preset":"composio","key":"ck_…"}}

ReedOS talks to this through reedosd; curl works too:
  curl -s localhost:{port}/api/health
  curl -s localhost:{port}/api/chat -d '{{"text":"you awake?"}}'
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "reedus-control/1.0"
    hooks: Hooks = None

    def log_message(self, fmt, *a):          # the voice loop owns stdout
        pass

    def _authed(self, query: dict) -> bool:
        if not TOKEN:
            return True
        sent = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return sent == TOKEN or (query.get("token") or [""])[0] == TOKEN

    def _send(self, obj, code: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return {}

    def do_OPTIONS(self):                                        # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):                                            # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._send({"error": "unauthorized"}, 401)
        path = u.path.rstrip("/") or "/"
        h = type(self).hooks
        if path in ("/api", "/"):
            body = INDEX.format(mode=mode_mod.current(), port=self.server.server_address[1])
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if path == "/api/health":
            return self._send(h.snapshot())
        if path == "/api/mode":
            s = h.snapshot()
            return self._send({k: s.get(k) for k in
                               ("mode", "pending", "switching", "budget", "cloud")})
        if path == "/api/connections":
            from reedus_companion.connect.manager import manager
            return self._send(manager().status())
        if path == "/api/lite":
            lite = h.snapshot().get("lite")
            return self._send({"lite": lite} if lite is not None else
                              {"error": "no Reedus Lite on this brain", "lite": None}, 200)
        return self._send({"error": "no such endpoint"}, 404)

    def do_POST(self):                                           # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._send({"error": "unauthorized"}, 401)
        path = u.path.rstrip("/") or "/"
        data = self._body()
        h = type(self).hooks
        try:
            if path == "/api/chat":
                text = str(data.get("text") or "").strip()
                if not text:
                    return self._send({"error": "empty text"}, 400)
                return self._send(h.chat(text, bool(data.get("speak"))))
            if path == "/api/speak":
                text = str(data.get("text") or "").strip()
                if not text:
                    return self._send({"error": "empty text"}, 400)
                return self._send(h.say(text))
            if path == "/api/listen":
                return self._send(h.listen(bool(data.get("speak", True))))
            if path == "/api/mode":
                out = h.set_mode(str(data.get("mode") or "toggle"))
                return self._send(out, 200 if out.get("accepted") else 409)
            if path == "/api/lite":
                out = h.lite(data)
                return self._send(out, 409 if out.get("error") else 200)
            if path == "/api/connections":
                from reedus_companion.connect.manager import manager
                out = manager().act(data)
                return self._send(out, 409 if out.get("error") else 200)
        except Exception as e:                                   # noqa: BLE001
            traceback.print_exc()
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._send({"error": "no such endpoint"}, 404)


def serve(hooks: Hooks, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          background: bool = True):
    """Start the server. Returns (server, thread|None)."""
    handler = type("_BoundHandler", (_Handler,), {"hooks": hooks})
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        print(f"[control-api] not started, {e} (port {port} busy?)")
        return None, None
    httpd.daemon_threads = True
    where = f"http://{host}:{port}/api"
    if not background:
        print(f"[control-api] serving {where}")
        httpd.serve_forever()
        return httpd, None
    t = threading.Thread(target=httpd.serve_forever, name="control-api", daemon=True)
    t.start()
    print(f"[control-api] {where}  (ReedOS talks to this)")
    return httpd, t


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Reedus control API (standalone brain)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--backend", default="gguf", choices=["gguf", "hf", "server", "gemini"])
    ap.add_argument("--gguf", default=None)
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--merged", default=None)
    ap.add_argument("--speak", action="store_true", help="speak replies out of the box")
    ap.add_argument("--mic", action="store_true", help="allow /api/listen on the box mic")
    ap.add_argument("--mode", default=None, choices=["local", "cloud"])
    args = ap.parse_args()
    args.local_backend = args.backend
    args._announce = None
    if args.mode:
        mode_mod.set_current(args.mode)
    print(f"[control-api] standalone, {mode_mod.current()} mode, backend {args.backend}")
    print("[control-api] the voice loop must NOT be running: it would be a second brain.")
    serve(StandaloneHooks(args), args.host, args.port, background=False)


if __name__ == "__main__":
    main()
