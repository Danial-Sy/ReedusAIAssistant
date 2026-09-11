#!/usr/bin/env python3
"""compile each group's open-vocab base model to openvino/onnx/pt, no training. run on the gpu box."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .config import CFG
from . import taxonomy


def build_group(group: str, backend: str, int8: bool, data: str | None,
                base_weights: str, imgsz: int) -> Path:
    from ultralytics import YOLOWorld  # lazy, needs torch

    vocab = taxonomy.vocabulary(group)
    out_dir = CFG.models_dir / group
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== {group}  ({len(vocab)} classes) ===")
    model = YOLOWorld(base_weights)
    model.set_classes(vocab)

    pt_path = out_dir / f"{group}.pt"
    model.save(str(pt_path))
    print(f"  saved offline-vocab weights -> {pt_path}")

    if backend == "pt":
        return pt_path

    export_kwargs = dict(imgsz=imgsz, verbose=False)
    if backend == "openvino":
        export_kwargs["format"] = "openvino"
        if int8:
            export_kwargs["int8"] = True
            if data:
                export_kwargs["data"] = data
    elif backend == "onnx":
        export_kwargs["format"] = "onnx"
        export_kwargs["opset"] = 12
        export_kwargs["simplify"] = True
    else:
        raise ValueError(f"unknown backend {backend!r}")

    t0 = time.time()
    exported = model.export(**export_kwargs)
    print(f"  exported {backend}{' int8' if int8 else ''} in "
          f"{time.time()-t0:.1f}s -> {exported}")
    return Path(exported)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build Reedus intent-group detectors")
    ap.add_argument("--groups", nargs="*", help="subset of groups (default: all)")
    ap.add_argument("--backend", default=CFG.backend, choices=["openvino", "onnx", "pt"])
    ap.add_argument("--int8", action="store_true", help="INT8 quantize (OpenVINO)")
    ap.add_argument("--data", default=None, help="data.yaml for INT8 calibration")
    ap.add_argument("--base-weights", default=CFG.base_weights)
    ap.add_argument("--imgsz", type=int, default=CFG.imgsz)
    ap.add_argument("--list", action="store_true", help="list groups + stats and exit")
    args = ap.parse_args(argv)

    if args.list:
        s = taxonomy.stats()
        print(f"groups={s['groups']}  labels={s['total_labels']} "
              f"(unique={s['unique_labels']})  triggers={s['total_triggers']}\n")
        for g in taxonomy.group_names():
            print(f"  {g:32s} {len(taxonomy.vocabulary(g)):3d} classes")
        return 0

    problems = taxonomy.validate()
    if problems:
        print("TAXONOMY PROBLEMS:", *problems, sep="\n  ")
        return 2

    CFG.ensure_dirs()
    groups = args.groups or taxonomy.group_names()
    unknown = [g for g in groups if g not in taxonomy.TAXONOMY]
    if unknown:
        print("unknown groups:", unknown)
        return 2

    if args.int8 and not args.data:
        calib = CFG.calib_dir
        if not any(calib.glob("*")):
            print(f"WARNING: --int8 set but no calibration images in {calib}. "
                  f"Ultralytics will fall back to a small default set; results "
                  f"are better with ~200-500 of your own room photos.")

    print(f"Building {len(groups)} group(s) | backend={args.backend} "
          f"int8={args.int8} base={args.base_weights} imgsz={args.imgsz}")
    t0 = time.time()
    for g in groups:
        try:
            build_group(g, args.backend, args.int8, args.data,
                        args.base_weights, args.imgsz)
        except Exception as exc:
            print(f"  !! failed {g}: {exc}")
    print(f"\nDone in {time.time()-t0:.1f}s. Models under {CFG.models_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
