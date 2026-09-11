"""auth, one shared passcode for the whole robot."""

import hashlib
import hmac
import json
import logging
import os
import pathlib
import secrets
import time

log = logging.getLogger("auth")

COOKIE = "reedos_key"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30      # a phone should ask once a month, not daily

MAX_FAILS = 5
FAIL_WINDOW_S = 300
LOCKOUT_S = 60


class Passcode:
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self._fails: dict[str, list] = {}
        self._data = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
        except Exception:
            d = {}
        if not d.get("salt"):
            d["salt"] = secrets.token_hex(16)
            self._write(d)
        return d

    def _write(self, d: dict) -> None:
        try:
            self.path.write_text(json.dumps(d, indent=2))
            os.chmod(self.path, 0o600)          # it is a passcode in the clear
        except Exception as e:                  # noqa: BLE001
            log.warning("could not save %s: %s", self.path, e)

    @property
    def code(self) -> str:
        env = (os.environ.get("REEDOS_PASSCODE") or "").strip()
        return env or (self._data.get("passcode") or "")

    @property
    def enabled(self) -> bool:
        return bool(self.code)

    @property
    def from_env(self) -> bool:
        return bool((os.environ.get("REEDOS_PASSCODE") or "").strip())

    @property
    def token(self) -> str:
        """Stable across restarts, tells you nothing about the passcode."""
        if not self.enabled:
            return ""
        return hmac.new(self._data["salt"].encode(), self.code.encode(),
                        hashlib.sha256).hexdigest()

    def set_code(self, code: str) -> None:
        code = (code or "").strip()
        self._data["passcode"] = code
        self._data["salt"] = secrets.token_hex(16)
        self._write(self._data)
        log.info("passcode %s", "changed" if code else "cleared, ReedOS is open")

    def check_token(self, tok: str) -> bool:
        if not self.enabled:
            return True
        return bool(tok) and hmac.compare_digest(tok, self.token)

    def check_code(self, code: str) -> bool:
        if not self.enabled:
            return True
        return hmac.compare_digest((code or "").strip(), self.code)

    def locked_out(self, who: str) -> int:
        """Seconds still to wait, or 0."""
        hits = [t for t in self._fails.get(who, []) if time.time() - t < FAIL_WINDOW_S]
        self._fails[who] = hits
        if len(hits) < MAX_FAILS:
            return 0
        wait = int(LOCKOUT_S - (time.time() - hits[-1]))
        return max(0, wait)

    def record_fail(self, who: str) -> None:
        self._fails.setdefault(who, []).append(time.time())
        log.warning("bad passcode from %s (%d recent)", who, len(self._fails[who]))

    def clear_fails(self, who: str) -> None:
        self._fails.pop(who, None)

    def authorised(self, request) -> bool:
        """Cookie, ?k=, or Authorization: Bearer, any of the three."""
        if not self.enabled:
            return True
        tok = request.cookies.get(COOKIE) or request.query_params.get("k") or ""
        if not tok:
            head = request.headers.get("authorization") or ""
            if head.lower().startswith("bearer "):
                tok = head[7:].strip()
        return self.check_token(tok)
