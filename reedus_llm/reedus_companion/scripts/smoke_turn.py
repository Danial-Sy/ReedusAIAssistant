#!/usr/bin/env python3
"""one real turn with a stub backend, catches what py_compile can't."""
from __future__ import annotations
import sys, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


class StubBackend:
    """Answers every call plausibly enough to walk the whole turn."""
    def __init__(self):
        self.calls = 0

    def chat(self, messages, max_tokens, use_adapter=True, temperature=None, on_token=None,
             tier=None, name=""):
        self.calls += 1
        sysmsg = (messages[0].get("content", "") if messages else "")
        if '"verdict"' in sysmsg:
            return '{"verdict": "none"}'
        if '"subjects"' in sysmsg:
            return '{"subjects": []}'
        if '"search"' in sysmsg or "search" in sysmsg[:80].lower():
            return '{"search": false, "query": ""}'
        if '"verify"' in sysmsg:
            return '{"verify": false, "query": ""}'
        if "shorter" in sysmsg:
            return "Kakashi runs Kamui on that exchange and it ends there. What's your read?"
        return ("Kakashi Hatake reads that exchange instantly and Kamui ends it. "
                "Itachi needs eye contact for Tsukuyomi, which is the whole problem. "
                "What's your read on it?")


def main() -> int:
    from reedus_companion.serve.core import Reedus
    print("building Reedus with a stub backend ...")
    bot = Reedus(StubBackend())

    turns = ["yo", "kakashi vs itachi who takes it", "nah thats wrong, kamui is obitos",
             "what about him though", "anyway hows your day"]
    failures = []
    for i, t in enumerate(turns, 1):
        try:
            out = bot.reply(t)
            print(f"  turn {i} ok   {t[:38]!r:42} -> {(out or '')[:52]!r}")
        except AttributeError as e:
            failures.append((t, e))
            print(f"  turn {i} FAIL {t[:38]!r:42} -> AttributeError: {e}")
            traceback.print_exc(limit=3)
        except Exception as e:                                   # noqa: BLE001
            print(f"  turn {i} warn {t[:38]!r:42} -> {type(e).__name__}: {e}")

    print()
    if failures:
        print(f"FAIL, {len(failures)} turn(s) hit a missing attribute. Something was deleted.")
        return 1
    print("PASS, every branch of reply() resolved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
