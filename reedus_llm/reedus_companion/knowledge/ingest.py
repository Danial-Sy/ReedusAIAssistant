"""ingest one topic: article -> chunks -> vectors -> shard."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional

CHUNK_WORDS = 180
CHUNK_OVERLAP = 40
MAX_CHUNKS_PER_TOPIC = 24
UA = "Reedus/3.0 (personal companion; contact: local)"
TIMEOUT = 12


@dataclass
class Article:
    title: str
    text: str
    url: str
    source: str


def _get_json(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _mediawiki_extract(api_base: str, topic: str, source: str) -> Optional[Article]:
    """Plain-text lead + body via the MediaWiki extracts API. Fandom and Wikipedia share it."""
    q = urllib.parse.urlencode({
        "action": "query", "format": "json", "prop": "extracts", "explaintext": "1",
        "redirects": "1", "titles": topic,
    })
    data = _get_json(f"{api_base}?{q}")
    if not data:
        return None
    pages = (data.get("query") or {}).get("pages") or {}
    for pid, page in pages.items():
        if pid == "-1" or not page.get("extract"):
            continue
        title = page.get("title", topic)
        return Article(title=title, text=page["extract"], source=source,
                       url=f"{api_base.rsplit('/api.php', 1)[0]}/wiki/"
                           f"{urllib.parse.quote(title.replace(' ', '_'))}")
    return None


def _fandom_search(topic: str) -> Optional[str]:
    """Which fandom wiki hosts a topic. Cheap and often wrong, wikipedia is the fallback."""
    slug = re.sub(r"[^a-z0-9]+", "", topic.lower().split()[0]) if topic.split() else ""
    return f"https://{slug}.fandom.com/api.php" if len(slug) >= 3 else None


def fetch_article(topic: str, extra_wikis: Optional[List[str]] = None) -> Optional[Article]:
    for base in (extra_wikis or []):
        art = _mediawiki_extract(base, topic, source=base)
        if art and len(art.text.split()) > 120:
            return art
    fandom = _fandom_search(topic)
    if fandom:
        art = _mediawiki_extract(fandom, topic, source="fandom")
        if art and len(art.text.split()) > 120:
            return art
    return _mediawiki_extract("https://en.wikipedia.org/w/api.php", topic, source="wikipedia")


_SECTION = re.compile(r"^==+\s*(.+?)\s*==+$", re.M)
_SKIP_SECTIONS = re.compile(
    r"^(references|external links|see also|notes|bibliography|further reading|"
    r"gallery|trivia|citations|sources)$", re.I)


def chunk(article: Article) -> List[dict]:
    """Split on section headings, then pack to ~CHUNK_WORDS with overlap."""
    parts, cur_head, buf = [], article.title, []
    for line in article.text.splitlines():
        m = _SECTION.match(line.strip())
        if m:
            if buf:
                parts.append((cur_head, " ".join(buf)))
                buf = []
            cur_head = m.group(1)
            continue
        if line.strip():
            buf.append(line.strip())
    if buf:
        parts.append((cur_head, " ".join(buf)))

    out: List[dict] = []
    for head, body in parts:
        if _SKIP_SECTIONS.match(head.strip()):
            continue
        words = body.split()
        if len(words) < 25:
            continue
        step = max(1, CHUNK_WORDS - CHUNK_OVERLAP)
        for i in range(0, len(words), step):
            piece = " ".join(words[i:i + CHUNK_WORDS])
            if len(piece.split()) < 25:
                break
            out.append({
                "topic": article.title,
                "title": f"{article.title} — {head}" if head != article.title else article.title,
                "text": piece,
                "url": article.url,
                "added": time.strftime("%Y-%m-%d"),
            })
            if len(out) >= MAX_CHUNKS_PER_TOPIC:
                return out
    return out


# link titles never worth a passage of their own
_LINK_JUNK = re.compile(
    r"^(list of|category:|template:|file:|help:|portal:|wikipedia:|talk:|user:|"
    r"index of|outline of|timeline of|bibliography|glossary)", re.I)
_LINK_YEARISH = re.compile(r"^\d{3,4}(s|\b)|^(january|february|march|april|may|june|july|"
                           r"august|september|october|november|december)\b", re.I)


def related_topics(topic: str, limit: int = 20,
                   extra_wikis: Optional[List[str]] = None) -> List[str]:
    """Outbound links from a topic's page, filtered to things worth knowing."""
    bases = list(extra_wikis or [])
    fandom = _fandom_search(topic)
    if fandom:
        bases.append(fandom)
    bases.append("https://en.wikipedia.org/w/api.php")

    for base in bases:
        q = urllib.parse.urlencode({
            "action": "query", "format": "json", "prop": "links", "redirects": "1",
            "titles": topic, "pllimit": str(min(200, limit * 6)), "plnamespace": "0",
        })
        data = _get_json(f"{base}?{q}")
        if not data:
            continue
        pages = (data.get("query") or {}).get("pages") or {}
        out: List[str] = []
        for pid, page in pages.items():
            if pid == "-1":
                continue
            for link in page.get("links", []):
                t = (link.get("title") or "").strip()
                if not t or _LINK_JUNK.match(t) or _LINK_YEARISH.match(t):
                    continue
                if len(t) < 3 or t.lower() == topic.lower():
                    continue
                out.append(t)
                if len(out) >= limit:
                    return out
        if out:
            return out
    return []


def ingest_topic(topic: str, store, embed: Callable[[List[str]], List[List[float]]],
                 domain: str, extra_wikis: Optional[List[str]] = None,
                 debug: bool = False) -> int:
    """Fetch, chunk, embed and store one topic. Returns passages added."""
    art = fetch_article(topic, extra_wikis)
    if not art:
        if debug:
            print(f"[knowledge] no article for {topic!r}")
        return 0
    rows = chunk(art)
    if not rows:
        return 0
    vecs = embed([r["text"] for r in rows])
    n = store.shard(domain).append(vecs, rows)
    if debug:
        print(f"[knowledge] +{n} passages for {art.title!r} ({art.source}) -> {domain}")
    return n
