"""a small, blocking MCP client over Streamable HTTP. stdlib only.

one URL gets a whole catalog: Composio (1000+ apps), Zapier (9000+), Home Assistant, or any
other MCP server. the voice loop is plain threads, so this stays synchronous on purpose.
"""
from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

PROTOCOL = "2025-06-18"          # servers answer with the version they speak; we follow it
UA = "reedus-companion/1.0"


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0):
        self.url = url
        self.headers = {k: v for k, v in (headers or {}).items() if k and v}
        self.timeout = timeout
        self.session: Optional[str] = None
        self.protocol = PROTOCOL
        self.server: Dict[str, Any] = {}
        self._ids = itertools.count(1)
        self._ready = False

    # --- json-rpc over http ---
    def _post(self, msg: dict) -> Optional[dict]:
        head = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": UA,
            **self.headers,
        }
        if self._ready:
            head["MCP-Protocol-Version"] = self.protocol
        if self.session:
            head["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=head,
                                     method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            body = (e.read() or b"")[:300].decode("utf-8", "replace")
            if e.code == 404 and self.session:
                raise _SessionGone() from e
            if e.code in (401, 403):
                raise MCPError(f"the server refused the key (HTTP {e.code})") from e
            raise MCPError(f"HTTP {e.code}: {body.strip() or e.reason}") from e
        except urllib.error.URLError as e:
            raise MCPError(f"can't reach {self.url}: {e.reason}") from e
        except OSError as e:                    # reset/timeout mid-response
            raise MCPError(f"lost the connection to {self.url}: {e}") from e

        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session = sid
            if "id" not in msg or resp.status == 202:
                return None
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in ctype:
                for obj in _sse_objects(resp):
                    if isinstance(obj, dict) and obj.get("id") == msg["id"] and (
                            "result" in obj or "error" in obj):
                        return obj
                raise MCPError("the server closed the stream without answering")
            raw = resp.read()
        try:
            obj = json.loads(raw or b"{}")
        except ValueError as e:
            raise MCPError(f"not JSON: {raw[:120]!r}") from e
        if isinstance(obj, list):               # a batch: pick ours out
            obj = next((o for o in obj if isinstance(o, dict) and o.get("id") == msg["id"]), {})
        return obj

    def _rpc(self, method: str, params: Optional[dict] = None) -> Any:
        if not self._ready and method != "initialize":
            self.initialize()
        msg = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            msg["params"] = params
        try:
            out = self._post(msg)
        except _SessionGone:
            # the server dropped our session (restart, idle timeout): start over once
            self.session, self._ready = None, False
            self.initialize()
            msg["id"] = next(self._ids)
            out = self._post(msg)
        if out is None:
            raise MCPError(f"no answer to {method}")
        if "error" in out:
            err = out["error"] or {}
            raise MCPError(err.get("message") or str(err))
        return out.get("result")

    # --- the three calls anyone needs ---
    def initialize(self) -> dict:
        self.session, self._ready = None, False
        res = self._rpc("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "reedus", "version": "1.0"},
        }) or {}
        self.protocol = res.get("protocolVersion") or PROTOCOL
        self.server = res.get("serverInfo") or {}
        self._ready = True
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except MCPError:
            pass                                 # some servers 4xx a notification; harmless
        return res

    def list_tools(self) -> List[dict]:
        tools, cursor = [], None
        for _ in range(20):                      # 20 pages is far past any sane server
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools += res.get("tools") or []
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> str:
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}}) or {}
        text = flatten_content(res)
        if res.get("isError"):
            return f"[tool error] {text}"
        return text

    def close(self) -> None:
        if not self.session:
            return
        req = urllib.request.Request(self.url, method="DELETE",
                                     headers={"Mcp-Session-Id": self.session, **self.headers})
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:                                        # noqa: BLE001
            pass
        self.session, self._ready = None, False


class _SessionGone(Exception):
    pass


def _sse_objects(resp) -> Iterator[Any]:
    """JSON payloads from an SSE body, one per event."""
    data: List[str] = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                try:
                    yield json.loads("\n".join(data))
                except ValueError:
                    pass
                data = []
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        try:
            yield json.loads("\n".join(data))
        except ValueError:
            pass


def flatten_content(res: dict, limit: int = 4000) -> str:
    """A tools/call result as plain text for a language model."""
    parts = []
    for c in res.get("content") or []:
        kind = c.get("type")
        if kind == "text":
            parts.append(c.get("text") or "")
        elif kind == "resource":
            r = c.get("resource") or {}
            parts.append(r.get("text") or f"[resource {r.get('uri', '')}]")
        elif kind in ("image", "audio"):
            parts.append(f"[{kind}]")
        elif kind == "resource_link":
            parts.append(f"[link {c.get('name') or c.get('uri', '')}]")
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"])[:limit])
    text = "\n".join(p for p in parts if p).strip()
    return text[:limit] + ("…" if len(text) > limit else "")
