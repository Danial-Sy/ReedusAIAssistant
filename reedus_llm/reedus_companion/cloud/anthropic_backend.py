"""Claude as the cloud brain, through the official anthropic SDK (Messages API).

tuned for a voice: low effort, thinking off where the model allows it, and Claude's own
web_search server tool on the persona call when search is "auto" (cloud/llm_settings.py).
temperature is never sent; current Claude models reject sampling parameters.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from reedus_companion.cloud import CloudUnavailable, llm_settings
from reedus_companion.cloud.base import CloudBackend, alternate, cancelled

# how to keep each model from thinking before it talks. models not listed get adaptive
# thinking at low effort (Opus 5.5 and Fable can't turn it off) plus token headroom.
_THINKING_OFF = {
    "claude-sonnet-5-5": {"type": "between_tools"},
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-sonnet-5": {"type": "disabled"},
    "claude-opus-4-8": {"type": "disabled"},
    "claude-opus-4-7": {"type": "disabled"},
}
# pre-4.6 models don't think unless asked and don't take effort
_NO_THINK_PREFIXES = ("claude-haiku-4", "claude-sonnet-4-5", "claude-opus-4-5", "claude-opus-4-1",
                      "claude-opus-4-0", "claude-sonnet-4-0", "claude-3")
# dynamic-filtering web search needs Opus 4.6+ / Sonnet 4.6+ / Fable; Haiku and older get the basic one
_SEARCH_BASIC_PREFIXES = ("claude-haiku", "claude-3", "claude-sonnet-4-5", "claude-opus-4-5",
                          "claude-opus-4-1", "claude-sonnet-4-0", "claude-opus-4-0")

_HEADROOM = int(os.environ.get("REEDUS_CLAUDE_THINK_HEADROOM", "1024"))

# 400 message fragments that mean "leave this field out for this model"
_OPTIONAL = {
    "thinking": ("thinking",),
    "output_config": ("effort", "output_config"),
    "tools": ("web_search", "tool"),
}


def thinking_for(model: str) -> Optional[dict]:
    """The thinking field for a quick spoken reply, or None to leave it out."""
    want = (os.environ.get("REEDUS_CLAUDE_THINK") or "auto").lower()
    if want == "adaptive":
        return {"type": "adaptive"}
    if model.startswith(_NO_THINK_PREFIXES):
        return None
    return _THINKING_OFF.get(model)


def thinks(model: str) -> bool:
    """Might this model spend output tokens thinking with our settings?"""
    if model.startswith(_NO_THINK_PREFIXES):
        return False
    t = thinking_for(model)
    return t is None or t.get("type") == "adaptive"


def search_tool(model: str) -> dict:
    kind = "web_search_20250305" if model.startswith(_SEARCH_BASIC_PREFIXES) else "web_search_20260209"
    return {"type": kind, "name": "web_search",
            "max_uses": int(os.environ.get("REEDUS_CLAUDE_MAX_SEARCHES", "2"))}


class AnthropicBackend(CloudBackend):
    PROVIDER = "anthropic"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        key = (api_key or llm_settings.key("anthropic")).strip()
        if not key:
            raise CloudUnavailable(
                "no Anthropic API key. Add one in ReedOS → Settings → Cloud brain "
                "(or ANTHROPIC_API_KEY in deploy/xeon.secrets.env).",
                provider="anthropic", retryable=False)
        try:
            import anthropic
        except ImportError as e:
            raise CloudUnavailable(
                "the anthropic package is missing: "
                "pip install -r reedus_companion/requirements/cloud.txt",
                provider="anthropic", retryable=False) from e
        super().__init__(model or llm_settings.model("anthropic"))
        # we retry ourselves (base.CloudBackend.chat), so the SDK shouldn't stack its own
        self._client = anthropic.Anthropic(api_key=key, timeout=self._timeout, max_retries=0)
        from reedus_companion.cloud import llm
        self._search = llm.inline_search("anthropic")
        self._effort = os.environ.get("REEDUS_CLAUDE_EFFORT", "low")
        self._effort_router = os.environ.get("REEDUS_CLAUDE_EFFORT_ROUTER", "low")
        # "adaptive": let him think before replies (a caller asks for it per turn, e.g. the
        # Lite relay's thinking setting); None: the quick spoken-reply defaults above
        self._think_mode: Optional[str] = None
        print(f"[reedus] claude backend: {self.model} (effort={self._effort}/{self._effort_router}, "
              f"thinking={(thinking_for(self.model) or {}).get('type', 'default')}, "
              f"web_search={'on' if self._search else 'off'})", flush=True)

    def _request(self, system: str, turns: List[Dict[str, str]], max_tokens: int,
                 is_router: bool) -> Tuple[dict, bool]:
        ceiling = max(1, int(max_tokens))
        adaptive = (self._think_mode == "adaptive" and not is_router
                    and not self.model.startswith(_NO_THINK_PREFIXES))
        if thinks(self.model) or adaptive or "thinking" in self._dropped:
            ceiling += _HEADROOM * (4 if adaptive and self._effort in ("medium", "high") else 1)
        kw: dict = {"model": self.model, "max_tokens": ceiling, "messages": alternate(turns)}
        if system:
            kw["system"] = system
        think = {"type": "adaptive"} if adaptive else thinking_for(self.model)
        if think:
            kw["thinking"] = think
        if not self.model.startswith(_NO_THINK_PREFIXES):
            kw["output_config"] = {"effort": self._effort_router if is_router else self._effort}
        grounded = bool(self._search and not is_router)
        if grounded:
            kw["tools"] = [search_tool(self.model)]
        return kw, grounded

    def _once(self, system, turns, max_tokens, *, is_router, on_token, cancel,
              temperature, use_adapter):
        kw, grounded = self._request(system, turns, max_tokens, is_router)
        client = self._client
        if client is None:
            raise CloudUnavailable("claude backend is closed", provider="anthropic")

        if on_token is None and cancel is None:
            msg = self._call(lambda k: client.messages.create(**k), kw, _OPTIONAL)
            return _from_message(msg)

        pieces: List[str] = []
        final = None

        def _stream(k):
            nonlocal final
            pieces.clear()
            with client.messages.stream(**k) as stream:
                for text in stream.text_stream:
                    if cancelled(cancel):
                        return None          # leaving the with-block closes the connection
                    pieces.append(text)
                    if on_token is not None:
                        on_token(text)
                final = stream.get_final_message()
            return final

        self._call(_stream, kw, _OPTIONAL)
        if final is None:                    # cancelled mid-reply
            return "".join(pieces), 0, 0, 0
        text, n_in, n_out, searches = _from_message(final)
        return text or "".join(pieces), n_in, n_out, searches


def _from_message(msg) -> Tuple[str, int, int, int]:
    """(text, input tokens, output tokens, web searches) from a Message."""
    text = "".join(getattr(b, "text", "") or "" for b in (getattr(msg, "content", None) or [])
                   if getattr(b, "type", "") == "text")
    u = getattr(msg, "usage", None)
    n_in = n_out = searches = 0
    if u is not None:
        n_in = int(getattr(u, "input_tokens", 0) or 0) \
            + int(getattr(u, "cache_read_input_tokens", 0) or 0) \
            + int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        n_out = int(getattr(u, "output_tokens", 0) or 0)
        stu = getattr(u, "server_tool_use", None)
        searches = int(getattr(stu, "web_search_requests", 0) or 0) if stu is not None else 0
    return text, n_in, n_out, searches
