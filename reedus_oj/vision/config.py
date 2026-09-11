"""vision config, all overridable by REEDUS_VISION_* env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


@dataclass
class VisionConfig:
    # where things live on the nvme
    home: Path = field(default_factory=lambda: _env_path("REEDUS_VISION_HOME", str(Path(__file__).parent)))

    # yolov8s-worldv2 is the sweet spot on a Broadwell CPU: small + accurate
    base_weights: str = os.environ.get("REEDUS_BASE_WEIGHTS", "yolov8s-worldv2.pt")

    models_subdir: str = "models"

    calib_subdir: str = "calib"

    # runtime model backend
    backend: str = os.environ.get("REEDUS_VISION_BACKEND", "openvino")  # openvino | onnx | pt
    use_int8: bool = os.environ.get("REEDUS_VISION_INT8", "1") == "1"

    num_threads: int = int(os.environ.get("REEDUS_VISION_THREADS", "8"))

    device: str = os.environ.get("REEDUS_VISION_DEVICE", "auto")

    # ram discipline
    max_hot_models: int = int(os.environ.get("REEDUS_VISION_MAX_HOT", "2"))

    # detection defaults
    imgsz: int = field(default_factory=lambda: int(os.environ.get("REEDUS_VISION_IMGSZ", "640")))
                                # inference resolution; 512 is faster, 640 more accurate
    conf: float = field(default_factory=lambda: float(os.environ.get("REEDUS_VISION_CONF", "0.25")))
    confident_conf: float = field(
        default_factory=lambda: float(os.environ.get("REEDUS_VISION_CONFIDENT_CONF", "0.40")))
    iou: float = 0.45           # NMS IoU threshold
    max_det: int = 60           # cap detections per frame

    fallback_group: str = "general_common"

    max_groups_per_request: int = 2

    @property
    def models_dir(self) -> Path:
        return self.home / self.models_subdir

    @property
    def calib_dir(self) -> Path:
        return self.home / self.calib_subdir

    def group_model_path(self, group: str) -> Path:
        """Preferred compiled path for a group, honoring the chosen backend."""
        base = self.models_dir / group
        if self.backend == "openvino":
            return base / f"{group}_openvino_model"
        if self.backend == "onnx":
            return base / f"{group}.onnx"
        return base / f"{group}.pt"

    def resolve_device(self) -> str:
        """Turn 'auto' into a concrete device string."""
        if self.device != "auto":
            return self.device
        try:
            import torch
            return "cuda:0" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def ensure_dirs(self) -> None:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.calib_dir.mkdir(parents=True, exist_ok=True)


CFG = VisionConfig()
