"""offline test suite, no weights needed."""

from __future__ import annotations

import sys
from pathlib import Path

# allow running both as a module and as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vision import taxonomy
from vision.intent_router import IntentRouter
from vision.model_manager import ModelManager
from vision.detector import Detection
from vision import describe
from vision.pipeline import _dedupe, _iou, _same_object_label


# --- taxonomy ---
def test_taxonomy_valid():
    assert taxonomy.validate() == []

def test_taxonomy_is_big():
    s = taxonomy.stats()
    assert s["groups"] >= 20, s
    assert s["total_labels"] >= 500, s
    assert s["total_triggers"] >= 150, s

def test_every_group_has_unique_safe_name():
    names = taxonomy.group_names()
    assert len(names) == len(set(names))
    assert all(n.isidentifier() for n in names)


# --- router ---
def test_router_direct_hits():
    r = IntentRouter()
    cases = {
        "grab me a coke": "drinks_beverages",
        "can you clear the table": "furniture_surfaces_storage",
        "where are my keys and wallet": "personal_items_wearables",
        "find my screwdriver": "tools_hardware",
        "is anyone in the room": "living_beings",
        "turn on the lamp": "lighting",
    }
    for q, expected in cases.items():
        groups = r.route(q).groups
        assert expected in groups, f"{q!r} -> {groups}, wanted {expected}"

def test_router_fallback_on_gibberish():
    r = IntentRouter()
    res = r.route("qwynzzz plorptak")
    assert res.method == "fallback"
    assert res.groups == ["general_common"]

def test_router_respects_group_cap():
    r = IntentRouter()
    # a request that trips many groups should still be capped
    res = r.route("coke plate chair lamp keys screwdriver towel")
    assert len(res.groups) <= 2

def test_router_llm_fallback_used_when_no_keyword():
    called = {}
    def fake_llm(request, groups):
        called["hit"] = True
        return ["tools_hardware"]
    r = IntentRouter(llm_classify=fake_llm)
    res = r.route("that whatchamacallit thingamajig")
    assert called.get("hit") is True
    assert res.method == "llm"
    assert res.groups == ["tools_hardware"]


# --- model manager LRU ---
class _FakeManager(ModelManager):
    """Overrides _load so we can test eviction without torch/weights."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.load_calls = []
    def _load(self, group):
        from vision.model_manager import LoadedModel
        import time
        self.load_calls.append(group)
        return LoadedModel(group, object(), "fake", time.time(), 0.0)

def test_manager_lru_eviction():
    mgr = _FakeManager()
    mgr.cfg.max_hot_models = 2
    mgr.get("drinks_beverages")
    mgr.get("tools_hardware")
    assert set(mgr.hot_groups()) == {"drinks_beverages", "tools_hardware"}
    mgr.get("lighting")                       # evicts LRU (drinks)
    assert "drinks_beverages" not in mgr.hot_groups()
    assert set(mgr.hot_groups()) == {"tools_hardware", "lighting"}

def test_manager_cache_hit_no_reload():
    mgr = _FakeManager()
    mgr.cfg.max_hot_models = 2
    mgr.get("lighting")
    mgr.get("lighting")
    assert mgr.load_calls.count("lighting") == 1   # second call served from cache

def test_manager_rejects_unknown_group():
    mgr = _FakeManager()
    try:
        mgr.get("not_a_group")
        assert False, "should have raised"
    except KeyError:
        pass


# --- describe ---
def _det(label, cx, cy, size=0.1, conf=0.9):
    x1, y1 = cx - size / 2, cy - size / 2
    x2, y2 = cx + size / 2, cy + size / 2
    return Detection(label, conf, (x1*640, y1*480, x2*640, y2*480),
                     (x1, y1, x2, y2))

def test_describe_empty():
    s = describe.summarize([])
    assert s["count"] == 0
    assert "don't see" in s["text"]

def test_describe_counts_and_positions():
    dets = [_det("chair", 0.2, 0.5), _det("chair", 0.25, 0.5), _det("coke can", 0.8, 0.5)]
    s = describe.summarize(dets, group="mix")
    assert s["count"] == 3
    labels = {i["label"]: i for i in s["items"]}
    assert labels["chair"]["count"] == 2
    assert "left" in labels["chair"]["positions"]
    assert "two chairs" in s["text"]

def test_describe_most_prominent_is_biggest():
    big = _det("sofa", 0.5, 0.5, size=0.6, conf=0.8)
    small = _det("pen", 0.1, 0.1, size=0.02, conf=0.99)
    s = describe.summarize([small, big])
    assert s["most_prominent"] == "sofa"


# --- dedupe ---
def test_dedupe_keeps_higher_conf_on_overlap():
    a = _det("cup", 0.5, 0.5, size=0.2, conf=0.6)
    b = _det("cup", 0.51, 0.51, size=0.2, conf=0.9)   # overlaps a
    kept = _dedupe([a, b])
    assert len(kept) == 1 and kept[0].confidence == 0.9

def test_dedupe_keeps_distinct_objects():
    a = _det("cup", 0.2, 0.2, size=0.1, conf=0.6)
    b = _det("cup", 0.8, 0.8, size=0.1, conf=0.9)     # no overlap
    assert len(_dedupe([a, b])) == 2

def test_iou_basic():
    a = _det("x", 0.5, 0.5, size=0.2)
    assert _iou(a, a) > 0.99


# --- parent/child label collapsing ---
def test_same_object_label_matches_parent_child():
    assert _same_object_label("speaker", "bookshelf speaker")
    assert _same_object_label("bed", "king size bed")
    assert _same_object_label("coke can", "diet coke can")
    assert _same_object_label("table", "coffee table")
    assert _same_object_label("water bottle", "tonic water bottle")


def test_same_object_label_rejects_unrelated():
    # whole-word matching: 'pen' must not swallow 'pencil'
    assert not _same_object_label("pen", "pencil")
    assert not _same_object_label("television", "speaker")
    assert not _same_object_label("coffee mug", "coffee table")
    assert not _same_object_label("cat", "dog")


def test_dedupe_collapses_generic_and_specific_on_same_box():
    generic = _det("speaker", 0.85, 0.6, size=0.15, conf=0.41)
    specific = _det("bookshelf speaker", 0.85, 0.6, size=0.15, conf=0.33)
    kept = _dedupe([generic, specific])
    assert len(kept) == 1 and kept[0].confidence == 0.41


def test_dedupe_keeps_overlapping_but_genuinely_different_objects():
    mug = _det("coffee mug", 0.45, 0.47, size=0.1, conf=0.6)
    table = _det("coffee table", 0.5, 0.7, size=0.5, conf=0.5)
    assert len(_dedupe([mug, table])) == 2


# --- pointing / center-object ---
def test_pointing_request_prefers_centered_object_over_biggest():
    tv = _det("television", 0.5, 0.35, size=0.7, conf=0.62)      # huge backdrop
    mug = _det("coffee mug", 0.5, 0.5, size=0.12, conf=0.55)     # small, centered
    s = describe.summarize([tv, mug], request="what am I pointing at")
    assert s["pointing"] is True
    assert s["most_prominent"] == "coffee mug"


def test_survey_request_still_prefers_biggest():
    tv = _det("television", 0.5, 0.35, size=0.7, conf=0.62)
    mug = _det("coffee mug", 0.5, 0.5, size=0.12, conf=0.55)
    s = describe.summarize([tv, mug], request="what do you see")
    assert s["pointing"] is False
    assert s["most_prominent"] == "television"


# --- runner ---
def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {passed+failed} total")
    return failed

if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
