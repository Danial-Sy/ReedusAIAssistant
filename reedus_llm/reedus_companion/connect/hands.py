"""the apps agent: turns one plain-words request into calls on connected app tools.

the persona never sees a thousand tool schemas, it sees one tool, apps(request). this loop
gets the request, a shortlist of real tools, and runs them with the same <tool_call> text
protocol the persona uses, so it works on Gemini and on the local model alike.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Callable, Dict, List

from reedus_companion.serve import tools as toolmod

MAX_STEPS = 6
SHORTLIST = 24

_SYSTEM = """You are the hands of Reedus, a home robot. You operate the user's connected apps.
Do exactly what the request asks using the tools below, then stop.

To use a tool, write EXACTLY one block and nothing else, then wait for the result:
<tool_call>
{{"name": "tool_name", "arguments": {{...}}}}
</tool_call>

Rules:
- Use only the tools listed. Fill arguments from the request; never invent IDs, look them up.
- If a tool says an app isn't connected or needs authorization, stop and report that.
- Don't ask the user questions. If something is ambiguous, pick the sensible default.
- When done (or stuck), reply with one line starting with DONE: and a short factual summary
  of what happened or the information that was asked for. No tool call in that message.

Tools:
{tools}"""


def shortlist(request: str, catalog: List[dict], n: int = SHORTLIST) -> List[dict]:
    """Rank tools by word overlap with the request. Meta tools (search/execute) always stay."""
    if len(catalog) <= n:
        return catalog
    words = set(re.findall(r"[a-z0-9]{3,}", request.lower()))

    def score(t: dict) -> float:
        name = t["name"].lower()
        if any(k in name for k in ("search_tools", "multi_execute", "get_tool_schemas",
                                   "manage_connections")):
            return 1e6
        hay = set(re.findall(r"[a-z0-9]{3,}", (name.replace("_", " ") + " " +
                                                (t.get("description") or "")).lower()))
        return len(words & hay) + 2 * sum(w in name for w in words)

    return sorted(catalog, key=score, reverse=True)[:n]


def describe(t: dict) -> str:
    schema = t.get("inputSchema") or {}
    props = schema.get("properties") or {}
    req = set(schema.get("required") or [])
    args = []
    for k, v in list(props.items())[:12]:
        typ = v.get("type") or ("object" if v.get("properties") else "any")
        if isinstance(typ, list):
            typ = "|".join(map(str, typ))
        bit = f"{k}{'' if k in req else '?'}: {typ}"
        d = (v.get("description") or "").strip()
        if d:
            bit += f" ({d[:90]})"
        args.append(bit)
    desc = re.sub(r"\s+", " ", t.get("description") or "").strip()[:240]
    return f"- {t['name']}: {desc}\n    args: {', '.join(args) or 'none'}"


def run(request: str, catalog: List[dict], call: Callable[[str, dict], str], backend,
        debug: bool = False) -> str:
    """Returns a short summary for the persona to say. Never raises."""
    if not catalog:
        return "No apps are connected yet. Set them up in ReedOS → Settings → Connections."
    tools = shortlist(request, catalog)
    names = {t["name"] for t in tools}
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": _SYSTEM.format(tools="\n".join(describe(t) for t in tools))},
        {"role": "user", "content": f"Request: {request}"},
    ]
    gemini = type(backend).__name__.lower().startswith("gemini")
    last_result = ""
    for step in range(MAX_STEPS):
        try:
            raw = backend.chat(messages, 700, use_adapter=False, temperature=0.0,
                               tier="router" if gemini else None, name="apps")
        except Exception as e:                                   # noqa: BLE001
            return f"The apps helper failed: {type(e).__name__}: {e}"
        calls = toolmod.parse_tool_calls(raw)
        if not calls:
            m = re.search(r"DONE:\s*(.+)", raw or "", re.S)
            return (m.group(1) if m else toolmod.strip_tool_calls(raw)).strip()[:600] or \
                (last_result[:600] or "Done.")
        c = calls[0]
        name, args = c["name"], c.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if name not in names:
            result = f"[error] no tool called {name}; use one from the list"
        else:
            result = call(name, args)
        last_result = result
        if debug:
            print(f"\n[debug] apps step {step + 1}: {name}({json.dumps(args)[:160]}) -> "
                  f"{result[:200]!r}", file=sys.stderr)
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"[tool result, {name}]\n{result[:3500]}\n"
                                                    f"Continue, or reply DONE: <summary>."})
    return f"Stopped after {MAX_STEPS} steps. Last result: {last_result[:300]}"
