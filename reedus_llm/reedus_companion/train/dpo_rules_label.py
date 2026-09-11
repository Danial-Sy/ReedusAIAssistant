#!/usr/bin/env python3
"""high-confidence rules auto-label for DPO samples."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

_HEDGE = re.compile(
    r"\b(i'?m not sure|not entirely sure|not certain|i don'?t (?:really )?"
    r"(?:know|recall|remember)|can'?t (?:say|recall|remember)|hard to say|"
    r"no idea|couldn'?t tell you|i'?ll be honest,? i|don'?t quote me|"
    r"i don'?t have|i can'?t really say|not too familiar|off the top of my head|"
    r"i'?d have to (?:check|look)|if i remember|correct me if|i could be wrong|"
    r"don'?t actually remember|haven'?t (?:seen|watched)|wasn'?t (?:clear|aware)|"
    r"don'?t remember the details|fuzzy)\b", re.I)

_GENERIC = re.compile(
    r"\b(sidestep|backdash|forward smash|strong attack|quick dash|combo string|"
    r"heavy attack|light attack|dash attack|spinning kick|final kick)\b", re.I)

_OWN = re.compile(
    r"\b([A-Z][A-Za-zÀ-ÿ'’\-]{2,})(?:'s|’s)\s+([A-Z][A-Za-zÀ-ÿ'’\-]{2,}(?:\s+[A-Z][A-Za-zÀ-ÿ'’\-]{2,}){0,3})")


def _known(ix, name: str) -> bool:
    return bool(ix.has_topic(name) or ix.has_topic_prefix(name))


def _on_topic(prompt: str, resp: str) -> bool:
    p = set(re.findall(r"[a-z']{4,}", (prompt or "").lower()))
    r = set(re.findall(r"[a-z']{4,}", (resp or "").lower()))
    if not p:
        return True
    return (len(p & r) / len(p)) >= 0.12


def _ownership_claim(resp: str, ix) -> bool:
    """The reply asserts 'X's Y' and Y looks ownable, a misattribution risk."""
    for m in _OWN.finditer(resp or ""):
        owned = m.group(2).strip()
        try:
            if ix.is_ownable(owned) or _known(ix, owned):
                return True
        except Exception:                                        # noqa: BLE001
            if _known(ix, owned):
                return True
    return False


def classify(resp: str, prompt: str, ix) -> Tuple[Optional[str], float, str, dict]:
    from reedus_companion.scripts.eval_grounding import proper_nouns

    text = (resp or "").strip()
    words = text.split()
    names = proper_nouns(text)
    known = [n for n in names if _known(ix, n)]
    unknown = [n for n in names if n not in known]
    hedges = _HEDGE.findall(text)
    generics = _GENERIC.findall(text)
    ev = {
        "names": names, "known": known, "unknown": unknown, "referents": known,
        "hedges": len(hedges), "words": len(words), "generics": len(generics),
        "provisional": None,
    }

    if not text or len(words) < 4:
        return "skip", 0.95, "empty_or_truncated", ev
    if text.endswith(("...", "…")) and len(words) < 25:
        return "skip", 0.85, "truncated_ellipsis", ev

    if hedges and len(unknown) == 0 and len(words) <= 70:
        return "hedged", 0.9, "hedge_phrases", ev
    if hedges and len(known) + len(unknown) <= 2 and len(words) <= 50:
        return "hedged", 0.8, "hedge_phrases_short", ev

    multi_unk = [n for n in unknown if len(n.split()) >= 2]
    if len(unknown) >= 2:
        return "invented", 0.9, "two_plus_unknown_names", ev
    if len(multi_unk) >= 1 and not known and len(words) >= 12:
        return "invented", 0.8, "lone_multiword_unknown", ev

    if not names and not hedges and len(words) >= 8:
        return "skip", 0.75, "claimless", ev

    if _ownership_claim(text, ix):
        ev["provisional"] = "grounded" if not unknown else "invented"
        return None, 0.35, "ownership_claim_needs_human", ev

    if (known and not unknown and _on_topic(prompt, text)
            and len(known) >= 1 and len(words) >= 10 and not hedges):
        conf = 0.7 if len(known) >= 2 else 0.55
        return "grounded", conf, "all_referents_resolve", ev

    if generics and not unknown and len(known) <= 1:
        ev["provisional"] = "pseudo"
        return None, 0.45, "generic_actions_maybe_pseudo", ev
    if unknown and known:
        ev["provisional"] = "invented"
        return None, 0.35, "mixed_known_unknown_needs_human", ev

    ev["provisional"] = "skip"
    return None, 0.3, "unclear", ev


def opening_4grams(texts: List[str]) -> List[Tuple[str, int]]:
    c: Counter = Counter()
    for t in texts:
        toks = re.findall(r"[a-z0-9']+", (t or "").lower())
        if len(toks) >= 4:
            c[" ".join(toks[:4])] += 1
    return c.most_common(5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="dpo_labels.json")
    ap.add_argument("--queue", default="dpo_human_queue.jsonl")
    ap.add_argument("--meta", default="dpo_label_meta.json",
                    help="per-response confidence + referents (g candidates especially)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--min-g-frac", type=float, default=0.15)
    ap.add_argument("--fail-under-gate", action="store_true")
    a = ap.parse_args()

    from reedus_companion.knowledge.entity_index import EntityIndex
    ix = EntityIndex()
    ix.db

    rows = [json.loads(l) for l in Path(a.inp).read_text().splitlines() if l.strip()]
    labels: Dict[str, Dict[str, str]] = {}
    if a.merge and Path(a.out).exists():
        labels = json.loads(Path(a.out).read_text())

    auto_counts: Counter = Counter()
    prov_counts: Counter = Counter()
    queue: List[dict] = []
    meta: Dict[str, Dict[str, dict]] = {}
    n_resp = 0
    n_auto = 0
    all_resps: List[str] = []
    domains: Counter = Counter()
    authored = logged = 0

    for row in rows:
        prompt = row["prompt"]
        domain = row.get("domain") or ""
        if domain:
            domains[domain] += 1
        src = row.get("source") or ("authored" if domain else "logged")
        if src == "authored":
            authored += 1
        else:
            logged += 1
        for resp in row.get("responses", []):
            n_resp += 1
            all_resps.append(resp)
            if labels.get(prompt, {}).get(resp):
                auto_counts[labels[prompt][resp]] += 1
                prov_counts[labels[prompt][resp]] += 1
                continue
            lbl, conf, reason, ev = classify(resp, prompt, ix)
            prov = lbl or ev.get("provisional") or "skip"
            prov_counts[prov] += 1
            meta.setdefault(prompt, {})[resp] = {
                "label": lbl, "provisional": prov, "conf": conf, "reason": reason,
                "referents": ev.get("referents", []), "unknown": ev.get("unknown", []),
            }
            if lbl is not None:
                labels.setdefault(prompt, {})[resp] = lbl
                auto_counts[lbl] += 1
                n_auto += 1
                if lbl == "grounded":
                    queue.append({
                        "prompt": prompt, "response": resp, "best": "grounded",
                        "conf": conf, "reason": reason,
                        "referents": ev.get("referents", []),
                        "confirm": True,
                    })
            else:
                queue.append({
                    "prompt": prompt, "response": resp, "best": prov,
                    "conf": conf, "reason": reason,
                    "unknown": ev.get("unknown", []), "known": ev.get("known", []),
                })

    queue.sort(key=lambda u: (0 if u.get("confirm") else 1, u["conf"]))

    out_path = Path(a.out)
    if out_path.exists() and not a.merge:
        bak = out_path.with_suffix(".json.regen.bak")
        if bak.exists():
            from datetime import datetime
            bak = out_path.with_suffix(
                f".json.regen.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        bak.write_text(out_path.read_text())
        print(f"  backed up existing {out_path.name} -> {bak.name}")

    out_path.write_text(json.dumps(labels, ensure_ascii=False))
    Path(a.queue).write_text(
        "\n".join(json.dumps(u, ensure_ascii=False) for u in queue) + ("\n" if queue else ""))
    Path(a.meta).write_text(json.dumps(meta, ensure_ascii=False))

    g = prov_counts.get("grounded", 0)
    h = prov_counts.get("hedged", 0)
    s = prov_counts.get("skip", 0)
    g_frac = g / n_resp if n_resp else 0.0
    h_frac = h / n_resp if n_resp else 0.0
    top4 = opening_4grams(all_resps)
    worst4 = (top4[0][1] / n_resp) if (top4 and n_resp) else 0.0

    print(f"\n  responses            {n_resp}")
    print(f"  prompts              {len(rows)}  authored={authored}  logged={logged}")
    print(f"  topic spread         {dict(domains) if domains else '(none on rows, set domain)'}")
    print(f"  auto-committed       {n_auto}  {dict(auto_counts)}")
    print(f"  human queue          {len(queue)} -> {a.queue}")
    print(f"  provisional+auto     {dict(prov_counts)}")
    print(f"  grounded frac        {g_frac:.1%}  (gate ~{a.min_g_frac:.0%})")
    print(f"  hedged frac          {h_frac:.1%}  (flag if >40% or ~0)")
    print(f"  top opening 4-grams:")
    for gram, cnt in top4:
        print(f"    {cnt:4d}  ({cnt/n_resp:5.1%})  {gram!r}")
    print(f"  labels -> {out_path}")
    print(f"  meta   -> {a.meta}")

    fail = False
    if a.report:
        print("\n  smoke / ship gates:")
        if g_frac < a.min_g_frac:
            print(f"    FAIL  g candidates under {a.min_g_frac:.0%}, STOP and send to Danial.")
            fail = True
        else:
            print(f"    ok    g at {g_frac:.1%}")
        if h == 0:
            print("    FAIL  h near zero, no A>C arm.")
            fail = True
        elif h_frac > 0.40:
            print(f"    FAIL  h at {h_frac:.1%} (>40%), stereotyped/manufactured C arm.")
            fail = True
        else:
            print(f"    ok    h = {h} ({h_frac:.1%})")
        if worst4 > 0.10:
            print(f"    FAIL  opening 4-gram {top4[0][0]!r} at {worst4:.1%} (>10%).")
            fail = True
        else:
            print("    ok    no opening 4-gram above 10%")
        if s == max(prov_counts.values() or [0]) and s > 0:
            print("    FAIL  s is the largest bucket, prompts aren't inviting claims.")
            fail = True
        else:
            print("    ok    s is not the largest bucket")

    if a.fail_under_gate and fail:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
