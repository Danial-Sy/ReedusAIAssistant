#!/usr/bin/env python3
"""benchmark a group: base open-vocab model vs its compiled artifact."""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

from .config import CFG
from . import taxonomy
from .detector import Detector


def _load(kind: str, group: str, int8: bool):
    """Return (model, description, cold_load_seconds)."""
    vocab = taxonomy.vocabulary(group)
    t0 = time.time()

    if kind == "base":
        from ultralytics import YOLOWorld
        m = YOLOWorld(CFG.base_weights)
        m.set_classes(vocab)              # the expensive CLIP text-encoder pass
        return m, f"base+set_classes ({len(vocab)} classes)", time.time() - t0

    from ultralytics import YOLO
    base_dir = CFG.models_dir / group
    if kind == "pt":
        p = base_dir / f"{group}.pt"
    elif kind == "openvino":
        p = base_dir / f"{group}_openvino_model"
        if int8:
            alt = base_dir / f"{group}_int8_openvino_model"
            if alt.exists():
                p = alt
    elif kind == "onnx":
        p = base_dir / f"{group}.onnx"
    else:
        raise ValueError(kind)

    if not p.exists():
        return None, f"{kind}: MISSING ({p})", 0.0
    m = YOLO(str(p))
    return m, f"{kind} <- {p.name}", time.time() - t0


def _collect_images(spec: str | None) -> list[Path]:
    if spec:
        p = Path(spec).expanduser()
        if p.is_file():
            return [p]
        if p.is_dir():
            return sorted(list(p.glob("*.jpg")) + list(p.glob("*.png")))[:50]
        raise SystemExit(f"no such path: {p}")
    if CFG.calib_dir.exists():
        imgs = sorted(CFG.calib_dir.glob("*.jpg"))[:50]
        if imgs:
            return imgs
    fallback = Path(__file__).resolve().parent.parent / "frame.jpg"
    if fallback.exists():
        return [fallback]
    raise SystemExit("No images. Pass --images, or run: python -m vision.fetch_calib")


def bench(kind: str, group: str, images: list[Path], warmup: int,
          repeats: int, int8: bool) -> dict | None:
    model, desc, cold = _load(kind, group, int8)
    print(f"\n--- {desc}")
    if model is None:
        print("    skipped")
        return None
    print(f"    cold load: {cold*1000:.0f} ms")

    det = Detector()
    for _ in range(warmup):
        det.detect(model, str(images[0]))

    times, labels, counts = [], set(), []
    for _ in range(repeats):
        for img in images:
            t0 = time.time()
            dets = det.detect(model, str(img))
            times.append((time.time() - t0) * 1000)
            counts.append(len(dets))
            labels.update(d.label for d in dets)

    res = {
        "kind": kind, "cold_ms": cold * 1000,
        "mean_ms": statistics.mean(times),
        "median_ms": statistics.median(times),
        "p90_ms": sorted(times)[int(len(times) * 0.9) - 1],
        "mean_dets": statistics.mean(counts),
        "labels": labels,
    }
    print(f"    warm/frame: mean {res['mean_ms']:.1f} ms | "
          f"median {res['median_ms']:.1f} | p90 {res['p90_ms']:.1f}")
    print(f"    detections: {res['mean_dets']:.1f}/frame, "
          f"{len(labels)} distinct labels")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", default=CFG.fallback_group)
    ap.add_argument("--images", default=None, help="image file or dir (default: calib set)")
    ap.add_argument("--kinds", nargs="+", default=["base", "pt", "openvino"],
                    choices=["base", "pt", "onnx", "openvino"])
    ap.add_argument("--int8", action="store_true", help="prefer the *_int8_openvino_model dir")
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    if args.group not in taxonomy.group_names():
        raise SystemExit(f"unknown group {args.group!r}")

    images = _collect_images(args.images)
    print("== Reedus vision benchmark ==")
    print(f"group   : {args.group} ({len(taxonomy.vocabulary(args.group))} classes)")
    print(f"images  : {len(images)}  device: {CFG.resolve_device()}  imgsz: {CFG.imgsz}")
    print(f"repeats : {args.repeats} (+{args.warmup} warmup)")

    results = [r for k in args.kinds
               if (r := bench(k, args.group, images, args.warmup, args.repeats, args.int8))]

    if len(results) < 2:
        print("\nNothing to compare, build the group first:")
        print(f"  python -m vision.build_models --backend openvino --int8 --groups {args.group}")
        return 0

    base = next((r for r in results if r["kind"] == "base"), results[0])
    print("\n== vs base ==")
    print(f"{'path':<12}{'warm ms':>10}{'speedup':>10}{'cold ms':>10}{'dets/frame':>12}{'labels kept':>13}")
    for r in results:
        sp = base["mean_ms"] / r["mean_ms"] if r["mean_ms"] else 0
        kept = len(r["labels"] & base["labels"]) / max(1, len(base["labels"])) * 100
        print(f"{r['kind']:<12}{r['mean_ms']:>10.1f}{sp:>9.2f}x{r['cold_ms']:>10.0f}"
              f"{r['mean_dets']:>12.1f}{kept:>12.0f}%")

    print("\nHow to read this:")
    print("  speedup, warm per-frame gain. Expect a large jump from base (it")
    print("                carries the text encoder) and a further one from INT8.")
    print("  cold ms, base should be the 6000 ms figure you saw in the console.")
    print("  labels kept, share of the base model's distinct labels the compiled")
    print("                path still finds. Below ~90% means quantization cost you")
    print("                real detections; try FP32 OpenVINO, or more/better calib")
    print("                images (python -m vision.fetch_calib --n 600).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
