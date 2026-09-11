#!/usr/bin/env python3
"""fine-tune one group on your own photos and export it cpu-ready. gpu box only."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import CFG
from . import taxonomy


def train_group(group: str, data: str, arch: str, base: str, epochs: int,
                imgsz: int, batch: int, device: str, export_int8: bool,
                calib_data: str | None):
    from ultralytics import YOLO, YOLOWorld  # lazy, gpu box only

    out_dir = CFG.models_dir / group
    out_dir.mkdir(parents=True, exist_ok=True)

    if arch == "world":
        weights = base or "yolov8s-worldv2.pt"
        model = YOLOWorld(weights)
        # anchor the head to this group's vocabulary before fine-tuning
        model.set_classes(taxonomy.vocabulary(group))
    else:  # plain detector
        weights = base or "yolov8s.pt"
        model = YOLO(weights)

    print(f"[train] {group} | arch={arch} base={weights} epochs={epochs} "
          f"imgsz={imgsz} batch={batch} device={device}")
    model.train(
        data=data, epochs=epochs, imgsz=imgsz, batch=batch, device=device,
        project=str(out_dir / "runs"), name="finetune", exist_ok=True,
        patience=20, verbose=True,
    )

    # best.pt from the run -> canonical group weights
    best = out_dir / "runs" / "finetune" / "weights" / "best.pt"
    canonical_pt = out_dir / f"{group}.pt"
    if best.exists():
        shutil.copy2(best, canonical_pt)
        print(f"[train] best weights -> {canonical_pt}")
    else:
        print(f"[train] WARNING: {best} not found; check the run output")
        return

    # export CPU-ready for the Xeon
    export_model = YOLO(str(canonical_pt))
    kwargs = dict(format="openvino", imgsz=imgsz, verbose=False)
    if export_int8:
        kwargs["int8"] = True
        kwargs["data"] = calib_data or data  # reuse val set for calibration
    exported = export_model.export(**kwargs)
    print(f"[train] exported CPU model -> {exported}")
    print(f"[train] rsync vision/models/{group}/ to the Xeon box and you're live.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fine-tune + export one intent group")
    ap.add_argument("--group", required=True)
    ap.add_argument("--data", required=True, help="YOLO data.yaml")
    ap.add_argument("--arch", choices=["world", "yolov8"], default="world")
    ap.add_argument("--base", default=None, help="starting weights override")
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--imgsz", type=int, default=CFG.imgsz)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", default="0", help="'0' for the 3090 Ti, 'cpu' otherwise")
    ap.add_argument("--no-int8", action="store_true", help="export FP32 OpenVINO instead")
    ap.add_argument("--calib-data", default=None)
    args = ap.parse_args(argv)

    if args.group not in taxonomy.TAXONOMY:
        raise SystemExit(f"unknown group {args.group!r}")
    train_group(args.group, args.data, args.arch, args.base, args.epochs,
                args.imgsz, args.batch, args.device,
                export_int8=not args.no_int8, calib_data=args.calib_data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
