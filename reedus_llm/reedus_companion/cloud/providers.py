"""every cloud LLM Reedus can run on: one row per provider.

the app (ReedOS → Settings → Cloud brain) renders straight from this, setup steps included,
so a new provider is one dict here plus nothing else when it speaks an API we already have:
    wire "gemini"     google-genai Interactions API        (cloud/gemini_backend.py)
    wire "anthropic"  Anthropic Messages API, official SDK (cloud/anthropic_backend.py)
    wire "responses"  OpenAI Responses API, official SDK   (cloud/openai_backend.py)
    wire "chat"       OpenAI-compatible chat completions   (cloud/openai_backend.py)

search says who answers "what's the score tonight":
    "native"  the provider searches inside the reply call (Google grounding, Claude's
              web_search tool, OpenAI's web_search tool, OpenRouter's web plugin)
    "local"   core.py's own search routing (SearXNG / Brave / DuckDuckGo), like local mode

prices are USD per million tokens and only feed the monthly budget breaker (cloud/budget.py).
they are list prices at the time of writing; a model not listed is billed by family, then
at a deliberately high default so an unknown model trips the cap early rather than late.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

DEFAULT_PROVIDER = "gemini"

PROVIDERS: Dict[str, dict] = {
    "gemini": {
        "name": "Google Gemini",
        "short": "Gemini",
        "wire": "gemini",
        "env": "GEMINI_API_KEY",
        "keyPrefix": "AIza",
        "default": "gemini-3.7-flash",
        "models": [
            {"id": "gemini-3.7-flash", "label": "Gemini 3.7 Flash", "note": "default, fast and cheap"},
            {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro (preview)", "note": "smarter, slower"},
            {"id": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite", "note": "cheapest"},
        ],
        "search": "native",
        "searchName": "Google Search grounding",
        "blurb": "The default. Fast, cheap, and answers live questions with Google Search.",
        "keyUrl": "https://aistudio.google.com/apikey",
        "billingUrl": "https://aistudio.google.com/usage",
        "steps": [
            "Go to aistudio.google.com/apikey and sign in.",
            "Create API key (starts with AIza).",
            "Click Set up billing next to it and add a card. The free tier is rate-limited and may train on your chats.",
            "Paste it below.",
        ],
        "billing": "Pay-as-you-go. A few dollars a month on Flash.",
    },
    "anthropic": {
        "name": "Anthropic Claude",
        "short": "Claude",
        "wire": "anthropic",
        "env": "ANTHROPIC_API_KEY",
        "keyPrefix": "sk-ant-",
        "default": "claude-sonnet-5-5",
        "models": [
            {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "note": "fast, very capable"},
            {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "note": "smartest Opus, slower, ~2x Sonnet's price"},
            {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5", "note": "quickest and cheapest"},
            {"id": "claude-fable-5-2", "label": "Claude Fable 5.2", "note": "most capable, priciest, slowest"},
        ],
        "search": "native",
        "searchName": "Claude web search ($10 per 1,000 searches)",
        "blurb": "Claude Sonnet, Opus or Haiku. Searches the web itself when it needs to.",
        "keyUrl": "https://console.anthropic.com/settings/keys",
        "billingUrl": "https://console.anthropic.com/settings/billing",
        "steps": [
            "Sign up at console.anthropic.com (separate from Claude.ai).",
            "Billing: buy credits ($5 min). A Claude Pro/Max plan doesn't include API use.",
            "API keys: create one (starts with sk-ant-). It's shown once.",
            "Paste it below.",
        ],
        "billing": "Prepaid credits. Turn on auto-reload so he never goes quiet.",
    },
    "openai": {
        "name": "OpenAI",
        "short": "OpenAI",
        "wire": "responses",
        "env": "OPENAI_API_KEY",
        "keyPrefix": "sk-",
        "default": "gpt-5-mini",
        "models": [
            {"id": "gpt-5-mini", "label": "GPT-5 mini", "note": "fast and cheap"},
            {"id": "gpt-5.1", "label": "GPT-5.1", "note": "flagship"},
            {"id": "gpt-5-nano", "label": "GPT-5 nano", "note": "cheapest"},
        ],
        "search": "native",
        "searchName": "OpenAI web search tool",
        "blurb": "GPT models through the Responses API, with OpenAI's own web search.",
        "keyUrl": "https://platform.openai.com/api-keys",
        "billingUrl": "https://platform.openai.com/settings/organization/billing/overview",
        "steps": [
            "Sign in at platform.openai.com (separate from ChatGPT).",
            "Billing: add credit ($5 min). ChatGPT Plus doesn't include API use.",
            "API keys: create one (starts with sk-). It's shown once.",
            "Paste it below. Refresh lists the models your key can use.",
        ],
        "billing": "Prepaid credits.",
    },
    "openrouter": {
        "name": "OpenRouter",
        "short": "OpenRouter",
        "wire": "chat",
        "baseUrl": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY",
        "keyPrefix": "sk-or-",
        "default": "openrouter/auto",
        "models": [
            {"id": "openrouter/auto", "label": "Auto", "note": "OpenRouter picks per request"},
        ],
        "search": "local",
        "blurb": "One key, hundreds of models: Claude, GPT, Gemini, Llama, Grok, DeepSeek, Qwen…",
        "keyUrl": "https://openrouter.ai/settings/keys",
        "billingUrl": "https://openrouter.ai/settings/credits",
        "steps": [
            "Sign in at openrouter.ai.",
            "Credits: add some. Free \":free\" models are slow and limited.",
            "Keys: create one (starts with sk-or-).",
            "Paste it below, then Refresh to pick any model.",
        ],
        "billing": "Prepaid credits at each model's price.",
    },
    "xai": {
        "name": "xAI Grok",
        "short": "Grok",
        "wire": "chat",
        "baseUrl": "https://api.x.ai/v1",
        "env": "XAI_API_KEY",
        "keyPrefix": "xai-",
        "default": "grok-4",
        "models": [{"id": "grok-4", "label": "Grok 4", "note": ""}],
        "search": "local",
        "blurb": "Grok models. Web search goes through Reedus's own search.",
        "keyUrl": "https://console.x.ai",
        "billingUrl": "https://console.x.ai",
        "steps": [
            "Sign in at console.x.ai.",
            "Billing: buy credits.",
            "API Keys: create one (starts with xai-).",
            "Paste it below.",
        ],
        "billing": "Prepaid credits.",
    },
    "deepseek": {
        "name": "DeepSeek",
        "short": "DeepSeek",
        "wire": "chat",
        "baseUrl": "https://api.deepseek.com",
        "env": "DEEPSEEK_API_KEY",
        "keyPrefix": "sk-",
        "default": "deepseek-chat",
        "models": [
            {"id": "deepseek-chat", "label": "DeepSeek Chat", "note": "very cheap"},
            {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner", "note": "thinks first, slower"},
        ],
        "search": "local",
        "blurb": "Very cheap. Web search goes through Reedus's own search.",
        "keyUrl": "https://platform.deepseek.com/api_keys",
        "billingUrl": "https://platform.deepseek.com/top_up",
        "steps": [
            "Sign up at platform.deepseek.com.",
            "Top up: add balance (requests fail at $0).",
            "API keys: create one (starts with sk-).",
            "Paste it below.",
        ],
        "billing": "Prepaid balance. Very cheap.",
    },
    "groq": {
        "name": "Groq",
        "short": "Groq",
        "wire": "chat",
        "baseUrl": "https://api.groq.com/openai/v1",
        "env": "GROQ_API_KEY",
        "keyPrefix": "gsk_",
        "default": "openai/gpt-oss-120b",
        "models": [
            {"id": "openai/gpt-oss-120b", "label": "gpt-oss 120B", "note": "open model, very fast"},
        ],
        "search": "local",
        "blurb": "Open models at very high speed. Web search goes through Reedus's own search.",
        "keyUrl": "https://console.groq.com/keys",
        "billingUrl": "https://console.groq.com/settings/billing",
        "steps": [
            "Sign in at console.groq.com.",
            "Billing: upgrade to pay-as-you-go. The free tier runs out fast.",
            "API Keys: create one (starts with gsk_).",
            "Paste it below.",
        ],
        "billing": "Pay-as-you-go.",
    },
    "mistral": {
        "name": "Mistral",
        "short": "Mistral",
        "wire": "chat",
        "baseUrl": "https://api.mistral.ai/v1",
        "env": "MISTRAL_API_KEY",
        "keyPrefix": "",
        "default": "mistral-large-latest",
        "models": [
            {"id": "mistral-large-latest", "label": "Mistral Large", "note": ""},
            {"id": "mistral-small-latest", "label": "Mistral Small", "note": "cheaper"},
        ],
        "search": "local",
        "blurb": "Mistral's models (EU-hosted). Web search goes through Reedus's own search.",
        "keyUrl": "https://console.mistral.ai/api-keys",
        "billingUrl": "https://console.mistral.ai/billing",
        "steps": [
            "Sign in at console.mistral.ai.",
            "Billing: pick the pay-as-you-go plan and add a card.",
            "API Keys: create one.",
            "Paste it below.",
        ],
        "billing": "Pay-as-you-go.",
    },
    "custom": {
        "name": "Other (OpenAI-compatible)",
        "short": "Custom",
        "wire": "chat",
        "baseUrl": "",
        "env": "REEDUS_LLM_CUSTOM_KEY",
        "keyPrefix": "",
        "keyOptional": True,
        "default": "",
        "models": [],
        "search": "local",
        "blurb": "Any server that speaks the OpenAI chat API: Together, Fireworks, Azure, "
                 "a local Ollama or LM Studio…",
        "keyUrl": "",
        "billingUrl": "",
        "steps": [
            "Find the server's OpenAI-compatible base URL (ends in /v1). Ollama: http://<machine>:11434/v1.",
            "Add a key if it needs one. Local servers don't.",
            "Fill both in, Refresh to list models, then Test.",
        ],
        "billing": "Whatever that service charges. Local models are free.",
    },
}

ORDER = ["gemini", "anthropic", "openai", "openrouter", "xai", "deepseek", "groq", "mistral", "custom"]

# USD per million tokens
PRICES: Dict[str, Dict[str, float]] = {
    "claude-fable-5-2": {"input": 8.00, "output": 40.00},
    "claude-fable-5-1": {"input": 10.00, "output": 50.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "deepseek-chat": {"input": 0.30, "output": 1.20},
    "deepseek-reasoner": {"input": 0.60, "output": 2.40},
}

# family fallbacks, first match wins
_FAMILY = [
    # Gemini 4 (Nov 2026 list prices from third-party trackers; Argon/Carbon at the 1 Dec price)
    (r"gemini-4.*hydrogen", {"input": 1.50, "output": 7.50}),
    (r"gemini-4.*neon", {"input": 3.00, "output": 15.00}),
    (r"gemini-4.*(?:argon|carbon)", {"input": 4.00, "output": 20.00}),
    (r"gemini.*flash-lite", {"input": 0.30, "output": 2.50}),
    (r"gemini.*flash", {"input": 0.75, "output": 3.75}),
    (r"gemini.*pro", {"input": 2.50, "output": 15.00}),
    (r"claude.*fable|claude.*mythos", {"input": 10.00, "output": 50.00}),
    (r"claude.*opus", {"input": 5.00, "output": 25.00}),
    (r"claude.*sonnet", {"input": 3.00, "output": 15.00}),
    (r"claude.*haiku", {"input": 1.00, "output": 5.00}),
    (r"gpt.*nano", {"input": 0.10, "output": 0.50}),
    (r"gpt.*mini", {"input": 0.40, "output": 2.00}),
    (r"gpt|o\d", {"input": 2.50, "output": 15.00}),
    (r"deepseek", {"input": 0.60, "output": 2.40}),
    (r"mistral.*small|ministral", {"input": 0.20, "output": 0.60}),
    (r"mistral|grok|llama|qwen|gpt-oss", {"input": 2.00, "output": 8.00}),
]
UNKNOWN_PRICE = {"input": 5.00, "output": 25.00}

# USD per 1,000 searches the provider runs inside a reply (Gemini's is metered in budget.py)
SEARCH_PER_1000 = {"anthropic": 10.00, "openai": 10.00}


def get(provider: str) -> dict:
    return PROVIDERS.get(provider) or PROVIDERS[DEFAULT_PROVIDER]


def known(provider: str) -> bool:
    return provider in PROVIDERS


def native_search(provider: str) -> bool:
    return get(provider).get("search") == "native"


def price(model: str) -> Dict[str, float]:
    m = (model or "").lower()
    # "anthropic/claude-sonnet-5.5" style ids from routers
    bare = m.split("/")[-1].replace(".", "-")
    for cand in (m, bare):
        if cand in PRICES:
            return PRICES[cand]
    for rx, p in _FAMILY:
        if re.search(rx, m):
            return p
    return UNKNOWN_PRICE


def label(provider: str, model: str) -> str:
    """'Claude Sonnet 5.5' for the UI and the health snapshot."""
    for m in get(provider).get("models") or []:
        if m["id"] == model:
            return m["label"]
    return model or get(provider)["name"]


def public() -> List[dict]:
    """The catalog as the app sees it (no secrets in here, just help text)."""
    out = []
    for pid in ORDER:
        p = PROVIDERS[pid]
        out.append({
            "id": pid, "name": p["name"], "short": p["short"], "blurb": p["blurb"],
            "default": p["default"], "models": p["models"], "search": p["search"],
            "searchName": p.get("searchName", "Reedus's own search (SearXNG / DuckDuckGo)"),
            "keyUrl": p["keyUrl"], "billingUrl": p["billingUrl"], "steps": p["steps"],
            "billing": p["billing"], "keyPrefix": p.get("keyPrefix", ""),
            "keyOptional": bool(p.get("keyOptional")), "needsBaseUrl": pid == "custom",
            "env": p["env"],
        })
    return out


def guess_provider(key: str) -> Optional[str]:
    """Which provider a pasted key belongs to, when the prefix says so."""
    k = (key or "").strip()
    for pid in ("anthropic", "openrouter", "xai", "groq", "gemini"):
        pre = PROVIDERS[pid].get("keyPrefix")
        if pre and k.startswith(pre):
            return pid
    return None
