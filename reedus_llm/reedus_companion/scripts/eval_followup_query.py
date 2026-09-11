#!/usr/bin/env python3
"""does a follow-up turn build the query it should, without the router."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from reedus_companion.serve.core import (            # noqa: E402
    _carry_subject,
    _followup_constraint,
    _is_search_followup,
    _kw_query,
    _strip_lead_reaction,
)

BUILD = [
    ("current weather in Woodbridge VA today 2026", "sick, what about at 5 pm specifically",
     ["weather", "Woodbridge", "VA", "5 pm"], ["sick", "specifically", "what about"]),
    ("current weather in Woodbridge VA today 2026", "bet, what about at 5 pm",
     ["weather", "Woodbridge", "VA", "5 pm"], ["bet"]),
    ("current weather in Woodbridge VA today 2026", "word, hows it looking tomorrow",
     ["weather", "Woodbridge", "VA", "tomorrow"], ["word"]),
    ("current weather in Woodbridge VA today 2026", "fire, what about tonight",
     ["weather", "Woodbridge", "VA", "tonight"], ["fire"]),
    ("current weather in Woodbridge VA today 2026", "zamn, what about at 9 pm",
     ["weather", "Woodbridge", "VA", "9 pm"], ["zamn"]),
    ("current weather in Woodbridge VA today 2026", "crodie, what about later",
     ["weather", "Woodbridge", "VA", "later"], ["crodie"]),
    # no comma, the pivot phrase is the fence instead
    ("current weather in Woodbridge VA today 2026", "sick what about at 5 pm",
     ["weather", "Woodbridge", "VA", "5 pm"], ["sick"]),
    ("current weather in Woodbridge VA today 2026", "what about at like 1:00 pm?",
     ["weather", "Woodbridge", "VA", "1:00"], ["like", "what about"]),
    ("current weather in Woodbridge VA today 2026", "oh wow, what temp is it at 1 pm",
     ["weather", "Woodbridge", "VA", "1 pm"], ["wow"]),
    # a month must not become the subject
    ("weather Woodbridge VA August 2026", "what about tonight",
     ["weather", "Woodbridge", "VA", "tonight"], ["August", "2026"]),
    ("Nintendo Switch 2 launch date 2026", "and what about later",
     ["Nintendo", "Switch", "launch"], []),
    ("weather Woodbridge VA at 5 pm 2026", "and at 7 pm?",
     ["weather", "Woodbridge", "VA", "7 pm"], ["5 pm"]),
]

STRIP = [
    ("sick, what about at 5 pm specifically", "what about at 5 pm specifically"),
    ("gotcha, hows the weather looking tomorrow", "hows the weather looking tomorrow"),
    ("dope, what time does the game start", "what time does the game start"),
    ("sick what about at 5 pm", "what about at 5 pm"),
    ("ayo bet, when does it come out", "when does it come out"),
]

KEEP = [
    # remainder is pure grammar -> the head was the subject
    ("sick day, what do i do", "sick day, what do i do"),
    ("gas prices, what are they", "gas prices, what are they"),
    # capitalised head -> could be a place or a title, never assume
    ("Chicago, what's the weather tomorrow", "Chicago, what's the weather tomorrow"),
    ("Woodbridge, VA weather at 5 pm", "Woodbridge, VA weather at 5 pm"),
    # digits in the head -> a comparison, not a reaction
    ("5 pm, what about tomorrow", "5 pm, what about tomorrow"),
    # no fence at all -> nothing to cut
    ("hows the weather in chicago", "hows the weather in chicago"),
    # remainder too short to stand alone
    ("nice, and tonight", "nice, and tonight"),
]

NOT_FOLLOWUP = [
    "hows the weather in chicago",
    "whats the score of the game",
    "who won the election",
    "sick day, what do i do",
    "whats the score of the game tonight",
    "who won the election earlier",
    "hows the traffic on the beltway later",
    "when does the new season drop tomorrow",
]

STILL_FOLLOWUP = [
    "what temp is it at 1 pm",
    "what about at 5 pm specifically",
    "and tonight?",
    "hows it looking later",
    "whats the humidity at 5 pm",
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    fails: list[str] = []
    npass = 0

    print("\n=== build: subject + constraint ===")
    for last, msg, want, unwanted in BUILD:
        if not _is_search_followup(msg):
            fails.append(f"[build] not detected as follow-up: {msg!r}")
            print(f"  FAIL  {msg!r}\n        not classified as a follow-up")
            continue
        built = f"{_carry_subject(last)} {_followup_constraint(msg)}".strip()
        low = _norm(built)
        missing = [w for w in want if _norm(w) not in low]
        leaked = [w for w in unwanted if _norm(w) in low]
        ok = not missing and not leaked
        npass += ok
        if not ok:
            fails.append(f"[build] {msg!r} -> {built!r} missing={missing} leaked={leaked}")
        if args.verbose or not ok:
            print(f"  {'ok  ' if ok else 'FAIL'}  {msg!r}\n        -> {built!r}"
                  + (f"\n        missing={missing} leaked={leaked}" if not ok else ""))

    print("\n=== strip: structural, not lexical ===")
    for msg, want in STRIP:
        got = _strip_lead_reaction(msg)
        ok = _norm(got) == _norm(want)
        npass += ok
        if not ok:
            fails.append(f"[strip] {msg!r} -> {got!r} (wanted {want!r})")
        if args.verbose or not ok:
            print(f"  {'ok  ' if ok else 'FAIL'}  {msg!r}\n        -> {got!r}")

    print("\n=== keep: guards hold ===")
    for msg, want in KEEP:
        got = _strip_lead_reaction(msg)
        ok = _norm(got) == _norm(want)
        npass += ok
        if not ok:
            fails.append(f"[keep] {msg!r} was mangled to {got!r}")
        if args.verbose or not ok:
            print(f"  {'ok  ' if ok else 'FAIL'}  {msg!r}\n        -> {got!r}")

    print("\n=== keep: not a follow-up ===")
    for msg in NOT_FOLLOWUP:
        ok = not _is_search_followup(msg)
        npass += ok
        if not ok:
            fails.append(f"[keep] {msg!r} wrongly classified as a follow-up")
        if args.verbose or not ok:
            print(f"  {'ok  ' if ok else 'FAIL'}  {msg!r}")

    print("\n=== detect: attribute follow-ups still caught ===")
    for msg in STILL_FOLLOWUP:
        ok = _is_search_followup(msg)
        npass += ok
        if not ok:
            fails.append(f"[detect] {msg!r} missed, would lose the carried subject")
        if args.verbose or not ok:
            print(f"  {'ok  ' if ok else 'FAIL'}  {msg!r}")

    print("\n=== kw_query fallback ===")
    for msg, banned in [("sick, what about at 5 pm specifically", "sick"),
                        ("gotcha, hows the weather tomorrow", "gotcha")]:
        got = _kw_query(msg)
        ok = banned not in _norm(got)
        npass += ok
        if not ok:
            fails.append(f"[kw_query] {msg!r} -> {got!r} still contains {banned!r}")
        if args.verbose or not ok:
            print(f"  {'ok  ' if ok else 'FAIL'}  {msg!r}\n        -> {got!r}")

    total = npass + len(fails)
    print(f"\n{npass}/{total} passed")
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
