#!/usr/bin/env python3
"""learn the labelling rubric from the hand labels, then apply it where it's confident."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np                                                    # noqa: E402

CLASSES = ["grounded", "invented", "pseudo", "hedged", "offtarget"]

_BOLD = re.compile(r"\*\*([^*\n]+?)\*\*")
_RUN = re.compile(r"(?<![.!?…]\s)(?<!^)\b([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+)+)")
_ONE = re.compile(r"(?<![.!?…]\s)(?<!^)\b([A-Z][a-z’'\-]{3,})")
_HEDGE = re.compile(
    r"\b(i'?m not sure|not certain|i don'?t (?:really )?(?:know|recall|remember)|"
    r"can'?t (?:say|recall|remember)|hard to say|no idea|couldn'?t tell you|"
    r"i'?ll be honest,? i|don'?t quote me)\b", re.I)
_APOLOGY = re.compile(r"\b(whoops|my bad|apolog|sorry|let'?s reset|i misread|overreach)\b", re.I)
_GENERIC = re.compile(
    r"\b(sidestep|backdash|forward smash|strong attack|quick dash|combo string|"
    r"heavy attack|light attack|dash attack|spinning kick|final kick)\b", re.I)


def candidate_names(text: str) -> List[str]:
    out, seen = [], set()
    for pat in (_BOLD, _RUN, _ONE):
        for m in pat.finditer(text or ""):
            n = re.sub(r"[*_`]", "", m.group(1)).strip(" .,!?—-")
            n = re.split(r"\s+\+|\s+—|\s+-\s", n)[0].strip()
            if len(n) >= 4 and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
    return out


_ENTITY_INDEX = []


def _entity_index():
    """The entity index, or None. Cached so 200 rows don't open 200 connections."""
    if not _ENTITY_INDEX:
        try:
            from reedus_companion.knowledge.entity_index import EntityIndex
            ix = EntityIndex()
            ix.db
            _ENTITY_INDEX.append(ix)
        except Exception:                                        # noqa: BLE001
            _ENTITY_INDEX.append(None)
    return _ENTITY_INDEX[0]


def misattribution_count(resp: str, prompt: str) -> int:
    """Names in the reply that are real but belong to somebody else."""
    ix = _entity_index()
    if ix is None:
        return 0
    from reedus_companion.knowledge.entity_index import drop_subspans, subjects_in
    names = drop_subspans(candidate_names(resp), ix.has_topic)
    if not names:
        return 0
    subjects = subjects_in(prompt, ix) + subjects_in(resp, ix)
    if not subjects:
        return 0
    bad = 0
    for n in names:
        if not ix.is_ownable(n):
            continue
        if not any(ix.owns(s, n) for s in subjects):
            bad += 1
    return bad


def features(resp: str, prompt: str, store) -> List[float]:
    """Deliberately plain and few features, ~200 rows can't support a rich feature space."""
    from reedus_companion.serve.core import _known_topic
    names = candidate_names(resp)
    known = sum(1 for n in names if _known_topic(store, n))
    unknown = len(names) - known
    misattributed = misattribution_count(resp, prompt)
    words = max(len((resp or "").split()), 1)
    p_terms = set(re.findall(r"[a-z']{4,}", (prompt or "").lower()))
    r_terms = set(re.findall(r"[a-z']{4,}", (resp or "").lower()))
    overlap = len(p_terms & r_terms) / (len(p_terms) or 1)      # on-topic proxy
    return [
        known, unknown,
        known / (known + unknown + 1),
        misattributed,
        misattributed / (len(names) + 1),
        len(_HEDGE.findall(resp or "")),
        len(_GENERIC.findall(resp or "")),
        len(_APOLOGY.findall(resp or "")),
        math.log1p(words),
        overlap,
        len(_BOLD.findall(resp or "")),
        sum(c.isdigit() for c in (resp or "")) / words,
    ]


def _softmax(z):
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def fit(X, y, n_cls, epochs=4000, lr=0.15, l2=1e-3, seed=0):
    rng = np.random.default_rng(seed)
    W = rng.normal(0, 0.01, (X.shape[1], n_cls))
    b = np.zeros(n_cls)
    Y = np.eye(n_cls)[y]
    for _ in range(epochs):
        P = _softmax(X @ W + b)
        g = (P - Y) / len(X)
        W -= lr * (X.T @ g + l2 * W)
        b -= lr * g.sum(axis=0)
    return W, b


def standardise(X, mu=None, sd=None):
    if mu is None:
        mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-9
    return (X - mu) / sd, mu, sd


def collect(labels: dict, samples: List[dict], store):
    X, y, meta = [], [], []
    for row in samples:
        marks = labels.get(row["prompt"], {})
        for resp in row["responses"]:
            lbl = marks.get(resp)
            if lbl in CLASSES:
                X.append(features(resp, row["prompt"], store))
                y.append(CLASSES.index(lbl))
                meta.append((row["prompt"], resp))
    return np.array(X, dtype=float), np.array(y), meta


def report(X, y) -> None:
    """5-fold cross-validation, per class. The per-class number is what matters."""
    n = len(y)
    if n < 40:
        print(f"\n  only {n} labelled rows, too few to say anything. Label more first.\n")
        return
    idx = np.arange(n)
    np.random.default_rng(0).shuffle(idx)
    folds = np.array_split(idx, 5)
    conf = np.zeros((len(CLASSES), len(CLASSES)), dtype=int)
    confs: List[Tuple[float, bool]] = []
    for k in range(5):
        te = folds[k]
        tr = np.concatenate([folds[j] for j in range(5) if j != k])
        Xtr, mu, sd = standardise(X[tr])
        Xte, _, _ = standardise(X[te], mu, sd)
        W, b = fit(Xtr, y[tr], len(CLASSES))
        P = _softmax(Xte @ W + b)
        pred = P.argmax(axis=1)
        for t, p, pr in zip(y[te], pred, P.max(axis=1)):
            conf[t, p] += 1
            confs.append((pr, t == p))

    print(f"\n  cross-validated on {n} of your labels\n")
    print(f"  {'class':11}{'n':>5}{'recall':>9}{'precision':>11}")
    for i, c in enumerate(CLASSES):
        sup = conf[i].sum()
        rec = conf[i, i] / sup if sup else float("nan")
        col = conf[:, i].sum()
        prec = conf[i, i] / col if col else float("nan")
        flag = ""
        if sup < 15:
            flag = "  <- too few to trust"
        elif c == "offtarget" and prec == prec and prec < 0.8:
            flag = "  <- SIGN-FLIP RISK"
        print(f"  {c:11}{sup:>5}{rec:>9.2f}{prec:>11.2f}{flag}")
    acc = np.trace(conf) / conf.sum()
    print(f"\n  overall {acc:.2f}, ignore this number, per-class above is what decides it.")

    # the number that governs --apply
    print(f"\n  {'min-conf':>10}{'covered':>10}{'accuracy':>10}")
    for t in (0.5, 0.6, 0.7, 0.8, 0.9):
        sel = [ok for pr, ok in confs if pr >= t]
        cov = len(sel) / len(confs)
        a = sum(sel) / len(sel) if sel else float("nan")
        print(f"  {t:>10.2f}{cov:>10.0%}{a:>10.2f}")
    print("\n  pick a floor where accuracy is acceptable; the uncovered rest stays yours.")
    fp_g = conf[:, 0].sum() - conf[0, 0]
    if fp_g:
        print(f"\n  NOTE: {fp_g} row(s) wrongly predicted `grounded`, those would become CHOSEN")
        print(f"        examples. Weigh that harder than the headline number.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labels", default="dpo_labels.json")
    ap.add_argument("--in", dest="inp", default="dpo_samples.pruned.jsonl")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-conf", type=float, default=0.75)
    ap.add_argument("--out", default="auto_labels.json")
    ap.add_argument("--unsure", default=None)
    a = ap.parse_args()

    from reedus_companion.knowledge.store import KnowledgeStore
    from reedus_companion import config
    store = KnowledgeStore(Path(config.KNOWLEDGE_DIR), max_hot=2)

    labels = json.loads(Path(a.labels).read_text()) if Path(a.labels).exists() else {}
    train_src = [json.loads(l) for l in Path("dpo_samples.jsonl").read_text().splitlines()
                 if l.strip()] if Path("dpo_samples.jsonl").exists() else []
    target = [json.loads(l) for l in Path(a.inp).read_text().splitlines() if l.strip()]

    X, y, _ = collect(labels, train_src or target, store)
    print(f"  {len(y)} labelled rows  {dict(Counter(CLASSES[i] for i in y))}")
    if a.report or not a.apply:
        report(X, y)
        return 0

    if len(y) < 40:
        print("  refusing to apply: fewer than 40 labelled rows to learn from.")
        return 1
    Xs, mu, sd = standardise(X)
    W, b = fit(Xs, y, len(CLASSES))

    auto, unsure, n_new = dict(labels), [], 0
    for row in target:
        for resp in row["responses"]:
            if labels.get(row["prompt"], {}).get(resp):
                continue
            f = np.array([features(resp, row["prompt"], store)], dtype=float)
            P = _softmax(((f - mu) / sd) @ W + b)[0]
            j = int(P.argmax())
            if P[j] >= a.min_conf:
                auto.setdefault(row["prompt"], {})[resp] = CLASSES[j]
                n_new += 1
            else:
                unsure.append({"prompt": row["prompt"], "response": resp,
                               "best": CLASSES[j], "conf": float(P[j])})
    Path(a.out).write_text(json.dumps(auto, ensure_ascii=False))
    if a.unsure:
        Path(a.unsure).write_text(
            "\n".join(json.dumps(u, ensure_ascii=False) for u in unsure) + "\n")
    print(f"\n  auto-labelled {n_new} at conf >= {a.min_conf}   -> {a.out}")
    print(f"  {len(unsure)} below the floor, still yours"
          + (f" -> {a.unsure}" if a.unsure else ""))
    print(f"\n  label those with:  python -m reedus_companion.train.dpo_review "
          f"--in {a.inp} --labels {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
