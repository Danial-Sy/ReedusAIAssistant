"""every app Reedus can reach, in one place: Spotify plus any number of MCP servers.

state lives in memory/connections.json (gitignored: it holds tokens and keys). the control
API exposes status() and act() to ReedOS; the persona gets one `apps` tool and a fast path.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

from reedus_companion import config
from reedus_companion.connect import hands, intents
from reedus_companion.connect.mcp import MCPClient, MCPError
from reedus_companion.connect.spotify import REDIRECT, Spotify, SpotifyError

PATH = Path(os.environ.get("REEDUS_CONNECTIONS_FILE", str(config.MEMORY_DIR / "connections.json")))
TOOLS_TTL = 600.0
DEBUG = bool(os.environ.get("REEDUS_DEBUG"))

# one URL each, the header is where the key goes. ReedOS shows these as one-tap starts
PRESETS = {
    "composio": {"name": "Composio", "url": "https://connect.composio.dev/mcp",
                 "header": "x-consumer-api-key", "prefix": "",
                 "blurb": "1000+ apps (Gmail, Calendar, Notion, GitHub, Slack…). Free tier.",
                 "keyHelp": "Consumer API key (starts with ck_) from dashboard.composio.dev"},
    "zapier": {"name": "Zapier", "url": "https://mcp.zapier.com/api/v1/connect",
               "header": "Authorization", "prefix": "Bearer ",
               "blurb": "9000+ apps, pick the actions at mcp.zapier.com. Free plan is ~50 calls/month.",
               "keyHelp": "Token from mcp.zapier.com → your server → Connect"},
    "homeassistant": {"name": "Home Assistant", "url": "http://homeassistant.local:8123/api/mcp",
                      "header": "Authorization", "prefix": "Bearer ",
                      "blurb": "Lights, plugs, thermostats. Needs the MCP Server integration.",
                      "keyHelp": "Long-lived access token from your HA profile page"},
    "custom": {"name": "Custom MCP server", "url": "", "header": "Authorization", "prefix": "Bearer ",
               "blurb": "Any Streamable HTTP MCP server.", "keyHelp": "Whatever the server wants"},
}


class _Server:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = MCPClient(cfg["url"], cfg.get("headers") or {})
        self.tools: List[dict] = []
        self.at = 0.0
        self.error = ""
        self.lock = threading.Lock()

    def refresh(self, force: bool = False) -> List[dict]:
        with self.lock:
            if not force and self.tools and time.time() - self.at < TOOLS_TTL:
                return self.tools
            try:
                self.tools = self.client.list_tools()
                self.at, self.error = time.time(), ""
            except Exception as e:                               # noqa: BLE001
                self.error = str(e)
                if force or not self.tools:
                    raise
            return self.tools


class Connections:
    def __init__(self, path: Path = PATH):
        self.path = path
        self.lock = threading.RLock()
        self.cfg = self._load()
        self.spotify = Spotify(self.cfg.setdefault("spotify", {}), self._save)
        self.servers: Dict[str, _Server] = {}
        for s in self.cfg.setdefault("mcp", []):
            if s.get("enabled", True):
                self.servers[s["id"]] = _Server(s)
        # warm the tool lists in the background so the first "add that to my calendar" is quick
        if self.servers:
            threading.Thread(target=self._warm, name="connections-warm", daemon=True).start()

    # ---------- storage ----------
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            return {}

    def _save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(self.path)

    def _warm(self) -> None:
        for s in list(self.servers.values()):
            try:
                s.refresh()
            except Exception:                                    # noqa: BLE001
                pass

    # ---------- for the persona ----------
    def any(self) -> bool:
        return self.spotify.connected or bool(self.servers)

    def persona_line(self) -> str:
        """The `apps` line in the tools block, or "" when nothing is connected."""
        if not self.any():
            return ""
        have = (["Spotify (music)"] if self.spotify.connected else []) + \
            [s.cfg.get("name") or "an app hub" for s in self.servers.values()]
        return ("- apps(request): act in the user's connected apps: " + ", ".join(have) +
                ". Pass what they want in plain words, e.g. {\"request\": \"add dentist friday "
                "3pm to my calendar\"}. Use it whenever they ask you to do something in an app; "
                "never claim you did it without calling it.")

    def fast_path(self, user_msg: str) -> Optional[str]:
        """A spoken reply for a plain music command, or None to let the persona handle it."""
        if not self.spotify.connected:
            return None
        hit = intents.parse(user_msg)
        if not hit:
            return None
        return intents.run(self.spotify, *hit)

    def catalog(self) -> List[dict]:
        out = list(self.spotify.tools()) if self.spotify.connected else []
        for sid, s in self.servers.items():
            try:
                for t in s.refresh():
                    out.append({**t, "_server": sid})
            except Exception:                                    # noqa: BLE001
                continue
        return out

    def call(self, name: str, args: dict, catalog: Optional[List[dict]] = None) -> str:
        if name.startswith("spotify_") and self.spotify.connected:
            return self.spotify.call(name, args)
        cat = catalog if catalog is not None else self.catalog()
        sid = next((t.get("_server") for t in cat if t["name"] == name), None)
        s = self.servers.get(sid or "")
        if s is None:
            return f"[error] no connected app has a tool called {name}"
        try:
            return s.client.call_tool(name, args)
        except MCPError as e:
            return f"[error] {s.cfg.get('name')}: {e}"

    def run_request(self, request: str, backend) -> str:
        cat = self.catalog()
        return hands.run(request, cat, lambda n, a: self.call(n, a, cat), backend, debug=DEBUG)

    # ---------- for ReedOS ----------
    def status(self) -> dict:
        sp = self.spotify.cfg
        items = [{
            "id": "spotify", "kind": "spotify", "name": "Spotify",
            "connected": self.spotify.connected, "account": sp.get("account"),
            "product": sp.get("product"), "clientId": bool(sp.get("client_id")),
            "pending": bool(sp.get("pending")), "redirect": REDIRECT,
        }]
        for s in self.cfg.get("mcp", []):
            live = self.servers.get(s["id"])
            items.append({
                "id": s["id"], "kind": "mcp", "preset": s.get("preset", "custom"),
                "name": s.get("name") or "MCP server",
                "host": urllib.parse.urlparse(s.get("url", "")).netloc,
                "enabled": s.get("enabled", True), "connected": bool(live and live.tools),
                "tools": len(live.tools) if live else 0,
                "error": live.error if live else "",
                "server": (live.client.server.get("name") if live else "") or "",
            })
        return {"items": items, "presets": PRESETS}

    def act(self, req: dict) -> dict:
        a, cid = (req.get("action") or "").strip(), (req.get("id") or "").strip()
        try:
            if cid == "spotify":
                return self._spotify_act(a, req)
            if a == "add":
                return self._add(req)
            s = next((x for x in self.cfg.get("mcp", []) if x["id"] == cid), None)
            if s is None:
                return {"error": f"no connection {cid!r}"}
            if a == "remove":
                live = self.servers.pop(cid, None)
                if live:
                    live.client.close()
                self.cfg["mcp"] = [x for x in self.cfg["mcp"] if x["id"] != cid]
                self._save()
                return {"ok": True}
            if a == "test":
                live = self.servers.get(cid) or _Server(s)
                self.servers[cid] = live
                tools = live.refresh(force=True)
                return {"ok": True, "tools": len(tools),
                        "sample": [t["name"] for t in tools[:8]]}
            return {"error": f"unknown action {a!r}"}
        except (SpotifyError, MCPError) as e:
            return {"error": str(e)}
        except Exception as e:                                   # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    def _spotify_act(self, a: str, req: dict) -> dict:
        sp = self.spotify
        if a == "start":
            return {"url": sp.start(req.get("clientId") or "")}
        if a == "finish":
            return {"ok": True, **sp.finish(req.get("url") or req.get("code") or "")}
        if a == "disconnect":
            sp.disconnect()
            return {"ok": True}
        if a == "now":
            return {"now": sp.now()}
        if a == "control":
            do = req.get("do") or ""
            if do == "volume":
                return {"ok": True, "volume": sp.volume(int(req.get("value", 50)))}
            return {"ok": True, "did": sp.control(do)}
        if a == "say":            # try a sentence the way a voice command would arrive
            return {"reply": self.fast_path(req.get("text") or "") or
                    "That isn't a music command I handle directly; Reedus will route it."}
        return {"error": f"unknown action {a!r}"}

    def _add(self, req: dict) -> dict:
        preset = PRESETS.get(req.get("preset") or "custom", PRESETS["custom"])
        url = (req.get("url") or preset["url"]).strip()
        if not re.match(r"^https?://", url):
            return {"error": "the server address must start with http:// or https://"}
        key = (req.get("key") or "").strip()
        header = (req.get("header") or preset["header"]).strip()
        headers = {}
        if key:
            pre = preset["prefix"] if header.lower() == "authorization" and \
                not key.lower().startswith("bearer ") else ""
            headers[header] = pre + key
        sid = re.sub(r"[^a-z0-9]+", "-", (req.get("name") or preset["name"]).lower()).strip("-")
        base, n = sid or "mcp", 2
        while any(x["id"] == sid for x in self.cfg.get("mcp", [])):
            sid, n = f"{base}-{n}", n + 1
        entry = {"id": sid, "preset": req.get("preset") or "custom",
                 "name": req.get("name") or preset["name"], "url": url,
                 "headers": headers, "enabled": True}
        live = _Server(entry)
        tools = live.refresh(force=True)            # refuse to save a server that doesn't answer
        self.cfg.setdefault("mcp", []).append(entry)
        self.servers[sid] = live
        self._save()
        return {"ok": True, "id": sid, "tools": len(tools),
                "sample": [t["name"] for t in tools[:8]]}


_one: Optional[Connections] = None
_one_lock = threading.Lock()


def manager() -> Connections:
    global _one
    with _one_lock:
        if _one is None:
            _one = Connections()
        return _one
