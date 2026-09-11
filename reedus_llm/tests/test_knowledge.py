#!/usr/bin/env python3
"""knowledge tests. no network, no embedding model, no LLM."""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                                    # noqa: E402
from reedus_companion.knowledge import domains                        # noqa: E402
from reedus_companion.knowledge.store import KnowledgeStore, Shard, DIM   # noqa: E402
from reedus_companion.knowledge.ingest import Article, chunk          # noqa: E402
from reedus_companion.knowledge.retrieve import Knowledge             # noqa: E402

_fails: list[str] = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


def fake_embed(texts):
    """Deterministic pseudo-embeddings: same text, same vector; shared words land nearby."""
    out = []
    for t in texts:
        v = np.zeros(DIM, dtype=np.float32)
        for w in t.lower().split():
            h = int(hashlib.md5(w.encode()).hexdigest()[:8], 16)
            v[h % DIM] += 1.0
        out.append(v.tolist())
    return out


root = Path(tempfile.mkdtemp()) / "knowledge"

# --- taxonomy ---
print("[1/6] domain taxonomy")
domains.validate()
check(len(domains.names()) >= 8, f"{len(domains.names())} domains defined")
check(domains.FALLBACK in domains.names(), "fallback domain exists")

# --- chunking ---
print("\n[2/6] chunking respects section boundaries")
art = Article(
    title="Test Game",
    text=("Test Game is a fighting game released in 2016. " * 12 +
          "\n== Gameplay ==\n" + "Combat uses a three character team with assists. " * 14 +
          "\n== Development ==\n" + "Development began in 2013 at a small studio. " * 14 +
          "\n== References ==\n" + "[1] some citation. " * 30),
    url="https://example.com/wiki/Test_Game", source="test")
rows = chunk(art)
check(len(rows) >= 3, f"{len(rows)} chunks produced")
heads = {r["title"] for r in rows}
check(any("Gameplay" in h for h in heads), "a Gameplay chunk exists")
check(not any("References" in h for h in heads), "References section is skipped")
check(all(r["topic"] == "Test Game" for r in rows), "every chunk carries the topic")
mixed = [r for r in rows if "assists" in r["text"] and "studio" in r["text"]]
check(not mixed, "no chunk spans two sections")

# --- store ---
print("\n[3/6] store: row alignment and search")
store = KnowledgeStore(root, max_hot=2)
vecs = fake_embed([r["text"] for r in rows])
n = store.shard("games").append(vecs, rows)
check(n == len(rows), f"appended {n} passages")

s = Shard(root, "games").load()
check(s.vecs.shape[0] == len(s), "vectors and passages stay row-aligned")
check(s.vecs.shape[1] == DIM, f"vector dim is {DIM}")
check(s.row(0) == rows[0], "row() reads passage 0 back byte-for-byte through the offset index")
check(s.row(len(s) - 1) == rows[len(s) - 1], "row() reads the last passage")

hits = store.search(fake_embed(["combat team assists"])[0], ["games"], k=3, min_score=0.0)
check(bool(hits), "search returns hits")
check("assists" in hits[0].text, f"top hit is the gameplay passage: {hits[0].title[:40]!r}")

# appending twice must not corrupt alignment
store.shard("games").append(fake_embed(["extra passage about something else"]),
                            [{"topic": "Other", "title": "Other", "text": "extra passage",
                              "url": "", "added": "2026-08-04"}])
s2 = Shard(root, "games").load()
check(s2.vecs.shape[0] == len(s2) == len(rows) + 1,
      f"still aligned after a second append ({len(s2)} rows)")
check(s2.row(len(s2) - 1)["topic"] == "Other",
      "the offset index tracked the append instead of going stale")
check(store.has_topic("Test Game") and not store.has_topic("Nonexistent Game"),
      "has_topic answers from the topic index")

# --- LRU ---
print("\n[4/6] LRU residency")
st = KnowledgeStore(root, max_hot=2)
for d in ("games", "music", "tech"):
    st.shard(d)
check(len(st._hot) == 2, f"at most max_hot resident (got {len(st._hot)})")
check("games" not in st._hot, "least-recently-used shard was evicted")
check(st._shards["games"].vecs is None, "evicted shard released its mmap")

# --- routing ---
print("\n[5/6] routing")
kb = Knowledge(embed=fake_embed, root=root, max_hot=2, min_score=0.05)
cases = [("whats a good roguelike on steam", "games"),
         ("been watching that anime with the arc everyone likes", "anime_manga"),
         ("who won the premier league fixture", "sport"),
         ("best beginner bass guitar", "music"),
         ("should i learn rust or python", "tech")]
for q, want in cases:
    got = kb.route(q)
    check(want in got, f"{q[:38]!r} -> {got}")
check(kb.route("mmm hello there friend") == [domains.FALLBACK],
      "no signal -> fallback domain (no LLM configured)")

# --- injection ---
print("\n[6/6] retrieval into a prompt block")
ctx = kb.context_for("combat team assists")
check(bool(ctx), "context_for returns a block")
check("assists" in ctx, "the retrieved passage is the relevant one")
check("binding" in ctx, "block states the attribution rule (labels are binding)")
check(len(ctx) < 2000, f"block is budgeted ({len(ctx)} chars)")
check(len(kb.context_for("completely unrelated aardvark taxonomy")) <= len(ctx),
      "irrelevant query retrieves less or nothing")

check(kb.route("hows the combat feel") == [domains.FALLBACK],
      "a bare follow-up routes to fallback (no keywords)")
check(bool(kb.recall("combat team assists")),
      "...and widen-on-miss still finds it in the games shard")

# --- bulk shards must not be masked ---
print("\n[7/7] bulk sub-shards coexist with the conversational shard")
from reedus_companion.knowledge.store import BulkWriter, shard_names     # noqa: E402

bulk_rows = [{"topic": "Bulk Topic", "title": "Bulk Topic", "text": f"bulk passage {i} zorblatt",
              "url": "", "added": "2026-08-06"} for i in range(5)]
w = BulkWriter(root)
w.add("games", fake_embed([r["text"] for r in bulk_rows]), bulk_rows)
w.finalize()

names = shard_names(root, "games")
check("games" in names and "games.00" in names,
      f"both the conversational and bulk shards are searchable: {names}")
found = store.search(fake_embed(["bulk passage zorblatt"])[0], ["games"], k=5, min_score=0.0)
check(any("zorblatt" in h.text for h in found),
      "a passage that exists ONLY in the bulk sub-shard is reachable")
found = store.search(fake_embed(["combat team assists"])[0], ["games"], k=5, min_score=0.0)
check(any("assists" in h.text for h in found),
      "...and the conversational shard is still reachable too")

before = (root / "games.00.jsonl").read_text()
w2 = BulkWriter(root)
r2 = [{"topic": "Second Build", "title": "Second Build", "text": "fandom passage quixzle",
       "url": "", "added": "2026-08-06"}]
w2.add("games", fake_embed([r["text"] for r in r2]), r2)
w2.finalize()
check((root / "games.00.jsonl").read_text() == before,
      "a second bulk build leaves the existing sub-shard byte-identical")
check((root / "games.01.jsonl").exists(),
      "...and lands in the next free sub-shard number")
found = store.search(fake_embed(["fandom passage quixzle"])[0], ["games"], k=8, min_score=0.0)
check(any("quixzle" in h.text for h in found), "...whose passages are searchable")

import os                                                             # noqa: E402
os.environ["REEDUS_KNOWLEDGE"] = "0"
check(kb.recall("combat team assists") == [], "REEDUS_KNOWLEDGE=0 disables retrieval")
check(kb.learn("Anything") == 0, "REEDUS_KNOWLEDGE=0 disables ingest")
os.environ.pop("REEDUS_KNOWLEDGE")

shutil.rmtree(root.parent, ignore_errors=True)
print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print("   -", f)
    sys.exit(1)
print("ALL PASS")
