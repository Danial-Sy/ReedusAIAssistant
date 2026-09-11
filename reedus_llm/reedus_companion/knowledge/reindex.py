"""build the row/topic/term indexes for a corpus that predates them. runs at serve startup, free when nothing changed."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

from .rerank import DF_FILE
from .rerank import terms as rerank_terms
from .store import (Shard, TOPICS_FILE, _index_path, _topic_hash, build_row_index,
                    save_topic_index)

DF_SAMPLE = 20          # tokenise one passage in this many
DF_MIN_COUNT = 3        # drop terms seen fewer times than this


def vector_shards(root: Path):
    """The real vector shards, not the int8 sidecars beside them."""
    return [p for p in root.glob("*.npy")
            if not (p.name.endswith(".i8.npy") or p.name.endswith(".scale.npy"))]


def rebuild_centroids(root: Path) -> int:
    """Recompute every shard's centroid from its .npy."""
    from .store import _load_centroids, _save_centroids, DIM
    root = Path(root)
    stems = sorted(p.stem for p in vector_shards(root))
    if not stems:
        return 0
    data = _load_centroids(root)
    stale = [k for k in data if k not in stems]
    for k in stale:
        del data[k]
    if stale:
        print(f"  dropped {len(stale)} centroid(s) for shards no longer on disk: "
              f"{', '.join(stale)}")

    done = 0
    for st in stems:
        arr = np.load(root / f"{st}.npy", mmap_mode="r")
        if arr.ndim != 2 or arr.shape[0] == 0:
            continue
        # chunked so a 600 MB shard doesn't need to be resident
        acc = np.zeros(arr.shape[1], dtype=np.float64)
        STEP = 200_000
        for i in range(0, arr.shape[0], STEP):
            acc += np.asarray(arr[i:i + STEP], dtype=np.float64).sum(axis=0)
        c = (acc / arr.shape[0]).astype(np.float32)
        nrm = float(np.linalg.norm(c)) or 1.0
        data[st] = {"n": int(arr.shape[0]), "c": (c / nrm).tolist()}
        done += 1
        print(f"  centroid {st:<20}{arr.shape[0]:>10,} vectors")
        del arr
    _save_centroids(root, data)
    print(f"  {done} centroid(s) written")
    return done


def _row_index_fresh(txt_path: Path) -> bool:
    idx = _index_path(txt_path)
    if not idx.exists():
        return False
    try:
        arr = np.memmap(idx, dtype=np.uint64, mode="r")
        return arr.shape[0] >= 2 and int(arr[-1]) == txt_path.stat().st_size
    except Exception:
        return False


DF_REBUILD_GROWTH = 0.25


def _corpus_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.glob("*.jsonl"))


def _df_is_stale(root: Path) -> bool:
    """True if the term table describes a meaningfully smaller corpus than the one on disk."""
    p = root / DF_FILE
    if not p.exists():
        return True
    try:
        blob = json.loads(p.read_text())
    except Exception:
        return True
    built_at = int(blob.get("corpus_bytes") or 0)
    if not built_at:
        return True
    now = _corpus_bytes(root)
    return now > built_at * (1.0 + DF_REBUILD_GROWTH)


def ensure_indexes(root: Path) -> None:
    """Bring the indexes up to date at serve startup. Free when nothing changed."""
    root = Path(root)
    shards = sorted(root.glob("*.jsonl"))
    if not shards:
        return
    stale = [p for p in shards if not _row_index_fresh(p)]
    tables_bad = not (root / TOPICS_FILE).exists() or _df_is_stale(root)
    from .store import _load_centroids
    have = set(_load_centroids(root))
    missing_cent = [p.stem for p in vector_shards(root) if p.stem not in have]
    if not stale and not tables_bad and not missing_cent:
        return
    if missing_cent and not tables_bad:
        print(f"[knowledge] {len(missing_cent)} shard(s) have no centroid, rebuilding "
              f"(queries are scanning every shard until this finishes)")
        rebuild_centroids(root)
    if tables_bad:
        print(f"[knowledge] term table is missing or predates a corpus that has since grown "
              f"- full reindex of {_corpus_bytes(root) / 1e9:.1f} GB, this takes a few minutes…")
        reindex(root, topics=True)
        return
    print(f"[knowledge] {len(stale)} shard(s) grew since last run, refreshing row indexes…")
    for p in stale:
        build_row_index(p)


def reindex(root: Path, force: bool = False, topics: bool = True) -> None:
    shards = sorted(p for p in root.glob("*.jsonl"))
    if not shards:
        print(f"no shards under {root}", file=sys.stderr)
        return

    total_bytes = sum(p.stat().st_size for p in shards)
    print(f"{len(shards)} shards, {total_bytes / 1e9:.1f} GB")

    seen: set = set()
    df: Counter = Counter()
    df_docs = 0
    done_bytes = 0
    t0 = time.time()
    for i, txt in enumerate(shards, 1):
        size = txt.stat().st_size
        idx = _index_path(txt)
        fresh = (not force and idx.exists()
                 and int(np.memmap(idx, dtype=np.uint64, mode="r")[-1]) == size)
        t = time.time()
        n = (int(np.memmap(idx, dtype=np.uint64, mode="r").shape[0]) - 1 if fresh
             else int(build_row_index(txt).shape[0]) - 1)
        if topics:
            for j, row in enumerate(Shard(root, txt.stem).iter_rows()):
                tp = (row.get("topic") or "").strip().lower()
                if tp:
                    seen.add(_topic_hash(tp))
                if j % DF_SAMPLE == 0:
                    df_docs += 1
                    df.update(rerank_terms(row.get("text", "")))
        done_bytes += size
        rate = done_bytes / max(time.time() - t0, 1e-6) / 1e6
        eta = (total_bytes - done_bytes) / 1e6 / max(rate, 1e-6)
        print(f"  [{i:>2}/{len(shards)}] {txt.stem:<18} {n:>9,} rows  "
              f"{'cached' if fresh else f'{time.time() - t:5.1f}s'}  "
              f"{rate:.0f} MB/s  eta {eta / 60:.1f} min")

    if topics and seen:
        save_topic_index(root, np.fromiter(seen, dtype=np.uint64, count=len(seen)))
        print(f"topic index: {len(seen):,} distinct topics -> "
              f"{(root / TOPICS_FILE).stat().st_size / 1e6:.1f} MB")
    print("\ncentroids…")
    rebuild_centroids(root)

    if topics and df_docs:
        kept = {t: c for t, c in df.items() if c >= DF_MIN_COUNT}
        (root / DF_FILE).write_text(json.dumps(
            {"n": df_docs, "df": kept, "corpus_bytes": _corpus_bytes(root)}))
        print(f"term index: {len(kept):,} terms from {df_docs:,} sampled passages -> "
              f"{(root / DF_FILE).stat().st_size / 1e6:.1f} MB")
    print(f"done in {(time.time() - t0) / 60:.1f} min")


def main() -> None:
    from reedus_companion import config
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path,
                    default=getattr(config, "KNOWLEDGE_DIR", config.MEMORY_DIR / "knowledge"))
    ap.add_argument("--force", action="store_true", help="rebuild indexes that look current")
    ap.add_argument("--no-topics", action="store_true",
                    help="row indexes only; skips the pass that reads passage text")
    a = ap.parse_args()
    reindex(a.root, force=a.force, topics=not a.no_topics)


if __name__ == "__main__":
    main()
