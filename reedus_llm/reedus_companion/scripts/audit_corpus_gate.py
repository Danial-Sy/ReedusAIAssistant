"""Mechanical audit of the corpus-retrieval decision. No models, no corpus, no network."""
from __future__ import annotations

import ast
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, "..", "serve", "core.py")
MODE = os.path.join(HERE, "..", "mode.py")


def _install_mode_stub() -> None:
    """Make `from reedus_companion import mode` work without importing the package."""
    import types
    spec = importlib.util.spec_from_file_location("reedus_companion.mode", MODE)
    mode = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mode)
    pkg = sys.modules.setdefault("reedus_companion", types.ModuleType("reedus_companion"))
    pkg.mode = mode
    sys.modules["reedus_companion.mode"] = mode


def load_predicates(path: str = CORE) -> dict:
    """Module-level names from core.py, without importing the package."""
    tree = ast.parse(open(path).read())
    keep = [
        n for n in tree.body
        if not (isinstance(n, ast.ImportFrom) and (n.module or "").startswith("reedus_companion"))
        and not isinstance(n, ast.ClassDef)
    ]
    ns: dict = {"__name__": "core_shim"}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "core_shim", "exec"), ns)
    return ns


def decide(g: dict, msg: str) -> tuple:
    """(verdict, reason, tiebreaker, runtime_action) for one message."""
    reason = g["_router_skip_reason"](msg)
    if reason == g["RUN"]:
        why = "router-no"
    else:
        why = reason
    decision = {"search": False, "query": "", "why": why}
    conv_turn = g["_is_personal_turn"](msg) or g["_is_checkin"](msg)
    verdict = g["_corpus_verdict"](decision, date_q=False, looked=False, look_failed=False,
                                   conv_turn=conv_turn, weather_fact=False, user_msg=msg)
    tiebreak = g["_corpus_has_target"](msg)
    if verdict == g["CORPUS_NO"] or g["_is_contentless"](msg):
        action = "skip"
    elif verdict == g["CORPUS_YES"]:
        action = "retrieve"
    else:
        action = "retrieve" if tiebreak else "probe"
    return verdict, reason, tiebreak, action


CHIT = [
    "Come on, dude.",
    "What's two plus two, bro?",
    "I'm not catching up. Just want to know what two plus two.",
    "what's 17 times 23",
    "can you work out 144 divided by 12",
    "Nothing much. Tell me what 2 plus 2 is real quick.",
    "I'm just asking what 2 plus 2 is.",
    "I use an push, man",
    "Oh, for sure... I love the new guns they added.",
    "Enable Cloud Mode.",
    "switch to cloud mode",
    "hellloooo",
    "i had a rough day at work",
    "thats insane bro",
    "nothing much, just chilling",
    "how was your day man",
    "the pressure you can put on people is insane",
    "yo my bud whats goin on",
]

CORPUS = [
    "who's stronger, Shikamaru or Neji",
    "been playing naruto ultimate ninja storm 4",
    "what about madara though",
    "the five kage summit arc was crazy",
    "is bg3 good",
    "whats sage mode",
    "tell me about the valley of the end fight",
    "i been watching naruto its so fun",
    "you remember when Naruto and Sasuke died?",
    "dont look it up, just tell me who mangekyou belongs to",
]


def main() -> int:
    _install_mode_stub()
    g = load_predicates()
    bad = probes = 0
    for label, msgs, forbidden in (("CHIT-CHAT, must not retrieve", CHIT, "retrieve"),
                                   ("CORPUS, must not be skipped", CORPUS, "skip")):
        print(f"\n=== {label}")
        for m in msgs:
            verdict, reason, tiebreak, action = decide(g, m)
            flag = ""
            if action == forbidden:
                flag = "  <-- WRONG"
                bad += 1
            elif action == "probe":
                probes += 1
            print(f"  {m[:44]:46} {action:9} verdict={verdict:6} {reason:18} "
                  f"tiebreak={tiebreak}{flag}")
    print(f"\n{bad} wrong, {probes} deferred to the runtime probe.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
