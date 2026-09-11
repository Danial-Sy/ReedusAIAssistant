#!/usr/bin/env python3
"""fetch a household-heavy INT8 calibration set from COCO. run on the build box."""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

from .config import CFG

COCO_IMAGES_URL = "http://images.cocodataset.org/zips/val2017.zip"
COCO_ANNOT_URL = "http://images.cocodataset.org/annotations/annotations_trainval2017.zip"

HOUSEHOLD_CATEGORIES = {
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl",
    "banana", "apple", "sandwich", "orange", "broccoli", "carrot", "pizza",
    "cake", "donut", "chair", "couch", "potted plant", "bed", "dining table",
    "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock",
    "vase", "scissors", "teddy bear", "hair drier", "toothbrush", "person",
    "backpack", "handbag", "tie", "suitcase", "cat", "dog",
}


def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [skip] {dest.name} already downloaded ({dest.stat().st_size/1e6:.0f} MB)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  downloading {url}")

    def _hook(blocks: int, bs: int, total: int) -> None:
        if total > 0:
            pct = min(100, blocks * bs * 100 // total)
            sys.stdout.write(f"\r    {pct:3d}%  ({blocks*bs/1e6:.0f}/{total/1e6:.0f} MB)")
            sys.stdout.flush()

    urllib.request.urlretrieve(url, tmp, reporthook=_hook)
    sys.stdout.write("\n")
    tmp.rename(dest)
    return dest


def _unzip(archive: Path, out: Path, members_prefix: str | None = None) -> None:
    print(f"  extracting {archive.name}…")
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        if members_prefix:
            names = [n for n in names if n.startswith(members_prefix)]
        zf.extractall(out, members=names)


def _rank_household_images(annot_json: Path) -> list[tuple[str, int]]:
    """Return [(file_name, household_object_count)] sorted most-dense first."""
    print("  scoring images by household-object density…")
    data = json.loads(annot_json.read_text())

    keep_ids = {c["id"] for c in data["categories"]
                if c["name"] in HOUSEHOLD_CATEGORIES}
    per_image: dict[int, int] = defaultdict(int)
    for ann in data["annotations"]:
        if ann["category_id"] in keep_ids:
            per_image[ann["image_id"]] += 1

    id_to_name = {img["id"]: img["file_name"] for img in data["images"]}
    ranked = [(id_to_name[i], n) for i, n in per_image.items() if i in id_to_name]
    ranked.sort(key=lambda kv: -kv[1])
    return ranked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=400,
                    help="how many calibration images to keep (default 400)")
    ap.add_argument("--out", default=None,
                    help=f"output dir (default {CFG.calib_dir})")
    ap.add_argument("--no-filter", action="store_true",
                    help="plain random sample instead of household-dense selection")
    ap.add_argument("--keep-archives", action="store_true",
                    help="keep the downloaded zips instead of deleting them")
    ap.add_argument("--work", default=None, help="scratch dir for downloads")
    args = ap.parse_args()

    out = Path(args.out).expanduser() if args.out else CFG.calib_dir
    work = Path(args.work).expanduser() if args.work else out.parent / "_calib_download"
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    existing = list(out.glob("*.jpg"))
    if len(existing) >= args.n:
        print(f"{out} already has {len(existing)} images (>= {args.n}). Nothing to do.")
        print("Delete them or pass --n higher to refresh.")
        return 0

    print("== Reedus INT8 calibration set ==")
    print(f"target: {args.n} images -> {out}\n")

    print("[1/4] images")
    img_zip = _download(COCO_IMAGES_URL, work / "val2017.zip")
    img_dir = work / "val2017"
    if not img_dir.exists():
        _unzip(img_zip, work)

    if args.no_filter:
        pool = sorted(p.name for p in img_dir.glob("*.jpg"))
        random.seed(0)
        chosen = random.sample(pool, min(args.n, len(pool)))
        print(f"\n[2/4] random sample: {len(chosen)} images")
    else:
        print("\n[2/4] annotations")
        ann_zip = _download(COCO_ANNOT_URL, work / "annotations_trainval2017.zip")
        ann_json = work / "annotations" / "instances_val2017.json"
        if not ann_json.exists():
            _unzip(ann_zip, work, members_prefix="annotations/instances_val2017.json")

        print("\n[3/4] selecting")
        ranked = _rank_household_images(ann_json)
        dense_half = ranked[: max(args.n * 4, len(ranked) // 2)]
        stride = max(1, len(dense_half) // args.n)
        chosen = [name for name, _ in dense_half[::stride]][: args.n]
        avg = sum(n for _, n in ranked[: len(chosen)]) / max(1, len(chosen))
        print(f"  {len(chosen)} images, ~{avg:.1f} household objects each")

    print(f"\n[4/4] copying to {out}")
    copied = 0
    for name in chosen:
        src = img_dir / name
        if src.exists():
            shutil.copy2(src, out / name)
            copied += 1
    print(f"  copied {copied}")

    if not args.keep_archives:
        print("\ncleaning up scratch…")
        shutil.rmtree(work, ignore_errors=True)

    print(f"\nDone. {copied} calibration images in {out}")
    print("Next:  python -m vision.build_models --backend openvino --int8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
