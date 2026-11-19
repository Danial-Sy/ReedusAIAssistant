"""the cloud brain switchboard: build the configured backend, decide who searches, and answer
ReedOS's Settings → Cloud brain page (GET/POST /api/llm in serve/control_api.py).

    build()                  backend for the saved provider/model (raises CloudUnavailable)
    inline_search(p)         does provider p search inside its own reply call?
    local_search_wanted()    should core.py route searches itself while in cloud mode?
    status() / act(req)      the app's page; act() never returns a key, only a masked hint
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Optional

from reedus_companion.cloud import CloudUnavailable, llm_settings, providers

def build(provider_id: Optional[str] = None, model: Optional[str] = None,
          api_key: Optional[str] = None, base_url: Optional[str] = None):
    pid = provider_id or llm_settings.provider()
    wire = providers.get(pid)["wire"]
    if wire == "gemini":
        from reedus_companion.cloud.gemini_backend import GeminiBackend
        return GeminiBackend(model=model or llm_settings.model("gemini"),
                             api_key=api_key or llm_settings.key("gemini"))
    if wire == "anthropic":
        from reedus_companion.cloud.anthropic_backend import AnthropicBackend
        return AnthropicBackend(model=model, api_key=api_key)
    from reedus_companion.cloud.openai_backend import OpenAIBackend
    return OpenAIBackend(pid, model=model, api_key=api_key, base_url=base_url)


def package_for(provider_id: str) -> str:
    return {"gemini": "google.genai", "anthropic": "anthropic"}.get(
        providers.get(provider_id)["wire"], "openai")


def preflight(provider_id: Optional[str] = None) -> tuple[bool, str]:
    """Can this provider run at all? (key present, SDK installed)."""
    pid = provider_id or llm_settings.provider()
    spec = providers.get(pid)
    if not llm_settings.has_key(pid):
        return False, f"I don't have a {spec['short']} key."
    if pid == "custom" and not llm_settings.base_url(pid):
        return False, "the custom brain has no base URL."
    if spec["wire"] != "gemini" and not llm_settings.model(pid):
        return False, f"no {spec['short']} model is picked."
    mod = package_for(pid)
    try:
        __import__(mod)
    except ImportError:
        return False, (f"the {mod.split('.')[0]} package isn't installed, "
                       "pip install -r reedus_companion/requirements/cloud.txt")
    return True, ""


# ---------- who searches ----------
def inline_search(provider_id: Optional[str] = None) -> bool:
    pid = provider_id or llm_settings.provider()
    if (os.environ.get("REEDUS_LOCAL_SEARCH") or "").strip() == "1":
        return False
    return llm_settings.search_mode() == "auto" and providers.native_search(pid)


def local_search_wanted() -> bool:
    """In cloud mode, does core.py run its own search routing?"""
    mode = llm_settings.search_mode()
    if mode == "off":
        return False
    if mode in ("local", "gemini"):
        return True
    return not providers.native_search(llm_settings.provider())


def search_summary() -> str:
    pid = llm_settings.provider()
    mode = llm_settings.search_mode()
    if mode == "off":
        return "off"
    if mode == "gemini":
        return "Gemini grounding (via Reedus's search router)"
    if inline_search(pid):
        return providers.get(pid).get("searchName") or "the brain's own search"
    return "Reedus's own search (SearXNG / DuckDuckGo / Brave)"


# ---------- status for the app ----------
def current() -> dict:
    pid = llm_settings.provider()
    mdl = llm_settings.model(pid)
    short, ml = providers.get(pid)["short"], providers.label(pid, mdl)
    return {"provider": pid, "model": mdl, "name": short,
            "label": ml if ml.lower().startswith(short.lower()) else f"{short} · {ml}",
            "search": llm_settings.search_mode()}


def status() -> dict:
    pid = llm_settings.provider()
    keys = {}
    for p in providers.ORDER:
        k = llm_settings.key(p)
        keys[p] = {"set": bool(k), "hint": llm_settings.masked(k),
                   "source": llm_settings.key_source(p),
                   "model": llm_settings.model(p), "baseUrl": llm_settings.base_url(p)
                   if p == "custom" else ""}
    ok, why = preflight(pid)
    return {
        **current(),
        "ready": ok, "why": why,
        "searchSummary": search_summary(),
        "searchModes": list(llm_settings.SEARCH_MODES),
        "keys": keys,
        "providers": providers.public(),
        "file": str(llm_settings.path()),
    }


# ---------- actions ----------
def act(req: dict, on_change=None) -> dict:
    """on_change() is called after a change the running brain should pick up."""
    a = (req.get("action") or "").strip()
    pid = (req.get("provider") or "").strip() or llm_settings.provider()
    if not providers.known(pid):
        return {"error": f"unknown provider {pid!r}"}
    try:
        if a == "key":
            k = (req.get("key") or "").strip()
            if not k:
                return {"error": "paste a key first"}
            guess = providers.guess_provider(k)
            if guess and guess != pid:
                return {"error": f"that looks like a {providers.get(guess)['name']} key, "
                                 f"not {providers.get(pid)['name']}"}
            llm_settings.update(key_for=pid, key_value=k)
            out = {"ok": True, "hint": llm_settings.masked(k)}
            if req.get("test", True):
                out["test"] = test(pid)
            _changed(on_change, pid)
            return out
        if a == "forget":
            llm_settings.update(forget=pid)
            _changed(on_change, pid)
            return {"ok": True}
        if a == "set":
            llm_settings.update(
                provider_id=pid,
                model_id=req.get("model") if req.get("model") is not None else None,
                search=req.get("search") or None,
                base=req.get("baseUrl") if req.get("baseUrl") is not None else None)
            _changed(on_change, pid, force=True)
            ok, why = preflight(pid)
            return {"ok": True, "ready": ok, "why": why, **current()}
        if a == "test":
            return test(pid, model=req.get("model") or None,
                        key=(req.get("key") or "").strip() or None,
                        base=req.get("baseUrl") or None)
        if a == "models":
            return {"models": list_models(pid, key=(req.get("key") or "").strip() or None,
                                          base=req.get("baseUrl") or None)}
        return {"error": f"unknown action {a!r}"}
    except ValueError as e:
        return {"error": str(e)}
    except CloudUnavailable as e:
        return {"error": str(e)}
    except Exception as e:                                       # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _changed(on_change, pid: str, force: bool = False) -> None:
    """Tell the running brain, but only when the change touches what it's running on."""
    if on_change is None:
        return
    if force or pid == llm_settings.provider():
        try:
            on_change()
        except Exception as e:                                   # noqa: BLE001
            print(f"[reedus] llm switch: couldn't apply live ({type(e).__name__}: {e})", flush=True)


def test(provider_id: str, model: Optional[str] = None, key: Optional[str] = None,
         base: Optional[str] = None) -> dict:
    """One tiny real call. {"ok", "ms", "reply"} or {"ok": False, "error"} in plain English."""
    from reedus_companion.cloud.base import explain
    t0 = time.perf_counter()
    try:
        b = build(provider_id, model=model, api_key=key, base_url=base)
    except CloudUnavailable as e:
        return {"ok": False, "error": str(e)}
    try:
        # a test shouldn't spend money on a search
        if hasattr(b, "_search"):
            b._search = False
        reply = b.chat([{"role": "system", "content": "Reply with one short word."},
                        {"role": "user", "content": "Say ready."}],
                       8, use_adapter=False, tier="router", name="key-test")
        return {"ok": True, "ms": int((time.perf_counter() - t0) * 1000),
                "reply": (reply or "")[:40], "model": getattr(b, "model", model)}
    except CloudUnavailable as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "error": explain(provider_id, e)}
    finally:
        try:
            b.close()
        except Exception:                                        # noqa: BLE001
            pass


def _get_json(url: str, headers: dict, timeout: float = 12.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Reedus/1.0", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:         # noqa: S310 (fixed hosts)
        return json.loads(r.read().decode("utf-8"))


def list_models(provider_id: str, key: Optional[str] = None, base: Optional[str] = None) -> list:
    """Every model the key can use, newest-looking first; the curated list when that fails."""
    spec = providers.get(provider_id)
    curated = [dict(m) for m in spec["models"]]
    k = key or llm_settings.key(provider_id)
    ids: list = []
    try:
        if spec["wire"] == "gemini":
            if not k:
                return curated
            data = _get_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                             {"x-goog-api-key": k})
            for m in data.get("models") or []:
                if "generateContent" in (m.get("supportedGenerationMethods") or []):
                    mid = (m.get("name") or "").removeprefix("models/")
                    if mid.startswith("gemini"):
                        ids.append((mid, m.get("displayName") or mid))
        elif spec["wire"] == "anthropic":
            if not k:
                return curated
            data = _get_json("https://api.anthropic.com/v1/models?limit=100",
                             {"x-api-key": k, "anthropic-version": "2023-06-01"})
            ids = [(m["id"], m.get("display_name") or m["id"]) for m in data.get("data") or []]
        else:
            # OpenRouter's list is public and local servers need no key; everyone else does
            if not k and provider_id not in ("openrouter", "custom"):
                return curated
            root = (base or llm_settings.base_url(provider_id) or "https://api.openai.com/v1").rstrip("/")
            hdr = {"Authorization": f"Bearer {k}"} if k else {}
            data = _get_json(f"{root}/models", hdr)
            rows = data.get("data") if isinstance(data, dict) else data
            for m in rows or []:
                mid = m.get("id") if isinstance(m, dict) else None
                if not mid:
                    continue
                if provider_id == "openai" and not mid.startswith(("gpt", "o1", "o3", "o4", "o5", "chatgpt")):
                    continue        # skip embeddings, tts, image, moderation models
                if any(s in mid for s in ("embed", "whisper", "tts", "dall-e", "moderation",
                                          "image", "audio", "realtime", "transcribe")):
                    continue
                ids.append((mid, (m.get("name") if isinstance(m, dict) else None) or mid))
    except Exception as e:                                       # noqa: BLE001
        from reedus_companion.cloud.base import explain
        return [*curated, {"id": "", "label": "", "note": "", "error": explain(provider_id, e)}] \
            if curated else [{"id": "", "label": "", "note": "", "error": explain(provider_id, e)}]
    seen = {m["id"] for m in curated}
    extra = [{"id": i, "label": lbl, "note": ""} for i, lbl in sorted(ids, key=lambda x: x[0], reverse=True)
             if i not in seen]
    live = {i for i, _ in ids}
    for m in curated:
        if ids and m["id"] not in live:
            m["note"] = (m["note"] + " · " if m["note"] else "") + "not offered to this key"
    return curated + extra
