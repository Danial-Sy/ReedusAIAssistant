"""gemini 3.7 flash backend, the cloud mode brain. uses the interactions api."""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Callable, Dict, Iterable, List, Optional

from reedus_companion import config
from reedus_companion.cloud import CloudUnavailable
from reedus_companion.cloud import budget
from reedus_companion.cloud import persona_shots

_DEF_MODEL = "gemini-3.7-flash"

_THINK_LEVELS = {
    "gemini-3.7-flash": ("low", "medium", "high"),
    "gemini-3.1-pro-preview": ("low", "medium", "high"),
}
_THINK_ORDER = ("minimal", "low", "medium", "high")
_THINK_UNSUPPORTED_RE = re.compile(
    r"'([^']+)' is not a supported thinking level", re.I)


def _clamp_think(model: str, level: str) -> str:
    """Nearest thinking level this model supports, walking up."""
    allowed = _THINK_LEVELS.get(model)
    if not allowed or level in allowed:
        return level
    try:
        i = _THINK_ORDER.index(level)
    except ValueError:
        return allowed[0]
    for cand in _THINK_ORDER[i:]:
        if cand in allowed:
            return cand
    return allowed[0]

_THINK_HEADROOM = int(os.environ.get("REEDUS_GEMINI_THINK_HEADROOM", "512"))
#: absolute floor for max_output_tokens when thinking is on
_MIN_OUTPUT = int(os.environ.get("REEDUS_GEMINI_MIN_OUTPUT", "256"))

_TRANSIENT = {
    "ServerError", "ServiceUnavailable", "DeadlineExceeded", "ResourceExhausted",
    "InternalServerError", "TooManyRequests", "APIError", "ConnectError",
    "ConnectTimeout", "ReadTimeout", "RemoteProtocolError", "TimeoutException",
    "ClientConnectorError", "TransportError",
}


def _is_transient(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in _TRANSIENT:
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        return int(code) in (408, 429, 500, 502, 503, 504)
    except (TypeError, ValueError):
        return False


class GeminiBackend:
    """Drop-in Backend protocol client for Gemini 3.x via the Interactions API."""

    PROFILE: List[Dict] = []

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        key = api_key or os.environ.get("GEMINI_API_KEY") or ""
        if not key.strip():
            raise CloudUnavailable(
                "GEMINI_API_KEY is not set. Add it to deploy/xeon.env "
                "(get one at https://aistudio.google.com/apikey and enable billing "
                "so your conversations are excluded from training).",
                provider="gemini", retryable=False)
        try:
            from google import genai
        except ImportError as e:
            raise CloudUnavailable(
                "the google-genai package is missing. "
                "pip install -r reedus_companion/requirements/cloud.txt",
                provider="gemini", retryable=False) from e

        self.model = model or os.environ.get("REEDUS_GEMINI_MODEL", _DEF_MODEL)
        self._client = genai.Client(api_key=key)
        self._timeout = float(os.environ.get("REEDUS_GEMINI_TIMEOUT", "45"))
        self._retries = int(os.environ.get("REEDUS_GEMINI_RETRIES", "2"))
        _inline = os.environ.get("REEDUS_GEMINI_INLINE_SEARCH")
        if _inline is None:
            from reedus_companion import mode as _mode
            self._search = not _mode.local_search_enabled()
        else:
            self._search = _inline == "1"
        self._think_main = os.environ.get("REEDUS_GEMINI_THINK", "low")
        self._think_router = os.environ.get("REEDUS_GEMINI_THINK_ROUTER", "minimal")
        self._send_temp = os.environ.get("REEDUS_GEMINI_SEND_TEMP", "0") == "1"
        self._input_style = os.environ.get("REEDUS_GEMINI_INPUT_STYLE", "auto")
        print(f"[reedus] gemini backend: {self.model} "
              f"(think={self._think_main}/{self._think_router}, "
              f"inline_search={'on' if self._search else 'off'})")
        try:
            from reedus_companion import mode as _mode
            if not self._search and not _mode.local_search_enabled():
                print("\n[reedus] *** WARNING: NOTHING CAN SEARCH ***\n"
                      "  core.py's search routing is off (cloud mode) AND the persona\n"
                      "  call cannot ground inline. Reedus will make up anything he is\n"
                      "  asked about the present.\n"
                      "  Cause is almost always a stale export from an older xeon.env:\n"
                      "      unset REEDUS_GEMINI_INLINE_SEARCH\n"
                      "  or force one owner:\n"
                      "      export REEDUS_GEMINI_INLINE_SEARCH=1   # gemini grounds\n"
                      "      export REEDUS_LOCAL_SEARCH=1           # core.py routes\n",
                      flush=True)
        except Exception:
            pass


    def router_ready(self) -> bool:
        return True

    def close(self) -> None:
        try:
            self._client = None
        except Exception:
            pass

    def warm(self) -> float:
        """Open the TLS connection and resolve DNS before the first real turn."""
        t0 = time.perf_counter()
        try:
            self._once([{"role": "user", "content": "hi"}], max_tokens=1,
                       tier="router", name="warm", on_token=None, cancel=None,
                       temperature=None, use_adapter=False)
        except Exception:
            pass
        return time.perf_counter() - t0

    def chat(self, messages: List[Dict[str, str]], max_tokens: int,
             use_adapter: bool = True, temperature: Optional[float] = None,
             on_token: Optional[Callable[[str], None]] = None,
             tier: Optional[str] = None, name: str = "",
             cancel=None) -> str:
        if budget.exhausted():
            raise CloudUnavailable(
                f"monthly cloud budget of ${budget.cap():.2f} is spent "
                f"(${budget.spent():.2f} used)", provider="budget", retryable=False)

        last: Optional[BaseException] = None
        for attempt in range(self._retries + 1):
            try:
                return self._once(messages, max_tokens, tier=tier, name=name,
                                  on_token=on_token, cancel=cancel,
                                  temperature=temperature, use_adapter=use_adapter)
            except CloudUnavailable:
                raise
            except Exception as e:                                  # noqa: BLE001
                last = e
                if not _is_transient(e) or attempt >= self._retries:
                    break
                # don't retry a turn the user already interrupted
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    break
                time.sleep(0.35 * (2 ** attempt))
        raise CloudUnavailable(f"{type(last).__name__}: {last}",
                               provider="gemini",
                               retryable=_is_transient(last) if last else True)


    def _once(self, messages, max_tokens, *, tier, name, on_token, cancel,
              temperature, use_adapter=True) -> str:
        system, turns = _split_system(messages)
        is_router = (tier == "router")
        if use_adapter and not is_router:
            shots = persona_shots.block()
            if shots:
                system = f"{system}\n\n{shots}" if system else shots
        want_think = self._think_router if is_router else self._think_main
        ceiling = int(max_tokens)
        if want_think != "off":
            ceiling = max(ceiling + _THINK_HEADROOM, _MIN_OUTPUT)
        gen_cfg = {
            "thinking_level": _clamp_think(self.model, want_think),
            "max_output_tokens": ceiling,
        }
        # persona stays at 1.0 (google warns low temp loops on gemini 3); classifiers need 0.0,
        # at 1.0 subjects came back empty every turn
        if temperature is not None and not use_adapter:
            gen_cfg["temperature"] = float(temperature)
        elif self._send_temp:
            gen_cfg["temperature"] = float(
                config.TEMPERATURE if temperature is None else temperature)
        kwargs = {
            "model": self.model,
            "input": self._to_input(turns),
            "generation_config": gen_cfg,
        }
        if system:
            kwargs["system_instruction"] = system
        grounded = bool(self._search and not is_router)
        if grounded:
            kwargs["tools"] = [{"type": "google_search"}]

        do_stream = on_token is not None or cancel is not None
        t0 = time.perf_counter()
        ttft = 0.0
        cancelled = False
        pieces: List[str] = []
        usage = {}

        usage_src = None
        if do_stream:
            stream = create_interaction(self._client, stream=True, **kwargs)
            for event in stream:
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    cancelled = True
                    break
                text = _delta_text(event)
                if not text:
                    usage = _usage_from(event) or usage
                    continue
                if not ttft:
                    ttft = time.perf_counter() - t0
                pieces.append(text)
                if on_token is not None:
                    on_token(text)
            reply = "".join(pieces).strip()
        else:
            interaction = create_interaction(self._client, **kwargs)
            usage_src = interaction
            reply = (getattr(interaction, "output_text", "") or "").strip()
            usage = _usage_from(interaction) or {}

        wall = time.perf_counter() - t0
        n_in = int(usage.get("prompt_tokens") or _estimate_tokens(system, turns))
        n_out = int(usage.get("completion_tokens") or _estimate_tokens("", [{"content": reply}]))
        try:
            budget.record_tokens(self.model, n_in, n_out,
                                 grounded=grounded and _had_citations(usage_src))
        except Exception:
            pass

        if os.environ.get("REEDUS_PROFILE"):
            self.PROFILE.append({
                "wall_s": wall,
                "prompt_tokens": n_in,
                "completion_tokens": n_out,
                "system_head": (system or "")[:48],
                "tier": "router" if is_router else "main",
                "name": name,
                "ttft_s": ttft,
                "gen_tok_s": (n_out / (wall - ttft)) if wall > ttft and ttft else 0.0,
                "cancelled": cancelled,
            })
        return reply

    def _to_input(self, turns: List[Dict[str, str]]):
        """Convert OpenAI-style turns into the Interactions API `input` field."""
        if self._input_style == "flat":
            return _flatten(turns)
        if len(turns) == 1 and _role(turns[0]) == "user" and self._input_style != "structured":
            return turns[0].get("content", "")
        return [_step(t) for t in turns]


def _role(turn: Dict[str, str]) -> str:
    r = (turn.get("role") or "user").lower()
    return "assistant" if r in ("assistant", "model") else "user"


def _split_system(messages: List[Dict[str, str]]):
    """System messages -> a single system_instruction string."""
    system_parts, turns = [], []
    for m in messages or []:
        if (m.get("role") or "").lower() == "system":
            content = m.get("content") or ""
            if content:
                system_parts.append(content)
        else:
            turns.append(m)
    if not turns:
        turns = [{"role": "user", "content": ""}]
    return "\n\n".join(system_parts), turns


def _step(turn: Dict[str, str]) -> Dict:
    """One conversation turn as an Interactions API Step."""
    kind = "user_input" if _role(turn) == "user" else "model_output"
    return {"type": kind,
            "content": [{"type": "text", "text": turn.get("content") or ""}]}


def _flatten(turns: List[Dict[str, str]]) -> str:
    out = []
    for t in turns:
        who = "Reedus" if _role(t) == "assistant" else "User"
        out.append(f"{who}: {t.get('content', '')}")
    out.append("Reedus:")
    return "\n".join(out)


def _delta_text(event) -> str:
    """Pull incremental text out of a streamed event, tolerating shape drift."""
    try:
        if getattr(event, "event_type", "") != "step.delta":
            return ""
        delta = getattr(event, "delta", None)
        if delta is None:
            return ""
        if getattr(delta, "type", "") != "text":
            return ""
        return getattr(delta, "text", "") or ""
    except Exception:
        return ""


def _usage_from(obj) -> Dict[str, int]:
    """Best-effort token usage. Falls back to estimation when absent."""
    for attr in ("usage", "usage_metadata", "metadata"):
        u = getattr(obj, attr, None)
        if u is None:
            continue
        got = {}
        for src, dst in (("prompt_token_count", "prompt_tokens"),
                         ("input_tokens", "prompt_tokens"),
                         ("prompt_tokens", "prompt_tokens"),
                         ("candidates_token_count", "completion_tokens"),
                         ("output_tokens", "completion_tokens"),
                         ("completion_tokens", "completion_tokens"),
                         ("thoughts_token_count", "thinking_tokens")):
            v = getattr(u, src, None)
            if isinstance(v, int):
                got[dst] = v
        if "thinking_tokens" in got:
            got["completion_tokens"] = got.get("completion_tokens", 0) + got.pop("thinking_tokens")
        if got:
            return got
    return {}


def _estimate_tokens(system: str, turns: Iterable[Dict[str, str]]) -> int:
    """~4 chars per token, only when the api won't tell us."""
    n = len(system or "")
    for t in turns:
        n += len(t.get("content") or "")
    return max(1, n // 4)


def _had_citations(obj) -> bool:
    """The response carries url_citation annotations, i.e. search ran."""
    if obj is None:
        return False
    try:
        for step in getattr(obj, "steps", None) or []:
            for block in getattr(step, "content", None) or []:
                for ann in getattr(block, "annotations", None) or []:
                    if getattr(ann, "type", "") == "url_citation":
                        return True
    except Exception:
        pass
    return False


_EXTRA_MODE = {"style": os.environ.get("REEDUS_GEMINI_EXTRA_BODY", "auto")}
_EXTRA_FIELDS = ("thinking_level", "max_output_tokens")


#: params this API build rejects outright, learned at runtime
_DROPPED: set = set()
_UNKNOWN_RE = re.compile(r"[Uu]nknown parameter '([^']+)'")


def _unknown_param(exc: BaseException) -> Optional[str]:
    """'Unknown parameter 'thinking_level'.' -> 'thinking_level'"""
    m = _UNKNOWN_RE.search(str(exc))
    return m.group(1) if m else None


def _wants_extra_body(exc: BaseException) -> bool:
    if not isinstance(exc, TypeError):
        return False
    msg = str(exc)
    return "extra_body" in msg or "unexpected keyword" in msg


def _create_once(client, *, stream: bool = False, **kwargs):
    """client.interactions.create() with the extra_body shape sorted out."""
    extra = {k: kwargs.pop(k) for k in _EXTRA_FIELDS if k in kwargs}
    if stream:
        kwargs["stream"] = True

    def _direct():
        return client.interactions.create(**kwargs, **extra)

    def _boxed():
        return client.interactions.create(**kwargs, extra_body=dict(extra))

    style = _EXTRA_MODE["style"]
    if not extra:
        return client.interactions.create(**kwargs)
    if style == "0":
        return _direct()
    if style == "1":
        return _boxed()
    try:
        return _direct()
    except TypeError as e:
        if not _wants_extra_body(e):
            raise
        got = _boxed()
        _EXTRA_MODE["style"] = "1"          # learned, skip the probe from here on
        print(f"[reedus] gemini: this SDK needs extra_body for "
              f"{', '.join(extra)}, using it from now on "
              f"(pin with REEDUS_GEMINI_EXTRA_BODY=1)", flush=True)
        return got


def create_interaction(client, *, stream: bool = False, **kwargs):
    """As above, plus retire any field the server calls an unknown parameter."""
    for _ in range(1 + len(_EXTRA_FIELDS) + 2):
        clean = {k: v for k, v in kwargs.items() if k not in _DROPPED}
        if isinstance(clean.get("generation_config"), dict):
            clean["generation_config"] = {
                k: v for k, v in clean["generation_config"].items() if k not in _DROPPED}
        try:
            return _create_once(client, stream=stream, **clean)
        except Exception as e:                                   # noqa: BLE001
            bad_level = _THINK_UNSUPPORTED_RE.search(str(e))
            gc = kwargs.get("generation_config")
            if bad_level and isinstance(gc, dict) and "thinking_level" in gc:
                worse = bad_level.group(1)
                nxt = _THINK_ORDER[min(_THINK_ORDER.index(worse) + 1,
                                       len(_THINK_ORDER) - 1)] \
                    if worse in _THINK_ORDER else "low"
                if nxt != gc["thinking_level"]:
                    print(f"[reedus] gemini: {kwargs.get('model')} rejects "
                          f"thinking_level={worse!r}, using {nxt!r}", flush=True)
                    gc["thinking_level"] = nxt
                    continue
            name = _unknown_param(e)
            known = name in kwargs or name in (kwargs.get("generation_config") or {})
            if not name or name in _DROPPED or not known:
                raise
            _DROPPED.add(name)
            print(f"[reedus] gemini: this API build rejects {name!r}, "
                  f"dropping it for the rest of the session. "
                  f"(Reedus still works; if it was thinking_level, turns may be "
                  f"slower and cost more.)", flush=True)
    raise CloudUnavailable("gemini: too many rejected parameters",
                           provider="gemini", retryable=False)
