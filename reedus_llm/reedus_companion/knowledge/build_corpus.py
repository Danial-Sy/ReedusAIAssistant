#!/usr/bin/env python3
"""build the corpus from wikipedia dumps. build box only."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Iterator, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from reedus_companion import config                              # noqa: E402
from reedus_companion.knowledge import domains                   # noqa: E402
from reedus_companion.knowledge.store import BulkWriter, DIM     # noqa: E402

DATASET = "wikimedia/wikipedia"
DEFAULT_SNAPSHOT = "20231101"

CHUNK_WORDS = 180
CHUNK_OVERLAP = 40
MIN_CHUNK_WORDS = 25
LEAD_WORDS = 320                 # enwiki: roughly the lead section
EMBED_BATCH = 512

# titles with no conversational value
_SKIP_TITLE = re.compile(
    r"^(list of|lists of|index of|outline of|timeline of|glossary of|bibliography of|"
    r"comparison of|table of|category:|template:|portal:|file:|wikipedia:|draft:|module:)",
    re.I)
_SKIP_SECTION = re.compile(
    r"^(references|external links|see also|notes|further reading|bibliography|citations|"
    r"sources|gallery|footnotes|contents|site navigation|navigation)\s*$", re.I)
_SECTION = re.compile(r"^==+\s*(.+?)\s*==+$", re.M)


def _route_domain(title: str, text: str) -> str:
    """Cheap keyword bucketing. Only decides which file a passage lands in, retrieval probes several."""
    hay = (title + " " + text[:400]).lower()
    best, best_n = domains.FALLBACK, 0
    for d in domains.names():
        if d == domains.FALLBACK:
            continue
        n = sum(1 for t in domains.triggers(d) if t in hay)
        if n > best_n:
            best, best_n = d, n
    return best


def _chunks(title: str, text: str, lead_only: bool) -> Iterator[dict]:
    if lead_only:
        words = text.split()
        if len(words) < MIN_CHUNK_WORDS:
            return
        body = " ".join(words[:LEAD_WORDS])
        step = max(1, CHUNK_WORDS - CHUNK_OVERLAP)
        w = body.split()
        for i in range(0, len(w), step):
            piece = " ".join(w[i:i + CHUNK_WORDS])
            if len(piece.split()) < MIN_CHUNK_WORDS:
                break
            yield {"topic": title, "title": title, "text": piece}
        return

    parts, head, buf = [], title, []
    for line in text.splitlines():
        m = _SECTION.match(line.strip())
        if m:
            if buf:
                parts.append((head, " ".join(buf)))
                buf = []
            head = m.group(1)
            continue
        if line.strip():
            buf.append(line.strip())
    if buf:
        parts.append((head, " ".join(buf)))

    for head, body in parts:
        if _SKIP_SECTION.match(head.strip()):
            continue
        w = body.split()
        if len(w) < MIN_CHUNK_WORDS:
            continue
        step = max(1, CHUNK_WORDS - CHUNK_OVERLAP)
        for i in range(0, len(w), step):
            piece = " ".join(w[i:i + CHUNK_WORDS])
            if len(piece.split()) < MIN_CHUNK_WORDS:
                break
            yield {"topic": title,
                   "title": f"{title} — {head}" if head != title else title,
                   "text": piece}


def _stream(snapshot: str, lang: str, limit: Optional[int]) -> Iterator[dict]:
    from datasets import load_dataset
    ds = load_dataset(DATASET, f"{snapshot}.{lang}", split="train", streaming=True)
    for i, row in enumerate(ds):
        if limit and i >= limit:
            return
        yield row


class _BuildEmbedder:
    """Embedder for the corpus build, separate from the runtime one on purpose."""

    def __init__(self, gpu: bool = True, model: str = "BAAI/bge-small-en-v1.5"):
        self.device = "cpu"
        self.backend = None
        self.model = None

        if gpu:
            try:
                import torch
                if torch.cuda.is_available():
                    from sentence_transformers import SentenceTransformer
                    self.model = SentenceTransformer(model, device="cuda")
                    self.backend, self.device = "sentence-transformers", "cuda"
            except Exception:
                self.model = None

        if self.model is None and gpu:
            try:
                import onnxruntime as ort
                from fastembed import TextEmbedding
                if "CUDAExecutionProvider" in ort.get_available_providers():
                    self.model = TextEmbedding(
                        model_name=model,
                        providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
                    self.backend, self.device = "fastembed", "cuda"
            except Exception:
                self.model = None

        if self.model is None:
            from fastembed import TextEmbedding
            self.model = TextEmbedding(model_name=model)
            self.backend, self.device = "fastembed", "cpu"

    def embed(self, texts: List[str]):
        import numpy as np
        if self.backend == "sentence-transformers":
            return self.model.encode(texts, batch_size=256, convert_to_numpy=True,
                                     normalize_embeddings=True,
                                     show_progress_bar=False).astype("float32")
        return np.asarray(list(self.model.embed(texts)), dtype=np.float32)


def build(langs: List[str], snapshot: str, out: Path, limit: Optional[int],
          batch: int = EMBED_BATCH, gpu: bool = True) -> None:
    try:
        emb = _BuildEmbedder(gpu=gpu)
    except ImportError:
        raise SystemExit(
            "\n  fastembed is not installed. A corpus built with the lexical fallback is\n"
            "  256-dim garbage the runtime cannot query.\n"
            "  pip install fastembed, then re-run.\n")

    if emb.device == "cpu":
        print("\n  ####  EMBEDDING ON CPU  ####\n"
              "  Measured ~27 passages/sec -> ~14 h for simplewiki, ~6 days for the full corpus.\n"
              "  The 3090 Ti is in this machine; this env just can't see it.\n\n"
              "      conda activate reedus-train      # the env with CUDA torch\n"
              "      pip install sentence-transformers datasets fastembed\n"
              "      python -m reedus_companion.knowledge.build_corpus --simple\n\n"
              "  Ctrl-C now unless you specifically want the CPU run.\n")
        time.sleep(5)
    else:
        print(f"\n  embedding on {emb.device} via {emb.backend}")

    out.mkdir(parents=True, exist_ok=True)
    writer = BulkWriter(out)
    seen_titles: Set[str] = set()
    t0 = time.time()
    totals = {}

    for lang in langs:
        lead_only = (lang == "en")
        n_art = n_pass = n_skip = 0
        pend_rows: List[dict] = []
        pend_dom: List[str] = []

        t_embed = [0.0]

        def flush():
            nonlocal pend_rows, pend_dom, n_pass
            if not pend_rows:
                return
            te = time.time()
            vecs = emb.embed([r["text"] for r in pend_rows])
            t_embed[0] += time.time() - te
            for d in set(pend_dom):
                idx = [i for i, x in enumerate(pend_dom) if x == d]
                writer.add(d, vecs[idx], [pend_rows[i] for i in idx])
            n_pass += len(pend_rows)
            pend_rows, pend_dom = [], []

        print(f"\n=== {lang} ({'lead only' if lead_only else 'full articles'}) ===")
        for row in _stream(snapshot, lang, limit):
            title = (row.get("title") or "").strip()
            text = (row.get("text") or "").strip()
            if not title or not text or _SKIP_TITLE.match(title):
                n_skip += 1
                continue
            key = title.lower()
            if key in seen_titles:
                n_skip += 1
                continue
            seen_titles.add(key)

            dom = _route_domain(title, text)
            for c in _chunks(title, text, lead_only):
                c["url"] = f"https://{'simple' if lang=='simple' else 'en'}.wikipedia.org/wiki/" \
                           + title.replace(" ", "_")
                c["added"] = time.strftime("%Y-%m-%d")
                pend_rows.append(c)
                pend_dom.append(dom)
            if len(pend_rows) >= batch:
                flush()

            n_art += 1
            if n_art % 5000 == 0:
                el = time.time() - t0
                emb_rate = n_pass / max(t_embed[0], 1e-9)
                print(f"  {n_art:>9,} articles  {n_pass:>11,} passages  "
                      f"embed {emb_rate:>6.0f} p/s  overall {n_pass/max(el,1):>6.0f} p/s  "
                      f"{el/60:>6.1f} min")
        flush()
        totals[lang] = (n_art, n_pass, n_skip)
        el = time.time() - t0
        print(f"  done: {n_art:,} articles, {n_pass:,} passages, {n_skip:,} skipped")
        print(f"        embed {t_embed[0]/60:.1f} min ({n_pass/max(t_embed[0],1e-9):.0f} p/s)  |  "
              f"stream+write {(el-t_embed[0])/60:.1f} min")

    print("\nfinalising shards…")
    per_domain = writer.finalize()
    el = (time.time() - t0) / 60
    print(f"\n=== corpus built in {el:.1f} min -> {out} ===")
    for d, n in sorted(per_domain.items(), key=lambda kv: -kv[1]):
        print(f"  {d:18}{n:>12,} passages")
    total = sum(per_domain.values())
    size = sum(p.stat().st_size for p in out.glob("*")) / 1e9
    print(f"  {'TOTAL':18}{total:>12,} passages   {size:.1f} GB on disk")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simple", action="store_true", help="Simple English Wikipedia, full text")
    ap.add_argument("--en", action="store_true", help="English Wikipedia, lead sections only")
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT,
                    help=f"dump snapshot (default {DEFAULT_SNAPSHOT})")
    ap.add_argument("--out", default=None, help="output dir (default config.KNOWLEDGE_DIR)")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N articles per language, use this to test the pipeline")
    ap.add_argument("--batch", type=int, default=EMBED_BATCH)
    ap.add_argument("--cpu", action="store_true",
                    help="force CPU embedding even if CUDA is available")
    args = ap.parse_args()

    langs = ([("simple")] if args.simple else []) + (["en"] if args.en else [])
    if not langs:
        ap.error("pick at least one of --simple / --en")
    out = Path(args.out).expanduser() if args.out else config.KNOWLEDGE_DIR
    build(langs, args.snapshot, out, args.limit, args.batch, gpu=not args.cpu)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
