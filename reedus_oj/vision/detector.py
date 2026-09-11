"""runs one loaded model on one frame and returns Detections."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, List, Optional, Tuple

from .config import CFG


@dataclass
class Detection:
    label: str
    confidence: float
    box_xyxy: Tuple[float, float, float, float]   # pixels: x1, y1, x2, y2
    box_norm: Tuple[float, float, float, float]   # 0..1 relative to image

    @property
    def center_norm(self) -> Tuple[float, float]:
        x1, y1, x2, y2 = self.box_norm
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    @property
    def area_norm(self) -> float:
        x1, y1, x2, y2 = self.box_norm
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    def to_dict(self) -> dict:
        return asdict(self)


class Detector:
    """Runs a single loaded model. ModelManager owns lifecycle, this owns inference."""

    def __init__(self, cfg=CFG):
        self.cfg = cfg

    def detect(self, model: Any, image: Any,
               conf: Optional[float] = None,
               iou: Optional[float] = None,
               imgsz: Optional[int] = None) -> List[Detection]:
        conf = self.cfg.conf if conf is None else conf
        iou = self.cfg.iou if iou is None else iou
        imgsz = self.cfg.imgsz if imgsz is None else imgsz

        results = model.predict(
            source=image,
            conf=conf,
            iou=iou,
            imgsz=imgsz,
            max_det=self.cfg.max_det,
            device=self.cfg.resolve_device(),
            verbose=False,
        )
        if not results:
            return []

        res = results[0]
        h, w = self._image_hw(res)
        names = res.names if hasattr(res, "names") else {}
        out: List[Detection] = []

        boxes = getattr(res, "boxes", None)
        if boxes is None:
            return out

        for b in boxes:
            cls_id = int(b.cls[0]) if b.cls is not None else -1
            label = names.get(cls_id, str(cls_id)) if isinstance(names, dict) else str(cls_id)
            confidence = float(b.conf[0]) if b.conf is not None else 0.0
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0].tolist())
            out.append(Detection(
                label=label,
                confidence=round(confidence, 4),
                box_xyxy=(round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)),
                box_norm=(
                    round(x1 / w, 4) if w else 0.0,
                    round(y1 / h, 4) if h else 0.0,
                    round(x2 / w, 4) if w else 0.0,
                    round(y2 / h, 4) if h else 0.0,
                ),
            ))
        # highest confidence first, most useful ordering for the LLM
        out.sort(key=lambda d: d.confidence, reverse=True)
        return out

    @staticmethod
    def _image_hw(res) -> Tuple[int, int]:
        # ultralytics stores original shape on the result
        shape = getattr(res, "orig_shape", None)
        if shape and len(shape) >= 2:
            return int(shape[0]), int(shape[1])
        img = getattr(res, "orig_img", None)
        if img is not None and hasattr(img, "shape"):
            return int(img.shape[0]), int(img.shape[1])
        return (1, 1)
