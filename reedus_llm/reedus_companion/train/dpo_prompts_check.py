#!/usr/bin/env python3
"""Validate a generated prompt set: domain mix, duplicates, history shape."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

DOMAINS = set("""naruto onepiece bleach dragonball jujutsu-kaisen myheroacademia attackontitan
kimetsu-no-yaiba hunterxhunter jojo onepunchman chainsaw-man minecraft eldenring zelda
supersmashbros tekken streetfighter fortnite gta""".split())

MIN_HIST, MAX_HIST = 2, 6
MIN_WORDS = 4

CASUAL = re.compile(
    r"\b(yo|yeah|yea|nah|man|bro|dawg|bud|smth|yk|fr|ngl|tbh|idk|imo|peak|goated|lol|gonna|"
    r"wanna|gotta|kinda|sorta|cuz|coz|tho|prolly|ur|u)\b|\b\w+in'|[A-Z]{3,}", re.I)


def register_score(s: str) -> int:
    """How much the text looks like the user actually types."""
    score = 0
    if CASUAL.search(s):
        score += 1
    if re.search(r"\b(i|im|ive|id|dont|cant|whats|thats|hes|shes|youre)\b", s):
        score += 1
    if not s[:1].isupper():
        score += 1
    if s.count(",") + s.count(".") <= 1:
        score += 1
    return score


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    rows = []
    hard, soft = [], []

    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception as e:                                    # noqa: BLE001
            hard.append(f"line {i}: not valid JSON ({e})")
            continue
        rows.append((i, r))

    seen_users = {}
    for i, r in rows:
        u = (r.get("user") or "").strip()
        d = (r.get("domain") or "").strip()
        h = r.get("history") or []

        if d not in DOMAINS:
            hard.append(f"line {i}: domain {d!r} is not one of the corpus wikis")
        if not u:
            hard.append(f"line {i}: empty 'user'")
            continue
        if len(u.split()) < MIN_WORDS:
            hard.append(f"line {i}: final turn too short ({len(u.split())}w): {u!r}")

        if not (MIN_HIST <= len(h) <= MAX_HIST):
            hard.append(f"line {i}: history has {len(h)} turns, need {MIN_HIST}-{MAX_HIST}")
        else:
            roles = [m.get("role") for m in h]
            if roles[0] != "user" or roles[-1] != "assistant":
                hard.append(f"line {i}: history must start 'user' and end 'assistant', got "
                            f"{roles[0]}..{roles[-1]}")
            if any(roles[j] == roles[j + 1] for j in range(len(roles) - 1)):
                hard.append(f"line {i}: history roles must alternate: {roles}")
            if any(not (m.get("content") or "").strip() for m in h):
                hard.append(f"line {i}: a history turn is empty")

        if register_score(u) == 0:
            soft.append(f"line {i}: reads as clean English, not the user's register: {u!r}")

        key = re.sub(r"[^a-z ]", "", u.lower())
        key = " ".join(sorted(set(key.split())))
        if key in seen_users:
            soft.append(f"line {i}: near-duplicate of line {seen_users[key]}: {u!r}")
        else:
            seen_users[key] = i

    by_domain = Counter((r.get("domain") or "?") for _, r in rows)
    print(f"\n  {len(rows)} prompts across {len(by_domain)} domains")
    for d, n in sorted(by_domain.items()):
        flag = "  <- thin" if n < 5 else ""
        print(f"    {d:18}{n:>4}{flag}")
    missing = DOMAINS - set(by_domain) - {"naruto"}
    if missing:
        soft.append(f"no prompts for: {', '.join(sorted(missing))}")

    shapes = Counter()
    for _, r in rows:
        u = (r.get("user") or "").lower()
        if re.search(r"\b(fav|favorite|favourite|go.?to|main|you run|you play)\b", u):
            shapes["preference"] += 1
        elif re.search(r"\b(vs|versus|who (would )?win|stronger|beat)\b", u):
            shapes["matchup"] += 1
        elif re.search(r"\b(how (does|do)|whats good about|why is|what makes)\b", u):
            shapes["mechanics"] += 1
        elif re.search(r"\b(nah|thats not|actually|wrong|isnt)\b", u):
            shapes["correction"] += 1
        elif re.search(r"^(ok |so |and )?(what about|how about|and) ", u):
            shapes["followup"] += 1
        else:
            shapes["other"] += 1
    print(f"\n  shapes: {dict(shapes)}")
    if shapes and max(shapes.values()) > 0.5 * len(rows):
        soft.append("over half the prompts are one shape, spread them out")

    if soft:
        print(f"\n  {len(soft)} WARNING(S):")
        for s in soft[:20]:
            print(f"    {s}")
        if len(soft) > 20:
            print(f"    … and {len(soft)-20} more")
    if hard:
        print(f"\n  {len(hard)} ERROR(S), must fix:")
        for s in hard[:25]:
            print(f"    {s}")
        if len(hard) > 25:
            print(f"    … and {len(hard)-25} more")
        print()
        return 1

    print("\n  schema OK." + ("  Warnings above are worth a look." if soft else "  Clean.")
          + "\n\n  next:  python -m reedus_companion.train.dpo_sample \\"
          + f"\n             --prompts-file {path} --out dpo_samples_multidomain.jsonl\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
