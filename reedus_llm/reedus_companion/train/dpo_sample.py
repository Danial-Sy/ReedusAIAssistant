#!/usr/bin/env python3
"""sample A/B/C candidate replies from the base model for DPO pairs."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

HISTORY_TURNS = 6
MIN_USER_WORDS = 4

HEDGE_CONSTRAINTS = (
    "\n\n(Reply constraint for this turn only: admit you don't remember the specifics, "
    "then pivot to a related take. No technique/scene names. 2 short sentences.)",
    "\n\n(Reply constraint for this turn only: say you're not sure, then ask a real "
    "question. Avoid naming moves or plot points. Keep it short.)",
    "\n\n(Reply constraint for this turn only: partial recall only, you remember the "
    "vibe, not the details. Say so. No proper names of abilities. Ask what stood out.)",
    "\n\n(Reply constraint for this turn only: admit the gap and offer to look it up / "
    "be told. Warm, short, no invented specifics.)",
)


def load_prompts(db: Path, limit: Optional[int] = None) -> List[dict]:
    """Real user turns with their preceding context, newest first."""
    con = sqlite3.connect(str(db))
    rows = list(con.execute("SELECT id, ts, role, text FROM turns ORDER BY id"))
    con.close()
    out = []
    for i, (_id, _ts, role, text) in enumerate(rows):
        if role != "user" or not text:
            continue
        if len(text.split()) < MIN_USER_WORDS:
            continue
        hist = []
        for r, t in ((r[2], r[3]) for r in rows[max(0, i - HISTORY_TURNS):i]):
            if t:
                hist.append({"role": r, "content": t})
        if len(hist) < 2:
            continue
        out.append({"user": text, "history": hist, "source": "logged"})
    out.reverse()
    return out[:limit] if limit else out


def _topic_names() -> List[str]:
    from reedus_companion import config
    p = Path(config.TOPICS_PATH)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return list(data.keys()) if isinstance(data, dict) else []
    except Exception:                                            # noqa: BLE001
        return []


def _retrieval_query(rec: dict, topic_names: Optional[List[str]] = None) -> tuple:
    """Approximates serve/core.py:_retrieval_query, content words not a transcript mash."""
    from reedus_companion.serve.core import _TOPIC_FILLER, _content_words

    user = rec["user"]
    prev_users = [m["content"] for m in rec["history"] if m.get("role") == "user"]
    prev_msg = prev_users[-1] if prev_users else ""
    haystack = (" ".join(prev_users) + " " + user).lower()
    haystack_words = set(re.findall(r"[a-z0-9']+", haystack))

    anchor = ""
    for name in reversed(topic_names or _topic_names()):
        sig = [w for w in re.split(r"\W+", name.lower())
               if len(w) > 3 and w not in _TOPIC_FILLER]
        if sig and sum(w in haystack_words for w in sig) >= max(1, len(sig) // 2):
            anchor = name
            break

    own = _content_words(user)
    if len(own) < 3 and prev_msg:
        own = _content_words(prev_msg) + own
    parts = ([anchor] if anchor else []) + own[-24:]
    return " ".join(p for p in parts if p.strip()), anchor


def build_messages(rec: dict, kb=None, persona: str = "") -> List[dict]:
    """Serve-time prompt shape, including the weak-knowledge warning when it fires."""
    import datetime
    from reedus_companion.serve.persona import build_runtime_system

    del persona
    topics = ""
    weak = False
    if kb is not None:
        try:
            q, anchor = _retrieval_query(rec)
            topics = kb.context_for(q, context=anchor, hint=anchor) or ""
            weak = bool(getattr(kb, "last_weak", False))
        except Exception:                                        # noqa: BLE001
            pass
    today = datetime.date.today().strftime("%A, %B %d, %Y")
    sysmsg = build_runtime_system(today=today, topics=topics or None, weak_knowledge=weak)
    return ([{"role": "system", "content": sysmsg}] + rec["history"]
            + [{"role": "user", "content": rec["user"]}])


def _looks_hedged(text: str) -> bool:
    if not (text or "").strip():
        return False
    hedge = re.search(
        r"\b(i'?m not sure|not certain|i don'?t (?:really )?(?:know|recall|remember)|"
        r"can'?t (?:say|recall|remember)|hard to say|no idea|couldn'?t tell you|"
        r"not too familiar|don'?t quote me|off the top of my head|"
        r"i'?d have to (?:check|look)|i could be wrong|don'?t actually remember|"
        r"haven'?t (?:seen|watched)|wasn'?t (?:clear|aware)|fuzzy)\b", text, re.I)
    return bool(hedge) and len(text.split()) <= 70


def _sample_hedge(be, msgs: List[dict], max_tokens: int, constraint: str,
                  temp: float = 1.0) -> Optional[str]:
    hedge_msgs = list(msgs)
    last = dict(hedge_msgs[-1])
    last["content"] = (last.get("content") or "") + constraint
    hedge_msgs[-1] = last
    try:
        return be.chat(hedge_msgs, min(max_tokens, 120), temperature=temp)
    except Exception:                                            # noqa: BLE001
        return None


def opening_4grams(texts: List[str]) -> List[tuple]:
    c: Counter = Counter()
    for t in texts:
        toks = re.findall(r"[a-z0-9']+", (t or "").lower())
        if len(toks) >= 4:
            c[" ".join(toks[:4])] += 1
    return c.most_common(5)


def _load_backend(cpu: bool):
    from reedus_companion.serve.backend_hf import HFBackend
    return HFBackend(base_only=True, load_4bit=not cpu)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="dpo_samples.jsonl")
    ap.add_argument("-n", "--per-prompt", type=int, default=3,
                    help="free samples per prompt (hedge arm added ONLY if none look hedged)")
    ap.add_argument("--limit", type=int, default=60, help="max prompts")
    ap.add_argument("--temp", type=float, default=0.9)
    ap.add_argument("--cpu", action="store_true")
    ap.add_argument("--prompts-only", action="store_true")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--no-knowledge", action="store_true")
    ap.add_argument("--prompts-file", default=None,
                    help="JSONL of {domain, history, user}")
    a = ap.parse_args()

    from reedus_companion import config
    if a.prompts_file:
        recs = []
        for line in Path(a.prompts_file).read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            recs.append({"user": r["user"], "history": r.get("history", []),
                         "domain": r.get("domain", ""), "source": "authored"})
        recs = recs[:a.limit] if a.limit else recs
        print(f"\n  {len(recs)} prompts from {a.prompts_file}")
        print(f"  domains: {dict(Counter(r.get('domain', '?') for r in recs))}")
    else:
        recs = load_prompts(Path(config.EPISODIC_DB), limit=a.limit)
        print(f"\n  {len(recs)} usable prompts from {config.EPISODIC_DB}")
    if a.prompts_only:
        for r in recs[:15]:
            print(f"    ({len(r['history'])} turns) [{r.get('domain','')}]  {r['user'][:80]}")
        return 0
    if not recs:
        print("  nothing to sample, author prompts or talk to Reedus first.")
        return 1

    from reedus_companion.serve.persona import REEDUS_SYSTEM
    kb = None
    if not a.no_knowledge:
        try:
            from reedus_companion.knowledge import Knowledge
            from reedus_companion.serve.memory import Memory
            kb = Knowledge(embed=Memory().embedder.embed)
        except Exception as e:                                   # noqa: BLE001
            print(f"  (no knowledge context: {type(e).__name__}: {e})")

    print(f"  loading base model ({'CPU, Xeon conditions' if a.cpu else 'GPU 4-bit'})…")
    t0 = time.time()
    be = _load_backend(a.cpu)
    print(f"  loaded in {time.time()-t0:.1f}s")

    if a.bench:
        print(f"\n  latency over {min(5, len(recs))} prompts"
              f"{', CPU, matching the run box' if a.cpu else ', GPU, NOT the run box'}:")
        times = []
        for r in recs[:5]:
            msgs = build_messages(r, kb, REEDUS_SYSTEM)
            t = time.time()
            be.chat(msgs, config.MAX_NEW_TOKENS, temperature=0.7)
            dt = time.time() - t
            times.append(dt)
            print(f"    {dt:6.1f}s   {r['user'][:64]}")
        times.sort()
        print(f"\n    median {times[len(times)//2]:.1f}s   range {times[0]:.1f}-{times[-1]:.1f}s")
        return 0

    out_path = Path(a.out)
    n_done = 0
    n_hedge_added = 0
    hedge_i = 0
    all_resps: List[str] = []
    with out_path.open("w") as f:
        for i, r in enumerate(recs, 1):
            msgs = build_messages(r, kb, REEDUS_SYSTEM)
            responses = []
            for _ in range(a.per_prompt):
                try:
                    responses.append(be.chat(msgs, config.MAX_NEW_TOKENS, temperature=a.temp))
                except Exception as e:                           # noqa: BLE001
                    print(f"    sample failed: {type(e).__name__}: {e}")
            # C arm only where missing
            if responses and not any(_looks_hedged(x) for x in responses):
                for _try in range(len(HEDGE_CONSTRAINTS)):
                    constraint = HEDGE_CONSTRAINTS[hedge_i % len(HEDGE_CONSTRAINTS)]
                    hedge_i += 1
                    text = _sample_hedge(be, msgs, config.MAX_NEW_TOKENS, constraint,
                                         temp=0.9 + 0.15 * _try)
                    if text and _looks_hedged(text):
                        responses.append(text)
                        n_hedge_added += 1
                        break
                    if text and _try == len(HEDGE_CONSTRAINTS) - 1:
                        responses.append(text)
                        n_hedge_added += 1
            if responses:
                row = {"prompt": r["user"], "messages": msgs, "responses": responses,
                       "domain": r.get("domain", ""), "source": r.get("source", "logged")}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                all_resps.extend(responses)
                n_done += 1
            if i % 5 == 0:
                print(f"    {i}/{len(recs)} prompts, {n_done} written, "
                      f"{n_hedge_added} C-arms added")

    top4 = opening_4grams(all_resps)
    n_resp = len(all_resps)
    print(f"\n  wrote {n_done} prompts -> {out_path}")
    print(f"  C-arms added (missing only): {n_hedge_added}/{n_done}")
    print(f"  top opening 4-grams ({n_resp} responses):")
    worst = 0.0
    for gram, cnt in top4:
        frac = cnt / n_resp if n_resp else 0
        worst = max(worst, frac)
        print(f"    {cnt:4d}  ({frac:5.1%})  {gram!r}")
    if worst > 0.10:
        print("  FAIL  an opening 4-gram exceeds 10%, re-sample hedges with more variety.")
    else:
        print("  ok    no opening 4-gram above 10%")
    print(f"\n  next:  python -m reedus_companion.train.dpo_rules_label \\")
    print(f"             --in {out_path} --out dpo_labels.json --queue dpo_human_queue.jsonl "
          f"--report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
