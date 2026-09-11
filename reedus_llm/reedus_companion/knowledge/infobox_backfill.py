"""re-fetch the fandom infoboxes the corpus build threw away and upgrade the entity index."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .entity_index import INDEX_PATH, KNOWLEDGE_DIR, EntityIndex, normalise

CACHE_PATH = KNOWLEDGE_DIR / "_infobox_cache.sqlite"

# one 50-title batch every 1.1s (~45 pages/s) has run clean; faster gets you blocked
USER_AGENT = "ReedusCorpusBot/1.0 (personal research project; contact via github)"
BATCH = 50
SLEEP = 1.1
TIMEOUT = 30
RETRIES = 3

_FIELDS = {
    "jutsu": "owns", "techniques": "owns", "abilities": "owns", "skills": "owns",
    "kekkeigenkai": "owns", "kekkei genkai": "owns", "weapons": "owns", "weapon": "owns",
    "tools": "owns", "devilfruit": "owns", "devil fruit": "owns", "fruit": "owns",
    "zanpakuto": "owns", "quirk": "owns", "stand": "owns", "nature type": "owns",
    "naturetype": "owns", "affinity": "owns",
    "users": "owned", "user": "owned", "wielders": "owned", "wielder": "owned",
    "owners": "owned", "owner": "owned", "known users": "owned", "knownusers": "owned",
}

# wiki-link and template noise inside a field value
_LINK = re.compile(r"\[\[\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]\]")
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_TAG = re.compile(r"<[^>]+>")
_REF = re.compile(r"(?is)<ref[^>]*>.*?</ref>|<ref[^>]*/>")


def _api(host: str, titles: Sequence[str]) -> dict:
    """One MediaWiki content query. Returns {} on permanent failure rather than raising."""
    params = {
        "action": "query", "format": "json", "prop": "revisions",
        "rvprop": "content", "rvslots": "main", "formatversion": "2",
        "titles": "|".join(titles),
    }
    url = f"https://{host}/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as fh:
                return json.loads(fh.read().decode("utf-8", "replace"))
        except Exception as e:                                   # noqa: BLE001
            if attempt == RETRIES - 1:
                print(f"    ! {host}: {e}", file=sys.stderr)
                return {}
            time.sleep(2 ** attempt)
    return {}


def parse_infobox(wikitext: str) -> Dict[str, List[str]]:
    """Ownership-bearing fields of the first infobox, as lists of clean names."""
    if not wikitext:
        return {}
    text = _REF.sub("", wikitext)

    start = text.find("{{")
    if start < 0:
        return {}
    depth, end = 0, -1
    for i in range(start, min(len(text), start + 20000)):
        if text.startswith("{{", i):
            depth += 1
        elif text.startswith("}}", i):
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return {}
    block = text[start + 2:end]

    parts, buf, d = [], [], 0
    for ch in block:
        if ch == "{" or ch == "[":
            d += 1
        elif ch == "}" or ch == "]":
            d -= 1
        if ch == "|" and d <= 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))

    out: Dict[str, List[str]] = {}
    for part in parts:
        if "=" not in part:
            continue
        field, _, value = part.partition("=")
        key = field.strip().lower().replace("_", " ")
        if key not in _FIELDS:
            continue
        names = _LINK.findall(value)
        if not names:
            # unlinked comma/newline lists still happen on older pages
            cleaned = _TAG.sub("", _TEMPLATE.sub("", value))
            names = [n for n in re.split(r"[,\n•*]+", cleaned)]
        vals = []
        for n in names:
            n = _TAG.sub("", _TEMPLATE.sub("", n)).strip(" \t'\"*•-, ")
            n = re.sub(r"\s*\(.*?\)\s*$", "", n).strip()
            if 2 < len(n) < 80 and not n.lower().startswith(("file:", "image:", "category:")):
                vals.append(n)
        if vals:
            out.setdefault(key, []).extend(vals)
    return out


def _open_cache() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(CACHE_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS fetched (
            host TEXT NOT NULL, title TEXT NOT NULL, ok INTEGER NOT NULL,
            fields TEXT, ts REAL, PRIMARY KEY (host, title));
    """)
    return db


def _targets(limit: Optional[int], host_filter: Optional[str]) -> List[Tuple[str, str, str]]:
    """(host, page_title, index_key) for pages worth backfilling, best first."""
    ix = EntityIndex()
    rows = ix.db.execute(
        "SELECT key, title, url, passages FROM topics "
        "WHERE url LIKE '%fandom.com%' ORDER BY passages DESC").fetchall()
    out = []
    for key, title, url, _n in rows:
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.netloc
            page = urllib.parse.unquote(parsed.path.split("/wiki/", 1)[1])
        except Exception:                                        # noqa: BLE001
            continue
        if host_filter and host != host_filter:
            continue
        if not page or "|" in page:
            continue
        out.append((host, page.replace("_", " "), key))
        if limit and len(out) >= limit:
            break
    return out


def run(limit: Optional[int], host_filter: Optional[str], plan_only: bool) -> None:
    if not INDEX_PATH.exists():
        raise SystemExit("build the entity index first: "
                         "python -m reedus_companion.knowledge.entity_index build")

    targets = _targets(limit, host_filter)
    by_host: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for host, page, key in targets:
        by_host[host].append((page, key))

    print(f"[backfill] {len(targets):,} candidate pages across {len(by_host)} wikis")
    for host, pages in sorted(by_host.items(), key=lambda kv: -len(kv[1]))[:12]:
        print(f"    {host:<38} {len(pages):,}")
    if plan_only:
        est = len(targets) / BATCH * SLEEP
        print(f"[backfill] --plan: nothing fetched. Estimated wall time ~{est / 60:.0f} min")
        return

    cache = _open_cache()
    done = {(h, t) for h, t in cache.execute("SELECT host, title FROM fetched")}
    index_db = sqlite3.connect(INDEX_PATH)
    known = {k for (k,) in index_db.execute("SELECT key FROM topics")}
    persons = {k for (k,) in index_db.execute("SELECT key FROM topics WHERE person=1")}

    edges: List[Tuple[str, str]] = []      # (subject_key, attribute_key)
    fetched = skipped = 0
    t0 = time.time()

    for host, pages in sorted(by_host.items(), key=lambda kv: -len(kv[1])):
        todo = [(p, k) for p, k in pages if (host, p) not in done]
        skipped += len(pages) - len(todo)
        if not todo:
            continue
        print(f"[backfill] {host}: {len(todo):,} to fetch")
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            data = _api(host, [p for p, _k in batch])
            time.sleep(SLEEP)
            page_map = {}
            for pg in (data.get("query", {}).get("pages", []) or []):
                try:
                    content = pg["revisions"][0]["slots"]["main"]["content"]
                except Exception:                                # noqa: BLE001
                    content = ""
                page_map[pg.get("title", "")] = content

            for page, key in batch:
                wikitext = page_map.get(page) or page_map.get(page.replace("_", " ")) or ""
                fields = parse_infobox(wikitext)
                cache.execute(
                    "INSERT OR REPLACE INTO fetched(host,title,ok,fields,ts) VALUES(?,?,?,?,?)",
                    (host, page, 1 if wikitext else 0, json.dumps(fields), time.time()))
                fetched += 1
                for field, values in fields.items():
                    direction = _FIELDS[field]
                    for raw in values:
                        other = normalise(raw)
                        if other not in known or other == key:
                            continue
                        if direction == "owns":
                            subj, attr = key, other
                        else:
                            subj, attr = other, key
                        if subj not in persons or attr in persons:
                            continue
                        edges.append((subj, attr))
            cache.commit()
            if fetched % 500 < BATCH:
                rate = fetched / max(1e-6, time.time() - t0)
                print(f"    {fetched:,} fetched, {len(edges):,} edges, {rate:.0f} pages/s",
                      flush=True)

    print(f"[backfill] fetched {fetched:,} pages ({skipped:,} already cached), "
          f"{len(edges):,} infobox edges")

    if edges:
        counts: Dict[Tuple[str, str], int] = defaultdict(int)
        for s, a in edges:
            counts[(s, a)] += 1
        index_db.executemany(
            "INSERT INTO edges(subject,attribute,frame,n,source) VALUES(?,?,'infobox',?, "
            "'infobox') ON CONFLICT(subject,attribute,frame,source) DO UPDATE SET n=n+excluded.n",
            [(s, a, n) for (s, a), n in counts.items()])
        index_db.commit()
        print(f"[backfill] wrote {len(counts):,} distinct infobox edges into {INDEX_PATH.name}")
    index_db.close()
    cache.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=None,
                    help="most-discussed N pages only (they are ordered by passage count)")
    ap.add_argument("--host", default=None, help="restrict to one wiki, e.g. naruto.fandom.com")
    ap.add_argument("--plan", action="store_true", help="show what would be fetched, fetch nothing")
    args = ap.parse_args(argv)
    run(args.limit, args.host, args.plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
