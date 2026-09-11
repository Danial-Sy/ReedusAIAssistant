"""Multi-scenario corpus + search-router smoke eval."""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("REEDUS_PROFILE", "1")
os.environ.setdefault("REEDUS_DEBUG", "1")

SCENARIOS = [
    {
        "name": "A_anime_watch_not_game",
        "goal": "Watching Naruto anime must NOT sticky-steer to Storm 4 game; no web.",
        "turns": [
            "yo",
            "yeah i been watching naruto its so fun",
            "the five kage summit arc is SO good",
        ],
    },
    {
        "name": "B_ability_no_web_learn",
        "goal": "Sage mode / Amaterasu talk must skip topic-brief web learn.",
        "turns": [
            "been watching naruto shippuden",
            "the five kage summit arc was crazy",
            ("literally nothing man like hes got nothing on it! sage mode naruto "
             "might have a chance but idk if he could get over amaterasu"),
        ],
    },
    {
        "name": "C_sticky_same_subject",
        "goal": "Same-subject follow-ups reuse sticky knowledge; no re-retrieve churn.",
        "turns": [
            "been playing naruto ultimate ninja storm 4",
            "the five kage summit arc was crazy",
            "sasuke was cold as hell in that arc",
            "what about madara though",
        ],
    },
    {
        "name": "D_live_weather_search",
        "goal": "Live weather MUST web-search (extract ok); corpus skipped.",
        "turns": [
            "hey",
            "whats the weather in woodbridge va today",
        ],
    },
    {
        "name": "E_score_search",
        "goal": "Sports score ask MUST route to search.",
        "turns": [
            "yo",
            "who won the world cup final",
        ],
    },
    {
        "name": "F_new_game_learn_ok",
        "goal": "Brand-new titled engagement may topic-route; prefer corpus if held.",
        "turns": [
            "whats up",
            "been playing elden ring the bosses are insane",
            "malenia is so hard man",
        ],
    },
]


def _summarize_calls(calls: list[dict]) -> dict:
    names = [c.get("name") or "?" for c in calls]
    return {
        "n": len(calls),
        "names": names,
        "has_topic_brief": "topic-brief" in names,
        "has_extract": "extract" in names,
        "has_search_router": "search-router" in names,
        "has_knowledge_brief": "knowledge-brief" in names,
        "has_subjects": "subjects" in names,
        "persona_s": sum(c["wall_s"] for c in calls if c.get("name") == "persona"),
        "total_model_s": sum(c["wall_s"] for c in calls),
    }


def main() -> int:
    from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
    from reedus_companion.serve import core as coremod

    gguf = os.environ.get(
        "REEDUS_GGUF",
        "reedus_companion/artifacts/gguf/base-qwen3-4b-Q4_K_M.gguf")
    router = os.environ.get(
        "REEDUS_ROUTER_GGUF",
        "reedus_companion/artifacts/gguf/qwen3-1.7b-Q4_K_M.gguf")

    t0 = time.perf_counter()
    backend = LlamaCppBackend(gguf_path=gguf, router_gguf=router)
    reedus = coremod.Reedus(backend)
    warm = reedus.warm()
    print(f"\nloaded+warm in {time.perf_counter() - t0:.1f}s (warm={warm:.1f}s)\n")

    report: list[dict] = []

    for sc in SCENARIOS:
        print("=" * 72)
        print(f"SCENARIO {sc['name']}: {sc['goal']}")
        print("=" * 72)
        # fresh conversation state between scenarios
        reedus.history.clear()
        reedus._sticky_knowledge = ""
        reedus._sticky_subject = ""
        reedus._sticky_weak = False
        reedus._session_subject = ""
        reedus._last_anchor = ""
        reedus.last_search_query = None
        reedus.last_fact = None
        reedus._learned_topic = ""
        kb = getattr(reedus, "_knowledge", None)
        if kb not in (None, False) and hasattr(kb, "last_topics"):
            kb.last_topics = []

        for i, msg in enumerate(sc["turns"], 1):
            LlamaCppBackend.PROFILE.clear()
            t = time.perf_counter()
            # capture path signals
            before_topics = dict(reedus.topics)
            try:
                answer = reedus.reply(msg)
            except Exception as e:  # noqa: BLE001
                answer = f"<ERROR {type(e).__name__}: {e}>"
            wall = time.perf_counter() - t
            calls = list(LlamaCppBackend.PROFILE)
            summ = _summarize_calls(calls)
            new_topics = [k for k in reedus.topics if k not in before_topics]
            row = {
                "scenario": sc["name"],
                "turn": i,
                "msg": msg,
                "wall_s": wall,
                "reply": (answer or "")[:160],
                "skip_corpus": bool(getattr(reedus, "_skip_corpus", False)),
                "sticky_subject": getattr(reedus, "_sticky_subject", ""),
                "session_subject": getattr(reedus, "_session_subject", ""),
                "learned_topic": getattr(reedus, "_learned_topic", ""),
                "last_anchor": getattr(reedus, "_last_anchor", ""),
                "knowledge_weak": bool(getattr(reedus, "_knowledge_weak", False)),
                "new_topics": new_topics,
                "last_fact": (reedus.last_fact or "")[:80],
                **summ,
            }
            report.append(row)
            print(f"\n  turn {i}: {msg!r}")
            print(f"    wall={wall:.2f}s  persona={summ['persona_s']:.2f}s  "
                  f"calls={summ['names']}")
            print(f"    skip_corpus={row['skip_corpus']}  weak={row['knowledge_weak']}  "
                  f"session={row['session_subject']!r}  sticky={row['sticky_subject']!r}  "
                  f"learned={row['learned_topic']!r}")
            print(f"    anchor={row['last_anchor']!r}  new_topics={new_topics}")
            if row["last_fact"]:
                print(f"    last_fact={row['last_fact']!r}")
            print(f"    reply={row['reply']!r}")

        print()

    # verdicts
    print("\n" + "#" * 72)
    print("VERDICTS")
    print("#" * 72)

    def rows(name):
        return [r for r in report if r["scenario"] == name]

    fails: list[str] = []

    a = rows("A_anime_watch_not_game")
    if any(r["has_topic_brief"] or r["has_extract"] for r in a):
        fails.append("A: unexpected topic-brief/extract on anime-watch convo")
    if any("storm" in (r["sticky_subject"] or "") for r in a[1:]):
        fails.append("A: sticky subject looks like Storm game during anime watch")

    # B: the long ability turn must not topic-brief
    b = rows("B_ability_no_web_learn")
    ability = b[-1] if b else None
    if ability and ability["has_topic_brief"]:
        fails.append("B: ability turn paid topic-brief (should corpus-resolve / skip cue)")
    if ability and ability["has_extract"]:
        fails.append("B: ability turn paid extract/web")

    c = rows("C_sticky_same_subject")
    if len(c) >= 3 and c[2]["has_knowledge_brief"]:
        fails.append("C: turn3 (sasuke) still ran knowledge-brief, session lock missed")
    if len(c) >= 4 and c[3]["has_knowledge_brief"]:
        fails.append("C: turn4 (madara) still ran knowledge-brief, session lock missed")
    if len(c) >= 4 and c[3].get("has_subjects"):
        fails.append("C: turn4 (madara) paid subjects call, should sticky-reuse")
    if any(r["has_extract"] for r in c):
        fails.append("C: fandom sticky convo hit extract/web")

    # D: weather must search
    d = rows("D_live_weather_search")
    weather = d[-1] if d else None
    if weather and not (weather["has_extract"] or weather["last_fact"] or weather["skip_corpus"]):
        fails.append("D: weather turn did not take live search path")

    # E: world cup must search-router or extract
    e = rows("E_score_search")
    score = e[-1] if e else None
    if score and not (score["has_search_router"] or score["has_extract"] or score["last_fact"]):
        fails.append("E: score turn did not search")

    # F: elden ring, no hard fail if corpus held, must not explode
    f = rows("F_new_game_learn_ok")
    if any(r["wall_s"] > 45 for r in f):
        fails.append("F: a turn exceeded 45s")

    for sc in SCENARIOS:
        rs = rows(sc["name"])
        total = sum(r["wall_s"] for r in rs)
        print(f"\n{sc['name']}: {len(rs)} turns, {total:.1f}s total, "
              f"{total / max(len(rs), 1):.1f}s/turn")
        for r in rs:
            flags = []
            if r["has_topic_brief"]:
                flags.append("TOPIC_BRIEF")
            if r["has_extract"]:
                flags.append("EXTRACT")
            if r["has_search_router"]:
                flags.append("SEARCH_ROUTER")
            if r["has_knowledge_brief"]:
                flags.append("KNOW_BRIEF")
            if r["skip_corpus"]:
                flags.append("SKIP_CORPUS")
            if r["sticky_subject"] and r["turn"] > 1:
                flags.append(f"sticky={r['sticky_subject'][:40]}")
            print(f"  t{r['turn']} {r['wall_s']:5.1f}s  "
                  f"{','.join(flags) or 'persona/corpus'}  | {r['msg'][:60]!r}")

    print()
    if fails:
        print(f"{len(fails)} FAIL:")
        for fmsg in fails:
            print(f"  - {fmsg}")
        reedus.close()
        return 1
    print("ALL SCENARIO CHECKS PASSED (see notes above for soft observations)")
    reedus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
