"""Spotify, spoken to directly (no hub hop) so "pause" lands in a few hundred ms.

PKCE login, no client secret. Since 2026 a development-mode app needs a Premium owner and
allows 5 users, and redirects must be HTTPS or a literal loopback IP, so the default redirect
is reedosd at http://127.0.0.1:8765/connect/callback/spotify. From a phone that page won't
load, and ReedOS lets you paste the address you landed on instead.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional

AUTH = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
REDIRECT = os.environ.get("REEDUS_SPOTIFY_REDIRECT",
                          "http://127.0.0.1:8765/connect/callback/spotify")
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "user-library-read user-library-modify playlist-read-private "
          "playlist-read-collaborative")


class SpotifyError(RuntimeError):
    pass


class Spotify:
    """`cfg` is this connector's slice of connections.json; `save` persists it."""

    def __init__(self, cfg: dict, save: Callable[[], None]):
        self.cfg = cfg
        self.save = save

    # ---------- login ----------
    @property
    def connected(self) -> bool:
        return bool(self.cfg.get("refresh_token"))

    def start(self, client_id: str) -> str:
        client_id = (client_id or self.cfg.get("client_id") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", client_id):
            raise SpotifyError("that doesn't look like a Spotify Client ID (32 hex characters)")
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        self.cfg.update(client_id=client_id,
                        pending={"verifier": verifier, "state": state, "at": time.time()})
        self.save()
        return AUTH + "?" + urllib.parse.urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
            "code_challenge_method": "S256", "code_challenge": challenge,
            "state": state, "scope": SCOPES,
        })

    def finish(self, url_or_code: str) -> dict:
        """The redirect URL (or just its ?code=) after the Spotify login page."""
        pend = self.cfg.get("pending") or {}
        if not pend or time.time() - pend.get("at", 0) > 1800:
            raise SpotifyError("that login expired, press Connect again")
        raw = (url_or_code or "").strip()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query) if "?" in raw else {}
        if q.get("error"):
            raise SpotifyError(f"Spotify said: {q['error'][0]}")
        code = (q.get("code") or [raw if "?" not in raw else ""])[0]
        if not code:
            raise SpotifyError("no ?code= in that address")
        if q.get("state") and q["state"][0] != pend.get("state"):
            raise SpotifyError("that login was for a different attempt, press Connect again")
        tok = _form(TOKEN, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": self.cfg["client_id"], "code_verifier": pend["verifier"],
        })
        self.cfg.pop("pending", None)
        self._take(tok)
        me = self.api("GET", "/me") or {}
        self.cfg["account"] = me.get("display_name") or me.get("id") or "Spotify"
        self.cfg["product"] = me.get("product")
        self.save()
        return {"account": self.cfg["account"], "product": self.cfg.get("product")}

    def disconnect(self) -> None:
        for k in ("access_token", "refresh_token", "expires_at", "account", "product", "pending"):
            self.cfg.pop(k, None)
        self.save()

    def _take(self, tok: dict) -> None:
        self.cfg["access_token"] = tok["access_token"]
        self.cfg["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
        if tok.get("refresh_token"):            # spotify may rotate it; keep the newest
            self.cfg["refresh_token"] = tok["refresh_token"]
        self.save()

    def _token(self) -> str:
        if not self.connected:
            raise SpotifyError("Spotify isn't connected yet, set it up in ReedOS → Connections")
        if time.time() >= self.cfg.get("expires_at", 0):
            try:
                self._take(_form(TOKEN, {"grant_type": "refresh_token",
                                         "refresh_token": self.cfg["refresh_token"],
                                         "client_id": self.cfg["client_id"]}))
            except SpotifyError as e:
                if "invalid_grant" in str(e):
                    self.disconnect()
                    raise SpotifyError("Spotify signed me out, reconnect it in ReedOS") from e
                raise
        return self.cfg["access_token"]

    # ---------- raw api ----------
    def api(self, method: str, path: str, query: Optional[dict] = None,
            body: Optional[dict] = None, _retry: bool = True):
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method=method, headers={
            "Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else
            (b"" if method in ("PUT", "POST") else None))
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 401 and _retry:
                self.cfg["expires_at"] = 0
                return self.api(method, path, query, body, _retry=False)
            try:
                err = json.loads(e.read() or b"{}").get("error") or {}
            except ValueError:
                err = {}
            raise SpotifyError(_explain(e.code, err)) from e
        except urllib.error.URLError as e:
            raise SpotifyError(f"can't reach Spotify: {e.reason}") from e
        except OSError as e:
            raise SpotifyError(f"lost the connection to Spotify: {e}") from e

    # ---------- what people actually say ----------
    def now(self) -> dict:
        st = self.api("GET", "/me/player") or {}
        item = st.get("item") or {}
        return {
            "playing": bool(st.get("is_playing")),
            "track": item.get("name"),
            "artist": ", ".join(a["name"] for a in item.get("artists") or []) or
                      (item.get("show") or {}).get("name"),
            "album": (item.get("album") or {}).get("name"),
            "art": ((item.get("album") or {}).get("images") or [{}])[0].get("url"),
            "device": (st.get("device") or {}).get("name"),
            "volume": (st.get("device") or {}).get("volume_percent"),
            "shuffle": st.get("shuffle_state"),
            "progress": st.get("progress_ms"), "duration": item.get("duration_ms"),
            "uri": item.get("uri"),
        }

    def devices(self) -> List[dict]:
        return (self.api("GET", "/me/player/devices") or {}).get("devices") or []

    def _device(self) -> Optional[str]:
        """Somewhere to play when nothing is active: the last-used device, else the first."""
        devs = self.devices()
        if not devs:
            raise SpotifyError("Spotify isn't open on any device. Open it on your phone or "
                               "computer once and I can take it from there")
        if any(d.get("is_active") for d in devs):
            return None
        pref = self.cfg.get("last_device")
        pick = next((d for d in devs if d.get("id") == pref), devs[0])
        return pick.get("id")

    def _with_device(self, method: str, path: str, body: Optional[dict] = None,
                     query: Optional[dict] = None):
        try:
            return self.api(method, path, query, body)
        except SpotifyError as e:
            if "no active device" not in str(e).lower():
                raise
            dev = self._device()
            return self.api(method, path, {**(query or {}), **({"device_id": dev} if dev else {})}, body)

    def control(self, action: str) -> str:
        a = action.lower()
        if a == "pause":
            self._with_device("PUT", "/me/player/pause")
            return "paused"
        if a in ("resume", "play"):
            self._with_device("PUT", "/me/player/play")
            return "playing"
        if a in ("next", "skip"):
            self._with_device("POST", "/me/player/next")
            return "skipped"
        if a in ("previous", "back"):
            self._with_device("POST", "/me/player/previous")
            return "went back"
        if a in ("shuffle_on", "shuffle_off"):
            self._with_device("PUT", "/me/player/shuffle", query={"state": str(a == "shuffle_on").lower()})
            return "shuffle " + ("on" if a == "shuffle_on" else "off")
        if a == "like":
            uri = self.now().get("uri")
            if not uri:
                raise SpotifyError("nothing's playing to like")
            try:
                self.api("PUT", "/me/library", {"uris": uri})
            except SpotifyError:
                self.api("PUT", "/me/tracks", {"ids": uri.rsplit(":", 1)[-1]})
            return "liked"
        raise SpotifyError(f"unknown action {action!r}")

    def volume(self, percent: Optional[int] = None, delta: Optional[int] = None) -> int:
        if percent is None:
            cur = self.now().get("volume")
            percent = (50 if cur is None else cur) + int(delta or 0)
        percent = max(0, min(100, int(percent)))
        self._with_device("PUT", "/me/player/volume", query={"volume_percent": percent})
        return percent

    def find(self, query: str, kind: Optional[str] = None) -> Optional[dict]:
        """Best match across tracks/artists/albums/playlists, the way a person means it."""
        q = query.strip()
        if not q:
            return None
        types = [kind] if kind in ("track", "artist", "album", "playlist") else \
            ["track", "artist", "album", "playlist"]
        res = self.api("GET", "/search", {"q": q, "type": ",".join(types), "limit": 5}) or {}
        best, best_score = None, -1.0
        nq = _norm(re.sub(r"\b(song|track|album|playlist|by)\b", " ", q))
        for t in types:
            for i, it in enumerate((res.get(t + "s") or {}).get("items") or []):
                if not it:
                    continue
                name = it.get("name") or ""
                label = name
                if t in ("track", "album"):
                    label += " " + " ".join(a["name"] for a in it.get("artists") or [])
                sim = max(_sim(nq, _norm(name)), _sim(nq, _norm(label)))
                # spotify's own ranking breaks ties; an artist whose name IS the query means
                # "play some of them", so it beats a track that happens to share the name
                score = sim - i * 0.03 + (0.12 if t == "artist" and sim > 0.92 else 0) + \
                    (0.05 if t == "track" and not kind else 0)
                if score > best_score:
                    best, best_score = {"type": t, "item": it}, score
        return best

    def play(self, query: str, kind: Optional[str] = None) -> str:
        hit = self.find(query, kind)
        if not hit:
            raise SpotifyError(f"couldn't find {query!r} on Spotify")
        it, t = hit["item"], hit["type"]
        body = {"uris": [it["uri"]]} if t == "track" else {"context_uri": it["uri"]}
        self._with_device("PUT", "/me/player/play", body)
        by = ", ".join(a["name"] for a in it.get("artists") or [])
        if t == "track":
            return f"{it['name']} by {by}" if by else it["name"]
        if t == "album":
            return f"the album {it['name']}" + (f" by {by}" if by else "")
        if t == "playlist":
            return f"the playlist {it['name']}"
        return f"{it['name']}"

    def queue(self, query: str) -> str:
        hit = self.find(query, "track")
        if not hit:
            raise SpotifyError(f"couldn't find {query!r} on Spotify")
        self._with_device("POST", "/me/player/queue", query={"uri": hit["item"]["uri"]})
        it = hit["item"]
        return f"{it['name']} by {', '.join(a['name'] for a in it.get('artists') or [])}"

    def transfer(self, name: str) -> str:
        devs = self.devices()
        if not devs:
            raise SpotifyError("Spotify isn't open on any device")
        n = _norm(name)
        pick = max(devs, key=lambda d: _sim(n, _norm(d.get("name") or "")))
        if _sim(n, _norm(pick.get("name") or "")) < 0.45:
            names = ", ".join(d.get("name", "?") for d in devs)
            raise SpotifyError(f"no device called {name!r}. I can see: {names}")
        self.api("PUT", "/me/player", body={"device_ids": [pick["id"]], "play": True})
        self.cfg["last_device"] = pick["id"]
        self.save()
        return pick.get("name") or "that device"

    # ---------- as tools for the hands agent ----------
    def tools(self) -> List[dict]:
        return [
            _tool("spotify_play", "Play a song, artist, album or playlist on Spotify.",
                  {"query": "what to play, e.g. 'blinding lights' or 'lofi beats playlist'",
                   "kind": "optional: track | artist | album | playlist"}, ["query"]),
            _tool("spotify_control", "Pause, resume, skip, go back, shuffle or like the current song.",
                  {"action": "pause | resume | next | previous | shuffle_on | shuffle_off | like"},
                  ["action"]),
            _tool("spotify_volume", "Set Spotify's volume (0-100), or nudge it with delta.",
                  {"percent": "0-100", "delta": "e.g. 15 or -15"}, []),
            _tool("spotify_now_playing", "What's playing right now, and on which device.", {}, []),
            _tool("spotify_queue", "Add a song to the queue.", {"query": "the song"}, ["query"]),
            _tool("spotify_devices", "List the devices Spotify can play on.", {}, []),
            _tool("spotify_transfer", "Move playback to another device by name.",
                  {"device": "device name, e.g. 'kitchen speaker'"}, ["device"]),
        ]

    def call(self, name: str, args: dict) -> str:
        try:
            if name == "spotify_play":
                return "Now playing " + self.play(args.get("query", ""), args.get("kind"))
            if name == "spotify_control":
                return self.control(args.get("action", ""))
            if name == "spotify_volume":
                pct = args.get("percent")
                return f"volume {self.volume(int(pct) if pct not in (None, '') else None, args.get('delta'))}%"
            if name == "spotify_now_playing":
                n = self.now()
                if not n.get("track"):
                    return "nothing is playing"
                return (f"{'playing' if n['playing'] else 'paused'}: {n['track']} by {n['artist']}"
                        f" on {n.get('device') or 'some device'}")
            if name == "spotify_queue":
                return "queued " + self.queue(args.get("query", ""))
            if name == "spotify_devices":
                return ", ".join(f"{d.get('name')} ({d.get('type')})" for d in self.devices()) or "none"
            if name == "spotify_transfer":
                return "moved to " + self.transfer(args.get("device", ""))
        except SpotifyError as e:
            return f"[spotify error] {e}"
        return f"[spotify error] unknown tool {name}"


def _tool(name: str, desc: str, props: Dict[str, str], required: List[str]) -> dict:
    return {"name": name, "description": desc, "inputSchema": {
        "type": "object", "required": required,
        "properties": {k: {"type": "string", "description": v} for k, v in props.items()}}}


def _form(url: str, fields: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        raise SpotifyError(f"{body.get('error', e.code)}: {body.get('error_description', '')}".strip(": ")) from e
    except urllib.error.URLError as e:
        raise SpotifyError(f"can't reach Spotify: {e.reason}") from e
    except OSError as e:
        raise SpotifyError(f"lost the connection to Spotify: {e}") from e


def _explain(code: int, err: dict) -> str:
    reason, msg = err.get("reason") or "", err.get("message") or ""
    if reason == "NO_ACTIVE_DEVICE" or (code == 404 and "device" in msg.lower()):
        return "no active device"
    if reason == "PREMIUM_REQUIRED":
        return "Spotify only lets Premium accounts control playback"
    if reason == "VOLUME_CONTROL_DISALLOW":
        return "that device won't let me change its volume"
    if code == 403 and "user may not be registered" in msg.lower():
        return ("this Spotify account isn't on the app's user list. Add it under "
                "User Management in the Spotify developer dashboard")
    if code == 429:
        return "Spotify says slow down, try again in a bit"
    return msg or f"Spotify error {code}"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()
