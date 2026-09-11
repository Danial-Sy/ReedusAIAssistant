#!/usr/bin/env python3
"""auto-label collected photos with the big teacher model. gpu box only."""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from pathlib import Path

from .config import CFG
from . import taxonomy

IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _write_data_yaml(out: Path, group: str, vocab):
    names = "\n".join(f"  {i}: {n}" for i, n in enumerate(vocab))
    (out / "data.yaml").write_text(
        f"# auto-generated for intent group '{group}'\n"
        f"path: {out.resolve()}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"names:\n{names}\n"
    )


def autolabel_group(group: str, images_dir: Path, out_dir: Path,
                    teacher: str, conf: float, imgsz: int, val_split: float):
    from ultralytics import YOLOWorld  # lazy, gpu box only

    vocab = taxonomy.vocabulary(group)
    imgs = [p for p in images_dir.rglob("*") if p.suffix.lower() in IMG_EXTS]
    if not imgs:
        raise SystemExit(f"no images found under {images_dir}")

    print(f"[autolabel] {group}: {len(vocab)} classes, {len(imgs)} images, "
          f"teacher={teacher}")

    # dirs
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    _write_data_yaml(out_dir, group, vocab)

    model = YOLOWorld(teacher)
    model.set_classes(vocab)

    random.seed(1234)
    kept = 0
    for i, img in enumerate(imgs):
        split = "val" if random.random() < val_split else "train"
        res = model.predict(source=str(img), conf=conf, imgsz=imgsz,
                            max_det=CFG.max_det, verbose=False)[0]

        lines = []
        if res.boxes is not None and len(res.boxes) > 0:
            h, w = res.orig_shape[:2]
            for b in res.boxes:
                cls_id = int(b.cls[0])
                x1, y1, x2, y2 = b.xyxy[0].tolist()
                cx, cy = ((x1 + x2) / 2) / w, ((y1 + y2) / 2) / h
                bw, bh = (x2 - x1) / w, (y2 - y1) / h
                lines.append(f"{cls_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")

        dst_img = out_dir / "images" / split / img.name
        shutil.copy2(img, dst_img)
        (out_dir / "labels" / split / (img.stem + ".txt")).write_text("\n".join(lines))
        kept += 1
        if (i + 1) % 50 == 0:
            print(f"  labeled {i+1}/{len(imgs)}")

    print(f"[autolabel] done -> {out_dir} ({kept} images). "
          f"Review a few, then: python -m vision.train_group --group {group} "
          f"--data {out_dir/'data.yaml'}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Auto-label images for an intent group")
    ap.add_argument("--group", required=True)
    ap.add_argument("--images", required=True, help="folder of collected photos")
    ap.add_argument("--out", required=True, help="output dataset dir")
    ap.add_argument("--teacher", default="yolov8x-worldv2.pt")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--imgsz", type=int, default=CFG.imgsz)
    ap.add_argument("--val-split", type=float, default=0.15)
    args = ap.parse_args(argv)

    if args.group not in taxonomy.TAXONOMY:
        raise SystemExit(f"unknown group {args.group!r}. See --list in build_models.")
    autolabel_group(args.group, Path(args.images), Path(args.out),
                    args.teacher, args.conf, args.imgsz, args.val_split)
    return 0


if __name__ == "__main__":
    sys.exit(main())
