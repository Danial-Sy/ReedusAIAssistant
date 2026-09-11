#!/usr/bin/env python3
"""whisper doesn't know the name Reedus. these lock the rewrite, not the model."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reedus_companion.voice.stt import canon_name  # noqa: E402

_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


print("[1/2] Whisper's usual Reedus misses become Reedus")
for raw, want in [
    ("Hey breeders, what's going on?", "Hey Reedus, what's going on?"),
    ("yo breeder", "yo Reedus"),
    ("reed us what's up", "Reedus what's up"),
    ("Hey Reedus", "Hey Reedus"),
    ("hey readers what's good", "hey Reedus what's good"),
    ("Readers", "Reedus"),
    ("BREEDERS", "REEDUS"),
]:
    got = canon_name(raw)
    check(got == want, f"{raw!r} -> {got!r} (want {want!r})")

print("\n[2/2] real words that are not a vocative stay put")
for raw in [
    "I've been talking to some readers about the book",
    "the turning radius is tight",
    "been watching Naruto. It's so fun",
]:
    check(canon_name(raw) == raw, f"unchanged: {raw!r}")

print("\n[3/3] Whisper Naruto mangling")
check(canon_name("I meant Naurruto, like the show") == "I meant Naruto, like the show",
      "Naurruto → Naruto")
check(canon_name("been watching norruto") == "been watching Naruto",
      "norruto → Naruto")

print("\n" + ("ALL PASS" if not _fails else f"{len(_fails)} FAILED"))
sys.exit(0 if not _fails else 1)
