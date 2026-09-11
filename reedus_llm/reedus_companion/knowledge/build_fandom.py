#!/usr/bin/env python3
"""bulk-import a fandom wiki into the corpus. build box only."""

from __future__ import annotations

import argparse
import gzip
import html as htmlmod
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

UA = "Reedus/3.0 (personal companion; local use)"
TIMEOUT = 20
PAUSE = 0.15
ALLPAGES_LIMIT = 500
WORKERS = int(os.environ.get("REEDUS_FANDOM_WORKERS", "4"))
ABORT_AFTER_EMPTY = 60
# pages the preflight fetches before committing to a wiki, ~2s
PREFLIGHT_PAGES = 8

_SKIP_EXTRA = re.compile(
    r"\((disambiguation|disambig)\)$|^(talk|user|forum|blog|thread|message wall):", re.I)

if not os.environ.get("REEDUS_FANDOM_NO_DEPS"):
    from reedus_companion import config                                # noqa: E402
    from reedus_companion.knowledge import domains                     # noqa: E402
    from reedus_companion.knowledge.store import BulkWriter            # noqa: E402
    from reedus_companion.knowledge.build_corpus import (              # noqa: E402
        _BuildEmbedder, _chunks, _route_domain, _SKIP_TITLE, EMBED_BATCH)
else:
    EMBED_BATCH = 512
    _SKIP_TITLE = re.compile(
        r"^(list of|lists of|index of|outline of|timeline of|glossary of|bibliography of|"
        r"comparison of|table of|category:|template:|portal:|file:|wikipedia:|draft:|module:)",
        re.I)


def _read(r) -> str:
    """Body as text, transparently gunzipped."""
    raw = r.read()
    if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw.decode("utf-8", "replace")


def _api(base: str, params: Dict[str, str], tries: int = 3) -> Tuple[Optional[dict], Optional[str]]:
    """(json, error). An `error` or relevant `warnings` block inside a 200 counts as failure."""
    url = f"{base}?{urllib.parse.urlencode(params)}"
    last = "unknown"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = _read(r)
            try:
                data = json.loads(body)
            except ValueError:
                last = f"non-JSON response ({body[:120].strip()!r})"
                time.sleep(1.5 * (attempt + 1))
                continue

            if isinstance(data.get("error"), dict):
                e = data["error"]
                return None, f"api error {e.get('code','?')}: {e.get('info','')}".strip()
            warn = data.get("warnings")
            if warn:
                msgs = []
                for mod, w in warn.items():
                    if mod not in ("parse", "query", "main"):
                        continue
                    txt = w.get("*") or w.get("warnings") or ""
                    if "Unrecognized value" in txt or "Unrecognized parameter" in txt:
                        msgs.append(f"{mod}: {txt.strip()}")
                if msgs:
                    return None, "api warning (this is the silent killer): " + " | ".join(msgs)
            return data, None

        except urllib.error.HTTPError as e:
            # real backoff on 429, hammering through the window turns a slowdown into a ban
            last = f"HTTP {e.code}"
            if e.code in (429, 503):
                try:
                    wait = float(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0.0
                time.sleep(max(wait, 5.0 * (attempt + 1)))
            elif e.code in (403, 404):
                return None, f"HTTP {e.code}, wrong subdomain, or the UA is being blocked"
            else:
                time.sleep(1.5 * (attempt + 1))
        except Exception as e:                                  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
    return None, f"failed after {tries} tries ({last})"


_BLOCK = re.compile(r"(?is)<(table|aside|figure|style|script|noscript)[^>]*>.*?</\1>")
_NOISE_DIV = re.compile(
    r"(?is)<div[^>]*class=\"[^\"]*\b(?:navbox|nav-?box|toc|wikia-gallery|gallery|"
    r"mw-references|references-?small|reference-?list|printfooter|catlinks|page-?footer|"
    r"portable-infobox|pi-\w+|infobox|quote|notice|ambox|messagebox|noprint)\b[^\"]*\"[^>]*>")
_REFS = re.compile(r"(?is)<(sup|span)\s[^>]*class=\"[^\"]*(?:reference|editsection|mw-editsection)"
                   r"[^\"]*\"[^>]*>.*?</\1>")
_HEAD = re.compile(r"(?is)<h([2-4])[^>]*>(.*?)</h\1>")
_PARA = re.compile(r"(?i)</(?:p|li|dd|dt|blockquote|div|h[1-6])>")
_TAG = re.compile(r"(?s)<[^>]+>")
_EDIT = re.compile(r"\[\s*edit(\s*\|\s*edit source)?\s*\]", re.I)
_DIV_ANY = re.compile(r"(?i)<(/?)div\b[^>]*>")


def _drop_div(h: str) -> str:
    """Remove noise <div>s with their contents, matching close tags by depth."""
    out, pos = [], 0
    for m in _NOISE_DIV.finditer(h):
        if m.start() < pos:
            continue
        out.append(h[pos:m.start()])
        depth, i = 1, m.end()
        while depth and i < len(h):
            nxt = _DIV_ANY.search(h, i)
            if not nxt:
                i = len(h)
                break
            depth += -1 if nxt.group(1) else 1
            i = nxt.end()
        pos = i
    out.append(h[pos:])
    return " ".join(out)


def html_to_text(raw: str) -> str:
    h = raw
    for _ in range(6):
        h2 = _drop_div(_BLOCK.sub(" ", h))
        if h2 == h:
            break
        h = h2
    h = _REFS.sub(" ", h)
    h = _HEAD.sub(lambda m: "\n== " + _TAG.sub(" ", m.group(2)).strip() + " ==\n", h)
    h = _PARA.sub("\n", h)
    txt = htmlmod.unescape(_TAG.sub(" ", h))
    txt = _EDIT.sub(" ", txt)
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in txt.splitlines())
    return "\n".join(ln for ln in lines if ln)


def _text_node(parse: dict) -> str:
    """The rendered HTML out of an action=parse body, under either JSON shape."""
    node = parse.get("text")
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return node.get("*") or ""
    return ""


def fetch_page_html(base: str, title: str) -> Tuple[str, Optional[str], Optional[str]]:
    """(title, rendered HTML, reason-it-failed). Exactly one of the last two is None."""
    data, err = _api(base, {"action": "parse", "format": "json", "page": title, "prop": "text",
                            "redirects": "1", "disablelimitreport": "1"})
    time.sleep(PAUSE)
    if err:
        return title, None, err
    raw = _text_node((data or {}).get("parse") or {})
    if not raw:
        return title, None, "parse returned no text node"
    return title, raw, None


def fetch_page(base: str, title: str) -> Tuple[str, Optional[str], Optional[str]]:
    """(title, plain text, reason-it-failed). Exactly one of the last two is None."""
    title, raw, err = fetch_page_html(base, title)
    if err or not raw:
        return title, None, err
    txt = html_to_text(raw)
    if not txt.strip():
        return title, None, f"{len(raw):,} bytes of HTML stripped to nothing"
    return title, txt, None


def random_titles(base: str, n: int = 10) -> List[str]:
    """N random content-namespace articles."""
    out: List[str] = []
    while len(out) < n:
        data, _ = _api(base, {"action": "query", "format": "json", "list": "random",
                              "rnnamespace": "0", "rnlimit": str(min(n - len(out), 20))})
        rows = ((data or {}).get("query") or {}).get("random") or []
        if not rows:
            break
        for row in rows:
            t = (row.get("title") or "").strip()
            if t and not _SKIP_TITLE.match(t) and not _SKIP_EXTRA.search(t):
                out.append(t)
        time.sleep(PAUSE)
    return out[:n]


def site_stats(wiki: str) -> Optional[dict]:
    """Article count for a wiki, without downloading anything."""
    data, err = _api(f"https://{wiki}.fandom.com/api.php",
                     {"action": "query", "format": "json", "meta": "siteinfo",
                      "siprop": "statistics|general"})
    if not data:
        return {"wiki": wiki, "error": err or "no response"}
    q = data.get("query") or {}
    st, gen = q.get("statistics") or {}, q.get("general") or {}
    if not st:
        return {"wiki": wiki, "error": "siteinfo returned no statistics"}
    return {"wiki": wiki, "name": gen.get("sitename", wiki),
            "articles": int(st.get("articles", 0)), "pages": int(st.get("pages", 0))}


HARD_FLOOR_YIELD = 2.0
MIN_BEST_YIELD = HARD_FLOOR_YIELD


def extraction_yield(raw_html: str, text: str) -> float:
    """Words of prose per KB of source HTML."""
    kb = max(len(raw_html) / 1024.0, 0.001)
    return len(text.split()) / kb


def sample_titles(wiki: str, base: str, n: int, fallback: Optional[List[str]] = None) -> List[str]:
    """Representative titles: random if the wiki supports it, else spread across the list."""
    picks = random_titles(base, n)
    if picks:
        return picks
    if not fallback:
        return []
    step = max(1, len(fallback) // n)
    return fallback[::step][:n]


def preflight(wiki: str, titles: Optional[List[str]] = None) -> Tuple[bool, str, list]:
    """Prove extraction works on this wiki before committing an hour to it."""
    base = f"https://{wiki}.fandom.com/api.php"
    picks = sample_titles(wiki, base, PREFLIGHT_PAGES, titles)
    if not picks:
        return False, "could not sample any titles", []

    got: List[Tuple[str, int, float]] = []
    reasons: List[str] = []
    for t in picks:
        _, raw, why = fetch_page_html(base, t)
        if not raw:
            reasons.append(f"{t}: {why}")
            continue
        text = html_to_text(raw)
        got.append((t, len(text.split()), extraction_yield(raw, text)))

    if not got:
        return False, "; ".join(reasons[:3]) or "every sampled page failed", got
    best = max(y for _, _, y in got)
    if best < MIN_BEST_YIELD:
        return False, (f"best of {len(got)} samples recovered only {best:.1f} words/KB of HTML "
                       f"(need {MIN_BEST_YIELD}), the stripper is eating article bodies"), got
    return True, "", got


def load_wiki_list(path: Path) -> List[str]:
    """Seed lines are `subdomain` or `subdomain domain`; the latter pins every page to that domain."""
    out = []
    for ln in path.read_text().splitlines():
        ln = ln.split("#")[0].strip()
        if ln:
            out.append(":".join(ln.split()[:2]))
    return out


def all_titles(base: str, limit: Optional[int] = None) -> Iterator[str]:
    """Every content-namespace article title, paged 500 at a time."""
    cont, n = None, 0
    while True:
        p = {"action": "query", "format": "json", "list": "allpages",
             "apnamespace": "0", "apfilterredir": "nonredirects",
             "aplimit": str(ALLPAGES_LIMIT)}
        if cont:
            p["apcontinue"] = cont
        data, err = _api(base, p)
        if not data:
            print(f"  allpages stopped after {n:,} titles: {err}", flush=True)
            return
        for row in (data.get("query") or {}).get("allpages", []):
            t = (row.get("title") or "").strip()
            if not t or _SKIP_TITLE.match(t) or _SKIP_EXTRA.search(t):
                continue
            yield t
            n += 1
            if limit and n >= limit:
                return
        cont = (data.get("continue") or {}).get("apcontinue")
        if not cont:
            return
        time.sleep(PAUSE)


def build_wiki(wiki: str, out: Path, emb, writer: BulkWriter, intro: bool,
               limit: Optional[int], batch: int, domain_hint: Optional[str] = None) -> int:
    base = f"https://{wiki}.fandom.com/api.php"
    print(f"\n=== {wiki}.fandom.com ({'lead only' if intro else 'full articles'}"
          f"{', domain=' + domain_hint if domain_hint else ''}) ===", flush=True)

    titles = list(all_titles(base, limit))
    if not titles:
        print(f"  no articles found, is {wiki!r} the right subdomain? "
              f"(check https://{wiki}.fandom.com exists)")
        return 0
    print(f"  {len(titles):,} article titles", flush=True)

    # preflight: two seconds of evidence before an hour of trust
    ok, why, samples = preflight(wiki, titles)
    if not ok:
        print(f"  PREFLIGHT FAILED for {wiki!r}: {why}")
        print(f"  not fetching the other {len(titles):,} pages, and NOT marking it done.")
        print(f"  run:  python -m reedus_companion.knowledge.fandom_doctor {wiki} -v")
        return 0
    print(f"  preflight ok, {len(samples)}/{PREFLIGHT_PAGES} random samples, "
          f"{min(n for _, n, _ in samples):,}-{max(n for _, n, _ in samples):,} words, "
          f"best yield {max(y for _, _, y in samples):.0f} words/KB", flush=True)

    n_pass, n_got, t0, t_embed = 0, 0, time.time(), 0.0
    n_fail, first_fail = 0, ""
    pend_rows: List[dict] = []
    pend_dom: List[str] = []

    def flush():
        nonlocal pend_rows, pend_dom, n_pass, t_embed
        if not pend_rows:
            return
        te = time.time()
        vecs = emb.embed([r["text"] for r in pend_rows])
        t_embed += time.time() - te
        for d in set(pend_dom):
            idx = [i for i, x in enumerate(pend_dom) if x == d]
            writer.add(d, vecs[idx], [pend_rows[i] for i in idx])
        n_pass += len(pend_rows)
        pend_rows, pend_dom = [], []

    STEP = 200
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i in range(0, len(titles), STEP):
            chunk = titles[i:i + STEP]
            for title, text, why in pool.map(lambda t: fetch_page(base, t), chunk):
                if not text:
                    n_fail += 1
                    if not first_fail:
                        first_fail = f"{title}: {why}"
                    continue
                if len(text.split()) < 25:
                    continue
                n_got += 1
                dom = domain_hint or _route_domain(title, text)
                for c in _chunks(title, text, lead_only=intro):
                    c["url"] = f"https://{wiki}.fandom.com/wiki/" + title.replace(" ", "_")
                    c["added"] = time.strftime("%Y-%m-%d")
                    c["topic"] = title
                    pend_rows.append(c)
                    pend_dom.append(dom)
                if len(pend_rows) >= batch:
                    flush()
            done = min(i + STEP, len(titles))
            if n_got == 0 and done >= ABORT_AFTER_EMPTY:
                print(f"  ABORT: {done:,} pages fetched and not one produced text. "
                      f"first failure was {first_fail or 'unrecorded'}. "
                      f"{wiki!r} is NOT marked done, investigate before re-running.")
                return 0
            el = time.time() - t0
            print(f"  {done:>7,}/{len(titles):,} titles  {n_pass + len(pend_rows):>9,} passages"
                  f"  {n_fail:>6,} failed  {done/max(el,1):>5.1f} titles/s  "
                  f"{el/60:>5.1f} min", flush=True)
    flush()
    el = time.time() - t0
    print(f"  done: {n_pass:,} passages from {n_got:,} articles in {el/60:.1f} min "
          f"(embed {t_embed/60:.1f} min, fetch {(el-t_embed)/60:.1f} min)", flush=True)
    if n_fail:
        pct = 100.0 * n_fail / max(len(titles), 1)
        print(f"  note: {n_fail:,} pages ({pct:.1f}%) produced no text, first: {first_fail}",
              flush=True)
    return n_pass


def repair_shards(out: Path) -> int:
    """Convert orphaned .f32 scratch left by a killed build into queryable .npy shards."""
    from reedus_companion.knowledge.store import DIM, build_row_index

    import numpy as np
    scratch = sorted(out.glob("*.f32"))
    if not scratch:
        print(f"  nothing to repair in {out}")
        return 0

    fixed = 0
    for raw in scratch:
        stem = raw.stem
        txt = out / f"{stem}.jsonl"
        n_vec = raw.stat().st_size // (DIM * 4)
        if not txt.exists() or n_vec == 0:
            print(f"  {stem}: {n_vec} vectors, no usable text, discarding")
            raw.unlink()
            txt.unlink(missing_ok=True)
            continue

        lines = txt.read_text(errors="replace").splitlines()
        while lines:
            try:
                json.loads(lines[-1])
                break
            except ValueError:
                lines.pop()
        n = min(n_vec, len(lines))
        if n == 0:
            print(f"  {stem}: nothing recoverable, discarding")
            raw.unlink()
            txt.unlink(missing_ok=True)
            continue

        arr = np.fromfile(raw, dtype=np.float32, count=n * DIM).reshape(n, DIM)
        np.save(out / f"{stem}.npy", arr)
        txt.write_text("\n".join(lines[:n]) + "\n", errors="replace")
        build_row_index(txt)
        raw.unlink()
        dropped = max(n_vec, len(lines)) - n
        print(f"  {stem}: recovered {n:,} passages"
              + (f" (dropped {dropped} unpaired at the tail)" if dropped else ""))
        fixed += n
    print(f"\n  repaired {fixed:,} passages across {len(scratch)} shard(s)")
    print("  NOTE: centroids and the topic index are rebuilt on the next completed build,")
    print("        or immediately with:  python -m reedus_companion.knowledge.reindex")
    return fixed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wikis", nargs="*",
                    help="fandom subdomains, e.g. naruto eldenring pokemon")
    ap.add_argument("--file", default=None,
                    help="newline-separated subdomains (see knowledge/seeds/fandom_wikis.txt)")
    ap.add_argument("--all", action="store_true",
                    help="use the bundled curated list of ~140 large wikis")
    ap.add_argument("--discover", action="store_true",
                    help="just report article counts and total estimate, ingest nothing")
    ap.add_argument("--repair", action="store_true",
                    help="recover .f32 scratch left by a build that was killed outright "
                         "(power loss, SIGKILL). Ctrl-C already finalises on its own.")
    ap.add_argument("--redo", action="store_true", help="re-ingest wikis already in the log")
    ap.add_argument("--full", action="store_true",
                    help="keep whole articles (chunked by section) instead of just the lead. "
                         "Fetch cost is identical either way; this changes corpus size only")
    ap.add_argument("--limit", type=int, default=None, help="max articles per wiki")
    ap.add_argument("--out", default=None)
    ap.add_argument("--batch", type=int, default=EMBED_BATCH)
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()

    if args.repair:
        out = Path(args.out).expanduser() if args.out else config.KNOWLEDGE_DIR
        print(f"\n  repairing scratch shards in {out}\n")
        repair_shards(out)
        return 0

    wikis = list(args.wikis)
    if args.all:
        wikis += load_wiki_list(Path(__file__).parent / "seeds" / "fandom_wikis.txt")
    if args.file:
        wikis += load_wiki_list(Path(args.file).expanduser())
    seen = set()
    wikis = [w for w in wikis if not (w in seen or seen.add(w))]
    if not wikis:
        ap.error("give some wikis, or --all, or --file")

    if args.discover:
        print(f"\n  probing {len(wikis)} wikis…\n")
        print(f"  {'wiki':28}{'articles':>10}")
        tot, dead = 0, []
        for w in wikis:
            sub = w.split(":")[0]
            st = site_stats(sub)
            time.sleep(PAUSE)
            if not st or st.get("error") or not st.get("articles"):
                dead.append(f"{sub} ({(st or {}).get('error', 'no articles')})")
                continue
            tot += st["articles"]
            print(f"  {sub:28}{st['articles']:>10,}")
        print(f"  {'TOTAL':28}{tot:>10,} articles")
        if dead:
            print(f"\n  {len(dead)} unreachable (renamed or wrong subdomain):")
            for d in dead:
                print(f"    {d}")
        p_est = tot * 2                      # ~2 chunks per article
        hrs = tot * (PAUSE + 0.25) / WORKERS / 3600
        print(f"\n  lead-only estimate: {p_est:,} passages, ~{p_est*2600/1e9:.1f} GB, "
              f"~{hrs:.1f} h of requests at {WORKERS} workers")
        return 0

    import signal

    def _term(signum, frame):                                     # noqa: ANN001
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)

    emb = _BuildEmbedder(gpu=not args.cpu)
    print(f"  embedding on {emb.device} via {emb.backend}")
    if emb.device == "cpu":
        print("  (CPU is ~18x slower, use the reedus-train env for CUDA)")

    out = Path(args.out).expanduser() if args.out else config.KNOWLEDGE_DIR
    done_log = out / ".fandom_done"
    done = set(done_log.read_text().split()) if done_log.exists() else set()
    writer = BulkWriter(out)
    total, n_wikis = 0, 0
    interrupted = False
    try:
        for i, w in enumerate(wikis, 1):
            sub, _, hint = w.partition(":")
            if sub in done and not args.redo:
                print(f"\n[{i}/{len(wikis)}] {sub}, already ingested, skipping (--redo to force)")
                continue
            print(f"\n[{i}/{len(wikis)}]", end="")
            n = build_wiki(sub, out, emb, writer, intro=not args.full,
                           limit=args.limit, batch=args.batch, domain_hint=hint or None)
            total += n
            if n > 0:
                n_wikis += 1
                out.mkdir(parents=True, exist_ok=True)
                with done_log.open("a") as f:
                    f.write(sub + "\n")
    except KeyboardInterrupt:
        interrupted = True
        print("\n\n  interrupted, finalising what's already ingested, do NOT kill again…",
              flush=True)
    finally:
        per = writer.finalize()
        print(f"\n=== {total:,} passages added across {n_wikis} wiki(s)"
              f"{' (INTERRUPTED)' if interrupted else ''} ===")
        for d, n in sorted(per.items(), key=lambda kv: -kv[1]):
            print(f"  {d:18}{n:>12,}")
        if interrupted:
            print("\n  Everything above is finalised and queryable. Re-run the same command to")
            print("  continue, the resume log skips the wikis already done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
