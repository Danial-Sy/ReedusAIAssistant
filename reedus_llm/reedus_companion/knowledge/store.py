"""sharded passage store with LRU residency. everything is mmapped, nothing is parsed on load."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import domains

DIM = 384                    # bge-small-en-v1.5

# a domain shard splits past this. one shard per domain would be ~2.5 GB, too big to stay in
# page cache on 16 GB, and page cache is what decides query latency. sub-shards scan in ~50 ms.
MAX_SHARD_PASSAGES = int(os.environ.get("REEDUS_SHARD_MAX", "400000"))


# sub-shards a query scans, picked by centroid proximity. without this search read every
# sub-shard of a domain: 9.6 GB for games alone, an 8.7s query.
PROBE_SHARDS = int(os.environ.get("REEDUS_PROBE_SHARDS", "3"))
CENTROIDS = "_centroids.json"

# byte offset of every .jsonl row as uint64 plus an end sentinel, so row i is offsets[i]:offsets[i+1].
# without it a shard load parsed 400k lines of json to return six passages (2.5s vs 0.13s of math).
IDX_SUFFIX = ".idx"

# sorted uint64 hashes of every topic, so has_topic() is a binary search instead of parsing the
# whole 28 GB corpus. hashes not strings because 3M names is ~300 MB resident.
TOPICS_FILE = "_topics.u64"


def _load_centroids(root: Path) -> Dict[str, dict]:
    p = Path(root) / CENTROIDS
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save_centroids(root: Path, data: Dict[str, dict]) -> None:
    """Write atomically. It already went wrong once."""
    import os
    import tempfile
    root = Path(root)
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".centroids-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, root / CENTROIDS)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def update_centroid(root: Path, stem: str, vecs: np.ndarray) -> None:
    """Fold new vectors into a shard's centroid, incrementally and exactly."""
    data = _load_centroids(root)
    v = np.asarray(vecs, dtype=np.float32)
    if v.ndim == 1:
        v = v[None, :]
    cur = data.get(stem)
    if cur:
        n0 = int(cur["n"])
        c0 = np.asarray(cur["c"], dtype=np.float32)
        c = (c0 * n0 + v.sum(axis=0)) / (n0 + v.shape[0])
        n = n0 + v.shape[0]
    else:
        c, n = v.mean(axis=0), v.shape[0]
    nrm = float(np.linalg.norm(c)) or 1.0
    data[stem] = {"n": int(n), "c": (c / nrm).astype(np.float32).tolist()}
    _save_centroids(root, data)


def pick_shards(root: Path, stems: List[str], q: np.ndarray,
                probe: int = PROBE_SHARDS) -> List[str]:
    """The `probe` shards whose centroid is nearest the query."""
    if len(stems) <= probe:
        return stems
    data = _load_centroids(root)
    scored, unknown = [], []
    for st in stems:
        c = data.get(st)
        if not c:
            unknown.append(st)
            continue
        scored.append((float(np.dot(np.asarray(c["c"], dtype=np.float32), q)), st))
    if not scored:
        return stems
    scored.sort(reverse=True)
    return [st for _, st in scored[:probe]] + unknown[:probe]


def _index_path(txt_path: Path) -> Path:
    return txt_path.with_suffix(txt_path.suffix + IDX_SUFFIX)


def build_row_index(txt_path: Path) -> np.ndarray:
    """Scan a .jsonl once and record where every row starts. Written next to it as <name>.idx."""
    offsets = [0]
    pos = 0
    with txt_path.open("rb") as f:
        while True:
            block = f.read(8 << 20)
            if not block:
                break
            start = 0
            while True:
                nl = block.find(b"\n", start)
                if nl < 0:
                    break
                offsets.append(pos + nl + 1)
                start = nl + 1
            pos += len(block)
    if offsets[-1] != pos:
        offsets.append(pos)
    arr = np.asarray(offsets, dtype=np.uint64)
    tmp = _index_path(txt_path).with_suffix(IDX_SUFFIX + ".tmp")
    arr.tofile(tmp)
    tmp.replace(_index_path(txt_path))
    return arr


def load_row_index(txt_path: Path, rebuild_if_stale: bool = True) -> np.ndarray:
    """The row index, built on demand. A stale index shows as EOF sentinel != file size."""
    idx = _index_path(txt_path)
    size = txt_path.stat().st_size
    if idx.exists():
        try:
            arr = np.memmap(idx, dtype=np.uint64, mode="r")
            if arr.shape[0] >= 2 and int(arr[-1]) == size:
                return arr
        except Exception:
            pass
        if not rebuild_if_stale:
            raise ValueError(f"{txt_path.name}: row index is stale and rebuild was disabled")
    return build_row_index(txt_path)


def _topic_hash(topic: str) -> np.uint64:
    import hashlib
    h = hashlib.blake2b(topic.strip().lower().encode("utf-8"), digest_size=8).digest()
    return np.frombuffer(h, dtype=np.uint64)[0]


def save_topic_index(root: Path, hashes: np.ndarray) -> None:
    arr = np.unique(np.asarray(hashes, dtype=np.uint64))
    tmp = Path(root) / (TOPICS_FILE + ".tmp")
    arr.tofile(tmp)
    tmp.replace(Path(root) / TOPICS_FILE)


def add_topics(root: Path, topics: List[str]) -> None:
    """Fold newly ingested topic names into the index. Re-sorts, fine at ingest time."""
    if not topics:
        return
    p = Path(root) / TOPICS_FILE
    new = np.asarray([_topic_hash(t) for t in topics], dtype=np.uint64)
    cur = np.fromfile(p, dtype=np.uint64) if p.exists() else np.empty(0, dtype=np.uint64)
    save_topic_index(root, np.concatenate([cur, new]))


def rank_domains(root: Path, doms: List[str], q: np.ndarray, top: int = 2) -> List[str]:
    """The `top` domains whose best sub-shard centroid is nearest the query."""
    data = _load_centroids(root)
    scored = []
    for d in doms:
        best = max((float(np.dot(np.asarray(data[st]["c"], dtype=np.float32), q))
                    for st in shard_names(root, d) if st in data), default=None)
        if best is not None:
            scored.append((best, d))
    if not scored:
        return doms[:top]
    scored.sort(reverse=True)
    return [d for _, d in scored[:top]]


def shard_names(root: Path, domain: str) -> List[str]:
    """All shard file stems for a domain: 'games.00', 'games.01', ... plus 'games' if present."""
    base = Path(root)
    subs = sorted(p.stem for p in base.glob(f"{domain}.[0-9][0-9].npy"))
    if (base / f"{domain}.npy").exists():
        return [domain] + subs
    return subs or [domain]


@dataclass
class Passage:
    topic: str
    title: str
    text: str
    url: str
    score: float = 0.0          # cosine against the query
    anchor: float = 0.0
    rank_score: float = 0.0     # the combination the final ordering uses

    def cite(self) -> str:
        return f"{self.title}" + (f" ({self.url})" if self.url else "")


class Shard:
    """One domain's vectors + passages, both mmapped."""

    def __init__(self, root: Path, domain: str):
        self.domain = domain
        self.vec_path = root / f"{domain}.npy"
        self.txt_path = root / f"{domain}.jsonl"
        self.vecs: Optional[np.ndarray] = None
        self.scales: Optional[np.ndarray] = None   # set only for int8 shards
        self.offsets: Optional[np.ndarray] = None
        self._fh = None
        self.loaded_at = 0.0

    def exists(self) -> bool:
        return self.vec_path.exists() and self.txt_path.exists()

    def load(self) -> "Shard":
        if self.vecs is not None:
            return self
        if not self.exists():
            self.vecs = np.zeros((0, DIM), dtype=np.float32)
            self.offsets = np.zeros(1, dtype=np.uint64)
            return self
        # mmap_mode='r': nothing read until indexed, an unused shard costs no RAM.
        # int8 shards (<domain>.i8.npy + .scale.npy) are used when present: the float32 corpus is
        # 19 GB and can't stay in page cache on 16 GB, int8 is 4.75 GB and does. rollback = delete .i8.npy.
        i8_path = self.vec_path.with_suffix(".i8.npy")
        sc_path = self.vec_path.with_suffix(".scale.npy")
        if i8_path.exists() and sc_path.exists():
            self.vecs = np.load(i8_path, mmap_mode="r")
            self.scales = np.load(sc_path, mmap_mode="r")
        else:
            self.vecs = np.load(self.vec_path, mmap_mode="r")
            self.scales = None
        self.offsets = load_row_index(self.txt_path)
        n_rows = self.offsets.shape[0] - 1
        if n_rows != self.vecs.shape[0]:
            raise ValueError(
                f"{self.domain}: {self.vecs.shape[0]} vectors vs {n_rows} passages, "
                f"the row-alignment invariant is broken. Rebuild this shard.")
        if self.vecs.shape[0] and self.vecs.shape[1] != DIM:
            raise ValueError(
                f"{self.domain}: shard is {self.vecs.shape[1]}-dim but the embedder produces "
                f"{DIM}-dim. This corpus was built with a DIFFERENT embedder, almost certainly "
                f"fastembed was unavailable and _Embedder fell back to its lexical hash. "
                f"Install fastembed, then delete and rebuild: rm -rf {self.vec_path.parent}")
        self.loaded_at = time.time()
        return self

    def unload(self) -> None:
        self.vecs = None
        self.scales = None
        self.offsets = None
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __len__(self) -> int:
        return int(self.offsets.shape[0] - 1) if self.offsets is not None else 0

    def row(self, i: int) -> dict:
        """Passage i straight off disk, one seek and one short read."""
        if self.offsets is None:
            self.load()
        if self._fh is None:
            self._fh = self.txt_path.open("rb")
        start, end = int(self.offsets[i]), int(self.offsets[i + 1])
        self._fh.seek(start)
        raw = self._fh.read(end - start)
        return json.loads(raw)

    # chunked on purpose: int8_array @ float32_vector upcasts the WHOLE array first (2.4 GB a shard)
    _I8_CHUNK = 16384

    def _sims_int8(self, q: np.ndarray) -> np.ndarray:
        """Cosine against int8-quantised vectors, using the per-row scale."""
        n = self.vecs.shape[0]
        sims = np.empty(n, dtype=np.float32)
        for i in range(0, n, self._I8_CHUNK):
            j = min(i + self._I8_CHUNK, n)
            blk = np.asarray(self.vecs[i:j], dtype=np.float32)
            np.multiply(blk @ q, self.scales[i:j], out=sims[i:j])
        return sims

    def search(self, q: np.ndarray, k: int) -> List[Passage]:
        self.load()
        if self.vecs is None or self.vecs.shape[0] == 0:
            return []
        # vectors are stored L2-normalised so cosine is a dot product
        if self.scales is None:
            sims = np.asarray(self.vecs @ q, dtype=np.float32)
        else:
            sims = self._sims_int8(q)
        k = min(k, sims.shape[0])
        idx = np.argpartition(-sims, k - 1)[:k] if k < sims.shape[0] else np.arange(sims.shape[0])
        idx = idx[np.argsort(-sims[idx])]
        out = []
        for i in idx:
            r = self.row(int(i))
            out.append(Passage(topic=r.get("topic", ""), title=r.get("title", ""),
                               text=r.get("text", ""), url=r.get("url", ""),
                               score=float(sims[int(i)])))
        return out

    def iter_rows(self):
        """Every passage, streamed. Index building only, never on a chat turn."""
        with self.txt_path.open("rb") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def topics(self) -> set:
        return {r.get("topic", "") for r in self.iter_rows()}

    def append(self, vecs: np.ndarray, rows: List[dict]) -> int:
        """Append passages. Rewrites the shard, fine at ingest time, never on a chat turn."""
        if not rows:
            return 0
        vecs = _normalise(np.asarray(vecs, dtype=np.float32))
        self.load()
        old = self.vecs if self.vecs is not None and self.vecs.shape[0] else None
        merged = np.concatenate([np.asarray(old), vecs]) if old is not None else vecs
        self.vec_path.parent.mkdir(parents=True, exist_ok=True)
        self.unload()                                  # release the mmap first
        np.save(self.vec_path, merged.astype(np.float32))
        with self.txt_path.open("a") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.unload()
        build_row_index(self.txt_path)
        add_topics(self.vec_path.parent, [r.get("topic", "") for r in rows if r.get("topic")])
        update_centroid(self.vec_path.parent, self.domain, vecs)
        return len(rows)


class BulkWriter:
    """Streaming O(n) shard writer for bulk ingest."""

    def __init__(self, root: Path, max_per_shard: int = MAX_SHARD_PASSAGES):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_per_shard = max_per_shard
        self._idx: Dict[str, int] = {}          # domain -> current sub-shard number
        self._count: Dict[str, int] = {}        # current sub-shard's row count
        self._total: Dict[str, int] = {}
        self._vec_fh: Dict[str, object] = {}
        self._txt_fh: Dict[str, object] = {}

    def _first_free_index(self, domain: str) -> int:
        """The lowest sub-shard number not already used on disk."""
        return self._next_free(domain, 0)

    def _next_free(self, domain: str, start: int) -> int:
        taken = set()
        for p in self.root.glob(f"{domain}.[0-9][0-9].*"):
            m = re.match(rf"^{re.escape(domain)}\.(\d\d)\.", p.name)
            if m:
                taken.add(int(m.group(1)))
        n = start
        while n in taken:
            n += 1
        return n

    def _stem(self, domain: str) -> str:
        if domain not in self._idx:
            self._idx[domain] = self._first_free_index(domain)
        return f"{domain}.{self._idx[domain]:02d}"

    def _open(self, domain: str):
        stem = self._stem(domain)
        if stem not in self._vec_fh:
            self._vec_fh[stem] = open(self.root / f"{stem}.f32", "wb")
            self._txt_fh[stem] = open(self.root / f"{stem}.jsonl", "w")
        return self._vec_fh[stem], self._txt_fh[stem]

    def _roll(self, domain: str):
        stem = self._stem(domain)
        for d in (self._vec_fh, self._txt_fh):
            if stem in d:
                d[stem].close()
                del d[stem]
        self._idx[domain] = self._next_free(domain, self._idx.get(domain, 0) + 1)
        self._count[domain] = 0

    def add(self, domain: str, vecs, rows: List[dict]) -> int:
        if not rows:
            return 0
        v = _normalise(np.asarray(vecs, dtype=np.float32))
        vf, tf = self._open(domain)
        vf.write(v.tobytes())
        for r in rows:
            tf.write(json.dumps(r, ensure_ascii=False) + "\n")
        self._count[domain] = self._count.get(domain, 0) + len(rows)
        self._total[domain] = self._total.get(domain, 0) + len(rows)
        if self._count[domain] >= self.max_per_shard:
            self._roll(domain)
        return len(rows)

    def finalize(self) -> Dict[str, int]:
        """Close scratch files and convert each .f32 to a proper .npy."""
        for d in (self._vec_fh, self._txt_fh):
            for fh in d.values():
                fh.close()
            d.clear()
        cents: Dict[str, dict] = {}
        for raw in sorted(self.root.glob("*.f32")):
            stem = raw.stem
            n_bytes = raw.stat().st_size
            if n_bytes == 0:
                raw.unlink()
                (self.root / f"{stem}.jsonl").unlink(missing_ok=True)
                continue
            arr = np.fromfile(raw, dtype=np.float32).reshape(-1, DIM)
            np.save(self.root / f"{stem}.npy", arr)
            c = arr.mean(axis=0)
            nrm = float(np.linalg.norm(c)) or 1.0
            cents[stem] = {"n": int(arr.shape[0]),
                           "c": (c / nrm).astype(np.float32).tolist()}
            del arr
            raw.unlink(missing_ok=True)
            build_row_index(self.root / f"{stem}.jsonl")
        if cents:
            existing = _load_centroids(self.root)
            existing.update(cents)
            _save_centroids(self.root, existing)
        self._rebuild_topic_index()
        return dict(self._total)

    def _rebuild_topic_index(self) -> None:
        seen = set()
        for txt in sorted(self.root.glob("*.jsonl")):
            for row in Shard(self.root, txt.stem).iter_rows():
                t = (row.get("topic") or "").strip().lower()
                if t:
                    seen.add(_topic_hash(t))
        if seen:
            save_topic_index(self.root, np.fromiter(seen, dtype=np.uint64, count=len(seen)))


def _normalise(v: np.ndarray) -> np.ndarray:
    if v.ndim == 1:
        v = v[None, :]
    n = np.linalg.norm(v, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (v / n).astype(np.float32)


class KnowledgeStore:
    """All shards with LRU residency. Thread-safe, the chat UI serves /status concurrently."""

    def __init__(self, root: Path, max_hot: int = 2):
        self.root = Path(root)
        self.max_hot = max(1, max_hot)
        self._shards: Dict[str, Shard] = {}
        self._hot: "OrderedDict[str, Shard]" = OrderedDict()
        self._topics = None                     # None=unread, False=absent, else uint64 array
        self._lock = threading.Lock()

    def shard(self, domain: str) -> Shard:
        with self._lock:
            s = self._shards.get(domain)
            if s is None:
                s = Shard(self.root, domain)
                self._shards[domain] = s
            if domain in self._hot:
                self._hot.move_to_end(domain)
            else:
                self._hot[domain] = s
                while len(self._hot) > self.max_hot:
                    _, evicted = self._hot.popitem(last=False)
                    evicted.unload()
            return s

    def search(self, q_vec, domains_: List[str], k: int = 4,
               min_score: float = 0.30) -> List[Passage]:
        q = _normalise(np.asarray(q_vec, dtype=np.float32))[0]
        hits: List[Passage] = []
        for d in domains_:
            stems = shard_names(self.root, d)
            for name in pick_shards(self.root, stems, q):
                hits += self.shard(name).search(q, k)
        hits = [h for h in hits if h.score >= min_score]
        hits.sort(key=lambda p: -p.score)
        return _dedupe(hits)[:k]

    def has_topic(self, topic: str) -> bool:
        """Do we already hold this topic? One hash and a binary search."""
        t = (topic or "").strip().lower()
        if not t:
            return False
        idx = self._topic_index()
        if idx is not None:
            h = _topic_hash(t)
            pos = int(np.searchsorted(idx, h))
            return pos < idx.shape[0] and idx[pos] == h
        for d in domains.names():
            for name in shard_names(self.root, d):
                s = Shard(self.root, name)
                if s.exists() and any(t == x.strip().lower() for x in s.topics()):
                    return True
        return False

    def _topic_index(self) -> Optional[np.ndarray]:
        if self._topics is None:
            p = self.root / TOPICS_FILE
            self._topics = np.fromfile(p, dtype=np.uint64) if p.exists() else False
        return self._topics if self._topics is not False else None

    def stats(self) -> List[Tuple[str, int, float]]:
        out = []
        for d in domains.names():
            n = mb = 0
            for name in shard_names(self.root, d):
                s = Shard(self.root, name)
                if not s.exists():
                    continue
                n += int(load_row_index(s.txt_path).shape[0] - 1)
                mb += (s.vec_path.stat().st_size + s.txt_path.stat().st_size) / 1e6
            if n:
                out.append((d, n, mb))
        return out


def _dedupe(hits: List[Passage]) -> List[Passage]:
    """Two domains can hold the same passage, a game adaptation of an anime lands in both."""
    seen, out = set(), []
    for h in hits:
        key = (h.title, h.text[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out
