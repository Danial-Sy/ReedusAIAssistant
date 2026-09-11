"""turns detections into the compact summary the LLM gets."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

from .config import CFG
from .detector import Detection


_POINTING_RE = re.compile(
    r"""\b(
          what(?:'s|\s+is|\s+are)?\s+(?:this|that|these|those)\b
        | what\s+am\s+i\s+(?:holding|pointing|showing|looking|touching)
        | what\s+(?:is|'s)\s+(?:in|on)\s+my\s+hand
        | point(?:ing)?\s+(?:at|to)
        | hold(?:ing)?\s+(?:up|this|that|it)?
        | show(?:ing)?\s+(?:you|this|that|it)
        | this\s+thing
        | right\s+here
        | in\s+my\s+hand
    )\b""",
    re.I | re.X,
)


def is_pointing_request(request: str) -> bool:
    """True when the user is indicating one object, not surveying the room."""
    return bool(request and _POINTING_RE.search(request))


def _pick_prominent(detections: List[Detection],
                    request: str = "") -> Tuple[Detection, bool]:
    """Choose the object the user most likely means."""
    if is_pointing_request(request):
        def _cost(d: Detection) -> float:
            cx, cy = d.center_norm
            dist = ((cx - 0.5) ** 2 + (cy - 0.5) ** 2) ** 0.5
            return dist + 0.35 * d.area_norm - 0.15 * d.confidence
        return min(detections, key=_cost), True

    return max(detections, key=lambda d: d.area_norm * d.confidence), False


def _horizontal(cx: float) -> str:
    if cx < 0.34:
        return "left"
    if cx > 0.66:
        return "right"
    return "center"


def _vertical(cy: float) -> str:
    if cy < 0.34:
        return "top"
    if cy > 0.66:
        return "bottom"
    return "middle"


def _pluralize(label: str, n: int) -> str:
    if n == 1:
        return f"a {label}"
    # naive but readable pluralisation
    if label.endswith(("s", "x", "z", "ch", "sh")):
        plural = label + "es"
    elif label.endswith("y") and label[-2:-1] not in "aeiou":
        plural = label[:-1] + "ies"
    else:
        plural = label + "s"
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
             6: "six", 7: "seven", 8: "eight", 9: "nine"}
    count = words.get(n, str(n))
    return f"{count} {plural}"


def summarize(detections: List[Detection], group: str = "", request: str = "") -> Dict:
    """Return a structured + textual summary for the LLM."""
    if not detections:
        return {
            "count": 0,
            "labels": [],
            "text": "I don't see anything I recognize for that.",
            "items": [],
            "most_prominent": None,
            "confident": [],
            "tentative": [],
            "grounding": "",
        }

    # collapse by label, keep positions
    by_label: Dict[str, List[Detection]] = defaultdict(list)
    for d in detections:
        by_label[d.label].append(d)

    items = []
    phrases = []
    for label, dets in sorted(by_label.items(), key=lambda kv: -max(d.confidence for d in kv[1])):
        n = len(dets)
        positions = Counter(_horizontal(d.center_norm[0]) for d in dets)
        pos_str = ", ".join(p for p, _ in positions.most_common())
        best = max(d.confidence for d in dets)
        items.append({
            "label": label,
            "count": n,
            "positions": dict(positions),
            "max_confidence": round(best, 3),
        })
        phrases.append(f"{_pluralize(label, n)} ({pos_str})")

    most_prominent, pointing = _pick_prominent(detections, request)
    prom_label = most_prominent.label

    text = "I can see " + "; ".join(phrases) + "."
    if len(detections) > 1:
        text += (f" Most likely the one meant: {prom_label}." if pointing
                 else f" Most prominent: {prom_label}.")

    def _item_for(label):
        return next(it for it in items if it["label"] == label)

    prom_item = _item_for(prom_label)
    prom_pos = ", ".join(prom_item["positions"].keys())

    confident, tentative = [], []
    for it in items:
        if it["label"] == prom_label:
            continue
        (confident if it["max_confidence"] >= CFG.confident_conf else tentative).append(it)

    def _phrase(it):
        pos = ", ".join(it["positions"].keys())
        return f"{_pluralize(it['label'], it['count'])} ({pos})"

    if pointing:
        g = [f"The object being pointed at or held is most likely "
             f"{_pluralize(prom_item['label'], prom_item['count'])} ({prom_pos}), "
             f"it is nearest the center of the frame (high confidence)."]
    else:
        g = [f"Most prominent: {_pluralize(prom_item['label'], prom_item['count'])} "
             f"({prom_pos}), high confidence."]
    if confident:
        g.append("Also clearly visible: " + ", ".join(_phrase(it) for it in confident) + ".")
    if tentative:
        g.append("Less certain, might also be there: "
                 + ", ".join(_phrase(it) for it in tentative) + ".")
    g.append("Speak ONLY these labels and positions; do not invent attributes.")
    grounding = " ".join(g)

    return {
        "count": len(detections),
        "labels": sorted(by_label.keys()),
        "group": group,
        "request": request,
        "items": items,
        "most_prominent": prom_label,
        "pointing": pointing,
        "confident": [it["label"] for it in confident],
        "tentative": [it["label"] for it in tentative],
        "text": text,
        "grounding": grounding,
    }
