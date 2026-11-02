#!/usr/bin/env python3
"""app connections: music intents, the MCP client against a fake server (JSON + SSE + session
expiry), Spotify against a fake API (PKCE, search, no-device retry, refresh), the apps agent,
and the manager. stdlib only, no network, no accounts."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = Path(tempfile.mkdtemp())
os.environ["REEDUS_CONNECTIONS_FILE"] = str(TMP / "connections.json")

_fails = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# [1] intents
print("[1] music intents")
from reedus_companion.connect import intents

yes = {
    "pause": ("pause", {}), "hey reedus, pause the music": ("pause", {}),
    "stop the music please": ("pause", {}), "skip": ("next", {}), "skip this song": ("next", {}),
    "next track": ("next", {}), "go back a song": ("previous", {}), "resume": ("resume", {}),
    "turn it up": ("louder", {}), "turn the music down": ("quieter", {}),
    "volume to 40": ("volume", {"percent": 40}), "set the volume to 70 percent": ("volume", {"percent": 70}),
    "what's this song": ("now", {}), "who sings this": ("now", {}),
    "play some music": ("resume", {}),
    "play blinding lights by the weeknd": ("play", {"query": "blinding lights by the weeknd", "kind": None}),
    "play the album random access memories": ("play", {"query": "random access memories", "kind": "album"}),
    "can you play lofi beats playlist on spotify": ("play", {"query": "lofi beats", "kind": "playlist"}),
    "queue up levitating": ("queue", {"query": "levitating"}),
    "move the music to the kitchen speaker": ("transfer", {"device": "kitchen speaker"}),
    "like this song": ("like", {}),
}
for text, want in yes.items():
    got = intents.parse(text)
    check(got == want, f"{text!r} -> {got}")
for text in ("play a game with me", "let's play twenty questions", "play dead", "stop",
             "what's the weather", "next time remind me", "pause for a second and think about it",
             "can you play chess", "I skipped breakfast"):
    check(intents.parse(text) is None, f"{text!r} stays with the persona")

# [2] MCP client against a fake Streamable HTTP server
print("[2] MCP client")
from reedus_companion.connect.mcp import MCPClient, MCPError

MCP = {"sessions": set(), "sse": False, "calls": [], "auth": "Bearer sekrit", "inits": 0}


class FakeMCP(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != MCP["auth"]:
            self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
        acc = self.headers.get("Accept") or ""
        assert "application/json" in acc and "text/event-stream" in acc
        method, sid = msg.get("method"), self.headers.get("Mcp-Session-Id")
        if method == "initialize":
            MCP["inits"] += 1
            sid = f"s{MCP['inits']}"
            MCP["sessions"].add(sid)
            return self._reply(msg, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                     "serverInfo": {"name": "fake-hub", "version": "1"}}, sid)
        if sid not in MCP["sessions"]:
            self.send_response(404); self.end_headers(); return
        assert self.headers.get("MCP-Protocol-Version") == "2025-06-18"
        if "id" not in msg:
            self.send_response(202); self.end_headers(); return
        if method == "tools/list":
            cur = (msg.get("params") or {}).get("cursor")
            page = [{"name": "calendar_create_event", "description": "Create a calendar event",
                     "inputSchema": {"type": "object", "required": ["title", "start"],
                                     "properties": {"title": {"type": "string"},
                                                    "start": {"type": "string", "description": "ISO time"}}}}] \
                if cur else [{"name": "gmail_send", "description": "Send an email",
                              "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}}}]
            return self._reply(msg, {"tools": page, **({} if cur else {"nextCursor": "p2"})}, sid)
        if method == "tools/call":
            MCP["calls"].append(msg["params"])
            p = msg["params"]
            if p["name"] == "explode":
                return self._reply(msg, {"content": [{"type": "text", "text": "kaboom"}], "isError": True}, sid)
            return self._reply(msg, {"content": [{"type": "text", "text": f"created {p['arguments'].get('title')}"}]}, sid)
        self._reply(msg, None, sid, error={"code": -32601, "message": "no such method"})

    def _reply(self, msg, result, sid, error=None):
        body = {"jsonrpc": "2.0", "id": msg["id"], **({"error": error} if error else {"result": result})}
        self.send_response(200)
        self.send_header("Mcp-Session-Id", sid)
        if MCP["sse"]:
            data = (": ping\n\n"
                    f"data: {json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress', 'params': {}})}\n\n"
                    f"event: message\ndata: {json.dumps(body)}\n\n").encode()
            self.send_header("Content-Type", "text/event-stream")
        else:
            data = json.dumps(body).encode()
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


msrv, murl = serve(FakeMCP)
c = MCPClient(murl, {"Authorization": "Bearer sekrit"})
tools = c.list_tools()
check([t["name"] for t in tools] == ["gmail_send", "calendar_create_event"], "initialize + paginated tools/list")
check(c.server.get("name") == "fake-hub" and c.session == "s1", "session id and server info kept")
check(c.call_tool("calendar_create_event", {"title": "dentist"}) == "created dentist", "tools/call (JSON reply)")
MCP["sse"] = True
check(c.call_tool("calendar_create_event", {"title": "gym"}) == "created gym", "tools/call (SSE reply, skips notifications)")
check(c.call_tool("explode", {}).startswith("[tool error]"), "isError comes back as text, not an exception")
MCP["sessions"].clear()                       # the server restarted
check(c.call_tool("calendar_create_event", {"title": "again"}) == "created again" and c.session == "s2",
      "expired session re-initializes once and retries")
try:
    MCPClient(murl, {"Authorization": "Bearer wrong"}).list_tools()
    check(False, "bad key refused")
except MCPError as e:
    check("refused the key" in str(e), f"bad key refused ({e})")
MCP["sse"] = False

# [3] Spotify against a fake API
print("[3] Spotify")
from reedus_companion.connect import spotify as spmod

SP = {"verifier_ok": False, "plays": [], "active": False, "devices": [{"id": "dev1", "name": "Kitchen speaker", "is_active": False, "type": "Speaker"}],
      "token_n": 0, "volume": 40, "refreshes": 0}
CHALLENGE = {}


class FakeSpotify(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj=None):
        data = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer tok{SP['token_n']}"

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/token":
            f = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            if f["grant_type"][0] == "authorization_code":
                v = f["code_verifier"][0]
                got = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
                SP["verifier_ok"] = got == CHALLENGE.get("c") and f["code"][0] == "abc123"
                if not SP["verifier_ok"]:
                    return self._json(400, {"error": "invalid_grant"})
            else:
                SP["refreshes"] += 1
            SP["token_n"] += 1
            return self._json(200, {"access_token": f"tok{SP['token_n']}", "expires_in": 3600,
                                    "refresh_token": f"ref{SP['token_n']}"})
        if not self._authed():
            return self._json(401, {"error": {"status": 401, "message": "expired"}})
        if u.path == "/v1/me/player/next":
            return self._json(204)
        self._json(404, {"error": {"message": "nope"}})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._authed():
            return self._json(401, {"error": {"status": 401, "message": "expired"}})
        if u.path == "/v1/me":
            return self._json(200, {"display_name": "Danial", "product": "premium"})
        if u.path == "/v1/me/player/devices":
            return self._json(200, {"devices": SP["devices"]})
        if u.path == "/v1/me/player":
            return self._json(200, {"is_playing": True, "device": {"name": "Kitchen speaker", "volume_percent": SP["volume"]},
                                    "item": {"name": "Blinding Lights", "uri": "spotify:track:t1",
                                             "artists": [{"name": "The Weeknd"}], "album": {"name": "After Hours", "images": []}}})
        if u.path == "/v1/search":
            return self._json(200, {
                "tracks": {"items": [{"name": "Blinding Lights", "uri": "spotify:track:t1", "artists": [{"name": "The Weeknd"}]},
                                     {"name": "Daft Punk Is Playing at My House", "uri": "spotify:track:t2", "artists": [{"name": "LCD Soundsystem"}]}]},
                "artists": {"items": [{"name": "Daft Punk", "uri": "spotify:artist:a1"}, {"name": "The Weeknd", "uri": "spotify:artist:a2"}]},
                "albums": {"items": [{"name": "Random Access Memories", "uri": "spotify:album:b1", "artists": [{"name": "Daft Punk"}]}]},
                "playlists": {"items": [None, {"name": "Lofi Beats", "uri": "spotify:playlist:p1"}]},
            })
        self._json(404, {"error": {"message": "nope"}})

    def do_PUT(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._authed():
            return self._json(401, {"error": {"status": 401, "message": "expired"}})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null") if n else None
        if u.path == "/v1/me/player/play":
            if not SP["active"] and "device_id" not in q:
                return self._json(404, {"error": {"status": 404, "message": "Player command failed: No active device found",
                                                  "reason": "NO_ACTIVE_DEVICE"}})
            SP["plays"].append({"body": body, "device": (q.get("device_id") or [None])[0]})
            return self._json(204)
        if u.path == "/v1/me/player/volume":
            SP["volume"] = int(q["volume_percent"][0])
            return self._json(204)
        self._json(404, {"error": {"message": "nope"}})


ssrv, surl = serve(FakeSpotify)
spmod.API, spmod.TOKEN = surl + "/v1", surl + "/api/token"

from reedus_companion.connect.manager import Connections

m = Connections(TMP / "connections.json")
check(not m.any() and m.persona_line() == "" and m.fast_path("pause") is None,
      "nothing connected: no apps line, no fast path")
r = m.act({"action": "start", "id": "spotify", "clientId": "nope"})
check("Client ID" in r.get("error", ""), "a malformed client id is refused")
r = m.act({"action": "start", "id": "spotify", "clientId": "0123456789abcdef0123456789abcdef"})
q = urllib.parse.parse_qs(urllib.parse.urlparse(r["url"]).query)
CHALLENGE["c"] = q["code_challenge"][0]
check(q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == [spmod.REDIRECT]
      and "user-modify-playback-state" in q["scope"][0], "authorize URL is PKCE with the loopback redirect")
bad = m.act({"action": "finish", "id": "spotify", "url": f"{spmod.REDIRECT}?code=abc123&state=forged"})
check("different attempt" in bad.get("error", ""), "a forged state is refused")
r = m.act({"action": "finish", "id": "spotify", "url": f"{spmod.REDIRECT}?code=abc123&state={q['state'][0]}"})
check(r.get("ok") and r.get("account") == "Danial" and SP["verifier_ok"], f"code + verifier exchanged ({r})")
saved = json.loads((TMP / "connections.json").read_text())
check(saved["spotify"]["refresh_token"] == "ref1" and "pending" not in saved["spotify"], "tokens persisted, pending cleared")
st = m.status()
check(st["items"][0]["connected"] and "refresh_token" not in json.dumps(st) and "tok1" not in json.dumps(st),
      "status shows the account, never the tokens")

said = m.fast_path("play blinding lights")
check(said == "Playing Blinding Lights by The Weeknd." and SP["plays"][-1] == {"body": {"uris": ["spotify:track:t1"]}, "device": "dev1"},
      f"no active device: picks one and retries ({said})")
SP["active"] = True
m.fast_path("play daft punk")
check(SP["plays"][-1]["body"] == {"context_uri": "spotify:artist:a1"}, "an artist name plays the artist, not a lookalike track")
m.fast_path("play the album random access memories")
check(SP["plays"][-1]["body"] == {"context_uri": "spotify:album:b1"}, "album asks play the album")
check(m.fast_path("turn it up") == "Volume's at 55." and SP["volume"] == 55, "volume nudge reads then sets")
check(m.fast_path("what's this song") == "That's Blinding Lights by The Weeknd.", "now playing")
m.spotify.cfg["expires_at"] = 0
check(m.fast_path("skip") in ("Skipped.", "Next one.", "Skipping it.") and SP["refreshes"] == 1,
      "expired token refreshes before the call")
check("apps(request)" in m.persona_line() and "Spotify" in m.persona_line(), "persona gets the apps line")

# [4] hubs + the apps agent
print("[4] hubs + apps agent")
r = m.act({"action": "add", "preset": "custom", "name": "Fake hub", "url": murl, "key": "wrong"})
check("refused" in r.get("error", ""), "a hub that rejects the key isn't saved")
r = m.act({"action": "add", "preset": "custom", "name": "Fake hub", "url": murl, "key": "sekrit"})
check(r.get("ok") and r["tools"] == 2, f"hub added, tools listed ({r})")
saved = json.loads((TMP / "connections.json").read_text())
check(saved["mcp"][0]["headers"] == {"Authorization": "Bearer sekrit"}, "Bearer prefix added for Authorization")
check("sekrit" not in json.dumps(m.status()), "status never echoes the key")


class ScriptedBackend:
    """Plays the model: one tool call, then DONE."""
    def __init__(self):
        self.seen = []

    def chat(self, messages, max_tokens, **kw):
        self.seen.append(messages)
        if len(self.seen) == 1:
            return ('<tool_call>\n{"name": "calendar_create_event", "arguments": '
                    '{"title": "dentist", "start": "2026-11-06T15:00"}}\n</tool_call>')
        return "DONE: added dentist on Friday at 3pm"


b = ScriptedBackend()
out = m.run_request("add dentist friday 3pm to my calendar", b)
check(out == "added dentist on Friday at 3pm", f"agent ran the tool and summarized ({out})")
check(MCP["calls"][-1] == {"name": "calendar_create_event", "arguments": {"title": "dentist", "start": "2026-11-06T15:00"}},
      "the hub got the call")
sys_prompt = b.seen[0][0]["content"]
check("spotify_play" in sys_prompt and "calendar_create_event" in sys_prompt and "start: string (ISO time)" in sys_prompt,
      "agent sees spotify + hub tools with compact schemas")


class Wanderer:
    def chat(self, messages, max_tokens, **kw):
        return '<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>'


out = m.run_request("do something", Wanderer())
check(out.startswith("Stopped after") and "no tool called rm_rf" in out, "unknown tools are refused, loop is bounded")

from reedus_companion.connect import hands
big = [{"name": f"tool_{i}", "description": "misc"} for i in range(60)] + \
      [{"name": "COMPOSIO_SEARCH_TOOLS", "description": "search"}, {"name": "notion_create_page", "description": "Create a page in Notion"}]
short = [t["name"] for t in hands.shortlist("make a notion page about robots", big)]
check(len(short) == hands.SHORTLIST and "COMPOSIO_SEARCH_TOOLS" in short and short.index("notion_create_page") < 3,
      "big catalogs are shortlisted, meta tools always kept")

# [5] the persona's tool plumbing
print("[5] persona plumbing")
import reedus_companion.connect.manager as mgrmod
mgrmod._one = m
from reedus_companion.serve import tools as toolmod
check("apps(request)" in toolmod.render_tools_block(), "tools block lists apps while connected")
reg = toolmod.ToolRegistry(apps=lambda req: f"did: {req}")
check(reg.run("apps", {"request": "x"}) == "did: x", "ToolRegistry routes apps")
m.act({"action": "remove", "id": "fake-hub"})
m.act({"action": "disconnect", "id": "spotify"})
check(toolmod.render_tools_block().count("apps(request)") == 0, "and drops it when everything is disconnected")

msrv.shutdown(); ssrv.shutdown()
print()
if _fails:
    print(f"{len(_fails)} FAILED")
    for f in _fails:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
