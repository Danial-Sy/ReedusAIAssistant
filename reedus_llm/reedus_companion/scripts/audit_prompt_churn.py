"""Prompt-block churn audit: which blocks change between turns, and what that re-prefills."""
from __future__ import annotations

import argparse
import hashlib
import os


def _h(s: str) -> str:
    return hashlib.sha1((s or "").encode()).hexdigest()[:10]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=("0", "1"), default=None,
                    help="set REEDUS_PERSONA_SPLIT before loading core")
    ap.add_argument("messages", nargs="*", default=[
        "yo",
        "been playing naruto ultimate ninja storm 4",
        "the five kage summit arc was crazy",
        "sasuke was cold as hell in that arc",
        "what about madara though",
    ])
    args = ap.parse_args()
    if args.split is not None:
        os.environ["REEDUS_PERSONA_SPLIT"] = args.split

    from reedus_companion.serve import core as coremod
    from reedus_companion.serve import tools as toolmod

    class StubBackend:
        def chat(self, messages, max_tokens, use_adapter=True, temperature=None,
                 on_token=None, tier=None, name=""):
            return "yeah"

    class FakeKB:
        """A slightly different passage string every call, the pre-sticky behaviour."""

        def __init__(self):
            self.n = 0
            self.last_topics = ["Naruto: Ultimate Ninja Storm"]
            self.last_weak = False
            self.calls = 0

        def context_for(self, q, context="", hint=""):
            self.calls += 1
            self.n += 1
            return (f"[Naruto: Ultimate Ninja Storm] passage#{self.n}: "
                    f"The Five Kage Summit arc features Sasuke. query={q[:40]!r}")

    r = coremod.Reedus(StubBackend())
    r.mem.recall = lambda *_a, **_k: ""          # type: ignore[method-assign]
    r.mem.read_bulletin = lambda *_a, **_k: ""   # type: ignore[method-assign]
    kb = FakeKB()
    r._knowledge = kb
    r.topics["Naruto: Ultimate Ninja Storm"] = "a fighting game"

    prev: dict[str, str] = {}
    print(f"PERSONA_SPLIT={coremod.PERSONA_SPLIT}")
    print(f"tools block hash={_h(toolmod.render_tools_block())}")
    print()

    for i, msg in enumerate(args.messages, 1):
        r._skip_corpus = False
        r._learned_topic = "Naruto: Ultimate Ninja Storm" if i > 1 else ""
        calls_before = kb.calls
        stable, vol = r._system_parts(msg)
        msgs = r._messages(msg)
        r.history.append({"role": "user", "content": msg})
        r.history.append({"role": "assistant", "content": "yeah"})

        hist_msgs = _history_only(msgs, vol)
        blocks = {
            "stable": stable,
            "volatile": vol,
            "tools": toolmod.render_tools_block(),
            "sticky_subj": getattr(r, "_sticky_subject", ""),
            "sticky_text": getattr(r, "_sticky_knowledge", ""),
            "hist_first": hist_msgs[0]["content"] if hist_msgs else "",
        }

        print(f"=== turn {i}: {msg!r}")
        print(f"  corpus_calls={kb.calls - calls_before}  sticky_weak={r._sticky_weak}")
        for name, text in blocks.items():
            digest = _h(text)
            changed = ""
            if name in prev and prev[name] != digest:
                changed = "  CHANGED"
            elif name in prev:
                changed = "  same"
            print(f"  {name:<14} {digest}  len={len(text)}{changed}")
            prev[name] = digest
        print(f"  hist_count={len(hist_msgs)}")
        print()

    print("A.11 tools: 'tools' should stay 'same' every turn.")
    print("A.1 sticky: after subject is established, corpus_calls should be 0 and "
          "sticky_text 'same' on repeat-subject turns.")
    print("A.7 history: hist_first should stay 'same' across several turns once the "
          "window is full (chunked drop), not change every turn.")


def _history_only(msgs, vol: str):
    """History messages inside the prompt, minus system, volatile ref and current user."""
    body = msgs[1:]
    if not body:
        return []
    body = body[:-1]  # drop current user
    if vol and body:
        body = body[:-1]  # drop volatile ref message
    return body


if __name__ == "__main__":
    main()
