"""search via gemini grounding, cloud mode only."""
from __future__ import annotations

import os
import threading
from typing import List, Optional, Tuple

_CLIENT = None
_LOCK = threading.RLock()

_INSTRUCTION = (
    "Search the web and answer with the specific facts requested, densely and "
    "without preamble. Lead with the single most direct answer -- a number, a "
    "name, a date -- then at most two supporting sentences. Do not hedge, do not "
    "explain your process, do not offer to help further. If the answer is not "
    "findable, say exactly: no results."
)


def available() -> bool:
    """True when cloud mode is active and a Gemini key exists."""
    if (os.environ.get("REEDUS_CLOUD_SEARCH") or "1") == "0":
        return False
    try:
        from reedus_companion import mode
        if mode.current() != mode.CLOUD:
            return False
    except Exception:
        return False
    return bool((os.environ.get("GEMINI_API_KEY") or "").strip())


def _client():
    global _CLIENT
    with _LOCK:
        if _CLIENT is None:
            from google import genai
            _CLIENT = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        return _CLIENT


def _citations(interaction) -> List[Tuple[str, str]]:
    """[(title, url)] from url_citation annotations, best effort."""
    out: List[Tuple[str, str]] = []
    try:
        for step in getattr(interaction, "steps", None) or []:
            if getattr(step, "type", "") != "model_output":
                continue
            for block in getattr(step, "content", None) or []:
                for ann in getattr(block, "annotations", None) or []:
                    if getattr(ann, "type", "") != "url_citation":
                        continue
                    url = getattr(ann, "url", "") or ""
                    title = getattr(ann, "title", "") or url
                    if url and (title, url) not in out:
                        out.append((title, url))
    except Exception:
        pass
    return out


def search(query: str, max_results: Optional[int] = None, full: bool = False) -> str:
    """Same contract as websearch.web_search: a text block for the extractor, or a failure line."""
    q = (query or "").strip()
    if not q:
        return f"[web_search: no results for {query!r}]"
    n = max_results or 5
    try:
        from reedus_companion.cloud.gemini_backend import create_interaction
        interaction = create_interaction(
            _client(),
            model=os.environ.get("REEDUS_GEMINI_MODEL", "gemini-3.7-flash"),
            input=q,
            system_instruction=_INSTRUCTION,
            tools=[{"type": "google_search"}],
            generation_config={"thinking_level": "low", "max_output_tokens": 900},
        )
        answer = (getattr(interaction, "output_text", "") or "").strip()
    except Exception as e:                                       # noqa: BLE001
        return f"[web_search error: {type(e).__name__}: {e}]"

    if not answer or answer.lower().startswith("no results"):
        return f"[web_search: no results for {q!r}]"

    cites = _citations(interaction)
    try:
        from reedus_companion.cloud import budget
        budget.record_tokens(os.environ.get("REEDUS_GEMINI_MODEL", "gemini-3.7-flash"),
                             len(q) // 4 + 80, len(answer) // 4,
                             grounded=bool(cites))
    except Exception:
        pass

    lines = [f"Search results for: {q}", f"1. {answer}"]
    for i, (title, url) in enumerate(cites[:max(0, n - 1)], 2):
        lines.append(f"{i}. {title}\n   {url}" if full else f"{i}. {title}")
    return "\n".join(lines)


def search_pages(query: str, n_pages: int = 1, max_results: Optional[int] = None) -> str:
    """Cloud equivalent of web_search_pages."""
    q = (query or "").strip()
    if not q:
        return ""
    got = search(q, max_results=max_results, full=True)
    return "" if got.startswith("[web_search") else got
