"""session-subject sticky lock, multi-franchise corpus probe."""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("REEDUS_PROFILE", "1")
os.environ.setdefault("REEDUS_DEBUG", "1")

SCENARIOS = [
    {
        "name": "HK_hollow_knight",
        "expect_session_substr": "hollow knight",
        "turns": [
            "been playing hollow knight man the exploration is crazy",
            "the fungal wastes are so annoying to navigate",
            "what about the radiance though",
            "hornet fights go crazy too",
        ],
    },
    {
        "name": "DS_dark_souls",
        "expect_session_substr": "dark souls",
        "turns": [
            "been playing dark souls the bosses are brutal",
            "anor londo still hits different",
            "what about artorias though",
            "ornstein and smough was rough",
        ],
    },
    {
        "name": "BB_breaking_bad",
        "expect_session_substr": "breaking bad",
        "turns": [
            "been watching breaking bad again",
            "the early seasons are so tense",
            "jesse was going through it man",
            "what about gus though",
        ],
    },
    {
        "name": "P5_persona",
        "expect_session_substr": "persona 5",
        "turns": [
            "been playing persona 5 the palaces are wild",
            "mementos grinding is a slog sometimes",
            "what about akechi though",
            "ryuji always cracks me up",
        ],
    },
    {
        "name": "OP_one_piece",
        "expect_session_substr": "one piece",
        "turns": [
            "been watching one piece lately its so long but worth it",
            "the enies lobby arc was insane",
            "luffy vs lucci goes crazy",
            "what about zoro though",
        ],
    },
    {
        "name": "PIVOT_hk_to_bb",
        "expect_session_substr": "breaking bad",  # final engagement
        "turns": [
            "been playing hollow knight",
            "the radiance fight is beautiful",
            "anyway been watching breaking bad instead",
            "jesse pinkman is something else",
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
    }


def _reset(reedus) -> None:
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
        print(f"SCENARIO {sc['name']}")
        print("=" * 72)
        _reset(reedus)

        for i, msg in enumerate(sc["turns"], 1):
            LlamaCppBackend.PROFILE.clear()
            t = time.perf_counter()
            try:
                answer = reedus.reply(msg)
            except Exception as e:  # noqa: BLE001
                answer = f"<ERROR {type(e).__name__}: {e}>"
            wall = time.perf_counter() - t
            summ = _summarize_calls(list(LlamaCppBackend.PROFILE))
            row = {
                "scenario": sc["name"],
                "turn": i,
                "msg": msg,
                "wall_s": wall,
                "reply": (answer or "")[:140],
                "session_subject": getattr(reedus, "_session_subject", ""),
                "sticky_subject": getattr(reedus, "_sticky_subject", ""),
                "last_anchor": getattr(reedus, "_last_anchor", ""),
                "learned_topic": getattr(reedus, "_learned_topic", ""),
                "skip_corpus": bool(getattr(reedus, "_skip_corpus", False)),
                **summ,
            }
            report.append(row)
            print(f"\n  turn {i}: {msg!r}")
            print(f"    wall={wall:.2f}s  persona={summ['persona_s']:.2f}s  "
                  f"calls={summ['names']}")
            print(f"    session={row['session_subject']!r}  "
                  f"sticky={row['sticky_subject']!r}")
            print(f"    anchor={row['last_anchor']!r}  learned={row['learned_topic']!r}")
            print(f"    reply={row['reply']!r}")
        print()

    print("\n" + "#" * 72)
    print("VERDICTS")
    print("#" * 72)
    fails: list[str] = []

    def rows(name: str) -> list[dict]:
        return [r for r in report if r["scenario"] == name]

    for sc in SCENARIOS:
        name = sc["name"]
        rs = rows(name)
        want = sc["expect_session_substr"].lower()
        total = sum(r["wall_s"] for r in rs)
        print(f"\n{name}: {len(rs)} turns, {total:.1f}s total, "
              f"{total / max(len(rs), 1):.1f}s/turn")

        if any(r["has_extract"] for r in rs):
            fails.append(f"{name}: unexpected extract/web")

        if name == "PIVOT_hk_to_bb":
            if len(rs) >= 2 and (rs[1]["has_knowledge_brief"] or rs[1]["has_subjects"]):
                fails.append(f"{name}: t2 (radiance) thrash after HK engage")
            if len(rs) >= 3:
                sess3 = (rs[2]["session_subject"] or "").lower()
                if "breaking bad" not in sess3:
                    fails.append(f"{name}: t3 did not retarget session to Breaking Bad "
                                 f"(got {rs[2]['session_subject']!r})")
            if len(rs) >= 4 and (rs[3]["has_knowledge_brief"] or rs[3]["has_subjects"]):
                fails.append(f"{name}: t4 (jesse) thrash after BB engage")
            # t1 may pay knowledge-brief once; t3 may too on pivot
        else:
            for r in rs[1:]:
                if r["has_knowledge_brief"]:
                    fails.append(f"{name}: t{r['turn']} ran knowledge-brief "
                                 f"(msg={r['msg'][:50]!r})")
                if r["has_subjects"]:
                    fails.append(f"{name}: t{r['turn']} paid subjects "
                                 f"(msg={r['msg'][:50]!r})")
            final_sess = (rs[-1]["session_subject"] or "").lower() if rs else ""
            if want not in final_sess:
                fails.append(f"{name}: final session missing {want!r} "
                             f"(got {rs[-1]['session_subject']!r})")
            # session should stay stable across follow-ups
            if len(rs) >= 2:
                s1 = (rs[0]["session_subject"] or "").lower()
                for r in rs[1:]:
                    s = (r["session_subject"] or "").lower()
                    if s1 and s and s != s1:
                        fails.append(f"{name}: session drifted t{r['turn']} "
                                     f"{s1!r} -> {s!r}")

        for r in rs:
            flags = []
            if r["has_knowledge_brief"]:
                flags.append("KNOW_BRIEF")
            if r["has_subjects"]:
                flags.append("SUBJECTS")
            if r["has_topic_brief"]:
                flags.append("TOPIC_BRIEF")
            if r["has_extract"]:
                flags.append("EXTRACT")
            if r["turn"] > 1 and r["sticky_subject"] and not r["has_knowledge_brief"]:
                flags.append("sticky_reuse")
            print(f"  t{r['turn']} {r['wall_s']:5.1f}s  "
                  f"{','.join(flags) or 'persona/corpus'}  | {r['msg'][:55]!r}")

        # sticky follow-ups must not crawl vs the engagement turn (weak-sticky prefill was ~15s once)
        follow = [r for r in rs[1:] if not r["has_knowledge_brief"] and not r["has_subjects"]
                  and not r["has_topic_brief"]]
        if name == "PIVOT_hk_to_bb":
            # t2 (after HK) and t4 (after BB) are the sticky follow-ups
            follow = [r for r in rs if r["turn"] in (2, 4)]
        if follow:
            avg_f = sum(r["wall_s"] for r in follow) / len(follow)
            worst = max(follow, key=lambda r: r["wall_s"])
            print(f"  latency: follow-ups avg={avg_f:.1f}s  "
                  f"worst=t{worst['turn']} {worst['wall_s']:.1f}s")
            if avg_f > 14.0:
                fails.append(f"{name}: follow-up avg {avg_f:.1f}s > 14s budget")
            if worst["wall_s"] > 16.0:
                fails.append(f"{name}: follow-up t{worst['turn']} "
                             f"{worst['wall_s']:.1f}s > 16s ceiling")

    print()
    if fails:
        print(f"{len(fails)} FAIL:")
        for fmsg in fails:
            print(f"  - {fmsg}")
        reedus.close()
        return 1
    print("ALL SESSION-STICKY CHECKS PASSED")
    reedus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
