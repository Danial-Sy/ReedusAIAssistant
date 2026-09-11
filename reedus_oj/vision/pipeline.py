"""the front door: route the request -> capture -> detect -> dedupe -> describe for the LLM."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .config import CFG
from .intent_router import IntentRouter, RouteResult
from .model_manager import ModelManager
from .detector import Detector, Detection
from . import describe


@dataclass
class LookResult:
    request: str
    route: RouteResult
    detections: List[Detection] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    timings_ms: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.summary.get("text", "")

    def for_llm(self) -> dict:
        """Compact payload to inject into the LLM prompt/grounding."""
        return {
            "request": self.request,
            "groups_used": self.route.groups,
            "count": self.summary.get("count", 0),
            "scene": self.summary.get("text", ""),
            "items": self.summary.get("items", []),
        }


class ReedusVision:
    def __init__(
        self,
        llm_classify: Optional[Callable[[str, List[str]], List[str]]] = None,
        capture: Optional[Callable[[], Any]] = None,
        cfg=CFG,
    ):
        self.cfg = cfg
        self.router = IntentRouter(llm_classify=llm_classify)
        self.manager = ModelManager(cfg)
        self.detector = Detector(cfg)
        self.capture = capture  # e.g. a function that returns a camera frame

    def look(self, request: str, image: Any = None, debug: bool = False) -> LookResult:
        t_start = time.time()
        timings = {}

        # 1) route
        t0 = time.time()
        route = self.router.route(request)
        timings["route"] = round((time.time() - t0) * 1000, 1)
        if debug:
            print(route.explain())

        # 2) capture if needed
        if image is None:
            if self.capture is None:
                raise ValueError("no image supplied and no capture() configured")
            t0 = time.time()
            image = self.capture()
            timings["capture"] = round((time.time() - t0) * 1000, 1)

        # 3) detect across the routed groups, merge results
        all_dets: List[Detection] = []
        per_group_ms = {}
        for group in route.groups:
            t0 = time.time()
            loaded = self.manager.get(group)
            dets = self.detector.detect(loaded.model, image)
            per_group_ms[group] = round((time.time() - t0) * 1000, 1)
            all_dets.extend(dets)
            if debug:
                print(f"[detect] {group}: {len(dets)} dets in {per_group_ms[group]}ms "
                      f"(load={round(loaded.load_seconds*1000,1)}ms, src={loaded.source})")
        timings["detect_per_group"] = per_group_ms

        all_dets = _dedupe(all_dets)

        # 4) describe for the LLM
        summary = describe.summarize(all_dets, group=",".join(route.groups), request=request)
        timings["total"] = round((time.time() - t_start) * 1000, 1)

        return LookResult(request=request, route=route, detections=all_dets,
                          summary=summary, timings_ms=timings)


def _iou(a: Detection, b: Detection) -> float:
    ax1, ay1, ax2, ay2 = a.box_norm
    bx1, by1, bx2, by2 = b.box_norm
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = a.area_norm + b.area_norm - inter
    return inter / ua if ua > 0 else 0.0


def _same_object_label(a: str, b: str) -> bool:
    """True when two labels cannot describe two different objects in one box."""
    if a == b:
        return True
    aw, bw = a.split(), b.split()
    short, long_ = (aw, bw) if len(aw) <= len(bw) else (bw, aw)
    if len(short) >= len(long_):
        return False
    it = iter(long_)
    return all(w in it for w in short)   # ordered subsequence of whole words


def _dedupe(dets: List[Detection], iou_thr: float = 0.6) -> List[Detection]:
    """Collapse detections that describe the same physical object."""
    kept: List[Detection] = []
    for d in sorted(dets, key=lambda x: x.confidence, reverse=True):
        if any(_same_object_label(d.label, k.label) and _iou(d, k) > iou_thr
               for k in kept):
            continue
        kept.append(d)
    return kept


if __name__ == "__main__":
    # dry run of routing only, no weights needed
    v = ReedusVision()
    for q in ["grab me a coke", "clear the table", "where are my keys"]:
        print(v.router.route(q).explain())
