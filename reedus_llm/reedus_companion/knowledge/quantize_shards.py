#!/usr/bin/env python3
"""quantise the corpus vectors to int8 so the working set fits in the xeon's page cache."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

_HERE = Path(__file__).resolve().parent
KNOWLEDGE_DIR = _HERE.parent / "memory" / "knowledge"

_SHARD_RE = re.compile(r"^(?P<domain>[a-z_]+)\.(?P<n>\d\d)\.npy$")
CHUNK = 65536       # rows at a time, 100 MB peak


def shards(domains: Optional[Sequence[str]] = None) -> List[Path]:
    out = []
    for p in sorted(KNOWLEDGE_DIR.glob("*.npy")):
        m = _SHARD_RE.match(p.name)
        if not m:
            continue
        if domains and m.group("domain") not in domains:
            continue
        out.append(p)
    return out


def quantise(path: Path, force: bool = False) -> Optional[tuple]:
    i8_path = path.with_suffix(".i8.npy")
    sc_path = path.with_suffix(".scale.npy")
    if i8_path.exists() and sc_path.exists() and not force:
        return None

    src = np.load(path, mmap_mode="r")
    n, d = src.shape
    i8_tmp = i8_path.with_suffix(".tmp.npy")
    sc_tmp = sc_path.with_suffix(".tmp.npy")

    out = np.lib.format.open_memmap(i8_tmp, mode="w+", dtype=np.int8, shape=(n, d))
    scales = np.empty(n, dtype=np.float32)
    for i in range(0, n, CHUNK):
        j = min(i + CHUNK, n)
        blk = np.asarray(src[i:j], dtype=np.float32)
        peak = np.abs(blk).max(axis=1)
        peak[peak == 0] = 1.0                       # all-zero rows
        out[i:j] = np.rint(blk * (127.0 / peak[:, None])).astype(np.int8)
        scales[i:j] = peak / 127.0
    out.flush()
    del out
    np.save(sc_tmp, scales)
    i8_tmp.replace(i8_path)
    sc_tmp.replace(sc_path)
    return (path.stat().st_size, i8_path.stat().st_size + sc_path.stat().st_size)


def check(domains: Optional[Sequence[str]], n_queries: int = 20, k: int = 10) -> int:
    """Rank agreement between float32 and int8 on random real query vectors."""
    paths = [p for p in shards(domains) if p.with_suffix(".i8.npy").exists()]
    if not paths:
        print("no int8 shards found, run without --check first")
        return 1
    rng = np.random.default_rng(0)
    overlaps, top1 = [], []
    for p in paths[:6]:
        f32 = np.load(p, mmap_mode="r")
        i8 = np.load(p.with_suffix(".i8.npy"), mmap_mode="r")
        sc = np.load(p.with_suffix(".scale.npy"), mmap_mode="r")
        n = f32.shape[0]
        for _ in range(n_queries):
            q = np.asarray(f32[int(rng.integers(n))], dtype=np.float32)
            lim = min(n, 200_000)
            a = np.asarray(f32[:lim], dtype=np.float32) @ q
            b = (np.asarray(i8[:lim], dtype=np.float32) @ q) * sc[:lim]
            ta = set(np.argsort(-a)[:k].tolist())
            tb = set(np.argsort(-b)[:k].tolist())
            overlaps.append(len(ta & tb) / k)
            top1.append(int(np.argmax(a)) == int(np.argmax(b)))
        print(f"  {p.name:<26} recall@{k}={np.mean(overlaps[-n_queries:]):.3f}")
    print(f"\n  mean recall@{k} : {np.mean(overlaps):.4f}")
    print(f"  top-1 agreement: {np.mean(top1):.4f}")
    if np.mean(overlaps) < 0.95:
        print("\n  BELOW 0.95, do not ship this. Retrieval quality would move, and every "
              "grounding\n  number measured afterwards would be measuring the quantiser "
              "instead of the index.")
        return 1
    print("\n  fine to ship.")
    return 0


def revert(domains: Optional[Sequence[str]]) -> int:
    n = 0
    for p in shards(domains):
        for q in (p.with_suffix(".i8.npy"), p.with_suffix(".scale.npy")):
            if q.exists():
                q.unlink()
                n += 1
    print(f"removed {n} int8 files, float32 shards were never touched")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domains", nargs="*", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args(argv)

    if args.revert:
        return revert(args.domains)
    if args.check:
        return check(args.domains)

    paths = shards(args.domains)
    if not paths:
        raise SystemExit(f"no shards found in {KNOWLEDGE_DIR}")
    before = after = 0
    t0 = time.time()
    for i, p in enumerate(paths, 1):
        r = quantise(p, force=args.force)
        if r is None:
            print(f"  [{i}/{len(paths)}] {p.name}, already done")
            continue
        before += r[0]
        after += r[1]
        print(f"  [{i}/{len(paths)}] {p.name}  {r[0]/1e6:,.0f} MB -> {r[1]/1e6:,.0f} MB",
              flush=True)
    if before:
        print(f"\n{before/1e9:,.1f} GB -> {after/1e9:,.1f} GB "
              f"({after/before:.0%}) in {time.time()-t0:.0f}s")
    print("\nNow verify before trusting it:\n"
          "  python -m reedus_companion.knowledge.quantize_shards --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
