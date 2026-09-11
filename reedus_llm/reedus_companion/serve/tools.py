"""tools + qwen-native <tool_call> handling."""
from __future__ import annotations
import re
import json
from typing import Callable, Dict, List, Any

from reedus_companion.serve.websearch import web_search

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)

TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "web_search",
        "description": "Search the web for current or unknown info (recent events, scores, "
                       "prices, news, anything that may have changed since training).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query."}},
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": "Pin a fact to long-term memory so it stays stable in every future "
                       "conversation. Use for durable facts about the user (name, preferences, "
                       "ongoing projects) AND for any fact the user corrects you on or confirms "
                       "after you got it wrong, pinning it stops you contradicting it later.",
        "parameters": {
            "type": "object",
            "properties": {"fact": {"type": "string", "description": "One-sentence fact to remember."}},
            "required": ["fact"],
        },
    },
]


def render_tools_block() -> str:
    """Compact tool instructions injected into the system prompt."""
    return (
        "You have tools. When one would help, emit EXACTLY one block then stop and wait:\n"
        '<tool_call>\n{"name": "web_search", "arguments": {"query": "..."}}\n</tool_call>\n'
        "Tools:\n"
        "- web_search(query): look up current or unknown info, news, scores, prices, "
        "release dates, who currently holds a role, anything that may have changed. Use it "
        "instead of guessing; never make up a current fact. If the user explicitly asks you "
        "to search, look up, google, find, or check something online, ALWAYS run web_search, "
        "even if you think you know the answer.\n"
        "- remember(fact): pin a durable fact to your bulletin, things about the user, and "
        "corrected/confirmed facts after a dispute, so you never contradict them again."
    )


class ToolRegistry:
    def __init__(self, memory=None):
        self._fns: Dict[str, Callable[..., str]] = {
            "web_search": lambda query="", **_: web_search(query),
        }
        if memory is not None:
            self._fns["remember"] = lambda fact="", **_: memory.remember(fact)

    def run(self, name: str, arguments: Dict[str, Any]) -> str:
        fn = self._fns.get(name)
        if fn is None:
            return f"[tool error: unknown tool {name!r}]"
        try:
            return fn(**(arguments or {}))
        except Exception as e:
            return f"[tool error: {type(e).__name__}: {e}]"


def parse_tool_calls(text: str) -> List[Dict[str, Any]]:
    calls = []
    for m in _TOOL_CALL_RE.finditer(text or ""):
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict) and "name" in obj:
                calls.append({"name": obj["name"], "arguments": obj.get("arguments", {})})
        except json.JSONDecodeError:
            continue
    return calls


def strip_tool_calls(text: str) -> str:
    return _TOOL_CALL_RE.sub("", text or "").strip()


_OPEN, _CLOSE = "<tool_call>", "</tool_call>"


class StreamFilter:
    """Suppress <tool_call> blocks in a token stream, not just a finished reply."""

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False

    def feed(self, piece: str) -> str:
        self.buf += piece or ""
        out = []
        while True:
            if self.inside:
                i = self.buf.find(_CLOSE)
                if i < 0:
                    # keep only what could still be part of the closing tag
                    self.buf = self.buf[-(len(_CLOSE) - 1):] if len(self.buf) > len(_CLOSE) \
                        else self.buf
                    break
                self.buf = self.buf[i + len(_CLOSE):]
                self.inside = False
                continue
            i = self.buf.find(_OPEN)
            if i >= 0:
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(_OPEN):]
                self.inside = True
                continue
            hold = 0
            for n in range(min(len(_OPEN) - 1, len(self.buf)), 0, -1):
                if _OPEN.startswith(self.buf[-n:]):
                    hold = n
                    break
            if hold:
                out.append(self.buf[:-hold])
                self.buf = self.buf[-hold:]
            else:
                out.append(self.buf)
                self.buf = ""
            break
        return "".join(out)

    def flush(self) -> str:
        """Whatever is still held back. Empty if the stream ended inside a tool call."""
        tail = "" if self.inside else self.buf
        self.buf, self.inside = "", False
        return tail


if __name__ == "__main__":
    s = ('let me check\n<tool_call>\n{"name":"web_search","arguments":{"query":"super bowl date"}}\n</tool_call>')
    print("parsed:", parse_tool_calls(s))
    print("stripped:", repr(strip_tool_calls(s)))
    print("tools block chars:", len(render_tools_block()))
