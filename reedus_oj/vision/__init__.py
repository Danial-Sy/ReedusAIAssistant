"""intent-based, lazy-loaded object detection for a cpu-only host."""

from .config import CFG, VisionConfig
from .pipeline import ReedusVision, LookResult
from .intent_router import IntentRouter, RouteResult
from .model_manager import ModelManager
from .detector import Detector, Detection
from . import taxonomy, describe

__all__ = [
    "CFG", "VisionConfig",
    "ReedusVision", "LookResult",
    "IntentRouter", "RouteResult",
    "ModelManager",
    "Detector", "Detection",
    "taxonomy", "describe",
]

__version__ = "0.1.0"
