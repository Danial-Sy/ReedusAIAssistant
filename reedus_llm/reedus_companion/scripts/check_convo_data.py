#!/usr/bin/env python3
"""data quality gate for reedus_convo.jsonl."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from reedus_companion import config          # noqa: E402

MIN_MSGS = 6
Q_RANGE = (55, 70)          # % of assistant replies containing a question
LATE_Q_MIN = 55             # % for replies at message 7+
LEN_RANGE = (15, 30)        # mean words per assistant reply
EXCL_MIN = 5                # % of replies containing "!"

_ADMITS = re.compile(
    r"(don'?t know|haven'?t (heard|played|seen|watched)|never heard|couldn'?t tell you|"
    r"not going to pretend|no idea)", re.I)
_DEAD_OPEN = re.compile(r"^(that'?s (awesome|great|cool|nice)|sounds (good|great|fun))", re.I)
_AI_CADENCE = re.compile(r"(not just \w+.{0,12}you'?re|and hey\s*[—-]|isn'?t just .{0,20}it'?s)", re.I)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(config.CONVO_SEEDS_JSONL))
    args = ap.parse_args()
    path = Path(args.file)
    if not path.exists():
        print(f"missing: {path}")
        return 1

    convos = [json.loads(l)["messages"] for l in path.read_text().splitlines() if l.strip()]
    if not convos:
        print("empty file")
        return 1

    fails, warns = [], []

    short = [i for i, c in enumerate(convos) if len(c) < MIN_MSGS]
    if short:
        fails.append(f"{len(short)} conversation(s) under {MIN_MSGS} messages (rows {short[:8]}) "
                     f"- these belong in reedus_voice.jsonl")

    asst = [m["content"] for c in convos for m in c if m["role"] == "assistant"]
    late = [c[i]["content"] for c in convos for i in range(6, len(c))
            if c[i]["role"] == "assistant"]

    qr = 100 * sum("?" in a for a in asst) / len(asst)
    lqr = 100 * sum("?" in a for a in late) / max(1, len(late))
    mean_len = sum(len(a.split()) for a in asst) / len(asst)

    if not Q_RANGE[0] <= qr <= Q_RANGE[1]:
        (fails if qr > 80 or qr < 40 else warns).append(
            f"question rate {qr:.0f}% outside {Q_RANGE[0]}-{Q_RANGE[1]}% "
            f"({'interrogates' if qr > Q_RANGE[1] else 'too passive'})")
    if lqr < LATE_Q_MIN:
        fails.append(f"LATE question rate {lqr:.0f}% below {LATE_Q_MIN}%, this is the metric "
                     f"the whole retrain is for")
    if not LEN_RANGE[0] <= mean_len <= LEN_RANGE[1]:
        warns.append(f"mean reply {mean_len:.0f} words outside {LEN_RANGE[0]}-{LEN_RANGE[1]}")

    admits = [a for a in asst if _ADMITS.search(a)]
    admits_dead = [a for a in admits if "?" not in a and "<tool_call>" not in a]
    if admits_dead:
        fails.append(f"{len(admits_dead)} reply(s) admit ignorance and then STOP, no question, "
                     f"no lookup. That is literally the v2 dead end: {admits_dead[0][:70]!r}")

    dead = [a for a in asst if _DEAD_OPEN.match(a.strip())]
    if dead:
        warns.append(f"{len(dead)} reply(s) open with bare validation: {dead[0][:60]!r}")
    ai = [a for a in asst if _AI_CADENCE.search(a)]
    if ai:
        warns.append(f"{len(ai)} reply(s) use AI cadence: {ai[0][:70]!r}")

    # energy: the first seed set was 1% exclamations vs 18% deadpan, so the fine-tune learned my
    # register and lost the base model's enthusiasm. nothing measured it then, this does now.
    excl = 100 * sum("!" in a for a in asst) / len(asst)
    if excl < EXCL_MIN:
        warns.append(f"only {excl:.0f}% of replies contain '!' (want >={EXCL_MIN}%), this is "
                     f"how v3 came out flat. Enthusiasm has to be IN the data.")

    tools = sum("<tool_call>" in a for a in asst)

    print(f"\n{path.name}: {len(convos)} conversations, {len(asst)} assistant turns")
    print(f"  msgs/convo        {min(len(c) for c in convos)}-{max(len(c) for c in convos)} "
          f"(mean {sum(len(c) for c in convos)/len(convos):.1f})")
    print(f"  question rate     {qr:5.0f}%   target {Q_RANGE[0]}-{Q_RANGE[1]}%")
    print(f"  LATE question     {lqr:5.0f}%   target >={LATE_Q_MIN}%   ({len(late)} late turns)")
    print(f"  mean reply len    {mean_len:5.0f} words")
    print(f"  admits ignorance  {len(admits):5d}   all followed by a question or lookup: "
          f"{not admits_dead}")
    print(f"  energy ('!')      {excl:5.0f}%   want >={EXCL_MIN}%")
    print(f"  tool-call turns   {tools:5d}   ({100*tools/len(asst):.0f}%, raise this)")

    for w in warns:
        print(f"\n  WARN  {w}")
    for f in fails:
        print(f"\n  FAIL  {f}")

    if fails:
        print(f"\n{len(fails)} blocking issue(s). Fix before blending.")
        return 1
    print("\nOK to blend." + ("  (warnings above are worth a look)" if warns else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
