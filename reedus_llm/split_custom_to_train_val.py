#!/usr/bin/env python3
import os, sys, json, math, random, argparse
from datasets import load_from_disk

def main(src, train_out, val_out, val_frac, min_val, min_train, seed):
    ds = load_from_disk(src)
    n = len(ds)
    if n == 0:
        print("Dataset is empty:", src); sys.exit(1)

    # first pass: target val size
    val_n = max(int(math.ceil(val_frac * n)), min_val)

    # guard: don’t starve the train split
    if val_n > n - min_train:
        val_n = max(1, min(n - 1, int(0.2 * n)))  # fallback ~20% val

    ds = ds.shuffle(seed=seed)
    train = ds.select(range(0, n - val_n))
    val   = ds.select(range(n - val_n, n))

    import shutil
    shutil.rmtree(train_out, ignore_errors=True)
    shutil.rmtree(val_out, ignore_errors=True)
    train.save_to_disk(train_out)
    val.save_to_disk(val_out)

    print(f"Split {n} -> train={len(train)}, val={len(val)}")
    print(f"Saved: {train_out} and {val_out}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src", default="data/normalized/custom")
    ap.add_argument("train_out", default="data/blended/custom_train")
    ap.add_argument("val_out", default="data/blended/custom_val")
    ap.add_argument("--val-frac", type=float, default=0.02)
    ap.add_argument("--min-val", type=int, default=50)     # smaller default for small sets
    ap.add_argument("--min-train", type=int, default=100)  # keep train reasonably sized
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()
    main(args.src, args.train_out, args.val_out, args.val_frac, args.min_val, args.min_train, args.seed)
