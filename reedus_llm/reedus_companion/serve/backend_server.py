"""native llama-server backend over its openai-compatible http api. this is what runs on the xeon."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Iterator, List, Optional

from reedus_companion import config
from reedus_companion.serve.backend_llamacpp import _no_think


def _env_url(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).rstrip("/")


class ServerBackend:
    """Drop-in Backend protocol client for one or two llama-server processes."""

    PROFILE: List[Dict] = []

    def __init__(self, main_url: Optional[str] = None, router_url: Optional[str] = None):
        self.main_url = (main_url or _env_url("REEDUS_MAIN_URL", "http://127.0.0.1:8080")).rstrip("/")
        self.router_url = (router_url if router_url is not None
                           else _env_url("REEDUS_ROUTER_URL")).rstrip("/") or None
        self.main_model = os.environ.get("REEDUS_SERVER_MAIN_MODEL", "main")
        self.router_model = os.environ.get("REEDUS_SERVER_ROUTER_MODEL", "router")
        self._timeout = float(os.environ.get("REEDUS_SERVER_TIMEOUT", "600"))
        print(f"[reedus] server backend: main={self.main_url}"
              + (f" router={self.router_url}" if self.router_url else " (single endpoint)"))
        self._ping(self.main_url, "main")
        if self.router_url:
            self._ping(self.router_url, "router")

    def _ping(self, base: str, label: str) -> None:
        """Fail fast at construct if the server isn't up, better than a 10-minute hang."""
        url = f"{base}/health"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                _ = resp.read(64)
            print(f"[reedus] llama-server[{label}] healthy at {base}")
        except Exception as e:                                  # noqa: BLE001
            try:
                req = urllib.request.Request(f"{base}/v1/models", method="GET")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    _ = resp.read(64)
                print(f"[reedus] llama-server[{label}] reachable at {base} (/v1/models)")
            except Exception:
                print(f"[reedus] WARNING: llama-server[{label}] not reachable at {base} "
                      f"({type(e).__name__}: {e}). Start it with "
                      f"deploy/run_llama_servers.sh")

    def router_ready(self) -> bool:
        return bool(self.router_url)

    def close(self) -> None:
        return None

    def _pick(self, use_adapter: bool, tier: Optional[str], name: str):
        if tier == "main" or use_adapter or not self.router_url:
            return self.main_url, self.main_model, "main"
        if tier != "router":
            if name and name in config.ROUTER_KEEP_ON_MAIN:
                return self.main_url, self.main_model, "main"
        return self.router_url, self.router_model, "router"

    def chat(self, messages: List[Dict[str, str]], max_tokens: int,
             use_adapter: bool = True, temperature: Optional[float] = None,
             on_token: Optional[Callable[[str], None]] = None,
             tier: Optional[str] = None, name: str = "",
             cancel=None) -> str:
        temp = config.TEMPERATURE if temperature is None else temperature
        base, model, tier_label = self._pick(use_adapter, tier, name)
        if tier_label == "router" and config.ROUTER_NO_THINK:
            messages = _no_think(messages)

        do_stream = on_token is not None or cancel is not None
        body = {
            "model": model,
            "messages": messages,
            "temperature": float(temp),
            "top_p": float(config.TOP_P),
            "max_tokens": int(max_tokens),
            "stream": bool(do_stream),
            "repeat_penalty": float(config.REPEAT_PENALTY),
        }
        url = f"{base}/v1/chat/completions"
        t0 = time.perf_counter()

        if do_stream:
            pieces: List[str] = []
            n_completion = 0
            n_usage_completion = 0
            n_prompt = 0
            ttft = 0.0
            cancelled = False
            try:
                for event in self._stream(url, body, cancel):
                    if cancel is not None and cancel.is_set():
                        cancelled = True
                        break
                    if event.get("usage"):
                        u = event["usage"]
                        n_prompt = u.get("prompt_tokens", n_prompt) or n_prompt
                        n_usage_completion = (u.get("completion_tokens")
                                              or n_usage_completion)
                    choices = event.get("choices") or [{}]
                    delta = (choices[0].get("delta") or {})
                    piece = delta.get("content") or ""
                    if piece:
                        if not ttft:
                            ttft = time.perf_counter() - t0
                        pieces.append(piece)
                        n_completion += 1
                        if on_token is not None:
                            try:
                                on_token(piece)
                            except Exception:
                                pass
                        if cancel is not None and cancel.is_set():
                            cancelled = True
                            break
            except Exception as e:                              # noqa: BLE001
                if not pieces:
                    raise RuntimeError(f"llama-server stream failed ({base}): {e}") from e
            if os.environ.get("REEDUS_PROFILE"):
                wall = time.perf_counter() - t0
                n_completion = n_usage_completion or n_completion
                self.PROFILE.append({
                    "wall_s": wall,
                    "prompt_tokens": n_prompt,
                    "completion_tokens": n_completion,
                    "system_head": (messages[0].get("content", "")[:48] if messages else ""),
                    "tier": tier_label,
                    "name": name,
                    "ttft_s": ttft,
                    "gen_tok_s": (n_completion / (wall - ttft)) if wall > ttft and ttft else 0.0,
                    "cancelled": cancelled or bool(cancel is not None and cancel.is_set()),
                })
            return "".join(pieces).strip()

        out = self._post_json(url, body)
        if os.environ.get("REEDUS_PROFILE"):
            u = out.get("usage") or {}
            self.PROFILE.append({
                "wall_s": time.perf_counter() - t0,
                "prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "system_head": (messages[0].get("content", "")[:48] if messages else ""),
                "tier": tier_label,
                "name": name,
            })
        return ((out.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()

    def _post_json(self, url: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e

    def _stream(self, url: str, body: dict, cancel) -> Iterator[dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        resp = urllib.request.urlopen(req, timeout=self._timeout)
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    break
                line = resp.readline()
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        finally:
            try:
                resp.close()
            except Exception:
                pass
