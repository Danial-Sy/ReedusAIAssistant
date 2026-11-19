"""which cloud LLM is the brain, with which model, key and search. persisted per run box.

memory/llm.json (gitignored, chmod 600) is what ReedOS writes. environment variables are the
defaults underneath it, so a box set up from deploy/xeon.env keeps working with no file:
    REEDUS_LLM_PROVIDER   gemini | anthropic | openai | openrouter | xai | deepseek | groq | mistral | custom
    REEDUS_LLM_MODEL      model id for that provider
    REEDUS_LLM_BASE_URL   base URL for "custom"
    REEDUS_LLM_SEARCH     auto | local | gemini | off
    <PROVIDER>_API_KEY    e.g. GEMINI_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY (see providers.py)
    REEDUS_GEMINI_MODEL   still honoured for gemini, as before

search:
    auto    the provider's own web search when it has one, else Reedus's (SearXNG / DDG / Brave)
    local   always Reedus's own search routing, even on a provider that could search itself
    gemini  Reedus's routing, answered by Gemini grounding (needs a Gemini key too)
    off     no web search at all
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from reedus_companion.cloud import providers

SEARCH_MODES = ("auto", "local", "gemini", "off")

_LOCK = threading.RLock()
_CACHE: dict = {"at": None, "data": None}


def path() -> Path:
    explicit = os.environ.get("REEDUS_LLM_FILE")
    if explicit:
        return Path(explicit).expanduser()
    from reedus_companion import config
    return config.MEMORY_DIR / "llm.json"


def _load() -> dict:
    p = path()
    with _LOCK:
        try:
            st = p.stat().st_mtime
        except OSError:
            return {}
        if _CACHE["at"] == (str(p), st) and _CACHE["data"] is not None:
            return _CACHE["data"]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:                                        # noqa: BLE001
            data = {}
        _CACHE["at"], _CACHE["data"] = (str(p), st), data
        return data


def _save(data: dict) -> None:
    p = path()
    with _LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(p)
        _CACHE["at"], _CACHE["data"] = None, None


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# ---------- reads ----------
def provider() -> str:
    p = (_load().get("provider") or _env("REEDUS_LLM_PROVIDER") or providers.DEFAULT_PROVIDER).lower()
    return p if providers.known(p) else providers.DEFAULT_PROVIDER


def model(pid: Optional[str] = None) -> str:
    pid = pid or provider()
    saved = (_load().get("models") or {}).get(pid)
    if saved:
        return saved
    if _env("REEDUS_LLM_MODEL") and pid == (_env("REEDUS_LLM_PROVIDER").lower() or providers.DEFAULT_PROVIDER):
        return _env("REEDUS_LLM_MODEL")
    if pid == "gemini" and _env("REEDUS_GEMINI_MODEL"):
        return _env("REEDUS_GEMINI_MODEL")
    return providers.get(pid)["default"]


def key(pid: Optional[str] = None) -> str:
    pid = pid or provider()
    saved = (_load().get("keys") or {}).get(pid)
    if saved:
        return saved
    return _env(providers.get(pid)["env"])


def key_source(pid: str) -> str:
    """'app', 'env' or '' (none)."""
    if (_load().get("keys") or {}).get(pid):
        return "app"
    return "env" if _env(providers.get(pid)["env"]) else ""


def base_url(pid: Optional[str] = None) -> str:
    pid = pid or provider()
    saved = (_load().get("baseUrls") or {}).get(pid)
    if saved:
        return saved
    if pid == "custom" and _env("REEDUS_LLM_BASE_URL"):
        return _env("REEDUS_LLM_BASE_URL")
    return providers.get(pid).get("baseUrl", "")


def search_mode() -> str:
    s = (_load().get("search") or _env("REEDUS_LLM_SEARCH") or "auto").lower()
    return s if s in SEARCH_MODES else "auto"


def has_key(pid: str) -> bool:
    return bool(key(pid)) or bool(providers.get(pid).get("keyOptional"))


def masked(secret: str) -> str:
    s = (secret or "").strip()
    if not s:
        return ""
    if len(s) <= 10:
        return "•" * len(s)
    return f"{s[:4]}…{s[-4:]}"


# ---------- writes ----------
def update(*, provider_id: Optional[str] = None, model_id: Optional[str] = None,
           search: Optional[str] = None, base: Optional[str] = None,
           key_for: Optional[str] = None, key_value: Optional[str] = None,
           forget: Optional[str] = None) -> dict:
    """Change and persist. Returns the new file contents (keys included, caller masks)."""
    with _LOCK:
        data = dict(_load())
        if provider_id is not None:
            if not providers.known(provider_id):
                raise ValueError(f"unknown provider {provider_id!r}")
            data["provider"] = provider_id
        target = provider_id or data.get("provider") or provider()
        if model_id is not None:
            data.setdefault("models", {})[target] = model_id.strip()
            if not data["models"][target]:
                data["models"].pop(target)
        if base is not None:
            data.setdefault("baseUrls", {})[target] = base.strip().rstrip("/")
        if search is not None:
            if search not in SEARCH_MODES:
                raise ValueError(f"search must be one of {', '.join(SEARCH_MODES)}")
            data["search"] = search
        if key_for is not None:
            if not providers.known(key_for):
                raise ValueError(f"unknown provider {key_for!r}")
            data.setdefault("keys", {})[key_for] = (key_value or "").strip()
            if not data["keys"][key_for]:
                data["keys"].pop(key_for)
        if forget:
            (data.get("keys") or {}).pop(forget, None)
        _save(data)
        return data
