"""eyes -> brain: wraps the reedus_oj vision pipeline for look turns."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

DEBUG = bool(os.environ.get("REEDUS_DEBUG"))


def vision_enabled() -> bool:
    return os.environ.get("REEDUS_VISION", "1") != "0"


def _find_vision_package() -> Optional[Path]:
    """Locate the folder containing the `vision` package so we can import it."""
    candidates = []
    env = os.environ.get("REEDUS_VISION_PATH")
    if env:
        candidates.append(Path(env).expanduser())
    candidates += [
        Path.home() / "Reedus" / "reedus_oj",
        Path(__file__).resolve().parents[3] / "reedus_oj",
        Path.home() / "reedus_oj",
    ]
    for c in candidates:
        if (c / "vision" / "__init__.py").exists():
            return c
    return None


def _pin_ultralytics_dirs() -> Optional[Path]:
    """Keep YOLO/CLIP downloads in the vision home, not cwd / ~/.config."""
    home = os.environ.get("REEDUS_VISION_HOME", "").strip()
    if not home:
        root = os.environ.get("REEDUS_ROOT", "").strip()
        if root:
            home = str(Path(root) / "vision_models")
        else:
            pkg = _find_vision_package()
            home = str((pkg / "vision" / "models") if pkg else Path.home() / ".reedus" / "vision")
    path = Path(home).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    os.environ.setdefault("YOLO_CONFIG_DIR", str(path))
    os.environ.setdefault("ULTRALYTICS_HOME", str(path))
    try:
        (path / "Ultralytics").mkdir(parents=True, exist_ok=True)
        probe = path / "Ultralytics" / ".reedus_write_test"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
    except OSError:
        fallback = Path("/tmp/reedus_ultralytics")
        fallback.mkdir(parents=True, exist_ok=True)
        os.environ["YOLO_CONFIG_DIR"] = str(fallback)
        os.environ["ULTRALYTICS_HOME"] = str(fallback)
        if DEBUG:
            print(f"[vision] YOLO config not writable under {path}; using {fallback}",
                  file=sys.stderr)
    if not os.environ.get("REEDUS_BASE_WEIGHTS"):
        for name in ("yolov8s-worldv2.pt", "yolov8s-world.pt"):
            candidate = path / name
            if candidate.is_file():
                os.environ["REEDUS_BASE_WEIGHTS"] = str(candidate)
                break
    return path


class VisionBridge:
    """Owns a persistent camera + the ReedusVision pipeline. Lazy by default, warm() preloads."""

    def __init__(self):
        self._pkg_ready = False
        self._pkg_failed = False
        self._vcap = None            # the vision.capture module
        self._vision = None          # ReedusVision instance
        self._cap = None             # persistent cv2 capture
        self._warmed = False

    def _ensure_pkg(self) -> bool:
        """Import the vision package + build the pipeline. Permanent-fail only if the import itself breaks."""
        if self._pkg_ready:
            return True
        if self._pkg_failed:
            return False
        try:
            _pin_ultralytics_dirs()
            root = _find_vision_package()
            if root is None:
                raise RuntimeError("could not locate the `vision` package; set REEDUS_VISION_PATH")
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            from vision import ReedusVision           # noqa
            from vision import capture as vcap
            self._vcap = vcap
            self._vision = ReedusVision(capture=self._grab_frame)
            self._pkg_ready = True
            if DEBUG:
                print(f"[vision] package ready (pkg={root})", file=sys.stderr)
            return True
        except Exception as exc:
            self._pkg_failed = True
            if DEBUG:
                print(f"[vision] disabled, package import failed: {exc}", file=sys.stderr)
            return False

    def _ensure_cam(self) -> bool:
        """Open the camera if it isn't already. Retried every look turn so you can plug one in later."""
        if self._cap is not None and self._cap.isOpened():
            return True
        source = os.environ.get("REEDUS_CAM", "0")
        try:
            if source.isdigit():
                cap = self._vcap._cv2().VideoCapture(int(source))
                if not cap.isOpened():
                    cap.release()
                    raise RuntimeError(f"webcam {source} not open (is OBS virtual camera running?)")
                cap.read()
                self._cap = cap
            else:
                self._cap, resolved = self._vcap.resolve_stream(source)
                if DEBUG:
                    print(f"[vision] stream on {resolved}", file=sys.stderr)
            if DEBUG:
                print(f"[vision] camera ready (source={source})", file=sys.stderr)
            return True
        except Exception as exc:
            if DEBUG:
                print(f"[vision] camera not available yet: {exc}", file=sys.stderr)
            return False

    def _grab_frame(self):
        return self._vcap.grab_fresh(self._cap)

    def warm(self, groups: Optional[list] = None) -> float:
        """Preload detector weights (+ optional camera) so the first look is fast."""
        if not vision_enabled():
            return 0.0
        t0 = time.perf_counter()
        if not self._ensure_pkg():
            return time.perf_counter() - t0
        try:
            from vision.config import CFG
            want = list(groups) if groups else [CFG.fallback_group]
            for g in want:
                try:
                    self._vision.manager.preload(g)
                    if DEBUG:
                        print(f"[vision] warmed group={g!r}", file=sys.stderr)
                except Exception as exc:                     # noqa: BLE001
                    if DEBUG:
                        print(f"[vision] warm({g}) failed: {exc}", file=sys.stderr)
        except Exception as exc:                             # noqa: BLE001
            if DEBUG:
                print(f"[vision] warm failed: {exc}", file=sys.stderr)
        # camera open is optional at warm, look turns still retry
        try:
            self._ensure_cam()
        except Exception:
            pass
        self._warmed = True
        return time.perf_counter() - t0

    def grounding_for(self, user_msg: str) -> Optional[dict]:
        """Capture + detect. A dict with grounding text + metadata, or None if vision is off / no camera."""
        if not vision_enabled() or not self._ensure_pkg() or not self._ensure_cam():
            return None
        try:
            result = self._vision.look(user_msg, debug=DEBUG)
        except Exception as exc:
            if DEBUG:
                print(f"[vision] look failed: {exc}", file=sys.stderr)
            return None
        s = result.summary
        return {
            "count": s.get("count", 0),
            "grounding": s.get("grounding", ""),
            "most_prominent": s.get("most_prominent"),
            "groups": result.route.groups,
            "text": s.get("text", ""),
        }

    def close(self):
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
