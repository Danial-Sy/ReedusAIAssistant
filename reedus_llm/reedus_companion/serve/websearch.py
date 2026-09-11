"""web search: self-hosted SearXNG if it's up, else Brave (BRAVE_API_KEY) or keyless DDG.
deps are lazy and nothing here raises to the caller."""
from __future__ import annotations
import html as html_mod
import os
import re
from typing import List, Dict

try:
    from reedus_companion import config
    _MAX, _PROVIDER = config.SEARCH_MAX_RESULTS, config.SEARCH_PROVIDER
except Exception:
    _MAX, _PROVIDER = 5, "auto"

SNIPPET_CHARS = int(os.environ.get("REEDUS_SNIPPET_CHARS", "240"))


# SearXNG (REEDUS_SEARXNG_URL). fixes the one-snippet-per-page problem: DDG returned a usable
# weather snippet once in three tries and widening results doesn't help when they all come from
# the same engine. searxng merges several engines so one of them usually has a content-bearing
# excerpt. json output has to be enabled in its settings.yml (formats: html, json) or
# ?format=json returns 403. brave/ddg/startpage refuse a home IP, mojeek/wikidata/marginalia
# don't; scripts/eval_search.py shows which answered. optional: if nothing is listening the first
# search notices instantly and DDG is used for the session.
SEARXNG_URL = os.environ.get("REEDUS_SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")

_SEARXNG_UP: "bool | None" = None
_SEARXNG_PROBED_AT: float = 0.0


def _searxng_recheck_s() -> float:
    try:
        return max(0.0, float(os.environ.get("REEDUS_SEARXNG_RECHECK_S", "180")))
    except (TypeError, ValueError):
        return 180.0


def _searxng_available() -> bool:
    global _SEARXNG_UP, _SEARXNG_PROBED_AT
    import time as _time
    ttl = _searxng_recheck_s()
    if _SEARXNG_UP is not None and (ttl <= 0
                                    or (_time.monotonic() - _SEARXNG_PROBED_AT) < ttl):
        return _SEARXNG_UP
    _was = _SEARXNG_UP
    _SEARXNG_PROBED_AT = _time.monotonic()
    if not SEARXNG_URL:
        _SEARXNG_UP = False
        return False
    try:
        import requests
        r = requests.get(f"{SEARXNG_URL}/healthz", timeout=1.5)
        _SEARXNG_UP = r.ok
    except Exception:                                            # noqa: BLE001
        try:
            import requests
            r = requests.get(SEARXNG_URL, timeout=1.5)           # older builds have no /healthz
            _SEARXNG_UP = r.ok
        except Exception:                                        # noqa: BLE001
            _SEARXNG_UP = False
    if os.environ.get("REEDUS_DEBUG") and _was != _SEARXNG_UP:
        import sys
        print(f"[search] searxng at {SEARXNG_URL}: "
              f"{'up' if _SEARXNG_UP else 'not reachable, using ddg'}", file=sys.stderr)
    return _SEARXNG_UP


def _pick_provider() -> str:
    try:
        from reedus_companion.cloud import gemini_search
        if gemini_search.available():
            return "gemini"
    except Exception:
        pass
    if _PROVIDER == "searxng":
        return "searxng"
    if _PROVIDER == "brave":
        return "brave"
    if _PROVIDER in ("ddg", "duckduckgo"):
        return "ddg"
    if _searxng_available():
        return "searxng"
    return "brave" if os.environ.get("BRAVE_API_KEY") else "ddg"


def _search_searxng(query: str, n: int) -> List[Dict]:
    """Query local SearXNG, fall back to DDG if it isn't reachable."""
    import requests
    r = requests.get(
        f"{SEARXNG_URL}/search",
        params={"q": query, "format": "json", "language": "en",
                "safesearch": 0, "categories": "general"},
        headers={"User-Agent": "Reedus/1.0"}, timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    out = []
    for i in (data.get("results") or [])[:n]:
        out.append({"title": i.get("title", ""),
                    "url": i.get("url", ""),
                    "snippet": i.get("content", "") or ""})
    return out


def _search_brave(query: str, n: int) -> List[Dict]:
    import requests
    r = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"},
        params={"q": query, "count": n}, timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": i.get("description", "") or ""}
            for i in (data.get("web", {}) or {}).get("results", [])[:n]]


def _search_ddg(query: str, n: int) -> List[Dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    out = []
    with DDGS() as ddgs:
        for i in ddgs.text(query, max_results=n):
            out.append({"title": i.get("title", ""), "url": i.get("href", "") or i.get("url", ""),
                        "snippet": i.get("body", "") or ""})
    return out


_SEARCH_NOISE = re.compile(
    r"\b(oh|ooo+|ah+|aight|yo|hey|man|bro|dude|buddy|lol|lmao|honestly|i mean|like|"
    r"you are|youre|thats|that's|its|it's|im|i'?m|ive|i'?ve|really|actually|of course|"
    r"absolutely|definitely|kinda|sorta|yeah|yea|nah|peak|sharp|awesome|excellent|amazing|"
    r"but|and|so|well|just|gotta|wanna|smth|yk|idk|ngl|tbh|"
    r"gotcha|gotchu|what about|how about|specifically|exactly|tho|though|"
    r"alright|okay|ok|anyway|anyways|btw|wait|hmm+|cool|nice|"
    r"wow+|woah|whoa|bruh|geez|damn|sheesh)\b", re.I)
_SEARCH_MAX_WORDS = int(os.environ.get("REEDUS_SEARCH_MAX_WORDS", "12"))


def clean_query(q: str) -> str:
    """Turn a conversational sentence into something a search engine can answer."""
    q = (q or "").strip()
    if not q:
        return q
    q = re.sub(r"[^\w\s'\-]", " ", q)          # punctuation confuses the index
    q = _SEARCH_NOISE.sub(" ", q)
    words = [w for w in q.split() if w]
    if len(words) > _SEARCH_MAX_WORDS:
        words = words[-_SEARCH_MAX_WORDS:]
    return " ".join(words).strip()


def web_search(query: str, max_results: int | None = None, full: bool = False) -> str:
    try:
        from reedus_companion.cloud import gemini_search
        if gemini_search.available():
            return gemini_search.search(query, max_results=max_results, full=full)
    except Exception:
        pass
    n = max_results or _MAX
    cleaned = clean_query(query)
    if len(cleaned.split()) >= 2:
        query = cleaned
    provider = _pick_provider()
    try:
        if provider == "searxng":
            try:
                results = _search_searxng(query, n)
            except Exception as e:                               # noqa: BLE001
                if os.environ.get("REEDUS_DEBUG"):
                    import sys
                    print(f"[searxng unreachable: {type(e).__name__}] falling back to ddg",
                          file=sys.stderr)
                results = _search_ddg(query, n)
            # empty is also a failure: searxng being up doesn't mean it can answer. the day google
            # refuses the container it returns 200 with an empty list. one extra DDG call on a total miss.
            if not results:
                if os.environ.get("REEDUS_DEBUG"):
                    import sys
                    print("[searxng returned 0 results] falling back to ddg", file=sys.stderr)
                try:
                    results = _search_ddg(query, n)
                except Exception:                                # noqa: BLE001
                    pass
        elif provider == "brave":
            results = _search_brave(query, n)
        else:
            results = _search_ddg(query, n)
    except Exception as e:
        return f"[web_search error: {type(e).__name__}: {e}]"
    if not results:
        return f"[web_search: no results for {query!r}]"
    # compact by default: this block is extractor prefill and prefill IS the latency on the xeon.
    # urls are ~20 tokens each and useless to a reader, and 300-char snippets are one sentence of
    # answer plus SEO boilerplate. full=True restores urls for a caller that needs to cite.
    lines = [f"Search results for: {query}"]
    for i, r in enumerate(results, 1):
        sn = (r["snippet"] or "").strip().replace("\n", " ")
        if len(sn) > SNIPPET_CHARS:
            sn = sn[:SNIPPET_CHARS - 3] + "..."
        if full:
            lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {sn}")
        else:
            lines.append(f"{i}. {r['title']}, {sn}")
    return "\n".join(lines)


# --- last resort: read the page ---
PAGE_CHARS = int(os.environ.get("REEDUS_PAGE_CHARS", "2400"))
PAGE_TIMEOUT = float(os.environ.get("REEDUS_PAGE_TIMEOUT", "6"))

_TAG_JUNK = re.compile(r"<(script|style|noscript|svg|head)\b.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n\s*\n+")


def _html_to_text(html: str) -> str:
    """HTML to readable text with no parser dependency."""
    html = _TAG_JUNK.sub(" ", html)
    html = re.sub(r"<(br|/p|/div|/li|/tr|/h[1-6]|/td|/th)\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"<(td|th)\b[^>]*>", " | ", html, flags=re.I)
    text = _TAGS.sub("", html)
    text = html_mod.unescape(text)
    text = _WS.sub(" ", text)
    return _BLANKS.sub("\n", text).strip()


def fetch_page_text(url: str, max_chars: int = PAGE_CHARS) -> str:
    """Readable text from one URL, or "" on any failure."""
    try:
        import requests
        r = requests.get(
            url, timeout=PAGE_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Reedus/1.0)",
                     "Accept-Language": "en-US,en;q=0.9"})
        r.raise_for_status()
        if "html" not in (r.headers.get("Content-Type") or "").lower():
            return ""
        # cap BEFORE parsing, some pages are 2 MB of inlined json
        text = _html_to_text(r.text[:400_000])
    except Exception:                                        # noqa: BLE001
        return ""
    return text[:max_chars]


def web_search_pages(query: str, n_pages: int = 1, max_results: int | None = None) -> str:
    """Search, then open the top `n_pages` results and return their text."""
    try:
        from reedus_companion.cloud import gemini_search
        if gemini_search.available():
            return gemini_search.search_pages(query, n_pages=n_pages,
                                              max_results=max_results)
    except Exception:
        pass
    n = max_results or _MAX
    cleaned = clean_query(query)
    if len(cleaned.split()) >= 2:
        query = cleaned
    provider = _pick_provider()
    try:
        if provider == "searxng":
            try:
                results = _search_searxng(query, n)
            except Exception:                                # noqa: BLE001
                results = _search_ddg(query, n)
            if not results:
                results = _search_ddg(query, n)
        elif provider == "brave":
            results = _search_brave(query, n)
        else:
            results = _search_ddg(query, n)
    except Exception:                                        # noqa: BLE001
        return ""
    blocks = []
    for r in results[:max(1, n_pages)]:
        url = (r.get("url") or "").strip()
        if not url:
            continue
        text = fetch_page_text(url)
        if len(text) < 120:
            continue
        blocks.append(f"Page: {r.get('title') or url}\n{text}")
    if not blocks:
        return ""
    return f"Full page text for: {query}\n\n" + "\n\n".join(blocks)


if __name__ == "__main__":
    import sys
    print("provider:", _pick_provider())
    q = " ".join(sys.argv[1:]) or "latest python version"
    print(web_search(q))
    print("\n--- page text (last resort path) ---")
    print(web_search_pages(q)[:1200] or "(nothing fetched)")
