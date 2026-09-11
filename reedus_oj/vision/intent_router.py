"""maps a request to taxonomy groups: keyword triggers first, LLM fallback, then a default group."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .config import CFG
from . import taxonomy


def _compile_trigger_index() -> Dict[str, List[tuple]]:
    index: Dict[str, List[tuple]] = {}
    for group in taxonomy.group_names():
        entries = []
        for phrase in taxonomy.triggers(group):
            # word-boundary match for single tokens, substring for phrases
            if " " in phrase:
                pat = re.compile(re.escape(phrase), re.IGNORECASE)
            else:
                pat = re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE)
            entries.append((pat, phrase))
        index[group] = entries
    return index


_TRIGGER_INDEX = _compile_trigger_index()


@dataclass
class RouteResult:
    groups: List[str]                       # ordered, best first, capped
    scores: Dict[str, int] = field(default_factory=dict)
    matched: Dict[str, List[str]] = field(default_factory=dict)  # group -> fired triggers
    method: str = "keyword"                 # keyword | llm | fallback
    request: str = ""

    def explain(self) -> str:
        if not self.groups:
            return f"[router] no group for {self.request!r}"
        bits = []
        for g in self.groups:
            fired = ", ".join(self.matched.get(g, [])) or "-"
            bits.append(f"{g}(score={self.scores.get(g, 0)}; {fired})")
        return f"[router:{self.method}] {self.request!r} -> " + " | ".join(bits)


class IntentRouter:
    def __init__(self, llm_classify: Optional[Callable[[str, List[str]], List[str]]] = None):
        """llm_classify(request, group_names) -> list[str]"""
        self.llm_classify = llm_classify

    def _keyword_route(self, request: str) -> RouteResult:
        scores: Dict[str, int] = {}
        matched: Dict[str, List[str]] = {}
        for group, entries in _TRIGGER_INDEX.items():
            fired: List[str] = []
            for pat, phrase in entries:
                if pat.search(request):
                    fired.append(phrase)
            if fired:
                # longer trigger phrases are more specific -> weight them
                score = sum(1 + phrase.count(" ") for phrase in fired)
                scores[group] = score
                matched[group] = fired
        ordered = sorted(scores, key=lambda g: scores[g], reverse=True)
        return RouteResult(
            groups=ordered[: CFG.max_groups_per_request],
            scores=scores, matched=matched, method="keyword", request=request,
        )

    def route(self, request: str, allow_llm: bool = True) -> RouteResult:
        request = (request or "").strip()
        result = self._keyword_route(request)
        if result.groups:
            return result

        # stage 2: LLM fallback
        if allow_llm and self.llm_classify is not None:
            try:
                picked = self.llm_classify(request, taxonomy.group_names())
                picked = [g for g in picked if g in taxonomy.TAXONOMY]
                if picked:
                    return RouteResult(
                        groups=picked[: CFG.max_groups_per_request],
                        scores={g: 1 for g in picked},
                        matched={g: ["<llm>"] for g in picked},
                        method="llm", request=request,
                    )
            except Exception as exc:  # never let routing crash a "look"
                result.matched.setdefault("_error", []).append(str(exc))

        # stage 3: fallback group
        return RouteResult(
            groups=[CFG.fallback_group],
            scores={CFG.fallback_group: 0},
            matched={CFG.fallback_group: ["<fallback>"]},
            method="fallback", request=request,
        )


if __name__ == "__main__":
    r = IntentRouter()
    for q in [
        "grab me a coke",
        "can you clear the table?",
        "where did I leave my keys and wallet",
        "is anyone in the room",
        "find my screwdriver in the garage",
        "what do you see",
        "asdfghjkl nonsense",
    ]:
        print(r.route(q).explain())
