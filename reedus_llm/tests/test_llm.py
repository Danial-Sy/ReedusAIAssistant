#!/usr/bin/env python3
"""cloud brain switching: provider catalog and prices, persisted settings (file over env), who
searches in each setup, preflight, the Claude and OpenAI backends against fake clients
(thinking/effort per model, web search tool, streaming, cancel, learned 400 drops), the
/api/llm actions, and, when the anthropic + openai SDKs are installed, both backends against
the real SDKs over a mocked HTTP transport. no network, no keys."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = Path(tempfile.mkdtemp())
os.environ["REEDUS_LLM_FILE"] = str(TMP / "llm.json")
os.environ["REEDUS_CLOUD_LEDGER"] = str(TMP / "ledger.json")
os.environ["INWORLD_API_KEY"] = "test-inworld"
for k in ("REEDUS_LLM_PROVIDER", "REEDUS_LLM_MODEL", "REEDUS_LLM_SEARCH", "REEDUS_GEMINI_MODEL",
          "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
          "REEDUS_LOCAL_SEARCH", "REEDUS_GEMINI_INLINE_SEARCH", "REEDUS_PROFILE", "REEDUS_MODE",
          "REEDUS_CLAUDE_THINK"):
    os.environ.pop(k, None)

_fails = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


def reset_settings():
    p = Path(os.environ["REEDUS_LLM_FILE"])
    if p.exists():
        p.unlink()


from reedus_companion import mode
from reedus_companion.cloud import CloudUnavailable, base, budget, llm, llm_settings, providers

# [1] catalog and prices
print("[1] provider catalog and prices")
check(providers.DEFAULT_PROVIDER == "gemini", "gemini is the default provider")
check(providers.get("gemini")["default"] == "gemini-3.7-flash", "gemini default model unchanged")
check(providers.price("claude-sonnet-5-5") == {"input": 2.00, "output": 10.00}, "sonnet 5.5 list price")
check(providers.price("claude-opus-5-5")["output"] == 20.00, "opus 5.5 list price")
check(providers.price("anthropic/claude-sonnet-5.5") == providers.price("claude-sonnet-5-5"),
      "router-style id finds the same price")
check(providers.price("gemini-3.8-flash")["output"] == 3.75, "unknown flash bills as flash")
check(providers.price("mystery-model-9") == providers.UNKNOWN_PRICE, "unknown model bills high")
check(providers.guess_provider("sk-ant-api03-xyz") == "anthropic", "guesses an Anthropic key")
check(providers.guess_provider("sk-or-v1-abc") == "openrouter", "guesses an OpenRouter key")
check(providers.guess_provider("sk-proj-abc") is None, "plain sk- is ambiguous")
pub = providers.public()
check([p["id"] for p in pub] == providers.ORDER, "public catalog keeps the app's order")
check(all(p["steps"] and "billing" in p for p in pub), "every provider has key steps and billing text")
check(all("key" not in p for p in pub), "catalog carries no key field")
check(budget._price("claude-haiku-5-5")["input"] == 0.10, "budget prices Claude models via the catalog")

# [2] settings
print("[2] settings: file over env")
reset_settings()
check(llm_settings.provider() == "gemini" and llm_settings.model() == "gemini-3.7-flash",
      "no file, no env: gemini 3.7 flash")
os.environ["REEDUS_GEMINI_MODEL"] = "gemini-3.1-pro-preview"
check(llm_settings.model("gemini") == "gemini-3.1-pro-preview", "REEDUS_GEMINI_MODEL still honoured")
os.environ["REEDUS_LLM_PROVIDER"] = "anthropic"
os.environ["REEDUS_LLM_MODEL"] = "claude-opus-5-5"
check(llm_settings.provider() == "anthropic" and llm_settings.model() == "claude-opus-5-5",
      "env picks provider and model")
check(llm_settings.model("deepseek") == "deepseek-chat", "REEDUS_LLM_MODEL only applies to its provider")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-from-env-0000"
check(llm_settings.key("anthropic") == "sk-ant-from-env-0000" and
      llm_settings.key_source("anthropic") == "env", "key from env")
llm_settings.update(provider_id="openai", model_id="gpt-5.1", search="local")
llm_settings.update(key_for="anthropic", key_value="sk-ant-from-app-1234")
check(llm_settings.provider() == "openai" and llm_settings.model() == "gpt-5.1",
      "file overrides env provider/model")
check(llm_settings.key("anthropic") == "sk-ant-from-app-1234" and
      llm_settings.key_source("anthropic") == "app", "app key overrides env key")
check(llm_settings.search_mode() == "local", "search saved")
check(llm_settings.masked("sk-ant-from-app-1234") == "sk-a…1234", "masked hint")
llm_settings.update(forget="anthropic")
check(llm_settings.key_source("anthropic") == "env", "forget falls back to env")
for bad in (lambda: llm_settings.update(provider_id="nope"), lambda: llm_settings.update(search="web")):
    try:
        bad()
        check(False, "bad value rejected")
    except ValueError:
        check(True, "bad value rejected")
saved = json.loads(Path(os.environ["REEDUS_LLM_FILE"]).read_text())
check(saved["provider"] == "openai" and "anthropic" not in (saved.get("keys") or {}),
      "file on disk matches")
for k in ("REEDUS_LLM_PROVIDER", "REEDUS_LLM_MODEL", "REEDUS_GEMINI_MODEL", "ANTHROPIC_API_KEY"):
    os.environ.pop(k, None)
reset_settings()

# [3] who searches
print("[3] search routing in cloud mode")
mode.set_current(mode.CLOUD)
from reedus_companion.cloud import gemini_search


def setup(provider_id, search="auto", gemini_key=True):
    reset_settings()
    llm_settings.update(provider_id=provider_id, search=search)
    if gemini_key:
        llm_settings.update(key_for="gemini", key_value="AIza-test-key-0000")


setup("gemini")
check(llm.inline_search() and not mode.local_search_enabled(), "gemini auto: grounding inline, no routing")
check(gemini_search.available(), "gemini auto: gemini_search is the engine for tool calls")
setup("anthropic")
check(llm.inline_search() and not mode.local_search_enabled(), "claude auto: web_search tool inline")
check(not gemini_search.available(), "claude auto: gemini_search stays out even with a gemini key")
setup("deepseek")
check(not llm.inline_search() and mode.local_search_enabled(), "deepseek auto: core.py routes searches")
setup("openrouter")
check(not llm.inline_search() and mode.local_search_enabled(), "openrouter auto: core.py routes searches")
setup("anthropic", "local")
check(not llm.inline_search() and mode.local_search_enabled() and not gemini_search.available(),
      "search=local: routing on, nothing inline, searxng/ddg")
setup("anthropic", "gemini")
check(not llm.inline_search() and mode.local_search_enabled() and gemini_search.available(),
      "search=gemini: routing on, gemini answers it")
setup("anthropic", "off")
check(not llm.inline_search() and not mode.local_search_enabled(), "search=off: nobody searches")
setup("gemini")
os.environ["REEDUS_LOCAL_SEARCH"] = "1"
check(not llm.inline_search() and mode.local_search_enabled(), "REEDUS_LOCAL_SEARCH=1 still wins")
os.environ.pop("REEDUS_LOCAL_SEARCH")
mode.set_current(mode.LOCAL)
check(mode.local_search_enabled(), "local mode always routes its own searches")
mode.set_current(mode.CLOUD)

# [4] preflight
print("[4] preflight")
setup("anthropic", gemini_key=False)
ok, why = mode.preflight(mode.CLOUD)
check(not ok and "Claude key" in why, f"no claude key -> {why!r}")
llm_settings.update(key_for="anthropic", key_value="sk-ant-test-0000")
stub_anthropic = types.ModuleType("anthropic")
real_anthropic = sys.modules.get("anthropic")
sys.modules["anthropic"] = stub_anthropic
ok, why = mode.preflight(mode.CLOUD)
check(ok, f"key + package -> ok ({why})")
os.environ.pop("INWORLD_API_KEY")
ok, why = mode.preflight(mode.CLOUD)
check(not ok and "Inworld" in why, "inworld key still required for cloud voice")
os.environ["INWORLD_API_KEY"] = "test-inworld"
setup("custom", gemini_key=False)
ok, why = llm.preflight()
check(not ok and "base URL" in why, "custom needs a base URL, not a key")

# [5] shared helpers
print("[5] message shaping and errors")
alt = base.alternate([{"role": "assistant", "content": "hey"}, {"role": "user", "content": "a"},
                      {"role": "user", "content": "b"}, {"role": "assistant", "content": ""}])
check([t["role"] for t in alt] == ["user", "assistant", "user", "assistant"],
      "starts with user, alternates")
check(alt[2]["content"] == "a\n\nb" and alt[3]["content"] == "…", "merges repeats, fills empties")


class Err(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


check("no credit" in base.explain("anthropic", Err("Your credit balance is too low", 400)),
      "credit balance -> add money")
check("no credit" in base.explain("openai", Err("You exceeded your current quota", 429)),
      "insufficient_quota 429 -> add money, not a rate limit")
check(not base.is_transient(Err("You exceeded your current quota", 429)), "quota isn't retried")
check(base.is_transient(Err("overloaded", 529)), "529 overloaded is retried")
check("rejected the key" in base.explain("anthropic", Err("invalid x-api-key", 401)), "401 -> key")
check("Refresh" in base.explain("openai", Err("model not found", 404)), "404 -> refresh models")

# [6] Claude backend
print("[6] Claude backend (fake client)")


class FakeStream:
    def __init__(self, chunks, final):
        self.text_stream = iter(chunks)
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._final


def message(text, n_in=100, n_out=12, searches=0):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="server_tool_use"), types.SimpleNamespace(type="text", text=text)],
        usage=types.SimpleNamespace(input_tokens=n_in, output_tokens=n_out, cache_read_input_tokens=0,
                                    cache_creation_input_tokens=0,
                                    server_tool_use=types.SimpleNamespace(web_search_requests=searches)))


class FakeMessages:
    def __init__(self):
        self.calls, self.reject = [], set()

    def _check(self, kw):
        self.calls.append(kw)
        for f in list(self.reject):
            if f in kw:
                raise Err(f"Error code: 400 - {f}: effort is not supported on this model", 400)

    def create(self, **kw):
        self._check(kw)
        return message("Sure thing.", searches=1 if "tools" in kw else 0)

    def stream(self, **kw):
        self._check(kw)
        return FakeStream(["Thirty", "-one ", "degrees."], message("Thirty-one degrees.", searches=1))


class FakeAnthropicClient:
    def __init__(self, **kw):
        self.kw = kw
        self.messages = FakeMessages()


stub_anthropic.Anthropic = FakeAnthropicClient
setup("anthropic", gemini_key=False)
llm_settings.update(key_for="anthropic", key_value="sk-ant-test-0000")
from reedus_companion.cloud.anthropic_backend import AnthropicBackend, thinking_for

b = AnthropicBackend(model="claude-sonnet-5-5")
check(b._client.kw.get("max_retries") == 0, "SDK retries off, the base class retries")
check(thinking_for("claude-sonnet-5-5") == {"type": "between_tools"}, "sonnet 5.5: thinking off via between_tools")
check(thinking_for("claude-opus-5-5") is None, "opus 5.5: thinking can't be disabled, left out")
toks = []
out = b.chat([{"role": "system", "content": "You are Reedus."}, {"role": "user", "content": "weather?"}],
             120, use_adapter=False, temperature=0.0, on_token=toks.append)
kw = b._client.messages.calls[-1]
check(out == "Thirty-one degrees." and "".join(toks) == "Thirty-one degrees.", "streams tokens and returns text")
check(kw["system"] == "You are Reedus." and kw["messages"][0]["role"] == "user", "system split out")
check(kw["thinking"] == {"type": "between_tools"} and kw["output_config"] == {"effort": "low"},
      "low effort, no thinking")
check(kw["max_tokens"] == 120, "no headroom when thinking is off")
check("temperature" not in kw, "temperature never sent")
check(kw["tools"][0]["type"] == "web_search_20260209" and kw["tools"][0]["name"] == "web_search",
      "dynamic web search tool on the persona call")
b.chat([{"role": "user", "content": "classify"}], 1, use_adapter=False, temperature=0.0, tier="router")
check("tools" not in b._client.messages.calls[-1], "no search on router calls")


class EventStream(FakeStream):
    """A real SDK stream is iterable: text events, plus a block start when a search runs."""
    def __iter__(self):
        ns = types.SimpleNamespace
        yield ns(type="text", text="Let me look that up. ")
        yield ns(type="content_block_start", content_block=ns(type="server_tool_use"))
        yield ns(type="content_block_start", content_block=ns(type="web_search_tool_result"))
        yield ns(type="text", text="The Hokies won.")


b._client.messages.stream = lambda **kw: EventStream([], message("Let me look that up. The Hokies won.", searches=1))
seen = []
b.on_search = lambda: seen.append(("search", "".join(toks2)))
toks2 = []
b.chat([{"role": "user", "content": "did vt win"}], 120, use_adapter=False, on_token=toks2.append)
b.on_search = None
check(seen == [("search", "Let me look that up. ")] and "".join(toks2).endswith("The Hokies won."),
      "the caller hears when a web search starts, between the text before it and after")
led = json.loads(Path(os.environ["REEDUS_CLOUD_LEDGER"]).read_text())
check("claude-sonnet-5-5" in led["by_model"] and led.get("searches", 0) >= 1, "tokens and searches billed")

o = AnthropicBackend(model="claude-opus-5-5")
o.chat([{"role": "user", "content": "hi"}], 40, use_adapter=False)
kw = o._client.messages.calls[-1]
check("thinking" not in kw and kw["max_tokens"] == 40 + 1024, "opus 5.5: adaptive left on, token headroom")

h = AnthropicBackend(model="claude-haiku-4-5")
h.chat([{"role": "user", "content": "hi"}], 40, use_adapter=False)
kw = h._client.messages.calls[-1]
check("output_config" not in kw and "thinking" not in kw, "haiku 4.5: no effort, no thinking")
check(kw["tools"][0]["type"] == "web_search_20250305", "older model gets the basic search tool")

d = AnthropicBackend(model="claude-sonnet-5-5")
d._client.messages.reject = {"output_config"}
out = d.chat([{"role": "user", "content": "hi"}], 40, use_adapter=False)
check(out == "Sure thing." and "output_config" in d._dropped, "a 400 on effort drops it and retries")
d.chat([{"role": "user", "content": "again"}], 40, use_adapter=False)
check("output_config" not in d._client.messages.calls[-1], "and stays dropped")

ev = threading.Event()
c = AnthropicBackend(model="claude-sonnet-5-5")
got = []


def stop_after_first(t):
    got.append(t)
    ev.set()


out = c.chat([{"role": "user", "content": "long story"}], 200, use_adapter=False,
             on_token=stop_after_first, cancel=ev)
check(got == ["Thirty"] and out == "Thirty", "barge-in cancel stops the stream")

budget_cap = os.environ.get("REEDUS_CLOUD_BUDGET")
os.environ["REEDUS_CLOUD_BUDGET"] = "0.000001"
try:
    b.chat([{"role": "user", "content": "x"}], 5)
    check(False, "budget breaker trips")
except CloudUnavailable as e:
    check(e.provider == "budget", "budget breaker trips")
os.environ.pop("REEDUS_CLOUD_BUDGET")

# [7] OpenAI backend
print("[7] OpenAI + OpenAI-compatible backend (fake client)")


class FakeResponses:
    def __init__(self):
        self.calls = []

    def create(self, stream=False, **kw):
        self.calls.append(kw)
        usage = types.SimpleNamespace(input_tokens=50, output_tokens=6)
        resp = types.SimpleNamespace(output_text="It's sunny.", usage=usage,
                                     output=[types.SimpleNamespace(type="web_search_call"),
                                             types.SimpleNamespace(type="message")])
        if not stream:
            return resp
        return iter([types.SimpleNamespace(type="response.output_text.delta", delta="It's "),
                     types.SimpleNamespace(type="response.output_text.delta", delta="sunny."),
                     types.SimpleNamespace(type="response.completed", response=resp)])


class FakeChat:
    def __init__(self):
        self.calls, self.reject = [], {"temperature"}

    def create(self, stream=False, **kw):
        self.calls.append(kw)
        for f in self.reject:
            if f in kw:
                raise Err(f"Unsupported parameter: '{f}' is not supported with this model.", 400)
        chunk = lambda t: types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(
            delta=types.SimpleNamespace(content=t))])
        if stream:
            return iter([chunk("Hel"), chunk("lo."),
                         types.SimpleNamespace(usage=types.SimpleNamespace(prompt_tokens=9, completion_tokens=2),
                                               choices=[])])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="Hello."))],
                                     usage=types.SimpleNamespace(prompt_tokens=9, completion_tokens=2))


class FakeOpenAIClient:
    def __init__(self, **kw):
        self.kw = kw
        self.responses = FakeResponses()
        self.chat = types.SimpleNamespace(completions=FakeChat())


stub_openai = types.ModuleType("openai")
stub_openai.OpenAI = FakeOpenAIClient
real_openai = sys.modules.get("openai")
sys.modules["openai"] = stub_openai
from reedus_companion.cloud.openai_backend import OpenAIBackend

setup("openai", gemini_key=False)
llm_settings.update(key_for="openai", key_value="sk-test-0000")
ob = OpenAIBackend("openai")
toks = []
out = ob.chat([{"role": "system", "content": "Be Reedus."}, {"role": "user", "content": "weather"}],
              80, on_token=toks.append, use_adapter=False)
kw = ob._client.responses.calls[-1]
check(out == "It's sunny." and "".join(toks) == "It's sunny.", "responses API streams")
check(kw["instructions"] == "Be Reedus." and kw["reasoning"] == {"effort": "low"} and kw["store"] is False,
      "instructions, low reasoning effort, not stored")
check(kw["tools"] == [{"type": "web_search"}], "OpenAI web_search tool on the persona call")
check(ob._client.kw.get("base_url") is None, "official endpoint for OpenAI")

setup("deepseek", gemini_key=False)
llm_settings.update(key_for="deepseek", key_value="sk-test-0000")
db = OpenAIBackend("deepseek")
check(db._client.kw["base_url"] == "https://api.deepseek.com", "deepseek base URL")
out = db.chat([{"role": "system", "content": "Be Reedus."}, {"role": "user", "content": "yes or no"}],
              1, use_adapter=False, temperature=0.0)
calls = db._client.chat.completions.calls
check(out == "Hello." and "temperature" in calls[0] and "temperature" not in calls[-1],
      "a model that refuses temperature gets it dropped")
check(calls[-1]["messages"][0] == {"role": "system", "content": "Be Reedus."}, "system message first")
check("tools" not in calls[-1], "no inline search on a local-search provider")
out = db.chat([{"role": "user", "content": "hi"}], 50, on_token=lambda t: None)
check(out == "Hello." and calls[-1]["stream_options"] == {"include_usage": True}, "chat streaming with usage")

setup("custom", gemini_key=False)
llm_settings.update(provider_id="custom", base="http://box:11434/v1", model_id="llama3.3")
cb = llm.build()
check(isinstance(cb, OpenAIBackend) and cb.model == "llama3.3" and cb._client.kw["api_key"] == "none",
      "custom/ollama: base URL, model, no key needed")

# [8] /api/llm actions
print("[8] llm.act (Settings > Cloud brain)")
setup("gemini", gemini_key=False)
real_test = llm.test
llm.test = lambda pid, **kw: {"ok": True, "ms": 5, "reply": "Ready."}
changes = []
out = llm.act({"action": "key", "provider": "anthropic", "key": "sk-ant-api03-abcdefgh1234"},
              on_change=lambda: changes.append(1))
check(out.get("ok") and out["hint"] == "sk-a…1234" and out["test"]["ok"], "key saved, masked, tested")
check(not changes, "a key for a provider not in use doesn't restart the brain")
out = llm.act({"action": "key", "provider": "anthropic", "key": "sk-or-v1-xxxx"})
check("OpenRouter" in out.get("error", ""), "a key pasted under the wrong provider is caught")
out = llm.act({"action": "set", "provider": "anthropic", "model": "claude-opus-5-5"},
              on_change=lambda: changes.append(1))
check(out.get("ok") and out["provider"] == "anthropic" and changes == [1], "set switches and applies live")
check(out["label"] == "Claude Opus 5.5", "label without a repeated name")
st = llm.status()
check(st["keys"]["anthropic"]["hint"] == "sk-a…1234" and "sk-ant-api03" not in json.dumps(st),
      "status never carries a whole key")
check(st["ready"], "ready once key + package are there")
out = llm.act({"action": "models", "provider": "xai"})
check([m["id"] for m in out["models"]] == ["grok-4"], "no key: curated list, no network")
check("error" in llm.act({"action": "set", "provider": "skynet"}), "unknown provider refused")
llm.test = real_test

# put the real SDKs back for the last section
for name, real in (("anthropic", real_anthropic), ("openai", real_openai)):
    if real is not None:
        sys.modules[name] = real
    else:
        sys.modules.pop(name, None)
for mod in ("reedus_companion.cloud.anthropic_backend", "reedus_companion.cloud.openai_backend"):
    sys.modules.pop(mod, None)

# [9] real SDKs over a mocked transport
print("[9] real SDKs, mocked HTTP")
try:
    import anthropic
    import openai
    try:
        import httpx2 as httpx       # what the current SDKs are built on
    except ImportError:
        import httpx
except ImportError:
    print("  skip  anthropic/openai not installed (pip install -r reedus_companion/requirements/cloud.txt)")
else:
    from reedus_companion.cloud.anthropic_backend import AnthropicBackend as RealAB
    from reedus_companion.cloud.openai_backend import OpenAIBackend as RealOB
    seen = {}

    def sse(events):
        return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()

    def claude_handler(request):
        body = json.loads(request.content)
        seen["claude"] = body
        msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 30, "output_tokens": 1}}
        events = [
            {"type": "message_start", "message": msg},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hey, "}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "what's up?"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))

    setup("anthropic", gemini_key=False)
    llm_settings.update(key_for="anthropic", key_value="sk-ant-test-0000")
    rb = RealAB(model="claude-sonnet-5-5")
    rb._client = anthropic.Anthropic(api_key="sk-ant-test", max_retries=0,
                                     http_client=httpx.Client(transport=httpx.MockTransport(claude_handler)))
    toks = []
    out = rb.chat([{"role": "system", "content": "You are Reedus."}, {"role": "user", "content": "yo"}],
                  100, use_adapter=False, on_token=toks.append)
    sent = seen["claude"]
    check(out == "Hey, what's up?" and "".join(toks) == out, "anthropic SDK stream parsed")
    check(sent["thinking"] == {"type": "between_tools"} and sent["output_config"] == {"effort": "low"}
          and sent["tools"][0]["type"] == "web_search_20260209" and sent["system"] == "You are Reedus.",
          "request body as intended on the wire")

    def openai_handler(request):
        body = json.loads(request.content)
        seen["openai"] = (str(request.url), body)
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json={
                "id": "resp_1", "object": "response", "created_at": 0, "status": "completed",
                "model": body["model"], "output": [{"type": "message", "id": "m1", "role": "assistant",
                                                     "status": "completed", "content": [
                                                         {"type": "output_text", "text": "Ready.", "annotations": []}]}],
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
                "usage": {"input_tokens": 12, "output_tokens": 2, "total_tokens": 14,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}})
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Ready."}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}})

    setup("openai", gemini_key=False)
    llm_settings.update(key_for="openai", key_value="sk-test-0000")
    ro = RealOB("openai")
    ro._client = openai.OpenAI(api_key="sk-test", max_retries=0,
                               http_client=httpx.Client(transport=httpx.MockTransport(openai_handler)))
    out = ro.chat([{"role": "user", "content": "say ready"}], 8, use_adapter=False, tier="router")
    url, body = seen["openai"]
    check(out == "Ready." and url.endswith("/v1/responses") and body["reasoning"] == {"effort": "low"},
          "openai SDK responses call parsed")
    setup("groq", gemini_key=False)
    llm_settings.update(key_for="groq", key_value="gsk_test")
    rg = RealOB("groq")
    rg._client = openai.OpenAI(api_key="gsk_test", base_url="https://api.groq.com/openai/v1", max_retries=0,
                               http_client=httpx.Client(transport=httpx.MockTransport(openai_handler)))
    out = rg.chat([{"role": "user", "content": "say ready"}], 8, use_adapter=False)
    url, body = seen["openai"]
    check(out == "Ready." and url == "https://api.groq.com/openai/v1/chat/completions",
          "openai SDK chat call to a compatible host parsed")

print()
if _fails:
    print(f"{len(_fails)} FAILED")
    for f in _fails:
        print(f"  - {f}")
    sys.exit(1)
print("all passed")
