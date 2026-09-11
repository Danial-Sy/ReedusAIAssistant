"""lazy LRU model manager: keeps a few groups hot, evicts the rest so 16 GB survives."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .config import CFG
from . import taxonomy


@dataclass
class LoadedModel:
    group: str
    model: Any
    source: str
    loaded_at: float
    load_seconds: float


class ModelManager:
    def __init__(self, cfg=CFG):
        self.cfg = cfg
        self._hot: "OrderedDict[str, LoadedModel]" = OrderedDict()
        self._lock = threading.RLock()

    def hot_groups(self):
        with self._lock:
            return list(self._hot.keys())

    def is_hot(self, group: str) -> bool:
        with self._lock:
            return group in self._hot

    def get(self, group: str) -> LoadedModel:
        """Return a ready model for `group`, loading/evicting as needed."""
        if group not in taxonomy.TAXONOMY:
            raise KeyError(f"unknown intent group: {group!r}")

        with self._lock:
            if group in self._hot:
                self._hot.move_to_end(group)          # mark most-recently-used
                return self._hot[group]

            loaded = self._load(group)
            self._hot[group] = loaded
            self._evict_if_needed()
            return loaded

    def preload(self, group: str) -> None:
        """Warm a group ahead of time (e.g. router's second-best guess)."""
        self.get(group)

    def release(self, group: str) -> None:
        with self._lock:
            self._drop(group)

    def clear(self) -> None:
        with self._lock:
            for g in list(self._hot):
                self._drop(g)

    def _load(self, group: str) -> LoadedModel:
        t0 = time.time()
        path = self.cfg.group_model_path(group)

        # try compiled artifact first
        if path.exists():
            model = self._load_ultralytics(str(path))
            return LoadedModel(group, model, self.cfg.backend, t0, time.time() - t0)

        # fallback: base open-vocab model + this group's vocabulary
        model = self._load_base_with_vocab(group)
        return LoadedModel(group, model, "base+set_classes", t0, time.time() - t0)

    def _load_ultralytics(self, path: str):
        from ultralytics import YOLO  # lazy import
        model = self._apply_threads(YOLO(path))
        return model

    def _load_base_with_vocab(self, group: str):
        """Fresh YOLOWorld instance per group."""
        from ultralytics import YOLOWorld  # lazy import
        model = YOLOWorld(self.cfg.base_weights)
        model.set_classes(taxonomy.vocabulary(group))
        device = self.cfg.resolve_device()
        if device != "cpu":
            try:
                model.to(device)
            except Exception:
                pass
        return self._apply_threads(model)

    @staticmethod
    def _apply_threads(model):
        try:
            import torch
            torch.set_num_threads(CFG.num_threads)
        except Exception:
            pass
        return model

    def _evict_if_needed(self) -> None:
        while len(self._hot) > self.cfg.max_hot_models:
            old_group, _ = next(iter(self._hot.items()))  # LRU = first item
            self._drop(old_group)

    def _drop(self, group: str) -> None:
        lm = self._hot.pop(group, None)
        if lm is None:
            return
        try:
            del lm.model
        except Exception:
            pass
        self._gc()

    @staticmethod
    def _gc():
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


if __name__ == "__main__":
    # smoke test without weights, LRU bookkeeping only
    mgr = ModelManager()
    print("configured max hot:", CFG.max_hot_models)
    print("hot groups (none yet):", mgr.hot_groups())
