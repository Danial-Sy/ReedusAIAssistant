"""OpenAI, and everything that speaks OpenAI's API, as the cloud brain (official openai SDK).

two wires (cloud/providers.py):
    "responses"  OpenAI itself: Responses API, low reasoning effort, the web_search tool
    "chat"       OpenRouter, xAI, DeepSeek, Groq, Mistral, Ollama, LM Studio, anything with
                 /v1/chat/completions. these leave search to core.py's own routing
                 (mode.local_search_enabled), which only searches when the turn needs it.
fields a given model or host refuses (temperature on reasoning models, stream_options on
some hosts, reasoning on non-reasoning models) are dropped the first time it says so.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from reedus_companion.cloud import CloudUnavailable, llm_settings, providers
from reedus_companion.cloud.base import CloudBackend, alternate, cancelled

_HEADROOM = int(os.environ.get("REEDUS_LLM_THINK_HEADROOM", "1024"))

_OPTIONAL_RESPONSES = {
    "reasoning": ("reasoning", "effort"),
    "tools": ("web_search", "tool"),
    "temperature": ("temperature",),
}
_OPTIONAL_CHAT = {
    "temperature": ("temperature",),
    "stream_options": ("stream_options", "include_usage"),
    "max_tokens": ("max_tokens",),
}


class OpenAIBackend(CloudBackend):
    PROVIDER = "openai"

    def __init__(self, provider_id: str = "openai", model: Optional[str] = None,
                 api_key: Optional[str] = None, base_url: Optional[str] = None):
        spec = providers.get(provider_id)
        key = (api_key if api_key is not None else llm_settings.key(provider_id)).strip()
        if not key and not spec.get("keyOptional"):
            raise CloudUnavailable(
                f"no {spec['name']} API key. Add one in ReedOS → Settings → Cloud brain "
                f"(or {spec['env']} in deploy/xeon.secrets.env).",
                provider=provider_id, retryable=False)
        base = (base_url if base_url is not None else llm_settings.base_url(provider_id)).strip()
        if spec["wire"] == "chat" and not base:
            raise CloudUnavailable(f"{spec['name']} needs a base URL (Settings → Cloud brain).",
                                   provider=provider_id, retryable=False)
        try:
            import openai
        except ImportError as e:
            raise CloudUnavailable(
                "the openai package is missing: "
                "pip install -r reedus_companion/requirements/cloud.txt",
                provider=provider_id, retryable=False) from e
        mdl = model or llm_settings.model(provider_id)
        if not mdl:
            raise CloudUnavailable(f"pick a {spec['name']} model first (Settings → Cloud brain).",
                                   provider=provider_id, retryable=False)
        super().__init__(mdl, provider_id)
        self.wire = spec["wire"]
        headers = {"X-Title": "Reedus"} if provider_id == "openrouter" else None
        self._client = openai.OpenAI(api_key=key or "none", base_url=base or None,
                                     timeout=self._timeout, max_retries=0,
                                     default_headers=headers)
        from reedus_companion.cloud import llm
        self._search = llm.inline_search(provider_id)
        self._effort = os.environ.get("REEDUS_OPENAI_EFFORT", "low")
        print(f"[reedus] {provider_id} backend: {self.model} ({self.wire} API, "
              f"web_search={'on' if self._search else 'off'})", flush=True)

    def _once(self, system, turns, max_tokens, *, is_router, on_token, cancel,
              temperature, use_adapter):
        if self._client is None:
            raise CloudUnavailable(f"{self.provider} backend is closed", provider=self.provider)
        grounded = bool(self._search and not is_router)
        stream = on_token is not None or cancel is not None
        if self.wire == "responses":
            return self._responses(system, turns, max_tokens, grounded, stream, on_token, cancel,
                                   temperature, use_adapter)
        return self._chat(system, turns, max_tokens, grounded, stream, on_token, cancel,
                          temperature, use_adapter)

    # ---------- OpenAI Responses API ----------
    def _responses(self, system, turns, max_tokens, grounded, stream, on_token, cancel,
                   temperature, use_adapter):
        kw: dict = {
            "model": self.model,
            "input": [{"role": t["role"], "content": t["content"]} for t in alternate(turns)],
            "max_output_tokens": max(16, int(max_tokens)) + _HEADROOM,
            "reasoning": {"effort": self._effort},
            "store": False,
        }
        if system:
            kw["instructions"] = system
        if grounded:
            kw["tools"] = [{"type": "web_search"}]
        if temperature is not None and not use_adapter:
            kw["temperature"] = float(temperature)
        client = self._client
        if not stream:
            resp = self._call(lambda k: client.responses.create(**k), kw, _OPTIONAL_RESPONSES)
            return _from_response(resp)

        pieces: List[str] = []
        done = {"resp": None}

        def _go(k):
            pieces.clear()
            events = client.responses.create(stream=True, **k)
            try:
                for ev in events:
                    if cancelled(cancel):
                        return None
                    kind = getattr(ev, "type", "")
                    if kind == "response.output_text.delta":
                        d = getattr(ev, "delta", "") or ""
                        if d:
                            pieces.append(d)
                            if on_token is not None:
                                on_token(d)
                    elif kind in ("response.completed", "response.incomplete"):
                        done["resp"] = getattr(ev, "response", None)
                    elif kind in ("response.failed", "error"):
                        err = getattr(getattr(ev, "response", None), "error", None) or ev
                        raise RuntimeError(getattr(err, "message", None) or str(err))
            finally:
                close = getattr(events, "close", None)
                if close:
                    close()
            return done["resp"]

        self._call(_go, kw, _OPTIONAL_RESPONSES)
        text = "".join(pieces)
        if done["resp"] is None:
            return text, 0, 0, 0
        _t, n_in, n_out, searches = _from_response(done["resp"])
        return text or _t, n_in, n_out, searches

    # ---------- OpenAI-compatible chat completions ----------
    def _chat(self, system, turns, max_tokens, grounded, stream, on_token, cancel,
              temperature, use_adapter):
        msgs = ([{"role": "system", "content": system}] if system else []) + alternate(turns)
        kw: dict = {"model": self.model, "messages": msgs,
                    "max_tokens": max(16, int(max_tokens)) + _HEADROOM // 4}
        if temperature is not None and not use_adapter:
            kw["temperature"] = float(temperature)
        client = self._client
        if not stream:
            resp = self._call(lambda k: client.chat.completions.create(**k), kw, _OPTIONAL_CHAT)
            choice = (getattr(resp, "choices", None) or [None])[0]
            text = getattr(getattr(choice, "message", None), "content", "") or ""
            n_in, n_out = _chat_usage(getattr(resp, "usage", None))
            return text, n_in, n_out, 0

        kw["stream_options"] = {"include_usage": True}
        pieces: List[str] = []
        usage = {"u": None}

        def _go(k):
            pieces.clear()
            chunks = client.chat.completions.create(stream=True, **k)
            try:
                for ch in chunks:
                    if cancelled(cancel):
                        return None
                    if getattr(ch, "usage", None):
                        usage["u"] = ch.usage
                    for c in getattr(ch, "choices", None) or []:
                        d = getattr(getattr(c, "delta", None), "content", None)
                        if d:
                            pieces.append(d)
                            if on_token is not None:
                                on_token(d)
            finally:
                close = getattr(chunks, "close", None)
                if close:
                    close()
            return True

        self._call(_go, kw, _OPTIONAL_CHAT)
        n_in, n_out = _chat_usage(usage["u"])
        return "".join(pieces), n_in, n_out, 0


def _from_response(resp) -> Tuple[str, int, int, int]:
    text = getattr(resp, "output_text", None)
    if text is None:
        text = ""
        for item in getattr(resp, "output", None) or []:
            for c in getattr(item, "content", None) or []:
                if getattr(c, "type", "") == "output_text":
                    text += getattr(c, "text", "") or ""
    searches = sum(1 for item in (getattr(resp, "output", None) or [])
                   if getattr(item, "type", "") == "web_search_call")
    u = getattr(resp, "usage", None)
    n_in = int(getattr(u, "input_tokens", 0) or 0) if u is not None else 0
    n_out = int(getattr(u, "output_tokens", 0) or 0) if u is not None else 0
    return text or "", n_in, n_out, searches


def _chat_usage(u) -> Tuple[int, int]:
    if u is None:
        return 0, 0
    return int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0)
