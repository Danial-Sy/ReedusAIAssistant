"""brain_link, reedosd's client for the Reedus brain."""

import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger("brain")

DEFAULT_URL = os.environ.get("REEDUS_BRAIN_URL", "http://127.0.0.1:8766")

# health is polled every 3s by the status pump. a down brain refuses instantly, but a
# HUNG brain would stall the pump, so health gets a short timeout and its own cache.
HEALTH_TIMEOUT = 1.5
HEALTH_TTL = 2.0
CHAT_TIMEOUT = 300.0        # a local search turn is 12s+; a cold llama.cpp is worse
MODE_TIMEOUT = 20.0


class BrainLink:
    def __init__(self, settings: dict):
        self.settings = {}
        self.base = DEFAULT_URL
        self.token = ""
        self._cache = {}
        self._cache_at = 0.0
        self.configure(settings)

    def configure(self, settings: dict):
        self.settings = settings or {}
        base = (self.settings.get("_brain_url") or DEFAULT_URL).rstrip("/")
        if base != self.base:
            self._cache, self._cache_at = {}, 0.0
        self.base = base
        self.token = (self.settings.get("_brain_token")
                      or os.environ.get("REEDUS_API_TOKEN") or "").strip()

    def _call(self, path: str, payload: dict | None = None, timeout: float = 10.0) -> dict:
        url = f"{self.base}{path}"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read() or b"{}")
            except Exception:
                return {"error": f"HTTP {e.code}"}
        except Exception as e:                                   # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}", "offline": True}

    def health(self, force: bool = False) -> dict:
        now = time.time()
        if not force and (now - self._cache_at) < HEALTH_TTL:
            return self._cache
        out = self._call("/api/health", timeout=HEALTH_TIMEOUT)
        was = bool(self._cache.get("ok"))
        self._cache, self._cache_at = out, now
        if bool(out.get("ok")) != was:
            log.info("brain %s (%s)", "up" if out.get("ok") else "down", self.base)
        return out

    @property
    def online(self) -> bool:
        return bool(self.health().get("ok"))

    def state(self) -> dict:
        """The subset the UI's status pills care about."""
        h = self.health()
        return {
            "brain": "on" if h.get("ok") else "off",
            "mode": h.get("mode"),
            "modePending": h.get("pending"),
            "modeSwitching": bool(h.get("switching")),
            "voice": bool(h.get("voice")),
            "micOwner": h.get("micOwner"),
            "backend": h.get("backend"),
            "busy": h.get("busy"),
            "budget": h.get("budget") or {},
            "cloudReady": (h.get("cloud") or {}).get("ok"),
            "cloudWhy": (h.get("cloud") or {}).get("why") or "",
            "lite": h.get("lite"),          # None: the brain is the robot's (or offline)
        }

    def ask(self, text: str, speak: bool = False) -> dict:
        out = self._call("/api/chat", {"text": text, "speak": bool(speak)},
                         timeout=CHAT_TIMEOUT)
        self._cache_at = 0.0            # a turn can change mode/budget
        return out

    def say(self, text: str) -> dict:
        return self._call("/api/speak", {"text": text}, timeout=CHAT_TIMEOUT)

    def listen(self, speak: bool = True) -> dict:
        return self._call("/api/listen", {"speak": bool(speak)}, timeout=CHAT_TIMEOUT)

    def set_mode(self, target: str) -> dict:
        out = self._call("/api/mode", {"mode": target}, timeout=MODE_TIMEOUT)
        self._cache_at = 0.0
        return out

    def connections(self, req: dict | None = None) -> dict:
        """App connections live in the brain: GET the list, or POST an action."""
        if req is None:
            return self._call("/api/connections", timeout=10.0)
        # adding a hub lists its tools before saving, a slow hub can take a while
        return self._call("/api/connections", req, timeout=45.0)

    def lite(self, req: dict) -> dict:
        """Reedus Lite knobs: volume, brightness, look, expr. Only when the brain runs Lite."""
        out = self._call("/api/lite", req, timeout=5.0)
        self._cache_at = 0.0
        return out
