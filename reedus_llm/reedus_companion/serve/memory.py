"""memory: the bulletin (durable user facts) + episodic semantic recall over past turns."""
from __future__ import annotations
import re
import json
import time
import math
import sqlite3
from pathlib import Path
from typing import List

try:
    from reedus_companion import config
    BULLETIN_PATH = Path(config.BULLETIN_PATH)
    DB_PATH = Path(config.EPISODIC_DB)
    EMBED_MODEL = config.EMBED_MODEL
    TOP_K, MIN_SCORE = config.RECALL_TOP_K, config.RECALL_MIN_SCORE
except Exception:
    _h = Path(__file__).resolve().parent.parent / "memory"
    BULLETIN_PATH, DB_PATH = _h / "bulletin.md", _h / "episodic.sqlite"
    EMBED_MODEL, TOP_K, MIN_SCORE = "BAAI/bge-small-en-v1.5", 4, 0.30


class _Embedder:
    def __init__(self, model_name=EMBED_MODEL):
        self.model_name, self._model, self._mode = model_name, None, None

    def _ensure(self):
        if self._mode is not None:
            return
        try:
            from fastembed import TextEmbedding
            # explicit cache dir: fastembed defaults to /tmp, which the OS clears on reboot, so it
            # re-downloaded 66 MB on every launch and made boot need internet
            import os
            cache = os.environ.get("FASTEMBED_CACHE_PATH")
            if not cache:
                try:
                    from reedus_companion import config as _cfg
                    cache = str(Path(_cfg.MODELS_DIR) / "fastembed")
                except Exception:
                    cache = str(Path(__file__).resolve().parent.parent / "models" / "fastembed")
            Path(cache).mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(model_name=self.model_name, cache_dir=cache)
            self._mode = "fastembed"
        except Exception:
            self._model, self._mode = None, "lexical"

    @property
    def mode(self):
        self._ensure()
        return self._mode

    def embed(self, texts: List[str]) -> List[List[float]]:
        self._ensure()
        if self._mode == "fastembed":
            return [list(map(float, v)) for v in self._model.embed(texts)]
        return [self._lex(t) for t in texts]

    @staticmethod
    def _lex(text: str, dim: int = 256) -> List[float]:
        v = [0.0] * dim
        for tok in re.findall(r"[a-z0-9']+", text.lower()):
            v[hash(tok) % dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


class Memory:
    def __init__(self):
        BULLETIN_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = _Embedder()
        self._init_db()
        if not BULLETIN_PATH.exists():
            BULLETIN_PATH.write_text(
                "# Reedus bulletin\n\nDurable facts about the user. Reedus keeps this current.\n\n",
                encoding="utf-8")

    def read_bulletin(self) -> str:
        try:
            return BULLETIN_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def remember(self, fact: str) -> str:
        fact = (fact or "").strip().rstrip(".")
        if not fact:
            return "[remember: empty, ignored]"
        if fact.lower() in self.read_bulletin().lower():
            return "[remember: already known]"
        with open(BULLETIN_PATH, "a", encoding="utf-8") as f:
            f.write(f"- {fact}\n")
        return f"[remembered: {fact}]"

    def _init_db(self):
        self.db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS turns ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, role TEXT, "
                        "text TEXT, embedding TEXT)")
        self.db.commit()

    def add_turn(self, role: str, text: str):
        text = (text or "").strip()
        if not text:
            return
        vec = self.embedder.embed([f"{role}: {text}"])[0]
        self.db.execute("INSERT INTO turns (ts, role, text, embedding) VALUES (?,?,?,?)",
                        (time.time(), role, text, json.dumps(vec)))
        self.db.commit()

    def recall(self, query: str, top_k: int = TOP_K, min_score: float = MIN_SCORE) -> str:
        rows = self.db.execute("SELECT role, text, embedding FROM turns WHERE role='user'").fetchall()
        if not rows:
            return ""
        qv = self.embedder.embed([query])[0]
        scored = []
        for role, text, emb in rows:
            try:
                v = json.loads(emb)
            except Exception:
                continue
            scored.append((_cosine(qv, v), role, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        picked = [(r, t) for (s, r, t) in scored[:top_k] if s >= min_score]
        return "\n".join(f"- ({r}) {t}" for r, t in picked) if picked else ""

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass


if __name__ == "__main__":
    m = Memory()
    print("embedder mode:", m.embedder.mode)
    print(m.remember("user's name is Danial"))
    m.add_turn("user", "I love my Xeon homelab server")
    m.add_turn("assistant", "That E5-2680 still holds up")
    print("recall:", m.recall("homelab hardware", min_score=0.0) or "(none)")
    m.close()
